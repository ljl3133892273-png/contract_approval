'''utils/task_utils.py'''

'''
节点级任务进度追踪（内存态 · 单进程）

作用：
    记录「某一个任务在 LangGraph 里跑到第几个节点了」，
    供前端轮询 /status/{task_id} 实时展示进度条。

职责边界（重要）：
    1. 本模块只维护【节点级进度】（running_list / done_list），纯内存、无 IO。
    2. 业务主状态（pending / parsing / reviewing / blocked / completed）
       由 SPEC 第 5 章的 Flow StateMachine 统一驱动，并落到 contract_task.review_status 表，
       两者职责不同，不要混用。

数据结构：
    key: task_id
    value: 节点名列表（节点的英文 name，如 node_entry）
'''

from typing import Dict, List, Sequence

# ---------------------------
# 内存态任务追踪（单进程）
# ---------------------------
# key: task_id → value: 正在运行的节点名列表
_tasks_running_list: Dict[str, List[str]] = {}
# key: task_id → value: 已完成的节点名列表（保持完成顺序）
_tasks_done_list: Dict[str, List[str]] = {}

# 节点名 -> 中文名映射（用于前端展示）
# 说明：这里的 key 必须与 LangGraph 的 add_node('xxx', ...) 中的节点名保持一致。
_NODE_NAME_TO_CN: Dict[str, str] = {
    'upload_file': '开始上传文件',
    # --- 解析子图 parse_processor ---
    'node_entry': '校验文件类型',
    'node_fetch_file': '附件落盘去重',
    'node_detect_type': '嗅探文件格式',
    'node_parse_docx': '解析 DOCX',
    'node_parse_pdf': '解析 PDF',
    'node_ocr': '扫描件识别',
    'node_clean_text': '文本预处理',
    'node_clause_split': '条款切分',
    'node_meta_extract': '元数据提取',
    'node_persist_parse': '解析结果落库',
    # --- 审查子图 review_processor ---
    'node_load_clause': '载入条款',
    'node_rule_match': '规则引擎筛查',
    'node_llm_review': '大模型语义研判',
    'node_risk_aggregate': '综合风险计算',
    'node_persist_risk': '风险项落库',
    # --- 推荐子图 recommend_processor ---
    'node_load_risk': '载入风险项',
    'node_template_recall': '示范条款召回',
    'node_llm_rewrite': '建议条款改写',
    'node_diff_build': '差异对比生成',
    'node_persist_suggestion': '修改建议落库',
    # --- 回写子图 writeback_processor ---
    'node_build_comment': '构建审查意见',
    'node_adapter_send': '调用审批接口回写',
    'node_writeback_log': '回写结果落库',
    'node_report_export': '导出审查报告',
    # --- 结束 ---
    '__end__': '处理完成',
    'END': '处理完成',
}


def _ensure_task(task_id: str) -> None:
    """确保 task_id 对应的数据结构已初始化。"""
    if task_id not in _tasks_running_list:
        _tasks_running_list[task_id] = []
    if task_id not in _tasks_done_list:
        _tasks_done_list[task_id] = []


def _to_cn(node_name: str) -> str:
    """将节点名转换为中文展示名；若无映射则返回原名。"""
    return _NODE_NAME_TO_CN.get(node_name, node_name)


def add_running_task(task_id: str, node_name: str) -> None:
    """
    标记某个节点为【正在运行】。

    参数：
    - task_id: 任务ID
    - node_name: 节点名称(节点ID)
    """
    _ensure_task(task_id)
    running = _tasks_running_list[task_id]
    # 避免重复追加
    if node_name not in running:
        running.append(node_name)


def add_done_task(task_id: str, node_name: str) -> None:
    """
    标记某个节点为【已完成】。

    注意：添加已完成任务时，会把同名的"正在运行"任务一并移除。

    参数：
    - task_id: 任务ID
    - node_name: 节点名称(节点ID)
    """
    _ensure_task(task_id)

    # 1) 从 running 中移除同名节点（可能出现重复，移除所有）
    running = _tasks_running_list[task_id]
    _tasks_running_list[task_id] = [n for n in running if n != node_name]

    # 2) 追加到 done（保持完成顺序），避免重复
    done = _tasks_done_list[task_id]
    if node_name not in done:
        done.append(node_name)


def get_done_task_list(task_id: str) -> List[str]:
    """获取已完成节点列表（中文展示）。"""
    _ensure_task(task_id)
    done = _tasks_done_list.get(task_id, [])
    return [_to_cn(n) for n in done]


def get_running_task_list(task_id: str) -> List[str]:
    """获取正在运行节点列表（中文展示）。"""
    _ensure_task(task_id)
    running = _tasks_running_list.get(task_id, [])
    return [_to_cn(n) for n in running]


def clear_task(task_id: str) -> None:
    """清理某个任务的节点进度缓存（任务结束或重试时调用）。"""
    _tasks_running_list.pop(task_id, None)
    _tasks_done_list.pop(task_id, None)


# ============================================================
# 节点分组与进度计算（供接入层的 /progress 接口使用）
# ============================================================
# 说明：这里只描述"跑起来一共会经过哪些节点"，用于算进度百分比。
# 与实际图结构是**两份信息**，但节点名与 main_graph 里的 add_node 名称一一对应，
# 改名时两边都要改（自测里会校验映射表里有这些名字，漏改会直接报错）。
PARSE_NODES = (
    'node_entry', 'node_fetch_file', 'node_detect_type',
    # 三个解析分支互斥，进度按"至少经过其中一个"计（见 get_progress 的说明）
    'node_parse_docx', 'node_parse_pdf', 'node_ocr',
    'node_clean_text', 'node_clause_split', 'node_meta_extract', 'node_persist_parse',
)
REVIEW_NODES = (
    'node_load_clause', 'node_rule_match', 'node_llm_review',
    'node_risk_aggregate', 'node_persist_risk',
)
RECOMMEND_NODES = (
    'node_load_risk', 'node_template_recall', 'node_llm_rewrite',
    'node_diff_build', 'node_persist_suggestion',
)
WRITEBACK_NODES = (
    'node_build_comment', 'node_adapter_send', 'node_writeback_log', 'node_report_export',
)

# 自动链路：解析 → 审查 → 推荐（web 层顺序调用）
AUTO_PIPELINE_NODES = PARSE_NODES + REVIEW_NODES + RECOMMEND_NODES

# 解析分支节点：三者只会走一个，算进度时按"最多算一个"计
_EXCLUSIVE_BRANCHES = ('node_parse_docx', 'node_parse_pdf', 'node_ocr')


def get_progress(task_id: str, nodes: Sequence[str] = None) -> Dict[str, object]:
    """
    计算某个任务的节点级进度快照

    参数：
    - task_id: 任务ID
    - nodes: 期望经过的节点序列，默认自动链路（解析+审查+推荐）

    返回：
    - {'done': [...中文...], 'running': [...中文...], 'total': int,
       'finished': int, 'percent': int, 'finished_names': [...英文...]}

    关于解析分支：
        docx / pdf / ocr 是互斥分支，只会走一个。所以进度计算时把它们
        合并成一个"槽位"（走了任意一个就算 1 个），否则百分比永远到不了 100%。
    """
    nodes = tuple(nodes) if nodes else AUTO_PIPELINE_NODES
    done_raw = _tasks_done_list.get(task_id, [])

    # 把互斥分支折算成一个槽位
    branch_nodes = [n for n in nodes if n in _EXCLUSIVE_BRANCHES]
    others = [n for n in nodes if n not in _EXCLUSIVE_BRANCHES]
    total = len(others) + (1 if branch_nodes else 0)

    finished = len([n for n in others if n in done_raw])
    if branch_nodes and any(n in done_raw for n in branch_nodes):
        finished += 1

    percent = int(round(finished / total * 100)) if total else 0
    return {
        'done': get_done_task_list(task_id),
        'running': get_running_task_list(task_id),
        'total': total,
        'finished': finished,
        'percent': min(100, percent),
        'finished_names': [n for n in nodes if n in done_raw],
    }


if __name__ == '__main__':
    # 轻量自测：验证 running → done 的流转与中文映射
    demo_task_id = 'demo-task-0001'
    add_running_task(demo_task_id, 'node_entry')
    assert get_running_task_list(demo_task_id) == ['校验文件类型'], 'running 列表应包含中文节点名'

    add_done_task(demo_task_id, 'node_entry')
    assert get_running_task_list(demo_task_id) == [], '节点完成后应从 running 列表移除'
    assert get_done_task_list(demo_task_id) == ['校验文件类型'], '节点完成后应进入 done 列表'

    # 未在映射表中的节点名，原样返回
    add_done_task(demo_task_id, 'node_not_mapped')
    assert get_done_task_list(demo_task_id)[-1] == 'node_not_mapped', '未映射节点名应原样返回'

    clear_task(demo_task_id)
    assert get_done_task_list(demo_task_id) == [], '清理后 done 列表应为空'

    # 所有节点名都必须在中文映射表里（漏改映射会导致前端显示英文节点名）
    for node_name in (PARSE_NODES + REVIEW_NODES + RECOMMEND_NODES + WRITEBACK_NODES):
        assert node_name in _NODE_NAME_TO_CN, f'节点 {node_name} 缺少中文名映射'

    # 进度：互斥分支只算一个槽位，否则百分比永远到不了 100%
    progress_task_id = 'progress-demo'
    empty = get_progress(progress_task_id)
    assert empty['finished'] == 0 and empty['percent'] == 0
    assert empty['total'] == len(PARSE_NODES) + len(REVIEW_NODES) + len(RECOMMEND_NODES) - 2,         f"互斥分支应折算成一个槽位，实际 total={empty['total']}"

    for node_name in PARSE_NODES + REVIEW_NODES + RECOMMEND_NODES:
        add_done_task(progress_task_id, node_name)
    full = get_progress(progress_task_id)
    assert full['percent'] == 100, f"全部走完应为 100%，实际 {full['percent']}%"
    assert full['finished'] == full['total']

    # 子图各自也能单独算
    parse_progress = get_progress(progress_task_id, PARSE_NODES)
    assert parse_progress['percent'] == 100
    clear_task(progress_task_id)

    print('task_utils 自测通过 ✅')
