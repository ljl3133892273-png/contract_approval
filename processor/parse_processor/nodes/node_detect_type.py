'''processor/parse_processor/nodes/node_detect_type.py'''

'''
为什么需要这个节点:
    node_entry 只按「扩展名」初判文件类别，而扩展名是完全可以被改掉的
    （把 .png 改名成 .pdf 就能绕过上传白名单）。所以这里再做一次
    「mimetype + 文件头魔数」的真实嗅探，让它成为唯一权威的类别判定结果，
    主图 route_after_detect 依据它来做条件路由。

步骤分解
1. 接收状态: 获取 `stored_file_path`（归档后的规范路径），完成非空 / 存在性 / 是否为文件校验。
2. 空文件判定: 文件字节数为 0 → 无法解析，直接报错。
3. 读取文件头: 读前 64 字节，供魔数比对。
4. 魔数嗅探: 先识别 OLE 复合文档容器（加密 Office / 老式 .doc）→ 报错；
             再按签名表识别 PDF / DOCX / 图片（PNG、JPEG、BMP、TIFF、WEBP），识别不到的报错；
             与 node_entry 的扩展名判定不一致时，以魔数为准并告警。
5. 容器与加密检测: DOCX 校验 zip 容器是否完好；PDF 扫描 /Encrypt 判断是否加密 → 报错。
6. 权威置位: 重置并置位三个类别开关 + 对应路径，供主图条件路由。
7. 输出统计日志。

需求分析
输入:
- `stored_file_path`：node_fetch_file 归档后的规范本地路径（为空时回退 import_file_path）
- `import_file_path`：原始接入路径（回退用）

输出:
- `is_docx_read_enabled` / `is_pdf_read_enabled` / `is_image_read_enabled`：**权威**类别开关（三者互斥）
- `docx_path` / `pdf_path` / `image_path`：随权威判定结果同步

异常出口（对应 SPEC 11.1，最终由状态机映射为 PARSE_FAIL → blocked）:
- 文档加密 → DocumentDetectError
- 正文为空 / 空文件 → DocumentDetectError
- 格式无法识别 / DOCX 容器损坏 → DocumentDetectError
'''

import json
import logging
import zipfile
from pathlib import Path
from typing import Tuple

from processor.parse_processor.base import BaseNode, setup_logging
from processor.parse_processor.exceptions import (
    DocumentDetectError,
    FileProcessingError,
    ParseProcessError,
    StateFieldError,
)
from processor.parse_processor.state import ParseGraphState, get_default_state


class NodeDetectType(BaseNode):
    '''格式嗅探节点：用文件头魔数确认真实格式 + 识别加密 / 空文件'''
    name = 'node_detect_type'

    # 读取的文件头长度（WEBP 需要校验第 8~12 字节，故至少 12 字节）
    _HEADER_SIZE = 64
    # 扫描加密标志时的分块大小：1MB
    _SCAN_CHUNK_SIZE = 1024 * 1024

    # 文件头魔数签名表：(类别, 相对文件头的偏移, 签名字节)
    # 说明：扩展名不可信，以这里的判定为准
    _MAGIC_SIGNATURES: Tuple[Tuple[str, int, bytes], ...] = (
        # PDF
        ('pdf', 0, b'%PDF-'),
        # OOXML（docx 本质是 zip 容器）
        ('docx', 0, b'PK\x03\x04'),  # 普通 zip
        ('docx', 0, b'PK\x05\x06'),  # 空 zip
        ('docx', 0, b'PK\x07\x08'),  # 分卷 zip
        # 图片
        ('image', 0, b'\x89PNG\r\n\x1a\n'),  # PNG
        ('image', 0, b'\xff\xd8\xff'),  # JPEG
        ('image', 0, b'BM'),  # BMP
        ('image', 0, b'II*\x00'),  # TIFF（小端）
        ('image', 0, b'MM\x00*'),  # TIFF（大端）
        ('image', 8, b'WEBP'),  # WEBP：RIFF????WEBP
    )

    # 类别 -> 状态字段名映射，避免到处写字符串
    _KIND_TO_FLAG = {
        'docx': 'is_docx_read_enabled',
        'pdf': 'is_pdf_read_enabled',
        'image': 'is_image_read_enabled',
    }
    _KIND_TO_PATH = {
        'docx': 'docx_path',
        'pdf': 'pdf_path',
        'image': 'image_path',
    }

    # OLE 复合文档容器头：加密的 Office 文档或老式 .doc / .xls，均无法直接解析
    _OLE_SIGNATURE = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1'
    # PDF 加密标志：加密的 PDF 的 trailer 中存在 /Encrypt 对象
    _PDF_ENCRYPT_MARKER = b'/Encrypt'

    def process(self, state: ParseGraphState) -> ParseGraphState:
        """
        节点：格式嗅探（node_detect_type）
        整体流程：取路径校验 → 空文件判定 → 读文件头 → 魔数嗅探 → 加密检测 → 权威置位 → 统计日志
        核心目的：用文件头魔数给出唯一权威的文件类别，避免被改扩展名绕过白名单，
                 并在进入重解析之前就拦住「加密 / 空文件 / 无法识别」三类必阻塞情况
        必要参数：stored_file_path（为空时回退 import_file_path）
        更新参数：is_docx_read_enabled、is_pdf_read_enabled、is_image_read_enabled、
                 docx_path、pdf_path、image_path

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取并校验文件路径=======
        # 作用：优先用 node_fetch_file 归档后的规范路径，取出后完成存在性 / 是否为文件校验
        # 输出：可供后续使用的 Path 对象
        file_path = self._step_1_get_inputs(state)

        # =======步骤二:空文件判定=======
        # 作用：0 字节文件必然解析不出正文，提前拦掉（对应 SPEC 11.1「正文为空」）
        # 输出：无返回值，命中直接抛 DocumentDetectError
        self._step_2_check_empty(file_path)

        # =======步骤三:读取文件头=======
        # 作用：读前 64 字节作为魔数比对样本
        # 输出：文件头字节串
        header = self._step_3_read_header(file_path)

        # =======步骤四:魔数嗅探=======
        # 作用：核心判定逻辑，按签名表识别真实格式，并与扩展名判定结果做一致性修正
        # 输出：权威类别字符串（docx / pdf / image）
        detected_kind = self._step_4_sniff_kind(state, header)

        # =======步骤五:容器完整性与加密检测=======
        # 作用：docx 校验 zip 容器是否完好；识别加密的 PDF
        # 输出：无返回值，命中直接抛 DocumentDetectError
        self._step_5_check_encrypted(state, file_path, detected_kind)

        # =======步骤六:权威置位类别开关=======
        # 作用：重置并置位三个互斥开关与对应路径，供主图 route_after_detect 路由
        # 输出：无返回值，直接写回状态
        self._step_6_apply_flags(state, file_path, detected_kind)

        # =======步骤七:输出统计日志=======
        # 作用：打印扩展名判定 vs 魔数判定的结果，便于排查「为什么走了这条分支」
        # 输出：无返回值，仅通过 logger 输出标准化日志
        self._step_7_print_stats(state, header, detected_kind)

        return state

    def _step_1_get_inputs(self, state: ParseGraphState) -> Path:
        """
        【步骤1】获取并校验文件路径
        功能：优先取 stored_file_path（归档后的规范路径），为空时回退 import_file_path；
             完成非空 / 存在性 / 是否为文件三重校验
        :param state: 项目状态字典（ParseGraphState）
        :return: 校验通过后的文件 Path 对象
        """
        # 1.取路径：归档路径优先，回退原始接入路径
        file_path_str = state.get('stored_file_path') or state.get('import_file_path')
        if not file_path_str:
            raise StateFieldError(
                field_name='stored_file_path',
                message='文件路径不能为空（stored_file_path 与 import_file_path 均为空）',
                expected_type=str
            )

        # 2.存在性校验
        file_path = Path(file_path_str)
        if not file_path.exists():
            raise FileProcessingError(message=f'文件 {file_path.name} 不存在')

        # 3.排除目录：必须是一个文件
        if not file_path.is_file():
            raise FileProcessingError(message=f'路径 {file_path} 不是一个文件')

        return file_path

    def _step_2_check_empty(self, file_path: Path) -> None:
        """
        【步骤2】空文件判定
        功能：文件字节数为 0 → 必然解析不出正文，直接判为不可解析
        :param file_path: 文件 Path 对象
        """
        if file_path.stat().st_size == 0:
            raise DocumentDetectError(
                message=f'文件 {file_path.name} 为空文件（0 字节），无法解析'
            )

    def _step_3_read_header(self, file_path: Path) -> bytes:
        """
        【步骤3】读取文件头
        功能：读取文件前 64 字节，供魔数比对（小文件会少于 64 字节，按实际读到多少算多少）
        :param file_path: 文件 Path 对象
        :return: 文件头字节串
        """
        with open(file_path, 'rb') as file_buffer:
            return file_buffer.read(self._HEADER_SIZE)

    def _step_4_sniff_kind(self, state: ParseGraphState, header: bytes) -> str:
        """
        【步骤4】魔数嗅探（核心：给出唯一权威的类别判定）
        功能：按签名表识别真实格式；识别不到直接报错；
             与 node_entry 的扩展名判定结果不一致时以魔数为准并告警
        :param state: 项目状态字典
        :param header: 文件头字节串
        :return: 权威类别字符串（docx / pdf / image）
        """
        # 1.先识别 OLE 复合文档容器：加密的 Office 文档 / 老式 .doc 都长这样，
        #   它不匹配下面的任何一类魔数，必须先拦掉才能给出准确的错误提示
        if header.startswith(self._OLE_SIGNATURE):
            raise DocumentDetectError(
                message='该文件是 OLE 复合文档（加密的 Office 文档或老式 .doc 格式），'
                        '无法解析，请提供未加密的 DOCX / PDF / 图片'
            )

        # 2.按签名表比对，得到魔数判定结果
        magic_kind = self._match_magic(header)

        # 3.识别不到 → 既不认识的文件头，直接报错
        if not magic_kind:
            raise DocumentDetectError(
                message=f'无法识别的文件格式，文件头为 {header[:8].hex(" ")}，'
                        f'仅支持 PDF / DOCX / 图片（PNG、JPEG、BMP、TIFF、WEBP）'
            )

        # 4.与扩展名初判结果比对：不一致时以魔数为准（扩展名不可信）
        ext_kind = self._kind_from_flags(state)
        if ext_kind and ext_kind != magic_kind:
            self.logger.warning(
                f'扩展名判定为 {ext_kind}，但文件头魔数判定为 {magic_kind}，'
                f'以魔数为准（扩展名可能被改过）'
            )

        return magic_kind

    def _match_magic(self, header: bytes) -> str:
        """
        【辅助函数】按签名表比对文件头
        :param header: 文件头字节串
        :return: 命中的类别；未命中返回空字符串
        """
        for kind, offset, magic in self._MAGIC_SIGNATURES:
            if header[offset:offset + len(magic)] == magic:
                return kind
        return ''

    def _kind_from_flags(self, state: ParseGraphState) -> str:
        """
        【辅助函数】把 node_entry 写下的三个开关反解成类别字符串
        :param state: 项目状态字典
        :return: 类别字符串；三个开关都为 False 时返回空字符串
        """
        for kind, flag_name in self._KIND_TO_FLAG.items():
            if state.get(flag_name):
                return kind
        return ''

    def _step_5_check_encrypted(self, state: ParseGraphState, file_path: Path,
                                detected_kind: str) -> None:
        """
        【步骤5】容器完整性与加密检测
        功能：docx 校验 zip 容器是否完好；识别加密的 PDF
             （加密的 Office 文档已在上一步的 OLE 容器判定中拦截）
        :param state: 项目状态字典
        :param file_path: 文件 Path 对象
        :param detected_kind: 魔数判定的类别
        """
        # 1.DOCX：校验 zip 容器是否完好，容器损坏后面必然解析失败，提前拦掉
        if detected_kind == 'docx' and not zipfile.is_zipfile(file_path):
            raise DocumentDetectError(
                message=f'文件 {file_path.name} 的 DOCX 容器已损坏（不是有效的 zip 包）'
            )

        # 2.PDF：扫描 /Encrypt 标志判断是否加密
        if detected_kind == 'pdf' and self._contains_marker(file_path, self._PDF_ENCRYPT_MARKER):
            raise DocumentDetectError(
                message=f'文件 {file_path.name} 是加密 PDF，需要密码才能打开，'
                        f'请提供解密后的文件'
            )

    def _contains_marker(self, file_path: Path, marker: bytes) -> bool:
        """
        【辅助函数】分块扫描文件中是否包含指定字节标志
        功能：跨块命中也能识别（保留 marker 长度-1 的重叠窗口），避免大文件一次性读进内存
        :param file_path: 文件 Path 对象
        :param marker: 要查找的字节标志
        :return: 包含返回 True，否则 False
        """
        overlap = len(marker) - 1
        tail = b''
        with open(file_path, 'rb') as file_buffer:
            while True:
                chunk = file_buffer.read(self._SCAN_CHUNK_SIZE)
                if not chunk:
                    return False
                window = tail + chunk
                if marker in window:
                    return True
                tail = window[-overlap:] if overlap else b''

    def _step_6_apply_flags(self, state: ParseGraphState, file_path: Path, detected_kind: str) -> None:
        """
        【步骤6】权威置位类别开关
        功能：先复位三个开关，再置位魔数判定的那一类，并同步对应路径
        :param state: 项目状态字典
        :param file_path: 文件 Path 对象
        :param detected_kind: 魔数判定的类别
        """
        stored_path = str(file_path)

        # 1.全部复位，保证互斥
        for flag_name in self._KIND_TO_FLAG.values():
            state[flag_name] = False
        for path_name in self._KIND_TO_PATH.values():
            state[path_name] = ''

        # 2.置位权威类别 + 对应路径
        state[self._KIND_TO_FLAG[detected_kind]] = True
        state[self._KIND_TO_PATH[detected_kind]] = stored_path

    def _step_7_print_stats(self, state: ParseGraphState, header: bytes, detected_kind: str) -> None:
        """
        【步骤7】输出统计日志
        :param state: 项目状态字典
        :param header: 文件头字节串
        :param detected_kind: 魔数判定的类别
        """
        self.log_step('文件头', header[:8].hex(' '))
        self.log_step('扩展名判定', state.get('file_ext') or '(无)')
        self.log_step('魔数判定', detected_kind)
        self.log_step(
            '类别开关',
            f'docx={state["is_docx_read_enabled"]} '
            f'pdf={state["is_pdf_read_enabled"]} '
            f'image={state["is_image_read_enabled"]}'
        )


if __name__ == '__main__':
    # 激活日志的全局配置
    setup_logging()

    import tempfile

    # 临时根目录：由系统生成，测试结束自动随进程清理
    root_dir = Path(tempfile.mkdtemp(prefix='node_detect_type_test_'))

    # 各类"货物真价实"的文件头（只造文件头即可，本节点只嗅探文件头/容器）
    PNG_HEADER = b'\x89PNG\r\n\x1a\n' + b'\x00' * 24
    JPEG_HEADER = b'\xff\xd8\xff\xe0' + b'\x00' * 28
    BMP_HEADER = b'BM' + b'\x00' * 30
    TIFF_HEADER = b'II*\x00' + b'\x00' * 28
    WEBP_HEADER = b'RIFF' + b'\x24\x00\x00\x00' + b'WEBP' + b'\x00' * 16
    PDF_BYTES = b'%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< /Root 1 0 R >>\n%%EOF\n'
    PDF_ENCRYPTED_BYTES = (b'%PDF-1.7\n5 0 obj\n<< /Filter /Standard >>\nendobj\n'
                           b'trailer\n<< /Encrypt 5 0 R /Root 1 0 R >>\n%%EOF\n')
    OLE_BYTES = b'\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1' + b'\x00' * 24

    def _make_file(name: str, content: bytes) -> Path:
        """在临时根目录下造一个文件"""
        file_path = root_dir / name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
        return file_path

    def _make_docx(name: str, valid: bool = True) -> Path:
        """造一个 DOCX：valid=True 生成合法的 zip 容器，False 生成损坏的容器"""
        file_path = root_dir / name
        file_path.parent.mkdir(parents=True, exist_ok=True)
        if valid:
            with zipfile.ZipFile(file_path, 'w') as zip_file:
                zip_file.writestr('word/document.xml', '<w:document />')
        else:
            # PK 开头的魔数 + 垃圾内容 → 容器损坏
            file_path.write_bytes(b'PK\x03\x04' + b'this-is-not-a-zip' * 4)
        return file_path

    node_detect_type = NodeDetectType()
    passed = 0

    # ---------- 一、正常场景：魔数与扩展名一致 ----------
    logging.getLogger().info('=' * 24 + ' 一、正常场景 ' + '=' * 24)
    ok_cases = [
        ('采购合同.docx', _make_docx('ok/采购合同.docx'), 'docx'),
        ('采购合同.pdf', _make_file('ok/采购合同.pdf', PDF_BYTES), 'pdf'),
        ('扫描件.png', _make_file('ok/扫描件.png', PNG_HEADER), 'image'),
        ('扫描件.jpg', _make_file('ok/扫描件.jpg', JPEG_HEADER), 'image'),
        ('扫描件.bmp', _make_file('ok/扫描件.bmp', BMP_HEADER), 'image'),
        ('扫描件.tiff', _make_file('ok/扫描件.tiff', TIFF_HEADER), 'image'),
        ('扫描件.webp', _make_file('ok/扫描件.webp', WEBP_HEADER), 'image'),
    ]

    for name, path, expected_kind in ok_cases:
        init_state = get_default_state()
        init_state.update({
            'task_id': f'task-ok-{Path(name).suffix.strip(".")}',
            'stored_file_path': str(path),
            'import_file_path': str(path),
            'file_ext': Path(name).suffix.lower(),
        })
        result = node_detect_type(init_state)

        assert result[NodeDetectType._KIND_TO_FLAG[expected_kind]] is True, \
            f'{name} 应判定为 {expected_kind}'
        flags = [
            result['is_docx_read_enabled'],
            result['is_pdf_read_enabled'],
            result['is_image_read_enabled'],
        ]
        assert sum(flags) == 1, f'{name} 三个类别开关应互斥，实际 {flags}'
        assert result[NodeDetectType._KIND_TO_PATH[expected_kind]] == str(path), \
            f'{name} 对应路径应指向该文件'
        passed += 1
        logging.getLogger().info(f'  ✅ {name:<16} → {expected_kind}')

    # ---------- 二、扩展名与魔数不一致：以魔数为准 ----------
    logging.getLogger().info('=' * 24 + ' 二、扩展名伪装 ' + '=' * 24)

    # 一个真实内容为 PNG、但被改名成 .pdf 的文件
    disguised = _make_file('disguise/伪装合同.pdf', PNG_HEADER)
    init_state = get_default_state()
    init_state.update({
        'task_id': 'task-disguise',
        'stored_file_path': str(disguised),
        'import_file_path': str(disguised),
        'file_ext': '.pdf',
        'is_pdf_read_enabled': True,  # 模拟 node_entry 按扩展名给出的错误初判
        'pdf_path': str(disguised),
    })
    result = node_detect_type(init_state)

    assert result['is_image_read_enabled'] is True, '应以魔数为准判定为 image'
    assert result['is_pdf_read_enabled'] is False, '扩展名给出的 pdf 开关应被纠正'
    assert result['pdf_path'] == '', '被纠正后 pdf_path 应清空'
    assert result['image_path'] == str(disguised), 'image_path 应指向该文件'
    passed += 1
    logging.getLogger().info('  ✅ 伪装 .pdf 的 PNG → 纠正为 image，pdf 开关被清掉')

    # ---------- 三、异常场景 ----------
    logging.getLogger().info('=' * 24 + ' 三、异常场景 ' + '=' * 24)

    err_cases = [
        ('空文件', _make_file('err/空文件.pdf', b''), DocumentDetectError),
        ('OLE加密文档', _make_file('err/加密.docx', OLE_BYTES), DocumentDetectError),
        ('加密PDF', _make_file('err/加密.pdf', PDF_ENCRYPTED_BYTES), DocumentDetectError),
        ('DOCX容器损坏', _make_docx('err/损坏.docx', valid=False), DocumentDetectError),
        ('格式无法识别', _make_file('err/说明.txt', b'hello world, not a contract'), DocumentDetectError),
    ]

    for case_name, path, expected_exc in err_cases:
        init_state = get_default_state()
        init_state.update({
            'task_id': 'task-error-case',
            'stored_file_path': str(path),
            'file_ext': Path(path).suffix.lower(),
        })
        try:
            node_detect_type(init_state)
        except Exception as e:
            assert isinstance(e, ParseProcessError), f'{case_name} 应被基类包装成 ParseProcessError'
            assert isinstance(e.cause, expected_exc), \
                f'{case_name} 应抛出 {expected_exc.__name__}，实际为 {type(e.cause).__name__}'
            passed += 1
            logging.getLogger().info(f'  ✅ {case_name:<12} → 正确抛出 {expected_exc.__name__}')
        else:
            raise AssertionError(f'{case_name} 未抛出异常，用例失败')

    # 路径类异常
    path_err_cases = [
        ('路径全为空', {}, StateFieldError),
        ('文件不存在', {'stored_file_path': str(root_dir / '不存在.pdf')}, FileProcessingError),
        ('路径是目录', {'stored_file_path': str(root_dir)}, FileProcessingError),
    ]
    for case_name, overrides, expected_exc in path_err_cases:
        init_state = get_default_state()
        init_state['task_id'] = 'task-error-case'
        init_state.update(overrides)
        try:
            node_detect_type(init_state)
        except Exception as e:
            assert isinstance(e.cause, expected_exc), \
                f'{case_name} 应抛出 {expected_exc.__name__}，实际为 {type(e.cause).__name__}'
            passed += 1
            logging.getLogger().info(f'  ✅ {case_name:<12} → 正确抛出 {expected_exc.__name__}')
        else:
            raise AssertionError(f'{case_name} 未抛出异常，用例失败')

    # 用例：stored_file_path 为空时回退 import_file_path
    fallback_file = _make_file('fallback/回退.pdf', PDF_BYTES)
    init_state = get_default_state()
    init_state.update({
        'task_id': 'task-fallback',
        'stored_file_path': '',
        'import_file_path': str(fallback_file),
    })
    result = node_detect_type(init_state)
    assert result['is_pdf_read_enabled'] is True, 'stored_file_path 为空时应回退 import_file_path'
    passed += 1
    logging.getLogger().info('  ✅ 路径回退     → 正确回退到 import_file_path')

    # ---------- 四、打印最终状态（便于人工核对）----------
    logging.getLogger().info('=' * 24 + ' 四、最终状态 ' + '=' * 24)
    logging.getLogger().info(json.dumps(result, ensure_ascii=False, indent=4))

    logging.getLogger().info(f'node_detect_type 自测全部通过 ✅ 共 {passed} 个用例')
