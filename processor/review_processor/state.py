'''processor/review_processor/state.py'''

"""
审查流程状态类型定义

定义完整的状态结构和辅助函数。

与解析子图的差别:
    审查子图是**独立子图**，初始状态只需要 task_id ——
    `node_load_clause` 会自己按 task_id 从库里把条款与元数据捞出来。
    这样 web 层串联子图时不必在内存里搬运大对象（SPEC 2.2 的分层意图）。
"""

from typing import TypedDict, List, Dict, Any
import copy


class ReviewGraphState(TypedDict):
    """
    审查流程图状态

    包含整个审查流程中传递的所有数据。
    """

    # ==================== 任务标识 ====================
    task_id: str  # 任务ID（contract_task.id）

    # ==================== 载入的审查输入（node_load_clause 产出）====================
    clauses: List[Dict[str, Any]]  # Clause[]（含 Locator）
    meta: Dict[str, Any]  # ContractMeta 合同元数据
    full_text: str  # 去噪后全文（可选，来自解析状态）
    contract_type: str  # 合同类型（采购/销售/服务/劳动），用于规则匹配

    # ==================== 规则引擎（node_rule_match 产出）====================
    rules: List[Dict[str, Any]]  # 载入的启用规则
    rule_risk_items: List[Dict[str, Any]]  # 规则层产出的风险项

    # ==================== LLM 语义审查（node_llm_review 产出）====================
    llm_risk_items: List[Dict[str, Any]]  # LLM 层产出的风险项
    llm_is_mock: bool  # LLM 结果是否来自 mock
    llm_failed: bool  # LLM 是否整体失败（触发降级）
    llm_fail_count: int  # 连续失败次数（达到阈值触发 REVIEW_FAIL）

    # ==================== 合并与综合风险（node_risk_aggregate 产出）====================
    risk_items: List[Dict[str, Any]]  # 合并去重后的最终风险项
    overall_risk_level: str  # 综合风险等级 high / mid / low / none
    review_conclusion: str  # 审查结论文案
    review_note: str  # 结论的附加说明（如「深度语义审查未完成」），前端据此提示


GRAPH_DEFAULT_STATE: ReviewGraphState = {
    'task_id': '',

    "clauses": [],
    "meta": {},
    "full_text": "",
    "contract_type": "*",

    "rules": [],
    "rule_risk_items": [],

    "llm_risk_items": [],
    "llm_is_mock": False,
    "llm_failed": False,
    "llm_fail_count": 0,

    "risk_items": [],
    "overall_risk_level": "",
    "review_conclusion": "",
    "review_note": "",
}


def get_default_state() -> ReviewGraphState:
    """
    获取默认状态副本
    :return: 状态副本（避免全局污染）
    """
    return copy.deepcopy(GRAPH_DEFAULT_STATE)


if __name__ == '__main__':
    # 轻量自测
    first = get_default_state()
    first['clauses'].append({'clause_id': 'cl_001'})
    first['task_id'] = 'task-0001'

    second = get_default_state()
    assert second['task_id'] == '', '新状态不应被上一次修改污染'
    assert second['clauses'] == [], '新状态的列表不应被污染'
    assert second['contract_type'] == '*', '合同类型默认应为通配'

    # 默认状态与 TypedDict 字段必须一一对应
    assert set(GRAPH_DEFAULT_STATE.keys()) == set(ReviewGraphState.__annotations__.keys()), \
        '默认状态与 TypedDict 的字段必须一一对应'

    print('review_processor.state 自测通过 ✅')
