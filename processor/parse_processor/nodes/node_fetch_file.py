'''processor/parse_processor/nodes/node_fetch_file.py'''

'''
步骤分解
1. 接收状态: 获取 `import_file_path`、`file_dir`、`source_type`、`external_doc_id`。
2. 大小校验: 按 `ParseConfig.max_file_size_mb` 拦截超限文件（落盘前先确认能不能收）。
3. 指纹计算: 分块读取文件字节，算出 SHA256（避免大文件一次性读进内存）。
4. 归档落盘: 把接入文件归档到本任务的 `file_dir` 下，统一后续解析入口；
             同名但内容不同时不覆盖，改用「文件名_指纹前8位.扩展名」。
5. 去重判定: 同指纹 + 同审批单号 → 判定为重复，复用已有任务，不再重复解析（SPEC 4.1.2）。
6. 输出统计日志。

需求分析
输入:
- `import_file_path`：接入（上传或审批拉取落盘）后的合同文件绝对路径
- `file_dir`：本任务的独立工作目录（由接入层创建）
- `source_type`：upload / approve_push / approve_pull
- `external_doc_id`：审批单号（去重键）

输出:
- `stored_file_path`：归档后的规范本地路径（下游节点统一读这个）
- `file_hash`：SHA256 指纹
- `file_size`：文件字节数
- `is_duplicate` / `duplicated_task_id`：是否命中重复及命中的已有任务
- 同步更新 `docx_path` / `pdf_path` / `image_path`，保证下游只认一份路径

边界说明:
- 审批来源（approve_push / approve_pull）的附件下载由接入层（web/orchestrator）完成，
  本节点只处理已经落在本地的文件。
- 图内去重（`_find_duplicated_task`）查 contract_task 表，口径与接入层一致；
  DB 不可用时只告警按未命中处理，接入层仍会兜底同一口径的去重。
'''

import hashlib
import json
import logging
import shutil
from pathlib import Path

from processor.parse_processor.base import BaseNode, setup_logging
from processor.parse_processor.config import ParseConfig
from processor.parse_processor.exceptions import (
    ParseProcessError,
    StateFieldError,
    FileProcessingError,
    ValidationError,
)
from processor.parse_processor.state import ParseGraphState, get_default_state


class NodeFetchFile(BaseNode):
    '''附件落盘节点：本地可用性确认 + 归档 + SHA256 指纹 + 去重判定'''
    name = 'node_fetch_file'

    # 合法的接入来源（SPEC 4.1.1）
    _VALID_SOURCE_TYPES = {'upload', 'approve_push', 'approve_pull'}
    # 审批来源（附件需由 Adapter 下载到本地）
    _APPROVE_SOURCE_TYPES = {'approve_push', 'approve_pull'}
    # SHA256 分块读取的块大小：1MB
    _HASH_CHUNK_SIZE = 1024 * 1024

    def process(self, state: ParseGraphState) -> ParseGraphState:
        """
        节点：附件落盘（node_fetch_file）
        整体流程：取参数校验 → 大小校验 → 指纹计算 → 归档落盘 → 去重判定 → 统计日志
        核心目的：把「刚接进来的文件」变成「本任务工作目录下、可安全解析的本地文件」，
                 并算出 SHA256 指纹供附件去重使用
        必要参数：import_file_path、file_dir
        可选参数：source_type（默认 upload）、external_doc_id
        更新参数：stored_file_path、file_hash、file_size、is_duplicate、duplicated_task_id，
                 docx_path / pdf_path / image_path

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取并校验输入参数=======
        # 作用：取出文件路径与任务工作目录，完成非空 / 存在性 / 是否为文件 / 来源合法性校验
        # 输出：可供后续使用的源文件 Path 对象
        src_path = self._step_1_get_inputs(state)

        # =======步骤二:文件大小校验=======
        # 作用：按配置的大小上限拦截超限文件，落盘前先确认「能不能收」
        # 输出：无返回值，超限直接抛 ValidationError
        self._step_2_check_size(state, src_path)

        # =======步骤三:计算 SHA256 指纹=======
        # 作用：分块读取文件字节计算指纹，作为附件去重与任务复用的唯一依据
        # 输出：64 位十六进制指纹字符串
        file_hash = self._step_3_calc_hash(src_path)

        # =======步骤四:归档落盘到任务工作目录=======
        # 作用：把接入文件归档到 file_dir，统一后续解析入口；同名不同内容时改名避免互相覆盖
        # 输出：归档后的目标路径
        stored_path = self._step_4_archive(state, src_path, file_hash)

        # =======步骤五:去重判定=======
        # 作用：同指纹 + 同审批单号判定为重复，复用已有任务，不再重复解析
        # 输出：无返回值，直接写回状态
        self._step_5_check_duplicate(state, file_hash)

        # =======步骤六:输出统计日志=======
        # 作用：打印指纹 / 大小 / 归档路径 / 去重结果，便于排查「为什么复用了任务」
        # 输出：无返回值，仅通过 logger 输出标准化日志
        self._step_6_print_stats(state, stored_path)

        return state

    def _step_1_get_inputs(self, state: ParseGraphState) -> Path:
        """
        【步骤1】获取并校验输入参数
        功能：校验 import_file_path / file_dir，并确认文件确实存在于本地
        :param state: 项目状态字典（ParseGraphState）
        :return: 校验通过后的源文件 Path 对象
        """
        # 1.文件路径非空校验
        import_file_path = state.get('import_file_path')
        if not import_file_path:
            raise StateFieldError(
                field_name='import_file_path',
                message='文件路径不能为空',
                expected_type=str
            )

        # 2.任务工作目录非空校验（决定归档位置，属于必要参数）
        file_dir = state.get('file_dir')
        if not file_dir:
            raise StateFieldError(
                field_name='file_dir',
                message='任务工作目录不能为空（归档落盘需要）',
                expected_type=str
            )

        # 3.接入来源校验：允许为空（默认按 upload 处理）
        source_type = state.get('source_type') or 'upload'
        if source_type not in self._VALID_SOURCE_TYPES:
            raise ValidationError(
                message=f'不支持的接入来源 {source_type}，仅支持 {sorted(self._VALID_SOURCE_TYPES)}'
            )
        state['source_type'] = source_type

        # 4.文件存在性与「是否为文件」校验
        src_path = Path(import_file_path)
        if not src_path.exists():
            # 审批来源的文件由 Adapter 下载到本地，这里给一个更明确的提示
            if source_type in self._APPROVE_SOURCE_TYPES:
                raise FileProcessingError(
                    message=f'审批来源附件尚未下载到本地：{src_path}（审批 Adapter 待接入）'
                )
            raise FileProcessingError(message=f'文件 {src_path.name} 不存在')

        if not src_path.is_file():
            raise FileProcessingError(message=f'路径 {src_path} 不是一个文件')

        return src_path

    def _step_2_check_size(self, state: ParseGraphState, src_path: Path) -> None:
        """
        【步骤2】文件大小校验
        功能：按 max_file_size_mb 拦截超限文件（SPEC 12 默认 50MB）
        :param state: 项目状态字典
        :param src_path: 源文件 Path 对象
        """
        file_size = src_path.stat().st_size
        max_bytes = self.config.max_file_size_mb * 1024 * 1024

        if file_size > max_bytes:
            raise ValidationError(
                message=f'文件大小 {file_size / 1024 / 1024:.2f}MB 超出上限 '
                        f'{self.config.max_file_size_mb}MB'
            )

        # 记录文件字节数（node_entry 已写过一次，这里按归档前的真实值再确认一遍）
        state['file_size'] = file_size

    def _step_3_calc_hash(self, src_path: Path) -> str:
        """
        【步骤3】计算 SHA256 指纹
        功能：分块读取文件字节计算摘要，避免大文件一次性读进内存
        :param src_path: 源文件 Path 对象
        :return: 64 位十六进制指纹字符串
        """
        sha256 = hashlib.sha256()
        with open(src_path, 'rb') as file_buffer:
            for chunk in iter(lambda: file_buffer.read(self._HASH_CHUNK_SIZE), b''):
                sha256.update(chunk)
        file_hash = sha256.hexdigest()
        self.log_step('指纹计算', f'{file_hash[:16]}…')
        return file_hash

    def _step_4_archive(self, state: ParseGraphState, src_path: Path, file_hash: str) -> Path:
        """
        【步骤4】归档落盘到任务工作目录
        功能：把接入文件归档到 file_dir；已在目录内则原地保留；
             同名但内容不同时不覆盖，改用「文件名_指纹前8位.扩展名」
        :param state: 项目状态字典
        :param src_path: 源文件 Path 对象
        :param file_hash: 已算出的 SHA256 指纹
        :return: 归档后的目标路径
        """
        file_dir = Path(state['file_dir'])
        file_dir.mkdir(parents=True, exist_ok=True)

        target_path = file_dir / src_path.name

        # 1.源文件已在目标位置 → 无需复制
        if src_path.resolve() == target_path.resolve():
            stored_path = target_path

        else:
            # 2.同名冲突：目标已存在且内容不同 → 加指纹前缀，避免互相覆盖
            if target_path.exists() and self._file_hash(target_path) != file_hash:
                target_path = file_dir / f'{src_path.stem}_{file_hash[:8]}{src_path.suffix}'
                self.log_step('归档改名', f'同名文件内容不同，改存为 {target_path.name}')

            # 3.复制归档（copy2 保留元数据）
            shutil.copy2(src_path, target_path)
            stored_path = target_path

        state['stored_file_path'] = str(stored_path)
        state['file_hash'] = file_hash
        state['file_size'] = stored_path.stat().st_size

        # 4.同步 node_entry 写下的类别路径，保证下游只认一份路径
        if state.get('is_docx_read_enabled'):
            state['docx_path'] = str(stored_path)
        elif state.get('is_pdf_read_enabled'):
            state['pdf_path'] = str(stored_path)
        elif state.get('is_image_read_enabled'):
            state['image_path'] = str(stored_path)

        return stored_path

    def _file_hash(self, path: Path) -> str:
        """计算任意文件的 SHA256（内部辅助，用于同名冲突比对）"""
        sha256 = hashlib.sha256()
        with open(path, 'rb') as file_buffer:
            for chunk in iter(lambda: file_buffer.read(self._HASH_CHUNK_SIZE), b''):
                sha256.update(chunk)
        return sha256.hexdigest()

    def _step_5_check_duplicate(self, state: ParseGraphState, file_hash: str) -> None:
        """
        【步骤5】去重判定
        功能：同指纹 + 同审批单号 → 判定为重复，复用已有任务（SPEC 4.1.2）
        :param state: 项目状态字典
        :param file_hash: SHA256 指纹
        """
        external_doc_id = state.get('external_doc_id', '')
        existing_task_id = self._find_duplicated_task(
            file_hash, external_doc_id, current_task_id=state.get('task_id', '')
        )

        state['is_duplicate'] = bool(existing_task_id)
        state['duplicated_task_id'] = existing_task_id

        if existing_task_id:
            self.logger.warning(
                f'文件重复：与任务 {existing_task_id} 同审批单号且同指纹，'
                f'接入层应复用该任务而非新建'
            )

    def _find_duplicated_task(self, file_hash: str, external_doc_id: str,
                              current_task_id: str = '') -> str:
        """
        查询「同指纹 + 同审批单号」的已有任务（SPEC 4.1.2 幂等接入）

        判定口径与接入层 `orchestrator.is_duplicated` 完全一致：
        只有**审批来源**（有单号）且**单号 + 指纹都相同**才算重复 ——
        上传来源没有单号，同名文件重复上传是合法场景，不能拦。

        :param file_hash: SHA256 指纹
        :param external_doc_id: 审批单号（空 → 不参与去重）
        :param current_task_id: 当前任务ID（排除自身，重跑场景）
        :return: 命中的已有任务ID；未命中 / 不可判定时返回空字符串
        """
        if not file_hash or not external_doc_id:
            return ''

        from sqlalchemy import select

        from db.models import ContractTask
        from db.session import session_scope

        try:
            with session_scope() as session:
                stmt = select(ContractTask).where(
                    ContractTask.external_doc_id == external_doc_id,
                    ContractTask.file_hash == file_hash,
                )
                if current_task_id.isdigit():
                    stmt = stmt.where(ContractTask.id != int(current_task_id))
                row = session.scalars(stmt).first()
                return str(row.id) if row is not None else ''
        except Exception as e:
            # 图内去重是便利不是门槛：DB 不可用（如独立跑子图）时只告警，
            # 接入层 `orchestrator.is_duplicated` 仍会兜底同一口径的去重。
            self.logger.warning(f'图内去重查询失败，按未命中处理（{e}）')
            return ''

    def _step_6_print_stats(self, state: ParseGraphState, stored_path: Path) -> None:
        """
        【步骤6】输出统计日志
        :param state: 项目状态字典
        :param stored_path: 归档后的路径
        """
        self.log_step('接入来源', state['source_type'])
        self.log_step('文件大小', f'{state["file_size"]} 字节')
        self.log_step('归档路径', str(stored_path))
        self.log_step(
            '去重判定',
            f'is_duplicate={state["is_duplicate"]} '
            f'duplicated_task_id={state["duplicated_task_id"] or "-"}'
        )


if __name__ == '__main__':
    # 激活日志的全局配置
    setup_logging()

    import tempfile

    # 临时根目录：由系统生成，测试结束自动随进程清理
    root_dir = Path(tempfile.mkdtemp(prefix='node_fetch_file_test_'))

    def _make_file(*parts: str, content: bytes = b'contract-content') -> Path:
        """在临时根目录下造一个占位文件"""
        file_path = root_dir.joinpath(*parts)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(content)
        return file_path

    def _sha256_of(path: Path) -> str:
        """独立实现一份 hash，用于交叉验证节点算出的指纹"""
        sha256 = hashlib.sha256()
        with open(path, 'rb') as file_buffer:
            for chunk in iter(lambda: file_buffer.read(1024), b''):
                sha256.update(chunk)
        return sha256.hexdigest()

    node_fetch_file = NodeFetchFile()
    passed = 0

    # ---------- 一、正常场景 ----------
    logging.getLogger().info('=' * 26 + ' 一、正常场景 ' + '=' * 26)

    # 用例 1：源文件在 incoming 目录 → 归档到任务目录，指纹正确，类别路径被同步
    src = _make_file('incoming', '软件采购合同.docx')
    init_state = get_default_state()
    init_state.update({
        'task_id': 'task-0001',
        'source_type': 'upload',
        'import_file_path': str(src),
        'file_dir': str(root_dir / 'task-0001'),
        'is_docx_read_enabled': True,
        'docx_path': str(src),
    })
    result = node_fetch_file(init_state)

    expected_stored = root_dir / 'task-0001' / '软件采购合同.docx'
    assert result['stored_file_path'] == str(expected_stored), '文件应被归档到任务工作目录'
    assert expected_stored.exists(), '归档后的文件应真实存在'
    assert result['file_hash'] == _sha256_of(expected_stored), 'SHA256 指纹应与独立计算结果一致'
    assert len(result['file_hash']) == 64, 'SHA256 指纹应为 64 位十六进制'
    assert result['file_size'] == len(b'contract-content'), '应记录文件字节数'
    assert result['docx_path'] == str(expected_stored), 'docx_path 应同步为归档后的路径'
    assert result['is_duplicate'] is False, '首次接入不应判定为重复'
    assert result['duplicated_task_id'] == '', '未命中重复时任务ID应为空'
    passed += 1
    logging.getLogger().info('  ✅ 用例1 归档落盘 + 指纹 + 类别路径同步')

    # 用例 2：文件已在任务目录内 → 原地保留，不重复复制
    src_in_place = _make_file('task-0002', '销售合同.pdf', content=b'pdf-content')
    init_state = get_default_state()
    init_state.update({
        'task_id': 'task-0002',
        'source_type': 'upload',
        'import_file_path': str(src_in_place),
        'file_dir': str(root_dir / 'task-0002'),
        'is_pdf_read_enabled': True,
        'pdf_path': str(src_in_place),
    })
    result = node_fetch_file(init_state)
    assert result['stored_file_path'] == str(src_in_place), '文件已在工作目录内应原地保留'
    assert result['pdf_path'] == str(src_in_place), 'pdf_path 应指向同一份路径'
    assert result['file_hash'] == _sha256_of(src_in_place)
    passed += 1
    logging.getLogger().info('  ✅ 用例2 已在工作目录内 → 原地保留')

    # 用例 3：同名但内容不同 → 加指纹前缀，不覆盖已有文件
    occupied = _make_file('task-0003', '合同.docx', content=b'OLD-CONTENT')
    src_conflict = _make_file('incoming3', '合同.docx', content=b'NEW-CONTENT')
    init_state = get_default_state()
    init_state.update({
        'task_id': 'task-0003',
        'source_type': 'upload',
        'import_file_path': str(src_conflict),
        'file_dir': str(root_dir / 'task-0003'),
        'is_docx_read_enabled': True,
        'docx_path': str(src_conflict),
    })
    result = node_fetch_file(init_state)

    stored = Path(result['stored_file_path'])
    assert stored != occupied, '同名但内容不同时不应覆盖已有文件'
    assert stored.name.startswith('合同_'), f'应改名为「文件名_指纹前8位」，实际 {stored.name}'
    assert stored.read_bytes() == b'NEW-CONTENT', '归档文件内容应为新文件内容'
    assert occupied.read_bytes() == b'OLD-CONTENT', '已有文件内容不应被改动'
    passed += 1
    logging.getLogger().info(f'  ✅ 用例3 同名冲突 → 改存为 {stored.name}')

    # 用例 4：指纹稳定性 —— 同内容、不同文件名/目录 → 指纹相同
    same_a = _make_file('incoming4', '甲.docx', content=b'SAME-BYTES')
    same_b = _make_file('incoming5', '乙-完全不同的名字.pdf', content=b'SAME-BYTES')
    hash_a, hash_b = '', ''
    for idx, (path, flag, path_key) in enumerate([
        (same_a, 'is_docx_read_enabled', 'docx_path'),
        (same_b, 'is_pdf_read_enabled', 'pdf_path'),
    ]):
        init_state = get_default_state()
        init_state.update({
            'task_id': f'task-0004-{idx}',
            'source_type': 'upload',
            'import_file_path': str(path),
            'file_dir': str(root_dir / f'task-0004-{idx}'),
            flag: True,
            path_key: str(path),
        })
        res = node_fetch_file(init_state)
        if idx == 0:
            hash_a = res['file_hash']
        else:
            hash_b = res['file_hash']
    assert hash_a == hash_b, '相同字节内容的文件，指纹必须一致（去重的前提）'
    passed += 1
    logging.getLogger().info('  ✅ 用例4 同内容不同文件名 → 指纹一致')

    # ---------- 用例5：图内去重接 DB（SPEC 4.1.2）----------
    logging.getLogger().info('=' * 26 + ' 用例5 图内去重（真实 DB）' + '=' * 26)

    from db.models import ContractTask
    from db.session import init_db, init_engine, reset_engine, session_scope

    db_dir = root_dir / 'dedup_db'
    reset_engine()
    init_engine(f'sqlite:///{db_dir.as_posix()}/d.db')
    init_db()

    file = _make_file('incoming5', '审批合同.docx', content=b'apv-content')
    apv_hash = _sha256_of(file)

    with session_scope() as session:
        new_task = ContractTask(contract_name='已有任务', source_type='approve_pull',
                                external_doc_id='APV-DEDUP-1', file_hash=apv_hash)
        session.add(new_task)
        session.flush()
        existing_id = new_task.id

    # 同单号 + 同指纹 → 命中
    hit = node_fetch_file._find_duplicated_task(apv_hash, 'APV-DEDUP-1')
    assert hit == str(existing_id), f'同单号同指纹应命中任务 {existing_id}，实际 {hit!r}'
    # 排除自身（重跑场景）
    assert node_fetch_file._find_duplicated_task(apv_hash, 'APV-DEDUP-1',
                                                 current_task_id=str(existing_id)) == '', \
        '重跑时应排除自身'
    # 指纹不同 / 单号不同 / 无单号 → 不命中
    assert node_fetch_file._find_duplicated_task('other-hash', 'APV-DEDUP-1') == ''
    assert node_fetch_file._find_duplicated_task(apv_hash, 'APV-OTHER') == ''
    assert node_fetch_file._find_duplicated_task(apv_hash, '') == '', '无审批单号不参与去重'
    passed += 1
    logging.getLogger().info('  ✅ 用例5 同单号+同指纹命中 / 排除自身 / 边界不命中')

    # 完整走一遍节点验证 is_duplicate 标志
    dup_state = get_default_state()
    dup_state.update({
        'task_id': 'task-0099',
        'source_type': 'approve_pull',
        'external_doc_id': 'APV-DEDUP-1',
        'import_file_path': str(file),
        'file_dir': str(root_dir / 'task-0099'),
        'is_docx_read_enabled': True,
        'docx_path': str(file),
    })
    dup_result = node_fetch_file(dup_state)
    assert dup_result['is_duplicate'] is True, '应判定为重复'
    assert dup_result['duplicated_task_id'] == str(existing_id)
    passed += 1
    logging.getLogger().info('  ✅ 用例5b 节点级 is_duplicate / duplicated_task_id 正确置位')

    reset_engine()

    # ---------- 二、异常场景 ----------
    logging.getLogger().info('=' * 26 + ' 二、异常场景 ' + '=' * 26)

    normal_file = _make_file('incoming6', '正常.docx')

    err_cases = [
        ('空路径', {'import_file_path': '', 'file_dir': str(root_dir / 't')}, StateFieldError),
        ('工作目录为空', {'import_file_path': str(normal_file), 'file_dir': ''}, StateFieldError),
        ('文件不存在',
         {'import_file_path': str(root_dir / '不存在.docx'), 'file_dir': str(root_dir / 't')},
         FileProcessingError),
        ('路径是目录',
         {'import_file_path': str(root_dir), 'file_dir': str(root_dir / 't')},
         FileProcessingError),
        ('来源非法',
         {'import_file_path': str(normal_file), 'file_dir': str(root_dir / 't'),
          'source_type': 'unknown_source'},
         ValidationError),
        ('审批附件未下载',
         {'import_file_path': str(root_dir / '未下载.docx'), 'file_dir': str(root_dir / 't'),
          'source_type': 'approve_push', 'external_doc_id': 'AP-2026-001'},
         FileProcessingError),
    ]

    for case_name, overrides, expected_exc in err_cases:
        init_state = get_default_state()
        init_state['task_id'] = 'task-error-case'
        init_state.update(overrides)
        try:
            node_fetch_file(init_state)
        except Exception as e:
            assert isinstance(e, ParseProcessError), f'{case_name} 应被基类包装成 ParseProcessError'
            assert isinstance(e.cause, expected_exc), \
                f'{case_name} 应抛出 {expected_exc.__name__}，实际为 {type(e.cause).__name__}'
            passed += 1
            logging.getLogger().info(f'  ✅ {case_name:<8} → 正确抛出 {expected_exc.__name__}')
        else:
            raise AssertionError(f'{case_name} 未抛出异常，用例失败')

    # 用例：大小超限 —— 注入 max_file_size_mb=0 的配置，任何非空文件都应被拦住
    strict_node = NodeFetchFile(ParseConfig(max_file_size_mb=0))
    init_state = get_default_state()
    init_state.update({
        'task_id': 'task-oversize',
        'source_type': 'upload',
        'import_file_path': str(normal_file),
        'file_dir': str(root_dir / 'task-oversize'),
    })
    try:
        strict_node(init_state)
    except Exception as e:
        assert isinstance(e.cause, ValidationError), \
            f'超限文件应抛 ValidationError，实际为 {type(e.cause).__name__}'
        passed += 1
        logging.getLogger().info('  ✅ 大小超限  → 正确抛出 ValidationError')
    else:
        raise AssertionError('超限文件未被拦截，用例失败')

    # ---------- 三、打印最终状态（便于人工核对）----------
    logging.getLogger().info('=' * 26 + ' 三、最终状态 ' + '=' * 26)
    logging.getLogger().info(json.dumps(result, ensure_ascii=False, indent=4))

    logging.getLogger().info(f'node_fetch_file 自测全部通过 ✅ 共 {passed} 个用例')
