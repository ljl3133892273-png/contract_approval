'''processor/parse_processor/main_graph.py'''

'''
目标:
- 构建完整的解析工作流图
- 定义节点执行顺序和条件路由
- 提供便捷的测试入口

需求分析
流程结构:
1. 入口节点: `node_entry` 校验路径 + 扩展名初判
2. 顺序执行: `node_fetch_file` 落盘 + SHA256 指纹
3. `node_detect_type` 文件头魔数嗅探（唯一权威的类别判定）
4. 条件路由: docx → `node_parse_docx`；pdf → `node_parse_pdf`；image → `node_ocr`；都不匹配 → END
5. `node_parse_pdf` 之后再条件路由: 无文本层（扫描件）→ `node_ocr`；有文本层 → `node_clean_text`
6. 顺序执行: `node_clean_text` → `node_clause_split` → `node_meta_extract`
   → `node_persist_parse` → END

关于第 5 步（对原节点图的一处补充）:
    原图把 PDF 一律路由到 node_parse_pdf，但 SPEC 4.2.1 要求「PDF 无文本层 → PaddleOCR」。
    扫描件 PDF 走 node_parse_pdf 只会抽出空文本、静默失败，
    所以这里补了一条 node_parse_pdf → node_ocr 的条件边。

异常流向:
    任一节点抛异常 → LangGraph 中断执行并把异常向上抛给调用方（run_graph_task）。
    由接入层捕获后把任务置为 blocked 并记录 blocked_reason（SPEC 11.1 / 11.2），
    本图内不做阻塞兜底，避免"图内吞异常"导致状态机失控。
'''

import logging

from langgraph.constants import END
from langgraph.graph import StateGraph

from processor.parse_processor.base import setup_logging
from processor.parse_processor.nodes.node_clause_split import NodeClauseSplit
from processor.parse_processor.nodes.node_clean_text import NodeCleanText
from processor.parse_processor.nodes.node_detect_type import NodeDetectType
from processor.parse_processor.nodes.node_entry import NodeEntry
from processor.parse_processor.nodes.node_fetch_file import NodeFetchFile
from processor.parse_processor.nodes.node_meta_extract import NodeMetaExtract
from processor.parse_processor.nodes.node_ocr import NodeOcr
from processor.parse_processor.nodes.node_parse_docx import NodeParseDocx
from processor.parse_processor.nodes.node_parse_pdf import NodeParsePdf
from processor.parse_processor.nodes.node_persist_parse import NodePersistParse
from processor.parse_processor.state import ParseGraphState, get_default_state


class ContractParseWorkflow:
    """解析子图工作流（① parse_processor）"""

    def __init__(self, provider=None):
        """
        初始化工作流对象
        :param provider: LLM Provider 覆盖项（P1 条款分类 / P2 元数据归一化共用）。
                        默认 None 表示按 .env 配置决定（真实/mock）；
                        传入可注入替身，用于自测与确定性场景（与审查/推荐主图同款约定）。
        """
        self._provider = provider
        self._compiled_graph = None

    @property
    def graph(self):
        """
        延迟加载（懒加载）：只在第一次使用工作流对象的时候编译图
        :return: 编译后的图
        """
        if self._compiled_graph is None:
            self._compiled_graph = self.build_graph()
        return self._compiled_graph

    def build_graph(self):
        """
        构建解析工作流图
        :return: 编译后的图
        """
        # 1.初始化图（工作流）
        graph = StateGraph(ParseGraphState)

        # 2.在图中注册节点
        graph.add_node('node_entry', NodeEntry())  # 入口：路径校验 + 扩展名初判
        graph.add_node('node_fetch_file', NodeFetchFile())  # 附件落盘 + SHA256 指纹
        graph.add_node('node_detect_type', NodeDetectType())  # 文件头魔数嗅探（权威判定）
        graph.add_node('node_parse_docx', NodeParseDocx())  # DOCX 解析
        graph.add_node('node_parse_pdf', NodeParsePdf())  # PDF 文本层解析
        graph.add_node('node_ocr', NodeOcr())  # 扫描件/图片 OCR
        graph.add_node('node_clean_text', NodeCleanText())  # 文本预处理 + 建立全局偏移
        graph.add_node('node_clause_split',  # 条款切分（P1 LLM 辅助分类）
                       NodeClauseSplit(provider=self._provider))
        graph.add_node('node_meta_extract',  # 元数据抽取（P2 LLM 归一化）
                       NodeMetaExtract(provider=self._provider))
        graph.add_node('node_persist_parse', NodePersistParse())  # 解析结果落库

        # 3.设置入口节点
        graph.set_entry_point('node_entry')

        # 4.注册顺序边（入口段）
        graph.add_edge('node_entry', 'node_fetch_file')
        graph.add_edge('node_fetch_file', 'node_detect_type')

        # 5.注册条件边：按魔数嗅探结果分流到三个解析节点之一
        graph.add_conditional_edges(
            'node_detect_type',
            self.route_after_detect,
            {
                # key：路由函数的返回值，value：节点的名字
                'node_parse_docx': 'node_parse_docx',
                'node_parse_pdf': 'node_parse_pdf',
                'node_ocr': 'node_ocr',
                END: END,
            }
        )

        # 6.注册条件边：PDF 无文本层时转 OCR（SPEC 4.2.1）
        graph.add_conditional_edges(
            'node_parse_pdf',
            self.route_after_parse_pdf,
            {
                'node_ocr': 'node_ocr',
                'node_clean_text': 'node_clean_text',
            }
        )

        # 7.注册顺序边（汇合段）：三条解析路径都汇到 node_clean_text
        graph.add_edge('node_parse_docx', 'node_clean_text')
        graph.add_edge('node_ocr', 'node_clean_text')

        # 8.注册顺序边（收尾段）
        graph.add_edge('node_clean_text', 'node_clause_split')
        graph.add_edge('node_clause_split', 'node_meta_extract')
        graph.add_edge('node_meta_extract', 'node_persist_parse')
        graph.add_edge('node_persist_parse', END)

        # 9.编译工作流
        return graph.compile()

    @staticmethod
    def route_after_detect(state: ParseGraphState) -> str:
        """
        决定 node_detect_type 之后的走向
        依据是 node_detect_type 用文件头魔数置位的三个互斥开关
        :param state: 图状态
        :return: 下一个节点名；都无法判定时返回 END
        """
        if state.get('is_docx_read_enabled'):
            return 'node_parse_docx'
        if state.get('is_pdf_read_enabled'):
            return 'node_parse_pdf'
        if state.get('is_image_read_enabled'):
            return 'node_ocr'
        return END

    @staticmethod
    def route_after_parse_pdf(state: ParseGraphState) -> str:
        """
        决定 node_parse_pdf 之后的走向
        无文本层（扫描件）→ 转 OCR；有文本层 → 直接进入文本预处理
        :param state: 图状态
        :return: 下一个节点名
        """
        if state.get('pdf_needs_ocr'):
            return 'node_ocr'
        return 'node_clean_text'

    def run(self, state: ParseGraphState, stream: bool = False):
        """
        执行解析工作流
        :param state: 初始状态
        :param stream: True 时以流式返回每个节点的执行结果
        :return: 最终状态，或流式迭代器
        """
        if stream:
            return self.graph.stream(state)
        return self.graph.invoke(state)


if __name__ == '__main__':
    # 激活全局日志
    setup_logging()

    import tempfile
    from pathlib import Path

    import docx
    import fitz
    from sqlalchemy import select

    from db.models import Clause, ContractMeta, ContractTask, TaskEventLog
    from db.session import get_engine, init_db, init_engine, reset_engine, session_scope

    from processor.parse_processor.nodes.node_ocr import is_paddleocr_available

    passed = 0

    # ---------- 准备：临时库 ----------
    work_dir = Path(tempfile.mkdtemp(prefix='parse_graph_test_'))
    reset_engine()
    init_engine(f'sqlite:///{work_dir.as_posix()}/graph_test.db')
    init_db()

    def _create_task(name: str) -> int:
        """建一条待审查任务，返回数据库主键"""
        with session_scope() as session:
            session.add(ContractTask(contract_name=name, source_type='upload'))
        with session_scope() as session:
            return session.scalars(
                select(ContractTask).where(ContractTask.contract_name == name)
            ).first().id

    def _make_docx(name: str) -> Path:
        """造一份带条款的真实 DOCX"""
        file_path = work_dir / 'incoming' / name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        document = docx.Document()
        for text in [
            '软件采购合同',
            '合同编号：CG-2026-0918-001',
            '甲方：某某科技有限公司',
            '乙方：某某软件股份有限公司\n统一社会信用代码：91310000MA1FL1234X',
            '第一条 合同金额：人民币 1,280,000.00 元（大写：壹佰贰拾捌万元整）。',
            '第二条 付款方式：货到即付全款，未设验收条件。',
            '第三条 知识产权：本合同软件的知识产权归乙方所有。',
            '第四条 履行期限：自2026年1月1日起至2026年12月31日止。',
        ]:
            document.add_paragraph(text)
        document.save(str(file_path))
        return file_path

    def _make_scanned_pdf(name: str, page_count: int = 2) -> Path:
        """造一份无文本层的 PDF（模拟扫描件）"""
        file_path = work_dir / 'incoming' / name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        document = fitz.open()
        for _ in range(page_count):
            document.new_page()
        document.save(str(file_path))
        document.close()
        return file_path

    workflow = ContractParseWorkflow()

    # ---------- 一、DOCX 端到端 ----------
    logging.getLogger().info('=' * 20 + ' 一、DOCX 端到端 ' + '=' * 20)

    docx_task_id = _create_task('DOCX 全链路')
    docx_path = _make_docx('软件采购合同.docx')

    init_state = get_default_state()
    init_state.update({
        'task_id': str(docx_task_id),
        'source_type': 'upload',
        'import_file_path': str(docx_path),
        'file_dir': str(work_dir / f'task-{docx_task_id}'),
        'file_title': '软件采购合同',
    })

    # 流式执行，顺便记录真实的节点执行顺序
    executed_nodes = []
    final_state = None
    for event in workflow.run(init_state, stream=True):
        for node_name, node_state in event.items():
            executed_nodes.append(node_name)
            final_state = node_state

    expected_order = [
        'node_entry', 'node_fetch_file', 'node_detect_type', 'node_parse_docx',
        'node_clean_text', 'node_clause_split', 'node_meta_extract', 'node_persist_parse',
    ]
    assert executed_nodes == expected_order, \
        f'节点执行顺序不符\n  期望: {expected_order}\n  实际: {executed_nodes}'

    assert final_state['clauses'], '条款未切出'
    assert final_state['meta']['party_a'] == '某某科技有限公司', '元数据未抽取'
    assert final_state['stored_file_path'].endswith('软件采购合同.docx'), '文件未归档'

    with session_scope() as session:
        task = session.get(ContractTask, docx_task_id)
        assert task.review_status == 'reviewing', f"任务状态应为 reviewing，实际 {task.review_status}"
        assert task.file_type == 'docx'
        assert task.amount is not None, '金额未回填到任务'
        assert session.scalars(
            select(ContractMeta).where(ContractMeta.task_id == docx_task_id)
        ).first() is not None, 'contract_meta 未落库'
        assert len(session.scalars(
            select(Clause).where(Clause.task_id == docx_task_id)
        ).all()) == len(final_state['clauses']), '条款落库数量不一致'
        assert session.scalars(
            select(TaskEventLog).where(TaskEventLog.task_id == docx_task_id)
        ).first() is not None, '事件日志未写入'

    passed += 1
    logging.getLogger().info(f'  ✅ DOCX 全链路走通，节点顺序正确（{len(executed_nodes)} 个节点）')

    # ---------- 二、扫描件 PDF → 自动转 OCR ----------
    logging.getLogger().info('=' * 20 + ' 二、扫描件 PDF 转 OCR ' + '=' * 20)

    pdf_task_id = _create_task('扫描件 PDF')
    scanned_path = _make_scanned_pdf('扫描合同.pdf', page_count=2)

    init_state = get_default_state()
    init_state.update({
        'task_id': str(pdf_task_id),
        'source_type': 'upload',
        'import_file_path': str(scanned_path),
        'file_dir': str(work_dir / f'task-{pdf_task_id}'),
        'file_title': '扫描合同',
    })

    executed_nodes = []
    final_state = None
    for event in workflow.run(init_state, stream=True):
        for node_name, node_state in event.items():
            executed_nodes.append(node_name)
            final_state = node_state

    # 关键：必须经过 node_ocr，而不是直接跳到 clean_text
    assert 'node_parse_pdf' in executed_nodes, 'PDF 应先走 node_parse_pdf'
    assert 'node_ocr' in executed_nodes, \
        f'无文本层 PDF 必须转 OCR（SPEC 4.2.1），实际路径 {executed_nodes}'
    assert executed_nodes.index('node_parse_pdf') < executed_nodes.index('node_ocr'), \
        'OCR 应在 node_parse_pdf 之后触发'
    assert final_state['pdf_needs_ocr'] is True, '应标记为需要 OCR'

    if not is_paddleocr_available():
        assert final_state['ocr_is_mock'] is True, '未装 PaddleOCR 时应标记为 mock 结果'
        logging.getLogger().info('       （本机未装 PaddleOCR，走 mock 分支）')

    with session_scope() as session:
        task = session.get(ContractTask, pdf_task_id)
        assert task.file_type == 'pdf', f"文件类型应为 pdf，实际 {task.file_type}"
        assert task.review_status == 'reviewing', '扫描件也应跑完整条链路'

    passed += 1
    logging.getLogger().info(f'  ✅ 扫描件 PDF 正确转入 OCR，路径：{" → ".join(executed_nodes)}')

    # ---------- 三、图片直连 OCR ----------
    logging.getLogger().info('=' * 20 + ' 三、图片直连 OCR ' + '=' * 20)

    img_task_id = _create_task('扫描图片')
    img_path = work_dir / 'incoming' / '扫描件.png'
    pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 120, 160))
    pixmap.save(str(img_path))

    init_state = get_default_state()
    init_state.update({
        'task_id': str(img_task_id),
        'source_type': 'upload',
        'import_file_path': str(img_path),
        'file_dir': str(work_dir / f'task-{img_task_id}'),
        'file_title': '扫描件',
    })

    executed_nodes = []
    for event in workflow.run(init_state, stream=True):
        for node_name, node_state in event.items():
            executed_nodes.append(node_name)

    assert 'node_ocr' in executed_nodes and 'node_parse_docx' not in executed_nodes, \
        f'图片应直连 OCR，实际路径 {executed_nodes}'
    passed += 1
    logging.getLogger().info(f'  ✅ 图片正确直连 OCR，路径：{" → ".join(executed_nodes)}')

    # ---------- 四、单次执行（invoke 模式）----------
    logging.getLogger().info('=' * 20 + ' 四、invoke 模式 ' + '=' * 20)

    invoke_task_id = _create_task('invoke 模式')
    invoke_path = _make_docx('invoke合同.docx')
    init_state = get_default_state()
    init_state.update({
        'task_id': str(invoke_task_id),
        'source_type': 'upload',
        'import_file_path': str(invoke_path),
        'file_dir': str(work_dir / f'task-{invoke_task_id}'),
        'file_title': 'invoke合同',
    })
    final_state = workflow.run(init_state)
    assert final_state['clauses'], 'invoke 模式下条款未切出'
    assert final_state['full_text'], 'invoke 模式下全文未产出'
    passed += 1
    logging.getLogger().info('  ✅ invoke 模式正常返回最终状态')

    reset_engine()
    logging.getLogger().info(f'parse_processor/main_graph 自测全部通过 ✅ 共 {passed} 个用例')
