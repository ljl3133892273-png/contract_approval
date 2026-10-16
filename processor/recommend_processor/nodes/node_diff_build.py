'''processor/recommend_processor/nodes/node_diff_build.py'''

'''
步骤分解
1. 接收状态: 取 `suggestions`（node_llm_rewrite 产出）与 `clause_map`（取条款原文）。
2. 逐条生成差异: 对每条建议，拿「条款原文 → 建议条款」做句子级 LCS 比对。
3. 写回状态 + 输出统计日志。

需求分析
输入:
- `suggestions`：每个风险点的最终建议（含 db_clause_id / suggestion_text）
- `clause_map`：{str(db_clause_id): 条款字典}，取原文用

输出:
- `diffs`：与 suggestions **按下标一一对应**的差异结果
   {'segments': [...], 'stats': {...}, 'original': .., 'suggested': ..}

为什么差异对比单独成节点（而不是塞在改写节点里）:
    两方面原因：
    1. 职责单一 —— 改写负责"产出什么"，对比负责"差在哪"，两者的失败模式不同。
       LLM 挂了不影响对比，而对比是纯计算、永远能成功。
    2. 前端依赖稳定 —— 工作台的 diff 组件只吃 `segments` 这一个结构。
       把它固化成独立节点，前端改动不会牵连 LLM 那侧的逻辑。

关于文档级风险（clause_id 为 NULL）:
    原文为空 → 差异结果就是「整段新增」。这不是异常，而是正确的表达：
    R-01 主体缺失这类风险，建议本来就是"补一条新条款"，没有可对比的旧文本。

关于"无建议"的条目:
    LLM 与模板都没给出内容时（node_llm_rewrite 如实留空），这里**不做差异对比**：
    拿空建议去和原文比会产出一段"整篇删除"的假差异，反而误导法务。
    这类条目标记 `skipped=True`，前端据此显示"未生成建议"。
'''

import logging
from typing import Any, Dict, List

from processor.recommend_processor.base import BaseNode, setup_logging
from processor.recommend_processor.state import RecommendGraphState, get_default_state
from utils.diff_utils import diff_text


class NodeDiffBuild(BaseNode):
    '''差异对比节点：生成「条款原文 vs 建议条款」的句子级差异'''
    name = 'node_diff_build'

    def process(self, state: RecommendGraphState) -> RecommendGraphState:
        """
        节点：差异对比（node_diff_build）
        整体流程：取建议与条款原文 → 逐条比对 → 写回状态 + 统计日志
        核心目的：让法务一眼看清"建议把哪句改成了哪句"，而不是自己逐字比对
        必要参数：suggestions（允许为空）
        更新参数：diffs

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取输入=======
        suggestions = state.get('suggestions') or []
        clause_map = state.get('clause_map') or {}

        if not suggestions:
            self.log_step('建议', '0 条 —— 无需对比')
            state['diffs'] = []
            return state

        # =======步骤二:逐条生成差异=======
        diffs = self._step_2_build_diffs(suggestions, clause_map)

        # =======步骤三:写回状态 + 输出统计日志=======
        state['diffs'] = diffs
        self._step_3_print_stats(suggestions, diffs)

        return state

    def _step_2_build_diffs(self, suggestions: List[Dict[str, Any]],
                            clause_map: Dict[str, Any]) -> List[Dict[str, Any]]:
        """
        【步骤2】逐条生成差异
        :param suggestions: 建议列表
        :param clause_map: 条款索引
        :return: 与 suggestions 同下标的差异列表
        """
        diffs: List[Dict[str, Any]] = []

        for index, suggestion in enumerate(suggestions):
            original = self._original_of(suggestion, clause_map)
            suggested = (suggestion.get('suggestion_text') or '').strip()

            base = {
                'index': index,
                'db_risk_id': suggestion.get('db_risk_id'),
                'db_clause_id': suggestion.get('db_clause_id'),
                'clause_id': suggestion.get('clause_id') or '',
                'original': original,
                'suggested': suggested,
            }

            # 1.没有建议正文 → 不做对比（拿空建议比会产出"整篇删除"的假差异）
            if not suggested:
                base.update({
                    'skipped': True,
                    'skip_reason': '未生成建议内容',
                    'segments': [],
                    'stats': self._empty_stats(original, suggested),
                })
                diffs.append(base)
                continue

            # 2.正常对比；文档级风险原文为空 → 结果就是"整段新增"，这是正确表达
            result = diff_text(original, suggested, max_cells=self.config.diff_max_cells)
            base.update({
                'skipped': False,
                'skip_reason': '',
                'segments': result['segments'],
                'stats': result['stats'],
            })

            # 3.LLM/模板产出的建议与原文完全相同 → 告知调用方"这条没改动"
            if not result['stats']['changed']:
                self.logger.warning(
                    f'风险项 {suggestion.get("db_risk_id")} 的建议条款与原文完全一致，'
                    f'该条实际上没有产生修改'
                )
                base['identical'] = True
            else:
                base['identical'] = False

            diffs.append(base)

        return diffs

    @staticmethod
    def _original_of(suggestion: Dict[str, Any], clause_map: Dict[str, Any]) -> str:
        """
        【辅助函数】取该建议对应的条款原文
        :param suggestion: 建议
        :param clause_map: 条款索引
        :return: 条款原文；文档级风险或条款已删除时返回空字符串
        """
        db_clause_id = suggestion.get('db_clause_id')
        if db_clause_id is None:
            return ''
        clause = clause_map.get(str(db_clause_id))
        return (clause or {}).get('content') or ''

    @staticmethod
    def _empty_stats(original: str, suggested: str) -> Dict[str, Any]:
        """【辅助函数】构造空差异的统计结构（保持 stats 字段形状一致）"""
        return {
            'original_chars': len(original),
            'suggested_chars': len(suggested),
            'added_chars': 0,
            'removed_chars': 0,
            'changed': False,
            'changed_ratio': 0.0,
        }

    def _step_3_print_stats(self, suggestions: List[Dict[str, Any]],
                            diffs: List[Dict[str, Any]]) -> None:
        """
        【步骤3】输出统计日志
        """
        compared = [d for d in diffs if not d['skipped']]
        changed = [d for d in compared if d['stats']['changed']]
        identical = [d for d in compared if d.get('identical')]
        new_clause = [d for d in compared if not d['original']]

        self.log_step('待对比建议', f'{len(suggestions)} 条')
        self.log_step('实际对比', f'{len(compared)} 条')
        self.log_step('有改动', f'{len(changed)} 条')
        self.log_step('新增条款', f'{len(new_clause)} 条（原文为空，属整段新增）')
        self.log_step('跳过对比', f'{len(diffs) - len(compared)} 条（未生成建议）')

        if identical:
            self.logger.warning(
                f'{len(identical)} 条建议与原文完全一致，实际上没有产生修改'
            )


if __name__ == '__main__':
    # 激活日志的全局配置
    setup_logging()

    from processor.recommend_processor.config import RecommendConfig

    node_diff = NodeDiffBuild()
    passed = 0

    def _suggestion(index: int, db_clause_id, suggested: str, **overrides) -> Dict[str, Any]:
        """构造一条建议"""
        item = {
            'index': index,
            'db_risk_id': index,
            'db_clause_id': db_clause_id,
            'clause_id': f'cl_{db_clause_id:03d}' if db_clause_id else '',
            'suggestion_text': suggested,
            'source': 'llm',
        }
        item.update(overrides)
        return item

    clause_map = {
        '101': {'content': '甲方应在收到货物后 3 日内支付全款。乙方不承担任何违约责任。'},
        '102': {'content': '知识产权归乙方所有。'},
    }

    # ---------- 一、正常对比 ----------
    logging.getLogger().info('=' * 20 + ' 一、正常对比 ' + '=' * 20)

    state = get_default_state()
    state['task_id'] = '1'
    state['clause_map'] = clause_map
    state['suggestions'] = [_suggestion(1, 101, '甲方应在验收合格后 15 个工作日内支付合同款项。'
                                                  '乙方应按合同总额的 10% 承担违约责任。')]
    result = node_diff(state)
    diff = result['diffs'][0]

    assert diff['skipped'] is False
    assert diff['original'].startswith('甲方应在收到货物后'), '必须带原文'
    assert diff['suggested'].startswith('甲方应在验收合格后'), '必须带建议'
    ops_used = {seg['op'] for seg in diff['segments']}
    assert 'delete' in ops_used and 'insert' in ops_used, '改写应同时产生删除与新增段'
    assert diff['stats']['changed'] is True
    assert diff['stats']['added_chars'] > 0 and diff['stats']['removed_chars'] > 0
    assert diff['identical'] is False
    passed += 1
    logging.getLogger().info('  ✅ 原文 vs 建议 → 句子级差异，新增/删除字符数正确')

    # ---------- 二、文档级风险（原文为空）----------
    logging.getLogger().info('=' * 20 + ' 二、文档级风险 ' + '=' * 20)

    state['suggestions'] = [_suggestion(1, None, '第一条 主体资质：乙方应提供年检有效的营业执照。')]
    result = node_diff(state)
    diff = result['diffs'][0]

    assert diff['original'] == '', '文档级风险没有原文'
    assert diff['skipped'] is False, '原文为空不等于跳过'
    assert [seg['op'] for seg in diff['segments']] == ['insert'], '应表达为整段新增'
    assert diff['stats']['removed_chars'] == 0
    assert diff['stats']['added_chars'] > 0
    passed += 1
    logging.getLogger().info('  ✅ 文档级风险 → 原文为空，差异表达为整段新增')

    # ---------- 三、无建议内容 → 不对比 ----------
    logging.getLogger().info('=' * 20 + ' 三、无建议跳过 ' + '=' * 20)

    state['suggestions'] = [_suggestion(1, 101, ''), _suggestion(2, 102, '知识产权归甲方所有。')]
    result = node_diff(state)
    diffs = result['diffs']

    assert diffs[0]['skipped'] is True, '无建议内容应跳过对比'
    assert diffs[0]['segments'] == [], '跳过的条目不应产出差异段'
    assert diffs[0]['stats']['removed_chars'] == 0, \
        '跳过的条目绝不能产出"整篇删除"的假差异'
    assert '未生成建议' in diffs[0]['skip_reason']
    assert diffs[1]['skipped'] is False, '有建议的条目照常对比'
    assert diffs[1]['stats']['changed'] is True
    passed += 1
    logging.getLogger().info('  ✅ 未生成建议 → 跳过对比，不产出误导性的假差异')

    # ---------- 四、建议与原文一致 ----------
    logging.getLogger().info('=' * 20 + ' 四、建议未改动 ' + '=' * 20)

    state['suggestions'] = [_suggestion(1, 102, '知识产权归乙方所有。')]
    result = node_diff(state)
    diff = result['diffs'][0]

    assert diff['identical'] is True, '与原文一致时应标记 identical'
    assert diff['stats']['changed'] is False
    assert all(seg['op'] == 'equal' for seg in diff['segments'])
    passed += 1
    logging.getLogger().info('  ✅ 建议与原文一致 → identical=True，提醒该条未产生修改')

    # ---------- 五、下标一一对应 ----------
    logging.getLogger().info('=' * 20 + ' 五、下标对应 ' + '=' * 20)

    state['suggestions'] = [
        _suggestion(11, 101, '新的付款条款。'),
        _suggestion(12, None, '新的主体条款。'),
        _suggestion(13, 102, ''),
    ]
    result = node_diff(state)
    diffs = result['diffs']

    assert len(diffs) == 3, '每条建议都应有一条差异记录'
    assert [d['index'] for d in diffs] == [0, 1, 2], 'index 必须是下标'
    assert [d['db_risk_id'] for d in diffs] == [11, 12, 13], '顺序必须与建议一致'
    passed += 1
    logging.getLogger().info('  ✅ diffs 与 suggestions 按下标严格一一对应')

    # ---------- 六、diff_max_cells 透传 ----------
    logging.getLogger().info('=' * 20 + ' 六、超限退化 ' + '=' * 20)

    long_original = '\n'.join(f'第{i}句原内容。' for i in range(200))
    long_suggested = '\n'.join(f'第{i}句新内容。' for i in range(200))
    clause_map['103'] = {'content': long_original}
    state['clause_map'] = clause_map
    state['suggestions'] = [_suggestion(1, 103, long_suggested)]
    result = NodeDiffBuild(config=RecommendConfig(diff_max_cells=100))(state)
    ops = [seg['op'] for seg in result['diffs'][0]['segments']]
    assert ops == ['delete', 'insert'], f'超限应退化为整体替换，实际 {ops}'
    passed += 1
    logging.getLogger().info('  ✅ diff_max_cells 生效，超限退化为整体替换（不卡死）')

    # ---------- 七、条款已删除 ----------
    logging.getLogger().info('=' * 20 + ' 七、条款缺失 ' + '=' * 20)

    state['clause_map'] = {}
    state['suggestions'] = [_suggestion(1, 999, '建议条款正文。')]
    result = node_diff(state)
    assert result['diffs'][0]['original'] == '', '条款不存在时原文应为空'
    assert result['diffs'][0]['skipped'] is False, '仍应产出差异（整段新增）'
    passed += 1
    logging.getLogger().info('  ✅ 条款已删除 → 原文为空但不报错')

    # ---------- 八、空建议列表 ----------
    logging.getLogger().info('=' * 20 + ' 八、空建议 ' + '=' * 20)

    state['suggestions'] = []
    result = node_diff(state)
    assert result['diffs'] == [], '无建议时不应产出差异'
    passed += 1
    logging.getLogger().info('  ✅ 无建议 → 空差异，不报错')

    logging.getLogger().info(f'node_diff_build 自测全部通过 ✅ 共 {passed} 个用例')
