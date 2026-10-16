'''processor/review_processor/nodes/node_risk_aggregate.py'''

'''
步骤分解
1. 接收状态: 取 `rule_risk_items`（规则层）与 `llm_risk_items`（LLM 层）。
2. 合并去重: 两层结果合并；同一处同类风险以规则层为准（规则层有确定的规则ID与依据）。
3. 排序: 高风险在前，同级按页码 / 段落先后，便于前端与报告按严重度呈现。
4. 综合风险: 取全部风险项中的最高等级（SPEC 4.3.2）。
5. 结论映射: 按 ReviewConfig.conclusion_map 映射审查结论。
6. 附加说明: LLM 失败 / mock 时给出提示语，前端据此告知"深度语义审查未完成"。
7. 写回状态 + 输出统计日志。

需求分析
输入:
- `rule_risk_items`：规则层风险项
- `llm_risk_items`：LLM 层风险项
- `llm_failed` / `llm_is_mock`：降级状态位

输出:
- `risk_items`：合并去重、排好序的最终风险项
- `overall_risk_level`：综合风险等级
- `review_conclusion`：审查结论
- `review_note`：结论附加说明

综合风险规则（SPEC 4.3.2）:
    任一 high → 高风险
    否则任一 mid → 中风险
    否则 → 低风险
    注意：**没有风险项时也是"低风险"，不是"none"**。
    `none` 表示"尚未审查出结果"，是任务状态层面的取值，不是综合计算的输出。

关于去重（重要）:
    规则层与 LLM 层可能对同一处报同一个风险（例如都指出付款条款缺验收前置）。
    若不去重，报告里会出现两条一样的风险，法务会重复处理。
    去重键取 (条款, 维度, 风险类型, 风险等级)，**保留规则层那条** ——
    因为规则层带确定的 rule_id、legal_basis 与推荐模板，可追溯性更强。
'''

import json
import logging
from typing import Any, Dict, List, Tuple

from db.constants import RiskLevel, RiskSource
from processor.review_processor.base import BaseNode, setup_logging
from processor.review_processor.config import ReviewConfig
from processor.review_processor.state import ReviewGraphState, get_default_state


class NodeRiskAggregate(BaseNode):
    '''综合风险节点：合并两层结果、计算综合风险、映射审查结论'''
    name = 'node_risk_aggregate'

    def process(self, state: ReviewGraphState) -> ReviewGraphState:
        """
        节点：综合风险计算（node_risk_aggregate）
        整体流程：取输入 → 合并去重 → 排序 → 算综合风险 → 映射结论 → 写回状态
        核心目的：把两层审查结果收敛成一个可展示、可回写、可归档的结论
        必要参数：无（两层都为空也允许，结果是低风险 + 可通过）
        更新参数：risk_items、overall_risk_level、review_conclusion、review_note

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取输入=======
        rule_items, llm_items = self._step_1_get_inputs(state)

        # =======步骤二:合并去重=======
        merged_items, duplicate_count = self._step_2_merge(rule_items, llm_items)

        # =======步骤三:排序（高风险在前）=======
        sorted_items = self._step_3_sort(merged_items)

        # =======步骤四:计算综合风险 + 映射结论=======
        overall_level = self._step_4_compute_overall(sorted_items)
        conclusion = self.config.conclusion_of(overall_level)

        # =======步骤五:生成结论附加说明=======
        note = self._step_5_build_note(state, overall_level)

        # =======步骤六:写回状态 + 输出统计日志=======
        state['risk_items'] = sorted_items
        state['overall_risk_level'] = overall_level
        state['review_conclusion'] = conclusion
        state['review_note'] = note

        self._step_6_print_stats(
            rule_items, llm_items, sorted_items, duplicate_count, overall_level, conclusion, note
        )

        return state

    def _step_1_get_inputs(self, state: ReviewGraphState) -> Tuple[
            List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        【步骤1】获取输入
        :param state: 项目状态字典（ReviewGraphState）
        :return: (规则层风险项, LLM 层风险项)
        """
        return state.get('rule_risk_items') or [], state.get('llm_risk_items') or []

    def _step_2_merge(self, rule_items: List[Dict[str, Any]],
                      llm_items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
        """
        【步骤2】合并去重
        功能：规则层在前（优先级更高），同键的 LLM 项被丢弃
        :param rule_items: 规则层风险项
        :param llm_items: LLM 层风险项
        :return: (合并后的风险项, 被去重的数量)
        """
        merged: List[Dict[str, Any]] = []
        seen = set()
        duplicate_count = 0

        for item in list(rule_items) + list(llm_items):
            # 非法等级统一压低为 low，避免脏数据把综合风险抬上去
            level = str(item.get('risk_level') or '').strip().lower()
            if level not in (RiskLevel.HIGH, RiskLevel.MID, RiskLevel.LOW):
                self.logger.warning(
                    f'风险项等级 {item.get("risk_level")!r} 非法，已按 low 处理'
                )
                item = dict(item)
                item['risk_level'] = RiskLevel.LOW
                level = RiskLevel.LOW

            key = (
                item.get('db_clause_id'),
                item.get('dimension') or '',
                item.get('risk_type') or '',
                level,
            )
            if key in seen:
                duplicate_count += 1
                continue
            seen.add(key)
            merged.append(item)

        return merged, duplicate_count

    def _step_3_sort(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        【步骤3】排序
        功能：按严重度倒序；同级按页码、段落先后，保证展示顺序与原文顺序一致
        :param items: 风险项列表
        :return: 排序后的列表
        """
        severity = RiskLevel.SEVERITY
        return sorted(
            items,
            key=lambda item: (
                -severity.get(item.get('risk_level'), 0),
                item.get('page_no') or 0,
                item.get('paragraph_index') or 0,
            ),
        )

    def _step_4_compute_overall(self, items: List[Dict[str, Any]]) -> str:
        """
        【步骤4】计算综合风险（SPEC 4.3.2）
        功能：取全部风险项中的最高等级；**没有风险项时结果为低风险**
        :param items: 合并后的风险项
        :return: 综合风险等级 high / mid / low
        """
        if not items:
            return RiskLevel.LOW

        highest = RiskLevel.LOW
        for item in items:
            level = item.get('risk_level')
            if RiskLevel.SEVERITY.get(level, 0) > RiskLevel.SEVERITY.get(highest, 0):
                highest = level
        return highest

    def _step_5_build_note(self, state: ReviewGraphState, overall_level: str) -> str:
        """
        【步骤5】生成结论附加说明
        功能：LLM 失败或走 mock 时，必须让结论带上"覆盖不完整"的提示，
             否则法务会把"没检出风险"误当成"真的没有风险"
        :param state: 项目状态字典
        :param overall_level: 综合风险等级
        :return: 提示语；无需提示时返回空字符串
        """
        notes: List[str] = []

        if state.get('llm_failed'):
            notes.append('LLM 深度语义审查未完成，本结论仅基于规则引擎结果')

        if state.get('llm_is_mock'):
            notes.append('LLM 未配置，深度语义审查以占位结果运行，本结论不可用于生产')

        if state.get('ocr_is_mock'):
            notes.append('OCR 未启用，扫描件内容为占位文本，解析结果不可信')

        if not notes:
            return ''

        # 低风险 + 覆盖不完整是最危险的组合，必须显式提醒
        if overall_level == RiskLevel.LOW:
            notes.append('当前未检出风险不等于无风险，请人工复核')

        return '；'.join(notes)

    def _step_6_print_stats(self, rule_items: List[Dict[str, Any]],
                            llm_items: List[Dict[str, Any]],
                            merged_items: List[Dict[str, Any]], duplicate_count: int,
                            overall_level: str, conclusion: str, note: str) -> None:
        """
        【步骤6】输出统计日志
        """
        level_counter: Dict[str, int] = {}
        source_counter: Dict[str, int] = {}
        for item in merged_items:
            level = item.get('risk_level') or RiskLevel.LOW
            level_counter[level] = level_counter.get(level, 0) + 1
            source = item.get('source') or RiskSource.RULE
            source_counter[source] = source_counter.get(source, 0) + 1

        self.log_step('规则层风险项', str(len(rule_items)))
        self.log_step('LLM 层风险项', str(len(llm_items)))
        self.log_step('去重条数', str(duplicate_count))
        self.log_step('最终风险项', str(len(merged_items)))
        self.log_step('等级分布', json.dumps(level_counter, ensure_ascii=False))
        self.log_step('来源分布', json.dumps(source_counter, ensure_ascii=False))
        self.log_step('综合风险', overall_level)
        self.log_step('审查结论', conclusion)
        if note:
            self.log_step('结论说明', note)


if __name__ == '__main__':
    # 激活日志的全局配置
    setup_logging()

    passed = 0

    def _risk(clause_id: Any, dimension: str, risk_type: str, level: str,
              source: str = RiskSource.RULE, page_no: int = 1, paragraph_index: int = 0,
              cause: str = '') -> Dict[str, Any]:
        """构造一条风险项"""
        return {
            'rule_code': 'R-XX' if source == RiskSource.RULE else '',
            'matched_rule_id': 1 if source == RiskSource.RULE else None,
            'clause_id': f'cl_{clause_id:03d}' if clause_id else '',
            'db_clause_id': clause_id,
            'page_no': page_no,
            'paragraph_index': paragraph_index,
            'dimension': dimension,
            'risk_type': risk_type,
            'risk_level': level,
            'cause': cause or f'{dimension}-{risk_type}',
            'legal_basis': '依据',
            'source': source,
        }

    def _run(**overrides) -> Dict[str, Any]:
        """跑一遍综合风险节点"""
        state = get_default_state()
        state['task_id'] = '1'
        state.update(overrides)
        return NodeRiskAggregate()(state)

    # ---------- 一、综合风险按最高等级取（SPEC 4.3.2）----------
    logging.getLogger().info('=' * 16 + ' 一、综合风险取最高 ' + '=' * 16)

    level_cases = [
        ('只有高风险', [_risk(1, 'payment', 'timing', 'high')], 'high', '整改'),
        ('只有中风险', [_risk(1, 'confidentiality', 'missing', 'mid')], 'mid', '整改后通过'),
        ('只有低风险', [_risk(1, 'other', 'high_risk_exp', 'low')], 'low', '可通过'),
        ('高+中+低混合',
         [_risk(1, 'payment', 'timing', 'high'),
          _risk(2, 'confidentiality', 'missing', 'mid'),
          _risk(3, 'breach', 'amount', 'low')],
         'high', '整改'),
        ('中+低混合',
         [_risk(1, 'confidentiality', 'missing', 'mid'),
          _risk(2, 'breach', 'amount', 'low')],
         'mid', '整改后通过'),
    ]
    for case_name, items, expected_level, conclusion_keyword in level_cases:
        result = _run(rule_risk_items=items)
        assert result['overall_risk_level'] == expected_level, \
            f'{case_name} 综合风险应为 {expected_level}，实际 {result["overall_risk_level"]}'
        assert conclusion_keyword in result['review_conclusion'], \
            f'{case_name} 结论应含「{conclusion_keyword}」，实际 {result["review_conclusion"]}'
        passed += 1
        logging.getLogger().info(
            f'  ✅ {case_name:<12} → {result["overall_risk_level"]} / {result["review_conclusion"]}'
        )

    # ---------- 二、无风险项 → 低风险（不是 none）----------
    logging.getLogger().info('=' * 16 + ' 二、无风险项 ' + '=' * 16)

    result = _run()
    assert result['overall_risk_level'] == 'low', \
        f'无风险项时综合风险应为 low（SPEC 4.3.2 的"否则→低风险"），实际 {result["overall_risk_level"]}'
    assert result['overall_risk_level'] != 'none', '综合计算不应输出 none（那是任务状态取值）'
    assert result['review_conclusion'] == '可通过'
    assert result['risk_items'] == []
    passed += 1
    logging.getLogger().info('  ✅ 无风险项 → low / 可通过（非 none）')

    # ---------- 三、两层合并与去重 ----------
    logging.getLogger().info('=' * 16 + ' 三、合并与去重 ' + '=' * 16)

    rule_items = [
        _risk(1, 'payment', 'timing', 'high', cause='规则层：付款缺验收前置'),
        _risk(2, 'breach', 'unbalanced', 'high', cause='规则层：违约责任不对等'),
    ]
    llm_items = [
        # 与第一条完全同键 → 应被去重（保留规则层）
        _risk(1, 'payment', 'timing', 'high', source=RiskSource.LLM, cause='LLM 层：同样的问题'),
        # 同条款但不同维度 → 应保留
        _risk(1, 'data_security', 'missing', 'mid', source=RiskSource.LLM, cause='LLM 层：数据安全'),
    ]
    result = _run(rule_risk_items=rule_items, llm_risk_items=llm_items)
    items = result['risk_items']

    assert len(items) == 3, f'应合并去重为 3 条，实际 {len(items)}'
    kept = next(i for i in items if i['db_clause_id'] == 1 and i['dimension'] == 'payment')
    assert kept['source'] == RiskSource.RULE, '同键冲突时应保留规则层（可追溯性更强）'
    assert kept['cause'].startswith('规则层'), '保留的应是规则层那条'
    assert kept['matched_rule_id'] is not None, '规则层条目应带命中规则ID'
    passed += 1
    logging.getLogger().info(f'  ✅ 4 条合并为 3 条，同键冲突保留规则层')

    # ---------- 四、排序：高风险在前 ----------
    logging.getLogger().info('=' * 16 + ' 四、排序 ' + '=' * 16)

    unsorted_items = [
        _risk(3, 'breach', 'amount', 'low', page_no=3),
        _risk(1, 'payment', 'timing', 'high', page_no=2),
        _risk(2, 'confidentiality', 'missing', 'mid', page_no=1),
        _risk(4, 'ip', 'high_risk_exp', 'high', page_no=1),
    ]
    result = _run(rule_risk_items=unsorted_items)
    levels = [item['risk_level'] for item in result['risk_items']]
    assert levels == ['high', 'high', 'mid', 'low'], f'排序错误：{levels}'
    # 同为 high 时按页码升序
    high_pages = [item['page_no'] for item in result['risk_items'] if item['risk_level'] == 'high']
    assert high_pages == sorted(high_pages), f'同级应按页码升序，实际 {high_pages}'
    passed += 1
    logging.getLogger().info(f'  ✅ 排序正确：{levels}，同级按页码升序')

    # ---------- 五、降级提示语 ----------
    logging.getLogger().info('=' * 16 + ' 五、降级提示 ' + '=' * 16)

    note_cases = [
        ('LLM 失败', {'llm_failed': True}, '深度语义审查未完成'),
        ('LLM 走 mock', {'llm_is_mock': True}, '不可用于生产'),
        ('OCR 走 mock', {'ocr_is_mock': True}, '扫描件内容为占位文本'),
        ('一切正常', {}, ''),
    ]
    for case_name, overrides, keyword in note_cases:
        result = _run(**overrides)
        note = result['review_note']
        if keyword:
            assert keyword in note, f'{case_name} 的提示语应含「{keyword}」，实际 {note!r}'
        else:
            assert note == '', f'{case_name} 不应有提示语，实际 {note!r}'
        passed += 1
        logging.getLogger().info(f'  ✅ {case_name:<12} → {note or "(无提示)"}')

    # 关键：低风险 + 覆盖不完整必须额外提醒
    result = _run(llm_failed=True)
    assert '不等于无风险' in result['review_note'], \
        '低风险且覆盖不完整时必须提醒"未检出风险不等于无风险"'
    passed += 1
    logging.getLogger().info('  ✅ 低风险 + 覆盖不完整 → 额外提醒人工复核')

    # ---------- 六、脏数据防御 ----------
    logging.getLogger().info('=' * 16 + ' 六、脏数据防御 ' + '=' * 16)

    dirty = [
        _risk(1, 'payment', 'timing', 'high'),
        {'db_clause_id': 2, 'dimension': 'breach', 'risk_type': 'amount',
         'risk_level': '超级高', 'cause': '脏数据', 'source': RiskSource.RULE},
    ]
    result = _run(rule_risk_items=dirty)
    assert result['overall_risk_level'] == 'high', '非法等级的项被压低为 low，不应影响最高等级'
    dirty_item = next(i for i in result['risk_items'] if i['db_clause_id'] == 2)
    assert dirty_item['risk_level'] == 'low', '非法等级应被压低为 low'
    passed += 1
    logging.getLogger().info('  ✅ 非法等级被压低为 low，不会把综合风险误抬为高风险')

    # ---------- 七、结论映射可配置 ----------
    logging.getLogger().info('=' * 16 + ' 七、结论映射可配置 ' + '=' * 16)

    custom = ReviewConfig(conclusion_map={
        'high': '自定义高风险结论', 'mid': '自定义中风险结论',
        'low': '自定义低风险结论', 'none': '自定义未审结论',
    })
    state = get_default_state()
    state['task_id'] = '1'
    state['rule_risk_items'] = [_risk(1, 'payment', 'timing', 'high')]
    result = NodeRiskAggregate(config=custom)(state)
    assert result['review_conclusion'] == '自定义高风险结论', \
        f'结论映射应可被配置覆盖，实际 {result["review_conclusion"]}'
    passed += 1
    logging.getLogger().info('  ✅ 结论映射可由配置覆盖（SPEC 4.3.2 允许管理员调整）')

    logging.getLogger().info(f'node_risk_aggregate 自测全部通过 ✅ 共 {passed} 个用例')
