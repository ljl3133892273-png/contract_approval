'''processor/recommend_processor/nodes/node_template_recall.py'''

'''
步骤分解
1. 接收状态: 取 `risk_items`（node_load_risk 产出，已按严重度降序）。
2. 一次性载入模板库: 把所有启用中的示范条款读进内存，按主键与条款类型建双向索引
   （避免逐条风险点查库）。
3. 逐风险点召回:
   (1) 规则绑定的模板优先 —— 风险项若由规则命中且规则带了 template_clause_id，
       直接采用该模板，得分最高；
   (2) 否则按「合同类型 + 条款类型」召回，本合同类型精确匹配的模板得分更高；
   (3) 严格匹配为空时放宽到「同条款类型的其他合同类型模板」并告警。
4. 写回状态 + 输出统计日志。

需求分析
输入:
- `risk_items`：待生成建议的风险项（含 clause_type / template_clause_id）

输出:
- `candidates`：与 risk_items **按下标一一对应**的候选列表
   （candidates[i] 是 risk_items[i] 的候选，改写节点按同样的下标取值，不依赖主键）

为什么"规则绑定模板"要优先（SPEC 4.3.3）:
    规则库里每条规则（R-01~R-08）都已经挂了 template_clause_id，
    这是人工审定过的「这类风险就该用这个条款」，可信度远高于按类型泛召回。
    所以给它一个碾压级的分数，保证它永远排在候选第一位。

为什么要一次载入全部模板:
    逐条风险点查库会产生 N 次 SELECT（风险点动辄几十条）。
    模板库是**小表**（人工维护，通常几十条），一次全量载入后在内存分派更划算，
    也让"放宽召回"这种需要回头看有没有别的候选的逻辑更容易写。

关于放宽召回:
    如果某条款类型在模板库里只配了"销售合同"的模板，而当前是"采购合同"，
    严格匹配会召回为空。此时宁可给出这个不同合同类型的模板（并在日志里告警），
    也好过让模型完全无参考地自由发挥 —— 后者更容易产生幻觉条款。
'''

import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from db.models import TemplateClause
from db.session import session_scope
from processor.recommend_processor.base import BaseNode, setup_logging
from processor.recommend_processor.exceptions import StorageError
from processor.recommend_processor.state import RecommendGraphState, get_default_state

# 候选来源标记
MATCH_BY_RULE = 'rule_bound'  # 规则直接绑定
MATCH_BY_TYPE = 'type'  # 合同类型 + 条款类型精确召回
MATCH_BY_RELAXED = 'relaxed'  # 放宽到其他合同类型
MATCH_BY_NONE = 'none'  # 无候选

# 类型召回的基础分（命中条款类型本身）
_TYPE_BASE_SCORE = 10


class NodeTemplateRecall(BaseNode):
    '''模板召回节点：为每个风险点从示范条款库召回候选'''
    name = 'node_template_recall'

    def process(self, state: RecommendGraphState) -> RecommendGraphState:
        """
        节点：模板召回（node_template_recall）
        整体流程：取风险项 → 载入模板库 → 逐条召回 → 写回状态 + 统计日志
        核心目的：给改写节点提供"有依据的参考条款"，降低 LLM 幻觉概率
        必要参数：risk_items（可为空）
        更新参数：candidates

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取输入=======
        risk_items = state.get('risk_items') or []
        contract_type = state.get('contract_type') or '*'

        if not risk_items:
            self.log_step('风险项', '0 条 —— 无需召回模板')
            state['candidates'] = []
            return state

        # =======步骤二:载入模板库=======
        try:
            by_id, by_clause_type = self._step_2_load_templates()
        except SQLAlchemyError as e:
            raise StorageError(message=f'载入模板库失败：{e}', cause=e)

        # =======步骤三:逐风险点召回=======
        candidates = self._step_3_recall(risk_items, contract_type, by_id, by_clause_type)

        # =======步骤四:写回状态 + 输出统计日志=======
        state['candidates'] = candidates
        self._step_4_print_stats(risk_items, candidates, contract_type, len(by_id))

        return state

    def _step_2_load_templates(self) -> Tuple[Dict[int, Dict[str, Any]], Dict[str, List[Dict[str, Any]]]]:
        """
        【步骤2】载入模板库
        功能：一次性取出全部启用中的示范条款，建两份索引
        :return: (按主键索引, 按条款类型索引)
        """
        with session_scope() as session:
            rows = session.scalars(
                select(TemplateClause)
                .where(TemplateClause.enabled.is_(True))
                .order_by(TemplateClause.id)
            ).all()

            templates = [
                {
                    'template_id': row.id,
                    'contract_type': row.contract_type or '*',
                    'clause_type': row.clause_type or '',
                    'title': row.title or '',
                    'content': row.content or '',
                }
                for row in rows
            ]

        by_id: Dict[int, Dict[str, Any]] = {t['template_id']: t for t in templates}
        by_clause_type: Dict[str, List[Dict[str, Any]]] = {}
        for template in templates:
            by_clause_type.setdefault(template['clause_type'], []).append(template)

        return by_id, by_clause_type

    def _step_3_recall(self, risk_items: List[Dict[str, Any]], contract_type: str,
                       by_id: Dict[int, Dict[str, Any]],
                       by_clause_type: Dict[str, List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
        """
        【步骤3】逐风险点召回
        :param risk_items: 风险项列表
        :param contract_type: 合同类型
        :param by_id: 模板主键索引
        :param by_clause_type: 模板条款类型索引
        :return: 与 risk_items 按下标一一对应的候选列表
        """
        candidates: List[Dict[str, Any]] = []

        for index, item in enumerate(risk_items):
            clause_type = item.get('clause_type') or ''
            matched_by, templates = self._recall_one(
                item, contract_type, by_id, by_clause_type
            )

            candidates.append({
                # 与 risk_items 同下标，改写节点按位取值
                'index': index,
                'db_risk_id': item.get('db_risk_id'),
                'clause_type': clause_type,
                'matched_by': matched_by,
                'templates': templates,
            })

        return candidates

    def _recall_one(self, item: Dict[str, Any], contract_type: str,
                    by_id: Dict[int, Dict[str, Any]],
                    by_clause_type: Dict[str, List[Dict[str, Any]]]
                    ) -> Tuple[str, List[Dict[str, Any]]]:
        """
        【辅助函数】召回单个风险点的候选模板
        :param item: 风险项
        :param contract_type: 合同类型
        :param by_id: 模板主键索引
        :param by_clause_type: 模板条款类型索引
        :return: (召回方式, 模板候选列表（按得分降序，已截断 top_k）)
        """
        clause_type = item.get('clause_type') or ''
        collected: Dict[int, Dict[str, Any]] = {}
        matched_by = MATCH_BY_NONE

        # 1.规则绑定的模板优先 —— 人工审定过的对应关系，可信度最高
        rule_template_id = item.get('template_clause_id')
        if rule_template_id is not None:
            template = by_id.get(rule_template_id)
            if template is not None:
                self._collect(collected, template, MATCH_BY_RULE,
                              self.config.template_rule_bound_score)
                matched_by = MATCH_BY_RULE
            else:
                self.logger.warning(
                    f'风险项 {item.get("db_risk_id")} 绑定的模板 {rule_template_id} '
                    f'不存在或已停用，退化为按类型召回'
                )

        # 2.按 (合同类型 + 条款类型) 召回
        #    注意：通配模板（contract_type='*'）**始终参与**，不能因为存在精确匹配就丢掉它 ——
        #    SPEC 的示范条款库默认全部是通配类型，漏掉它等于召回为空。
        pool = by_clause_type.get(clause_type, [])
        if pool:
            strict = [t for t in pool if t['contract_type'] == contract_type]
            wildcard = [t for t in pool if t['contract_type'] == '*']

            if strict or wildcard:
                if matched_by == MATCH_BY_NONE:
                    matched_by = MATCH_BY_TYPE
                # 精确合同类型得分更高，排序时自然靠前
                for template in strict:
                    self._collect(collected, template, MATCH_BY_TYPE,
                                  _TYPE_BASE_SCORE + self.config.template_contract_type_bonus)
                for template in wildcard:
                    self._collect(collected, template, MATCH_BY_TYPE, _TYPE_BASE_SCORE)
            else:
                # 3.既无精确匹配也无通配 → 放宽到同条款类型的其他合同类型模板
                if matched_by == MATCH_BY_NONE:
                    matched_by = MATCH_BY_RELAXED
                self.logger.warning(
                    f'条款类型 {clause_type} 下没有合同类型为 {contract_type} 或 * 的模板，'
                    f'放宽召回其他合同类型的模板'
                )
                for template in pool:
                    self._collect(collected, template, MATCH_BY_RELAXED, _TYPE_BASE_SCORE)

        ranked = sorted(collected.values(), key=lambda t: (-t['score'], t['template_id']))
        return matched_by, ranked[:max(1, self.config.template_recall_top_k)]

    @staticmethod
    def _collect(collected: Dict[int, Dict[str, Any]], template: Dict[str, Any],
                 matched_by: str, score: int) -> None:
        """
        【辅助函数】把候选模板收进集合
        说明：同一模板可能被多次命中（规则绑定 + 类型召回），取**较高分**那次
        :param collected: {template_id: 候选}
        :param template: 模板
        :param matched_by: 本次召回方式
        :param score: 本次得分
        """
        existing = collected.get(template['template_id'])
        if existing is not None and existing['score'] >= score:
            return

        entry = dict(template)
        entry['score'] = score
        entry['matched_by'] = matched_by
        collected[template['template_id']] = entry

    def _step_4_print_stats(self, risk_items: List[Dict[str, Any]],
                            candidates: List[Dict[str, Any]], contract_type: str,
                            template_total: int) -> None:
        """
        【步骤4】输出统计日志
        """
        match_counter: Dict[str, int] = {}
        for candidate in candidates:
            key = candidate['matched_by']
            match_counter[key] = match_counter.get(key, 0) + 1

        with_candidate = sum(1 for c in candidates if c['templates'])
        total_candidates = sum(len(c['templates']) for c in candidates)

        self.log_step('合同类型', contract_type)
        self.log_step('模板库规模', f'{template_total} 条（启用中）')
        self.log_step('待召回风险点', f'{len(risk_items)} 条')
        self.log_step('召回方式分布', str(match_counter))
        self.log_step('候选合计', f'{total_candidates} 条')

        no_candidate = len(candidates) - with_candidate
        if no_candidate:
            self.logger.warning(
                f'{no_candidate} 条风险点未召回到任何候选模板 —— '
                f'改写节点将只能依赖 LLM 自由生成（或保留审查阶段的原建议）'
            )


if __name__ == '__main__':
    # 激活日志的全局配置
    setup_logging()

    import tempfile
    from pathlib import Path

    from sqlalchemy import select as sa_select

    from db.models import TemplateClause
    from db.session import init_db, init_engine, reset_engine
    from processor.recommend_processor.config import RecommendConfig

    node_recall = NodeTemplateRecall()
    passed = 0

    # 临时库
    work_dir = Path(tempfile.mkdtemp(prefix='node_template_recall_test_'))
    reset_engine()
    init_engine(f'sqlite:///{work_dir.as_posix()}/recall_test.db')
    init_db()

    with session_scope() as session:
        session.add_all([
            # 通配合同类型 + payment
            TemplateClause(contract_type='*', clause_type='payment',
                           title='付款与验收前置条款（示范）',
                           content='甲方应在验收合格后 15 个工作日内付款。', enabled=True),
            # 精确合同类型 + payment
            TemplateClause(contract_type='采购', clause_type='payment',
                           title='采购付款条款（示范）',
                           content='采购款项应在验收合格且收到发票后支付。', enabled=True),
            # 其他合同类型 + payment（用于放宽召回）
            TemplateClause(contract_type='销售', clause_type='payment',
                           title='销售回款条款（示范）',
                           content='乙方应在交付后 30 日内支付货款。', enabled=True),
            # 只有"销售"合同类型、没有通配版本的条款类型 —— 用于验证放宽召回
            TemplateClause(contract_type='销售', clause_type='jurisdiction',
                           title='销售合同管辖条款（示范）',
                           content='争议提交甲方所在地人民法院管辖。', enabled=True),
            # 停用模板，不应被召回
            TemplateClause(contract_type='*', clause_type='payment',
                           title='已停用的付款条款',
                           content='不应被召回。', enabled=False),
            # ip 类型
            TemplateClause(contract_type='*', clause_type='ip',
                           title='知识产权归属条款（示范）',
                           content='本合同产生的知识产权归甲方所有。', enabled=True),
        ])

    with session_scope() as session:
        payment_wildcard_id = session.scalars(
            sa_select(TemplateClause).where(TemplateClause.title == '付款与验收前置条款（示范）')
        ).first().id

    def _item(index: int, clause_type: str, **overrides) -> Dict[str, Any]:
        """构造一条风险项"""
        item = {
            'db_risk_id': index,
            'clause_type': clause_type,
            'dimension': clause_type,
            'risk_type': 'missing',
            'risk_level': 'high',
            'cause': f'{clause_type} 风险',
            'template_clause_id': None,
        }
        item.update(overrides)
        return item

    # ---------- 一、按合同类型 + 条款类型召回 ----------
    logging.getLogger().info('=' * 20 + ' 一、类型召回 ' + '=' * 20)

    state = get_default_state()
    state['task_id'] = '1'
    state['contract_type'] = '采购'
    state['risk_items'] = [_item(1, 'payment')]
    result = node_recall(state)

    candidate = result['candidates'][0]
    assert candidate['matched_by'] == 'type', f"应为类型召回，实际 {candidate['matched_by']}"
    titles = [t['title'] for t in candidate['templates']]
    # 精确匹配的采购模板应排在通配模板之前
    assert titles[0] == '采购付款条款（示范）', f'精确合同类型应排第一，实际 {titles}'
    assert '付款与验收前置条款（示范）' in titles, '通配模板也应参与候选'
    assert '已停用的付款条款' not in titles, '停用模板绝不能被召回'
    passed += 1
    logging.getLogger().info('  ✅ 精确合同类型模板排第一，通配模板参与，停用模板被排除')

    # ---------- 二、规则绑定模板优先 ----------
    logging.getLogger().info('=' * 20 + ' 二、规则绑定优先 ' + '=' * 20)

    state['risk_items'] = [_item(1, 'payment', template_clause_id=payment_wildcard_id)]
    result = node_recall(state)
    candidate = result['candidates'][0]

    assert candidate['matched_by'] == 'rule_bound', '规则绑定模板应标记为 rule_bound'
    assert candidate['templates'][0]['template_id'] == payment_wildcard_id, \
        '规则绑定的模板必须排在候选第一位（即使它是通配类型）'
    assert candidate['templates'][0]['score'] == node_recall.config.template_rule_bound_score
    assert len(candidate['templates']) > 1, '规则绑定模板之外仍应补充类型召回候选'
    passed += 1
    logging.getLogger().info('  ✅ 规则绑定模板得分碾压，稳居候选第一位')

    # ---------- 三、放宽召回 ----------
    logging.getLogger().info('=' * 20 + ' 三、放宽召回 ' + '=' * 20)

    # jurisdiction 只有"销售"类型的模板：采购合同既无精确匹配、也无通配 → 触发放宽
    state['contract_type'] = '采购'
    state['risk_items'] = [_item(1, 'jurisdiction')]
    result = node_recall(state)
    candidate = result['candidates'][0]

    assert candidate['matched_by'] == 'relaxed', \
        f"既无精确匹配也无通配时应放宽召回，实际 {candidate['matched_by']}"
    assert candidate['templates'], '放宽后仍应有候选'
    assert all(t['matched_by'] == 'relaxed' for t in candidate['templates'])
    passed += 1
    logging.getLogger().info('  ✅ 无精确匹配且无通配 → 放宽到其他合同类型模板，不返回空')

    # ---------- 四、无候选 ----------
    logging.getLogger().info('=' * 20 + ' 四、无候选 ' + '=' * 20)

    state['risk_items'] = [_item(1, 'force_majeure')]
    result = node_recall(state)
    candidate = result['candidates'][0]
    assert candidate['matched_by'] == 'none', f"应为 none，实际 {candidate['matched_by']}"
    assert candidate['templates'] == [], '模板库里没有该类型时应返回空候选'
    passed += 1
    logging.getLogger().info('  ✅ 模板库无该类型 → 空候选，不报错')

    # ---------- 五、top_k 截断 ----------
    logging.getLogger().info('=' * 20 + ' 五、top_k 截断 ' + '=' * 20)

    state['contract_type'] = '采购'
    state['risk_items'] = [_item(1, 'payment')]
    result = NodeTemplateRecall(config=RecommendConfig(template_recall_top_k=1))(state)
    assert len(result['candidates'][0]['templates']) == 1, 'top_k=1 应只留 1 条候选'
    assert result['candidates'][0]['templates'][0]['title'] == '采购付款条款（示范）', \
        '截断后留下的必须是最优候选'
    passed += 1
    logging.getLogger().info('  ✅ top_k 生效，保留得分最高的候选')

    # ---------- 六、与 risk_items 按下标一一对应 ----------
    logging.getLogger().info('=' * 20 + ' 六、下标对应 ' + '=' * 20)

    state['contract_type'] = '采购'
    state['risk_items'] = [_item(7, 'payment'), _item(8, 'ip'), _item(9, 'force_majeure')]
    result = node_recall(state)

    assert len(result['candidates']) == 3, '每个风险点都应有一条候选记录'
    assert [c['index'] for c in result['candidates']] == [0, 1, 2], 'index 必须是下标'
    assert [c['db_risk_id'] for c in result['candidates']] == [7, 8, 9], '顺序必须与风险项一致'
    assert result['candidates'][1]['templates'][0]['title'] == '知识产权归属条款（示范）'
    passed += 1
    logging.getLogger().info('  ✅ candidates 与 risk_items 按下标严格一一对应')

    # ---------- 七、空风险项 ----------
    logging.getLogger().info('=' * 20 + ' 七、空风险项 ' + '=' * 20)

    empty_state = get_default_state()
    empty_state['task_id'] = '1'
    result = node_recall(empty_state)
    assert result['candidates'] == [], '无风险项时不应召回'
    passed += 1
    logging.getLogger().info('  ✅ 无风险项 → 空候选，不报错')

    reset_engine()
    logging.getLogger().info(f'node_template_recall 自测全部通过 ✅ 共 {passed} 个用例')
