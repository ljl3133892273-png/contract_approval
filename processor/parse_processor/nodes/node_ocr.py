'''processor/parse_processor/nodes/node_ocr.py'''

'''
步骤分解
1. 接收状态: 取图片来源 —— `image_path`（图片/扫描件）或 `pdf_path`（pdf_needs_ocr=True 时）。
2. 构建页图: 图片直接使用；PDF 用 PyMuPDF 按 `pdf_ocr_dpi` 逐页渲染成 PNG。
3. 选择引擎: PaddleOCR 可用 → 真实识别；不可用 → 按 `ocr_allow_mock` 决定降级 mock 还是报错。
4. 执行识别: 逐页识别，拿到行文本与置信度。
5. 置信度判定: 全篇平均置信度低于 `ocr_min_confidence` → 判「严重模糊」→ 报错（SPEC 11.1）。
6. 转中间结构: 每页合成一个 DocFragment（行文本用换行连接）。
7. 写回状态 + 统计日志。

需求分析
输入:
- `image_path` 或 `pdf_path`（+ `pdf_needs_ocr`）

输出:
- `fragments`：DocFragment[]（每页一块）
- `page_count`：页数
- `ocr_is_mock`：结果是否来自 mock（前端据此提示用户，不能当成真实识别结果）

关于 mock 开关（用户已确认「真实接入 + mock 开关」）:
- PaddleOCR 已安装 → 走真实识别。
- 未安装且 `OCR_ALLOW_MOCK` 未设为 false → 产出**明显标记**的占位文本，保证演示链路能跑通；
  同时置 `ocr_is_mock=True`，绝不把假数据伪装成真结果。
- 未安装且 `OCR_ALLOW_MOCK=false` → 直接抛 OcrError 进入 blocked。

关于 PaddleOCR 版本适配（重要）:
    本节点的 `_parse_paddle_result()` 按 PaddleOCR **2.x** 的返回结构编写
    （`engine.ocr(img_path, cls=True)` → `[[[quad, (text, score)], ...]]`）。
    PaddleOCR 3.x 的 `predict()` 返回结构不同，接入真实引擎时需同步调整该方法。
    本机未安装 PaddleOCR，**真实识别分支尚未经过实机验证**。
'''

import importlib.util
import json
import logging
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Tuple

from processor.parse_processor.base import BaseNode, setup_logging
from processor.parse_processor.exceptions import (
    FileProcessingError,
    OcrError,
    ParseProcessError,
    StateFieldError,
)
from processor.parse_processor.state import ParseGraphState, get_default_state


def is_paddleocr_available() -> bool:
    """
    探测 PaddleOCR 是否可用。
    只做「能不能 import」的轻量探测，不真正加载模型（加载模型很贵）。
    :return: 可用返回 True
    """
    return importlib.util.find_spec('paddleocr') is not None


class NodeOcr(BaseNode):
    '''OCR 识别节点：把扫描件/图片转成可审查的文本'''
    name = 'node_ocr'

    # 进程级缓存 PaddleOCR 实例：模型加载开销很大，同一进程内只加载一次
    _engine = None

    def process(self, state: ParseGraphState) -> ParseGraphState:
        """
        节点：OCR 识别（node_ocr）
        整体流程：取来源 → 构建页图 → 选引擎 → 逐页识别 → 置信度判定 → 转中间结构 → 写回状态
        核心目的：让扫描件/图片合同也能进入审查链路；
                 同时把「结果是不是真的」这件事如实记进状态，不伪造
        必要参数：image_path 或 pdf_path

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取并校验图片来源=======
        source_path, source_kind = self._step_1_get_inputs(state)

        # =======步骤二:构建页图（图片直接用，PDF 逐页渲染）=======
        page_images = self._step_2_build_page_images(state, source_path, source_kind)

        # =======步骤三:选择识别引擎（真实 / mock）=======
        use_mock = self._step_3_decide_engine()

        # =======步骤四:逐页识别=======
        page_results = (
            self._step_4_recognize_mock(page_images)
            if use_mock
            else self._step_4_recognize(page_images)
        )

        # =======步骤五:置信度判定（严重模糊 → 报错）=======
        self._step_5_check_confidence(page_results, source_path)

        # =======步骤六:转统一中间结构 + 写回状态=======
        state['fragments'] = self._step_6_build_fragments(page_results)
        state['page_count'] = len(page_images)
        state['ocr_is_mock'] = use_mock

        # =======步骤七:输出统计日志=======
        self._step_7_print_stats(state, source_path, page_images, page_results, use_mock)

        return state

    def _step_1_get_inputs(self, state: ParseGraphState) -> Tuple[Path, str]:
        """
        【步骤1】获取并校验图片来源
        功能：优先取图片来源，其次取 PDF（无文本层转 OCR）；校验存在性
        :param state: 项目状态字典（ParseGraphState）
        :return: (文件 Path 对象, 来源类型 'image' | 'pdf')
        """
        # 1.判定来源类型
        if state.get('image_path') or state.get('is_image_read_enabled'):
            path_str = (
                state.get('image_path')
                or state.get('stored_file_path')
                or state.get('import_file_path')
            )
            source_kind = 'image'
        elif state.get('pdf_path') or state.get('is_pdf_read_enabled'):
            path_str = (
                state.get('pdf_path')
                or state.get('stored_file_path')
                or state.get('import_file_path')
            )
            source_kind = 'pdf'
        else:
            path_str, source_kind = '', ''

        # 2.非空校验
        if not path_str:
            raise StateFieldError(
                field_name='image_path',
                message='OCR 来源路径不能为空（image_path 与 pdf_path 均为空）',
                expected_type=str
            )

        # 3.存在性校验
        source_path = Path(path_str)
        if not source_path.exists():
            raise FileProcessingError(message=f'文件 {source_path.name} 不存在')
        if not source_path.is_file():
            raise FileProcessingError(message=f'路径 {source_path} 不是一个文件')

        return source_path, source_kind

    def _step_2_build_page_images(self, state: ParseGraphState, source_path: Path,
                                  source_kind: str) -> List[Dict[str, Any]]:
        """
        【步骤2】构建页图列表
        功能：图片来源直接用原文件；PDF 来源按 pdf_ocr_dpi 逐页渲染成 PNG
        :param state: 项目状态字典
        :param source_path: 来源文件 Path 对象
        :param source_kind: 'image' | 'pdf'
        :return: 页图列表 [{'page_no': int, 'path': str}]
        """
        # 1.图片来源：本身就是一页
        if source_kind == 'image':
            return [{'page_no': 1, 'path': str(source_path)}]

        # 2.PDF 来源：逐页渲染成 PNG
        #    输出目录优先用任务工作目录，便于排查；没有则落到系统临时目录
        base_dir = state.get('file_dir') or tempfile.mkdtemp(prefix='ocr_pages_')
        page_dir = Path(base_dir) / 'ocr_pages'
        page_dir.mkdir(parents=True, exist_ok=True)

        import fitz

        page_images: List[Dict[str, Any]] = []
        try:
            document = fitz.open(str(source_path))
        except Exception as e:
            raise OcrError(message=f'打开 PDF 文件 {source_path.name} 失败：{e}', cause=e)

        try:
            if document.needs_pass:
                raise OcrError(message=f'文件 {source_path.name} 是加密 PDF，无法转 OCR')

            for page_index in range(document.page_count):
                page = document.load_page(page_index)
                pixmap = page.get_pixmap(dpi=self.config.pdf_ocr_dpi)
                page_path = page_dir / f'page_{page_index + 1}.png'
                pixmap.save(str(page_path))
                page_images.append({'page_no': page_index + 1, 'path': str(page_path)})
        finally:
            document.close()

        self.log_step('PDF渲染', f'{len(page_images)} 页 → {page_dir}（DPI={self.config.pdf_ocr_dpi}）')
        return page_images

    def _step_3_decide_engine(self) -> bool:
        """
        【步骤3】选择识别引擎
        功能：PaddleOCR 可用则用真实引擎；不可用时按 ocr_allow_mock 决定降级或报错
        :return: 是否使用 mock（True=用 mock）
        """
        if is_paddleocr_available():
            return False

        if self.config.ocr_allow_mock:
            self.logger.warning(
                '未检测到 PaddleOCR，按 OCR_ALLOW_MOCK 配置降级为 mock 结果 —— '
                '链路可照常跑通，但结果不可用于真实合同审查'
            )
            return True

        raise OcrError(
            message='未检测到 PaddleOCR，且配置 OCR_ALLOW_MOCK=false，无法执行 OCR 识别。'
                    '请执行 uv add paddleocr paddlepaddle 后重试'
        )

    def _get_engine(self):
        """
        【辅助函数】惰性初始化并缓存 PaddleOCR 引擎实例
        :return: PaddleOCR 实例
        """
        if NodeOcr._engine is None:
            from paddleocr import PaddleOCR

            self.log_step('加载引擎', f'PaddleOCR lang={self.config.ocr_lang} device={self.config.ocr_device}')
            NodeOcr._engine = PaddleOCR(
                use_angle_cls=True,
                lang=self.config.ocr_lang,
            )
        return NodeOcr._engine

    def _step_4_recognize(self, page_images: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        【步骤4-A】真实识别（PaddleOCR）
        :param page_images: 页图列表
        :return: 每页结果 [{'page_no': int, 'lines': [str], 'scores': [float]}]
        """
        engine = self._get_engine()
        page_results: List[Dict[str, Any]] = []

        for page_image in page_images:
            try:
                raw = engine.ocr(page_image['path'], cls=True)
            except Exception as e:
                raise OcrError(
                    message=f'第 {page_image["page_no"]} 页 OCR 识别失败：{e}',
                    cause=e
                )
            lines, scores = self._parse_paddle_result(raw)
            page_results.append({
                'page_no': page_image['page_no'],
                'lines': lines,
                'scores': scores,
            })

        return page_results

    @staticmethod
    def _parse_paddle_result(raw: Any) -> Tuple[List[str], List[float]]:
        """
        【辅助函数】解析 PaddleOCR 2.x 的识别结果
        结构：[[ [quad, (text, score)], ... ]]
        注意：PaddleOCR 3.x 的 predict() 返回结构不同，接入真实引擎时需改这里。
        :param raw: 引擎原始返回
        :return: (行文本列表, 置信度列表)
        """
        lines: List[str] = []
        scores: List[float] = []

        if not raw:
            return lines, scores

        # 2.x 的外层是「每张图一个元素」，我们一次只传一张图 → 取第 0 项
        page_result = raw[0] if isinstance(raw[0], list) else raw
        for item in page_result or []:
            try:
                text, score = item[1]
            except (IndexError, TypeError, ValueError):
                continue
            if text and str(text).strip():
                lines.append(str(text).strip())
                scores.append(float(score))

        return lines, scores

    def _step_4_recognize_mock(self, page_images: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        【步骤4-B】mock 识别（未安装 PaddleOCR 时的降级路径）
        功能：产出**明显标记**的占位文本，保证链路可跑通；置信度置 1.0 以免误触「严重模糊」
        :param page_images: 页图列表
        :return: 每页结果 [{'page_no': int, 'lines': [str], 'scores': [float]}]
        """
        return [
            {
                'page_no': page_image['page_no'],
                'lines': [
                    f'[OCR-MOCK] 第 {page_image["page_no"]} 页占位文本'
                    f'（未安装 PaddleOCR，未执行真实识别）'
                ],
                'scores': [1.0],
            }
            for page_image in page_images
        ]

    def _step_5_check_confidence(self, page_results: List[Dict[str, Any]], source_path: Path) -> None:
        """
        【步骤5】置信度判定
        功能：未识别到任何文字 → 报错；全篇平均置信度低于阈值 → 判「严重模糊」报错
             （对应 SPEC 11.1：扫描件严重模糊 → blocked）
        :param page_results: 每页识别结果
        :param source_path: 来源文件 Path 对象
        """
        all_scores = [s for result in page_results for s in result['scores']]

        # 1.一个字都没识别出来
        if not all_scores:
            raise OcrError(
                message=f'文件 {source_path.name} 未识别到任何文字，可能是严重模糊或空白扫描件'
            )

        # 2.平均置信度过低 → 判严重模糊
        average_score = sum(all_scores) / len(all_scores)
        if average_score < self.config.ocr_min_confidence:
            raise OcrError(
                message=f'文件 {source_path.name} 识别置信度均值 {average_score:.3f} 低于阈值 '
                        f'{self.config.ocr_min_confidence}，判定为严重模糊扫描件，请重新上传清晰文件'
            )

    def _step_6_build_fragments(self, page_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        【步骤6】转统一中间结构
        功能：每页合成一个 DocFragment，行文本用换行连接（保持阅读顺序，便于后续条款切分）
        :param page_results: 每页识别结果
        :return: DocFragment[] 列表
        """
        fragments: List[Dict[str, Any]] = []
        seq = 0

        for result in page_results:
            text = '\n'.join(result['lines'])
            if not text.strip():
                continue
            fragments.append({
                'seq': seq,
                'page_no': result['page_no'],
                'paragraph_index': result['page_no'] - 1,
                'text': text,
                'bbox': None,  # OCR 四点框暂不落状态，需要画框时再补
                'kind': 'paragraph',
            })
            seq += 1

        return fragments

    def _step_7_print_stats(self, state: ParseGraphState, source_path: Path,
                            page_images: List[Dict[str, Any]],
                            page_results: List[Dict[str, Any]], use_mock: bool) -> None:
        """
        【步骤7】输出统计日志
        """
        total_lines = sum(len(r['lines']) for r in page_results)
        all_scores = [s for r in page_results for s in r['scores']]
        average_score = sum(all_scores) / len(all_scores) if all_scores else 0.0

        self.log_step('来源文件', f'{source_path.name}（{len(page_images)} 页）')
        self.log_step('识别引擎', 'mock（占位）' if use_mock else 'PaddleOCR')
        self.log_step('识别行数', str(total_lines))
        self.log_step('置信度均值', f'{average_score:.3f}')


if __name__ == '__main__':
    # 激活日志的全局配置
    setup_logging()

    import tempfile

    import fitz

    from processor.parse_processor.config import ParseConfig

    # 临时根目录：由系统生成，测试结束自动随进程清理
    root_dir = Path(tempfile.mkdtemp(prefix='node_ocr_test_'))

    def _make_png(name: str) -> Path:
        """造一张真实的 PNG 图片"""
        file_path = root_dir / name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        pixmap = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 120, 160))
        pixmap.save(str(file_path))
        return file_path

    def _make_blank_pdf(name: str, page_count: int) -> Path:
        """造一个无文本层的 PDF（模拟扫描件）"""
        file_path = root_dir / name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        document = fitz.open()
        for _ in range(page_count):
            document.new_page()
        document.save(str(file_path))
        document.close()
        return file_path

    node_ocr = NodeOcr()
    paddleocr_ready = is_paddleocr_available()
    logging.getLogger().info(f'本机 PaddleOCR 可用：{paddleocr_ready}')
    passed = 0

    # ---------- 一、图片来源 ----------
    logging.getLogger().info('=' * 24 + ' 一、图片来源 ' + '=' * 24)

    png_path = _make_png('sample/扫描件.png')
    init_state = get_default_state()
    init_state.update({
        'task_id': 'task-ocr-image',
        'image_path': str(png_path),
        'is_image_read_enabled': True,
    })
    result = node_ocr(init_state)

    assert result['page_count'] == 1, '单张图片应算 1 页'
    assert len(result['fragments']) == 1, '应产出 1 个文本块'
    assert result['fragments'][0]['page_no'] == 1, '页码应为 1'
    assert result['fragments'][0]['seq'] == 0, 'seq 应从 0 开始'
    if not paddleocr_ready:
        # 未装 PaddleOCR → 必须走 mock，且必须明确标记，不能伪装成真实结果
        assert result['ocr_is_mock'] is True, '未装 PaddleOCR 时应置 ocr_is_mock=True'
        assert '[OCR-MOCK]' in result['fragments'][0]['text'], 'mock 文本必须带明显标记'
    else:
        assert result['ocr_is_mock'] is False, 'PaddleOCR 可用时不应走 mock'
    passed += 1
    logging.getLogger().info('  ✅ 图片来源 → 1 页 1 块，mock 标记正确')

    # ---------- 二、PDF 来源（无文本层，逐页渲染）----------
    logging.getLogger().info('=' * 24 + ' 二、PDF 来源（扫描件）' + '=' * 24)

    scan_pdf = _make_blank_pdf('sample/扫描合同.pdf', page_count=3)
    init_state = get_default_state()
    init_state.update({
        'task_id': 'task-ocr-pdf',
        'pdf_path': str(scan_pdf),
        'is_pdf_read_enabled': True,
        'pdf_needs_ocr': True,
        'file_dir': str(root_dir / 'task-ocr-pdf'),
    })
    result = node_ocr(init_state)

    assert result['page_count'] == 3, f'应渲染 3 页，实际 {result["page_count"]}'
    assert [f['page_no'] for f in result['fragments']] == [1, 2, 3], '每页应各产出一块，页码连续'
    assert [f['seq'] for f in result['fragments']] == [0, 1, 2], 'seq 应连续递增'
    # 渲染出的页图应真实落盘，便于排查
    rendered = sorted((root_dir / 'task-ocr-pdf' / 'ocr_pages').glob('*.png'))
    assert len(rendered) == 3, f'应落盘 3 张页图，实际 {len(rendered)}'
    passed += 1
    logging.getLogger().info('  ✅ PDF 来源 → 逐页渲染并识别，页码连续')

    # ---------- 三、mock 开关关闭时不得产出假数据 ----------
    logging.getLogger().info('=' * 24 + ' 三、mock 开关关闭 ' + '=' * 24)

    if paddleocr_ready:
        logging.getLogger().info('  ⏭ 本机已装 PaddleOCR，跳过「mock 关闭」用例')
    else:
        strict_node = NodeOcr(ParseConfig(ocr_allow_mock=False))
        init_state = get_default_state()
        init_state.update({
            'task_id': 'task-ocr-strict',
            'image_path': str(png_path),
            'is_image_read_enabled': True,
        })
        try:
            strict_node(init_state)
        except Exception as e:
            assert isinstance(e.cause, OcrError), \
                f'应抛 OcrError，实际为 {type(e.cause).__name__}'
            passed += 1
            logging.getLogger().info('  ✅ mock 关闭 → 正确抛出 OcrError（不伪造结果）')
        else:
            raise AssertionError('mock 关闭时不应产出结果，用例失败')

    # ---------- 四、异常场景 ----------
    logging.getLogger().info('=' * 24 + ' 四、异常场景 ' + '=' * 24)

    err_cases = [
        ('路径全为空', {}, StateFieldError),
        ('文件不存在',
         {'image_path': str(root_dir / '不存在.png'), 'is_image_read_enabled': True},
         FileProcessingError),
        ('路径是目录',
         {'image_path': str(root_dir), 'is_image_read_enabled': True},
         FileProcessingError),
    ]

    for case_name, overrides, expected_exc in err_cases:
        init_state = get_default_state()
        init_state['task_id'] = 'task-ocr-error'
        init_state.update(overrides)
        try:
            node_ocr(init_state)
        except Exception as e:
            assert isinstance(e, ParseProcessError), f'{case_name} 应被基类包装成 ParseProcessError'
            assert isinstance(e.cause, expected_exc), \
                f'{case_name} 应抛出 {expected_exc.__name__}，实际为 {type(e.cause).__name__}'
            passed += 1
            logging.getLogger().info(f'  ✅ {case_name:<10} → 正确抛出 {expected_exc.__name__}')
        else:
            raise AssertionError(f'{case_name} 未抛出异常，用例失败')

    # ---------- 五、打印最终状态（便于人工核对）----------
    logging.getLogger().info('=' * 24 + ' 五、最终状态 ' + '=' * 24)
    logging.getLogger().info(json.dumps(result['fragments'], ensure_ascii=False, indent=4))

    logging.getLogger().info(f'node_ocr 自测全部通过 ✅ 共 {passed} 个用例')
