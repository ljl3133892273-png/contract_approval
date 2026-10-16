'''processor/parse_processor/state.py'''

"""
解析流程状态类型定义

定义完整的状态结构和辅助函数

设计要点:
    1. 用 TypedDict 描述图状态，键名与节点读写严格对应。
    2. 提供 GRAPH_DEFAULT_STATE 全量默认值 + get_default_state() 深拷贝工厂，
       避免多次运行共用同一份可变对象（list/dict）造成状态污染。
"""

from typing import TypedDict, List, Dict, Any
import copy


class ParseGraphState(TypedDict):
    """
    解析流程图状态

    包含整个解析流程中传递的所有数据。
    """

    # ==================== 任务标识 ====================
    task_id: str  # 任务ID，用于任务追踪

    # ==================== 接入来源 ====================
    source_type: str  # 接入方式：upload / approve_push / approve_pull
    external_doc_id: str  # 审批单号（附件去重键 + 回写幂等键）

    # ==================== 控制标志（互斥，供条件路由使用）====================
    is_docx_read_enabled: bool  # 是否启用 DOCX 解析
    is_pdf_read_enabled: bool  # 是否启用 PDF 解析
    is_image_read_enabled: bool  # 是否启用 图片/扫描件 OCR

    # ==================== 路径信息 ====================
    import_file_path: str  # 接入的合同文件绝对路径（原始输入）
    file_dir: str  # 该任务的本地独立工作目录
    docx_path: str  # DOCX 文件路径
    pdf_path: str  # PDF 文件路径
    image_path: str  # 图片/扫描件路径

    # ==================== 文件信息 ====================
    file_name: str  # 文件名（含扩展名）
    file_ext: str  # 扩展名（小写、带点，如 .pdf）
    file_title: str  # 文件标题（不含扩展名）
    file_hash: str  # SHA256 文件指纹，用于附件去重
    file_size: int  # 文件字节数

    # ==================== 落盘与去重（node_fetch_file 产出）====================
    stored_file_path: str  # 归档到任务工作目录后的规范本地路径（后续解析统一读这里）
    is_duplicate: bool  # 是否命中重复（同指纹 + 同审批单号）
    duplicated_task_id: str  # 命中的已有任务ID（空字符串表示未命中）

    # ==================== PDF 解析控制（node_parse_pdf 产出）====================
    pdf_needs_ocr: bool  # PDF 无文本层（扫描件）→ 需转 node_ocr 走 OCR 识别
    page_count: int  # PDF 总页数（图片/扫描件为 0）

    # ==================== OCR 结果标识（node_ocr 产出）====================
    ocr_is_mock: bool  # OCR 结果是否来自 mock（未安装 PaddleOCR 时为 True，前端据此提示）

    # ==================== 处理中间数据 ====================
    fragments: List[Dict[str, Any]]  # DocFragment[] 统一中间结构（段落/表格/页眉页脚）
    clauses: List[Dict[str, Any]]  # Clause[] 条款列表（含 Locator）
    meta: Dict[str, Any]  # ContractMeta 合同元数据
    full_text: str  # 去噪后的全文（Locator 的全局字符偏移基准）


GRAPH_DEFAULT_STATE: ParseGraphState = {
    'task_id': '',

    "source_type": "",
    "external_doc_id": "",

    "is_docx_read_enabled": False,
    "is_pdf_read_enabled": False,
    "is_image_read_enabled": False,

    "file_dir": "",
    "import_file_path": "",
    "docx_path": "",
    "pdf_path": "",
    "image_path": "",

    "file_name": "",
    "file_ext": "",
    "file_title": "",
    "file_hash": "",
    "file_size": 0,

    "stored_file_path": "",
    "is_duplicate": False,
    "duplicated_task_id": "",

    "pdf_needs_ocr": False,
    "page_count": 0,

    "ocr_is_mock": False,

    "fragments": [],
    "clauses": [],
    "meta": {},
    "full_text": "",
}


def get_default_state() -> ParseGraphState:
    """
    获取默认状态副本
    :return: 状态副本（避免全局污染）
    """
    return copy.deepcopy(GRAPH_DEFAULT_STATE)  # `copy.deepcopy()`避免修改全局模板 每次创建独立的状态实例


if __name__ == '__main__':
    # 轻量自测：验证深拷贝不会污染全局模板
    first = get_default_state()
    first['task_id'] = 'task-0001'
    first['fragments'].append({'seq': 1, 'text': '甲方应于…'})

    second = get_default_state()
    assert second['task_id'] == '', '新状态的任务ID不应被上一次修改污染'
    assert second['fragments'] == [], '新状态的列表不应被上一次 append 污染'

    # 验证所有键都已定义默认值
    assert set(GRAPH_DEFAULT_STATE.keys()) == set(ParseGraphState.__annotations__.keys()), \
        '默认状态与 TypedDict 的字段必须一一对应'

    print('parse_processor.state 自测通过 ✅')
