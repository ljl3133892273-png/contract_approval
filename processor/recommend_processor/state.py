'''processor/recommend_processor/state.py'''

"""
推荐流程状态类型定义

定义完整的状态结构和辅助函数。

与审查子图的差别:
    推荐子图也是**独立子图**，初始状态同样只需要 task_id ——
    `node_load_risk` 会自己按 task_id 把风险项与相关条款从库里捞出来。
    推荐依赖审查的产出，但**不共享内存状态**，通过数据库交接。

数据流:
    node_load_risk          → risk_items / clause_map / meta / contract_type
    node_template_recall    → candidates
    node_llm_rewrite        → suggestions（+ rewrite_is_mock / rewrite_failed / rewrite_fail_count）
    node_diff_build         → diffs
    node_persist_suggestion → persisted_count / skipped_count / suggestion_note
"""

from typing import TypedDict, List, Dict, Any
import copy


class RecommendGraphState(TypedDict):
    """
    推荐流程图状态

    包含整个推荐流程中传递的所有数据。
    """

    # ==================== 任务标识 ====================
    task_id: str  # 任务ID（contract_task.id）

    # ==================== 载入的推荐输入（node_load_risk 产出）====================
    risk_items: List[Dict[str, Any]]  # 待生成建议的风险项（含 db_risk_id / 条款上下文）
    clause_map: Dict[str, Any]  # {str(db_clause_id): 条款字典}，供改写与差异对比取原文
    meta: Dict[str, Any]  # 合同元数据（我方/相对方/金额）
    contract_type: str  # 合同类型（采购/销售/服务/劳动），用于模板召回

    # ==================== 模板召回（node_template_recall 产出）====================
    candidates: List[Dict[str, Any]]  # 每个风险点的候选模板，按得分降序

    # ==================== 建议生成（node_llm_rewrite 产出）====================
    suggestions: List[Dict[str, Any]]  # 每个风险点的最终建议条款
    rewrite_is_mock: bool  # 建议是否来自 mock（前端据此提示）
    rewrite_failed: bool  # LLM 是否整体失败（触发模板模式降级）
    rewrite_fail_count: int  # LLM 失败次数

    # ==================== 差异对比（node_diff_build 产出）====================
    diffs: List[Dict[str, Any]]  # 每个风险点的「原文 vs 建议」差异

    # ==================== 落库（node_persist_suggestion 产出）====================
    persisted_count: int  # 成功回填 suggestion_text 的条数
    skipped_count: int  # 因法务已处理而跳过回填的条数
    suggestion_note: str  # 本次推荐结果的附加说明（如「已退化为模板模式」）


GRAPH_DEFAULT_STATE: RecommendGraphState = {
    'task_id': '',

    "risk_items": [],
    "clause_map": {},
    "meta": {},
    "contract_type": "*",

    "candidates": [],

    "suggestions": [],
    "rewrite_is_mock": False,
    "rewrite_failed": False,
    "rewrite_fail_count": 0,

    "diffs": [],

    "persisted_count": 0,
    "skipped_count": 0,
    "suggestion_note": "",
}


def get_default_state() -> RecommendGraphState:
    """
    获取默认状态副本
    :return: 状态副本（避免全局污染）
    """
    return copy.deepcopy(GRAPH_DEFAULT_STATE)


if __name__ == '__main__':
    # 轻量自测
    first = get_default_state()
    first['risk_items'].append({'db_risk_id': 1})
    first['clause_map']['1'] = {'content': '原文'}
    first['task_id'] = 'task-0001'

    second = get_default_state()
    assert second['task_id'] == '', '新状态不应被上一次修改污染'
    assert second['risk_items'] == [], '新状态的列表不应被污染'
    assert second['clause_map'] == {}, '新状态的字典不应被污染'
    assert second['contract_type'] == '*', '合同类型默认应为通配'

    # 默认状态与 TypedDict 字段必须一一对应
    assert set(GRAPH_DEFAULT_STATE.keys()) == set(RecommendGraphState.__annotations__.keys()), \
        '默认状态与 TypedDict 的字段必须一一对应'

    print('recommend_processor.state 自测通过 ✅')
