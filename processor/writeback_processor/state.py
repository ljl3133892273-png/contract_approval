'''processor/writeback_processor/state.py'''

"""
回写流程状态类型定义

定义完整的状态结构和辅助函数。

与解析子图的差别:
    回写子图同样是**独立子图**，初始状态只需要 task_id ——
    `node_build_comment` 会自己把任务、元数据、条款、风险项全部载入。
    这样 web 层串联时不必在内存里搬运大对象（SPEC 2.2 的分层意图）。

与其他子图的另一处差别:
    本子图的状态里带**两个状态机字段**：
    `task['review_status']`（只读，用于展示）与 `writeback_status`（本子图负责推进）。
    回写失败**绝不回退** review_status（SPEC 11.1）—— 审查结果本身是有效的。
"""

from typing import TypedDict, List, Dict, Any
import copy


class WritebackGraphState(TypedDict):
    """
    回写流程图状态

    包含整个回写流程中传递的所有数据。
    """

    # ==================== 任务标识 ====================
    task_id: str  # 任务ID（contract_task.id）

    # ==================== 载入的输入（node_build_comment 产出）====================
    task: Dict[str, Any]  # contract_task 概要快照
    meta: Dict[str, Any]  # ContractMeta 合同元数据
    clauses: List[Dict[str, Any]]  # Clause[]（用于「涉及条款」列与页码）
    risk_items: List[Dict[str, Any]]  # risk_item[]（含条款信息与法务批注）
    external_doc_id: str  # 审批单号（回写目标）
    risk_count: int  # 风险项总数

    # ==================== 评论构建（node_build_comment 产出）====================
    comment_md: str  # 按 SPEC 10.1 模板渲染出的 Markdown 审查意见
    highlights: List[str]  # 重点关注事项
    idempotency_key: str  # 幂等键：task_id + 审批单号（SPEC 4.5）

    # ==================== 回写（node_adapter_send / node_writeback_log 产出）====================
    writeback_from_status: str  # 发起回写前的 writeback_status（审计用）
    writeback_status: str  # not_written / writing / success / failed
    writeback_is_mock: bool  # 是否走了 mock Adapter（未配置审批地址时的降级）
    external_comment_id: str  # 审批系统返回的评论 ID
    writeback_attempts: int  # 实际尝试次数
    writeback_error: str  # 失败原因（成功时为空）
    writeback_duplicated: bool  # 审批系统判定为重复提交（同幂等键已存在）

    # ==================== 报告导出（node_report_export 产出）====================
    report_md: str  # 报告 Markdown 正文
    report_md_path: str  # 落盘的 .md 路径
    report_pdf_path: str  # 落盘的 .pdf 路径
    report_id: int  # report 表记录主键（最近一条）


GRAPH_DEFAULT_STATE: WritebackGraphState = {
    'task_id': '',

    "task": {},
    "meta": {},
    "clauses": [],
    "risk_items": [],
    "external_doc_id": "",
    "risk_count": 0,

    "comment_md": "",
    "highlights": [],
    "idempotency_key": "",

    "writeback_from_status": "",
    "writeback_status": "",
    "writeback_is_mock": False,
    "external_comment_id": "",
    "writeback_attempts": 0,
    "writeback_error": "",
    "writeback_duplicated": False,

    "report_md": "",
    "report_md_path": "",
    "report_pdf_path": "",
    "report_id": 0,
}


def get_default_state() -> WritebackGraphState:
    """
    获取默认状态副本
    :return: 状态副本（避免全局污染）
    """
    return copy.deepcopy(GRAPH_DEFAULT_STATE)


if __name__ == '__main__':
    # 轻量自测
    first = get_default_state()
    first['risk_items'].append({'db_risk_id': 1})
    first['task_id'] = 'task-0001'
    first['task']['review_status'] = 'completed'

    second = get_default_state()
    assert second['task_id'] == '', '新状态不应被上一次修改污染'
    assert second['risk_items'] == [], '新状态的列表不应被污染'
    assert second['task'] == {}, '新状态的字典不应被污染（deepcopy 生效）'
    assert second['writeback_status'] == '', 'writeback_status 初始应为空（由节点载入）'

    # 默认状态与 TypedDict 字段必须一一对应
    assert set(GRAPH_DEFAULT_STATE.keys()) == set(WritebackGraphState.__annotations__.keys()), \
        '默认状态与 TypedDict 的字段必须一一对应'

    # 关键字段类型自检（防止后续误改类型）
    assert isinstance(GRAPH_DEFAULT_STATE['risk_count'], int)
    assert isinstance(GRAPH_DEFAULT_STATE['writeback_is_mock'], bool)
    assert isinstance(GRAPH_DEFAULT_STATE['report_id'], int)

    print('writeback_processor.state 自测通过 ✅')
