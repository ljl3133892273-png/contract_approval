'''processor/parse_processor/nodes/node_parse_pdf.py'''

'''
步骤分解
1. 接收状态: 获取 `pdf_path`（回退 stored_file_path / import_file_path）。
2. 打开文档: 用 PyMuPDF(fitz) 打开；命中加密 → PdfParseError（需要密码）。
3. 逐页抽取: 每页取文本块，记录 bbox 与页码，转成统一中间结构 DocFragment[]。
4. 文本层判定: 全文可提取文本量低于阈值 → 判定为「无文本层」（扫描件），
             置 `pdf_needs_ocr = True`，由主图路由到 node_ocr。
5. 写回状态 + 统计日志。

需求分析
输入:
- `pdf_path`：PDF 文件路径（node_detect_type 权威判定后写入）

输出:
- `fragments`：DocFragment[]（有文本层时）
- `page_count`：PDF 总页数
- `pdf_needs_ocr`：是否需要转 OCR（对应 SPEC 4.2.1「PDF无文本层 → PaddleOCR」）

为什么要有 pdf_needs_ocr 这个开关:
    原节点图把 PDF 一律路由到 node_parse_pdf，但 SPEC 4.2.1 要求
    「PDF 无文本层 → PaddleOCR」。所以 node_parse_pdf 必须能回报
    「我搞不定，请转 OCR」，主图据此加一条 node_parse_pdf → node_ocr 的条件边。
    这是对原节点图的一处必要补充。

关于页码:
    PDF 的页码是真实的（逐页遍历得到），记 1 起，与前端 PDF.js 的页码一致。
'''

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Tuple

import fitz  # PyMuPDF

from processor.parse_processor.base import BaseNode, setup_logging
from processor.parse_processor.exceptions import (
    FileProcessingError,
    ParseProcessError,
    PdfParseError,
    StateFieldError,
)
from processor.parse_processor.state import ParseGraphState, get_default_state


class NodeParsePdf(BaseNode):
    '''PDF 解析节点：逐页抽取文本与坐标，并判定是否需要转 OCR'''
    name = 'node_parse_pdf'

    # 块类型：PyMuPDF get_text('blocks') 的 block_type，0=文本，1=图片
    _BLOCK_TYPE_TEXT = 0

    def process(self, state: ParseGraphState) -> ParseGraphState:
        """
        节点：PDF 解析（node_parse_pdf）
        整体流程：取路径校验 → 打开文档 → 逐页抽取块 → 文本层判定 → 写回状态
        核心目的：把 PDF 的文本层连同页码与坐标一起抽出来；
                 抽不出来（扫描件）就明确回报「需要 OCR」，而不是静默返回空
        必要参数：pdf_path
        更新参数：fragments、page_count、pdf_needs_ocr

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取并校验文件路径=======
        file_path = self._step_1_get_inputs(state)

        # =======步骤二:打开文档（含加密判定）=======
        document = self._step_2_open_document(file_path)

        try:
            # =======步骤三:逐页抽取文本块=======
            # 作用：核心抽取逻辑，逐页拿到文本块 + bbox + 页码
            fragments, empty_pages = self._step_3_extract_fragments(document)
            page_count = document.page_count

            # =======步骤四:文本层判定=======
            # 作用：全文文本量不足 → 判定为扫描件，回报需要转 OCR
            needs_ocr = self._step_4_decide_ocr(fragments, page_count, empty_pages)

            # =======步骤五:写回状态 + 输出统计日志=======
            state['fragments'] = fragments
            state['page_count'] = page_count
            state['pdf_needs_ocr'] = needs_ocr
            self._step_5_print_stats(file_path, fragments, page_count, empty_pages, needs_ocr)

        finally:
            # 无论成功失败都要关闭文档，避免文件句柄泄漏
            document.close()

        return state

    def _step_1_get_inputs(self, state: ParseGraphState) -> Path:
        """
        【步骤1】获取并校验文件路径
        功能：优先取 pdf_path，回退 stored_file_path / import_file_path
        :param state: 项目状态字典（ParseGraphState）
        :return: 校验通过后的文件 Path 对象
        """
        file_path_str = (
            state.get('pdf_path')
            or state.get('stored_file_path')
            or state.get('import_file_path')
        )
        if not file_path_str:
            raise StateFieldError(
                field_name='pdf_path',
                message='PDF 文件路径不能为空',
                expected_type=str
            )

        file_path = Path(file_path_str)
        if not file_path.exists():
            raise FileProcessingError(message=f'文件 {file_path.name} 不存在')
        if not file_path.is_file():
            raise FileProcessingError(message=f'路径 {file_path} 不是一个文件')

        return file_path

    def _step_2_open_document(self, file_path: Path) -> "fitz.Document":
        """
        【步骤2】打开 PDF 文档
        功能：用 PyMuPDF 打开；文件不是 PDF → PdfParseError；需要密码 → PdfParseError
             （文件加密在 node_detect_type 已用 /Encrypt 预判过，这里是确证）
        :param file_path: PDF 文件 Path 对象
        :return: fitz.Document 对象
        """
        try:
            document = fitz.open(str(file_path))
        except Exception as e:
            raise PdfParseError(
                message=f'打开 PDF 文件 {file_path.name} 失败（可能不是有效 PDF）：{e}',
                cause=e
            )

        # needs_pass 为 True 表示文档需要密码才能解密
        if document.needs_pass:
            document.close()
            raise PdfParseError(
                message=f'文件 {file_path.name} 是加密 PDF，需要密码才能打开'
            )

        return document

    def _step_3_extract_fragments(self, document: "fitz.Document") -> Tuple[List[Dict[str, Any]], List[int]]:
        """
        【步骤3】逐页抽取文本块
        功能：每页取 get_text('blocks')，只保留文本块（跳过图片块），
             记录 bbox 与 1 起页码，转成统一中间结构
        :param document: fitz.Document 对象
        :return: (DocFragment[] 列表, 无文本页的页码列表)
        """
        fragments: List[Dict[str, Any]] = []
        empty_pages: List[int] = []
        seq = 0  # 有效块计数
        block_index = 0  # 全文档块序号（含跨页连续编号）

        for page_index in range(document.page_count):
            page = document.load_page(page_index)
            page_no = page_index + 1  # 页码 1 起，与前端 PDF.js 一致
            page_block_count = 0

            for block in page.get_text('blocks'):
                # blocks 每项为 (x0, y0, x1, y1, text, block_no, block_type)
                x0, y0, x1, y1 = block[0], block[1], block[2], block[3]
                text = block[4]
                block_type = block[6] if len(block) > 6 else self._BLOCK_TYPE_TEXT

                # 跳过图片块（无文本层时整页都是图片块，这里自然拿到空文本）
                if block_type != self._BLOCK_TYPE_TEXT:
                    continue

                current_index = block_index
                block_index += 1

                if not text.strip():
                    continue

                fragments.append({
                    'seq': seq,
                    'page_no': page_no,
                    'paragraph_index': current_index,
                    'text': text.strip(),
                    'bbox': [round(x0, 2), round(y0, 2), round(x1, 2), round(y1, 2)],
                    'kind': 'paragraph',
                })
                seq += 1
                page_block_count += 1

            if page_block_count == 0:
                empty_pages.append(page_no)

        return fragments, empty_pages

    def _step_4_decide_ocr(self, fragments: List[Dict[str, Any]], page_count: int,
                           empty_pages: List[int]) -> bool:
        """
        【步骤4】文本层判定
        功能：全文可提取文本量低于 pdf_min_text_chars → 判定为无文本层（扫描件），需转 OCR
        :param fragments: DocFragment[] 列表
        :param page_count: 总页数
        :param empty_pages: 无文本页的页码列表
        :return: 是否需要转 OCR
        """
        total_chars = sum(len(f['text']) for f in fragments)

        # 1.有部分页无文本 → 至少告警，避免"半扫描"文档被静默漏掉
        if empty_pages and len(empty_pages) < page_count:
            self.logger.warning(
                f'该 PDF 为混合文档：共 {page_count} 页，其中 {len(empty_pages)} 页无文本层'
                f'（页码：{empty_pages}），这些页面的内容将不会被抽取'
            )

        # 2.全文文本量不足 → 整份转 OCR
        return total_chars < self.config.pdf_min_text_chars

    def _step_5_print_stats(self, file_path: Path, fragments: List[Dict[str, Any]], page_count: int,
                            empty_pages: List[int], needs_ocr: bool) -> None:
        """
        【步骤5】输出统计日志
        """
        total_chars = sum(len(f['text']) for f in fragments)
        self.log_step('文件', file_path.name)
        self.log_step('总页数', str(page_count))
        self.log_step('文本块数', str(len(fragments)))
        self.log_step('总字符数', str(total_chars))
        self.log_step('无文本页', str(empty_pages) if empty_pages else '-')
        self.log_step('是否转OCR', str(needs_ocr))
        if fragments:
            preview = fragments[0]['text'][:30].replace('\n', ' ')
            self.log_step('首块预览', f'第{fragments[0]["page_no"]}页 | {preview}')


if __name__ == '__main__':
    # 激活日志的全局配置
    setup_logging()

    import tempfile

    # 临时根目录：由系统生成，测试结束自动随进程清理
    root_dir = Path(tempfile.mkdtemp(prefix='node_parse_pdf_test_'))

    def _make_text_pdf(name: str, page_texts: List[str]) -> Path:
        """用 PyMuPDF 造一个带文本层的 PDF
        说明：测试里统一用 ASCII 文本，避开 CJK 内嵌字体问题；
             本节点只负责抽取文本层，不关心字体与语言。"""
        file_path = root_dir / name
        file_path.parent.mkdir(parents=True, exist_ok=True)

        document = fitz.open()
        for page_text in page_texts:
            page = document.new_page()
            page.insert_text((72, 100), page_text)
        document.save(str(file_path))
        document.close()
        return file_path

    def _make_blank_pdf(name: str, page_count: int = 1) -> Path:
        """造一个完全没有文本层的 PDF（模拟扫描件）"""
        file_path = root_dir / name
        file_path.parent.mkdir(parents=True, exist_ok=True)

        document = fitz.open()
        for _ in range(page_count):
            document.new_page()
        document.save(str(file_path))
        document.close()
        return file_path

    def _make_encrypted_pdf(name: str) -> Path:
        """造一个加密 PDF（需要密码）"""
        file_path = root_dir / name
        file_path.parent.mkdir(parents=True, exist_ok=True)

        document = fitz.open()
        page = document.new_page()
        page.insert_text((72, 100), 'confidential contract')
        document.save(
            str(file_path),
            encryption=fitz.PDF_ENCRYPT_AES_256,
            user_pw='user-password',
            owner_pw='owner-password',
        )
        document.close()
        return file_path

    node_parse_pdf = NodeParsePdf()
    passed = 0

    # ---------- 一、正常场景：有文本层 ----------
    logging.getLogger().info('=' * 24 + ' 一、正常场景 ' + '=' * 24)

    text_pdf = _make_text_pdf(
        'sample/软件采购合同.pdf',
        ['Software Purchase Contract', 'Article 1 Subject Matter', 'Article 2 Payment Terms'],
    )
    init_state = get_default_state()
    init_state.update({
        'task_id': 'task-pdf-ok',
        'pdf_path': str(text_pdf),
        'stored_file_path': str(text_pdf),
        'is_pdf_read_enabled': True,
    })
    result = node_parse_pdf(init_state)
    fragments = result['fragments']

    assert result['page_count'] == 3, f'应有 3 页，实际 {result["page_count"]}'
    assert result['pdf_needs_ocr'] is False, '有文本层的 PDF 不应转 OCR'
    assert len(fragments) == 3, f'应有 3 个文本块，实际 {len(fragments)}'

    # 断言 seq 连续递增
    assert [f['seq'] for f in fragments] == [0, 1, 2], 'seq 应从 0 连续递增'

    # 断言页码真实且为 1 起（与前端 PDF.js 一致）
    assert [f['page_no'] for f in fragments] == [1, 2, 3], '页码应为 1 起且逐页递增'

    # 断言文本内容与坐标
    assert 'Software Purchase Contract' in fragments[0]['text'], '第一页文本抽取错误'
    assert 'Article 2 Payment Terms' in fragments[2]['text'], '第三页文本抽取错误'
    assert all(len(f['bbox']) == 4 for f in fragments), '每个块都应带 4 元 bbox'
    assert all(f['kind'] == 'paragraph' for f in fragments), 'PDF 文本块 kind 应为 paragraph'

    passed += 1
    logging.getLogger().info(f'  ✅ 有文本层 PDF：{len(fragments)} 块 / {result["page_count"]} 页，页码正确')

    # ---------- 二、无文本层：判定需转 OCR ----------
    logging.getLogger().info('=' * 24 + ' 二、无文本层（扫描件）' + '=' * 24)

    blank_pdf = _make_blank_pdf('sample/扫描件.pdf', page_count=2)
    init_state = get_default_state()
    init_state.update({
        'task_id': 'task-pdf-scan',
        'pdf_path': str(blank_pdf),
        'is_pdf_read_enabled': True,
    })
    result = node_parse_pdf(init_state)

    assert result['pdf_needs_ocr'] is True, '无文本层 PDF 应置 pdf_needs_ocr=True'
    assert result['fragments'] == [], '无文本层时不应产出文本块'
    assert result['page_count'] == 2, '页数应仍被正确记录'
    passed += 1
    logging.getLogger().info('  ✅ 无文本层 PDF → pdf_needs_ocr=True，转 OCR')

    # ---------- 三、异常场景 ----------
    logging.getLogger().info('=' * 24 + ' 三、异常场景 ' + '=' * 24)

    encrypted_pdf = _make_encrypted_pdf('err/加密合同.pdf')
    fake_pdf = root_dir / 'err' / '假文件.pdf'
    fake_pdf.write_bytes(b'this is definitely not a pdf')

    err_cases = [
        ('加密PDF', {'pdf_path': str(encrypted_pdf)}, PdfParseError),
        ('非PDF文件', {'pdf_path': str(fake_pdf)}, PdfParseError),
        ('文件不存在', {'pdf_path': str(root_dir / '不存在.pdf')}, FileProcessingError),
        ('路径是目录', {'pdf_path': str(root_dir)}, FileProcessingError),
        ('路径全为空', {}, StateFieldError),
    ]

    for case_name, overrides, expected_exc in err_cases:
        init_state = get_default_state()
        init_state['task_id'] = 'task-pdf-error'
        init_state.update(overrides)
        try:
            node_parse_pdf(init_state)
        except Exception as e:
            assert isinstance(e, ParseProcessError), f'{case_name} 应被基类包装成 ParseProcessError'
            assert isinstance(e.cause, expected_exc), \
                f'{case_name} 应抛出 {expected_exc.__name__}，实际为 {type(e.cause).__name__}'
            passed += 1
            logging.getLogger().info(f'  ✅ {case_name:<10} → 正确抛出 {expected_exc.__name__}')
        else:
            raise AssertionError(f'{case_name} 未抛出异常，用例失败')

    # ---------- 四、打印最终状态（便于人工核对）----------
    logging.getLogger().info('=' * 24 + ' 四、最终状态 ' + '=' * 24)
    logging.getLogger().info(json.dumps(result, ensure_ascii=False, indent=4))

    logging.getLogger().info(f'node_parse_pdf 自测全部通过 ✅ 共 {passed} 个用例')
