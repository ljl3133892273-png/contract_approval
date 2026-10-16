'''processor/parse_processor/nodes/node_entry.py'''

'''
步骤分解
1. 接收状态: 获取 `import_file_path`。
2. 路径校验: 非空校验 → 文件是否存在 → 是否是一个文件（不是目录）。
3. 判断类型: 按扩展名判定 DOCX / PDF / 图片 三类（大小写不敏感）。
4. 设置标记: 更新 state 中的 `is_docx_read_enabled` / `is_pdf_read_enabled` / `is_image_read_enabled`
   与对应的 `docx_path` / `pdf_path` / `image_path`，供主图 `route_after_detect` 路由使用。
5. 提取文件信息: `file_name` / `file_ext` / `file_title` / `file_size`，供后续节点与元数据复用。

需求分析
输入:
- `import_file_path`：接入（上传或审批拉取落盘）后的合同文件绝对路径

输出:
- `is_docx_read_enabled` / `is_pdf_read_enabled` / `is_image_read_enabled`：文件类别开关（三者互斥）
- `docx_path` / `pdf_path` / `image_path`：对应类别的文件路径
- `file_name`：文件名（含扩展名）
- `file_ext`：扩展名（小写、带点）
- `file_title`：文件标题（不含扩展名）
- `file_size`：文件字节数

说明:
- 本节点只做「按扩展名初判」，真正的格式嗅探（mimetype + 文件头魔数、加密/空文件判定）
  由后续的 `node_detect_type` 节点完成。
- 不做完整磁盘级校验的业务前置判断：文件存在性与类型合法性必须校验，其余交给后续节点。
'''

import json
import logging
from pathlib import Path

from processor.parse_processor.base import BaseNode, setup_logging
from processor.parse_processor.exceptions import (
    ParseProcessError,
    StateFieldError,
    FileProcessingError,
    ValidationError,
)
from processor.parse_processor.state import ParseGraphState, get_default_state


class NodeEntry(BaseNode):
    '''入口节点：路径校验 + 文件类别初判（任务分发）'''
    name = 'node_entry'

    def process(self, state: ParseGraphState) -> ParseGraphState:
        """
        节点：入口（node_entry）
        整体流程：校验路径 → 判定类别 → 提取文件信息 → 输出统计日志
        核心目的：把住解析流水线的第一道门，把「文件类别」翻译成三个互斥的状态开关，
                 让主图能据此做条件路由（docx / pdf / image）
        必要参数：import_file_path
        更新参数：is_docx_read_enabled、is_pdf_read_enabled、is_image_read_enabled、
                 docx_path、pdf_path、image_path、file_name、file_ext、file_title、file_size

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取并校验文件路径=======
        # 作用：从状态字典取出待解析文件路径，完成非空 / 存在性 / 是否为文件三重校验
        # 输出：可供后续使用的 Path 对象
        import_file_path_obj = self._step_1_get_file_path(state)

        # =======步骤二:按扩展名判定文件类别=======
        # 作用：核心判定逻辑，把扩展名翻译成三个互斥开关 + 对应路径，供主图条件路由
        # 输出：小写扩展名（带点）
        file_ext = self._step_2_decide_file_type(state, import_file_path_obj)

        # =======步骤三:提取文件基础信息=======
        # 作用：补齐 file_name / file_ext / file_title / file_size，供后续节点与元数据复用
        # 输出：无返回值，直接写回状态
        self._step_3_extract_file_info(state, import_file_path_obj, file_ext)

        # =======步骤四:输出本次判定结果日志=======
        # 作用：打印判定结果，便于调试与排查「为什么走了这条分支」
        # 输出：无返回值，仅通过 logger 输出标准化日志
        self._step_4_print_stats(state)

        return state

    def _step_1_get_file_path(self, state: ParseGraphState) -> Path:
        """
        【步骤1】获取并校验文件路径
        功能：非空校验 → 存在性校验 → 是否是一个文件（排除目录）
        :param state: 项目状态字典（ParseGraphState），须包含 import_file_path
        :return: 校验通过后的 Path 对象
        """
        # 1.从 state 中获取文件的绝对路径
        import_file_path = state.get('import_file_path')
        # 判断路径是否为空 (非空校验 鲁棒性/健壮性)
        if not import_file_path:
            raise StateFieldError(
                field_name='import_file_path',  # 出错字段名
                message='文件路径不能为空',  # 错误提示文案
                expected_type=str  # 期望字段类型为字符串
            )

        # 2.将 import_file_path 转为 Path 对象
        import_file_path_obj = Path(import_file_path)

        # 3.存在性校验
        if not import_file_path_obj.exists():
            raise FileProcessingError(message=f'文件 {import_file_path_obj.name} 不存在')

        # 4.排除目录：必须是一个文件
        if not import_file_path_obj.is_file():
            raise FileProcessingError(message=f'路径 {import_file_path_obj} 不是一个文件')

        return import_file_path_obj

    def _step_2_decide_file_type(self, state: ParseGraphState, import_file_path_obj: Path) -> str:
        """
        【步骤2】按扩展名判定文件类别（核心：三个互斥开关 + 对应路径）
        功能：把扩展名映射为 docx / pdf / image 三类，置位对应开关并写入路径；
             其余两个开关显式置 False，避免重试换文件后残留上一次的标志。
        :param state: 项目状态字典
        :param import_file_path_obj: 已校验的文件 Path 对象
        :return: 小写扩展名（带点），如 .pdf
        """
        # 1.取扩展名并统一小写（.PDF 与 .pdf 视为同一种）
        file_ext = import_file_path_obj.suffix.lower()
        import_file_path = str(import_file_path_obj)

        # 2.先把三个开关全部复位，保证互斥
        state['is_docx_read_enabled'] = False
        state['is_pdf_read_enabled'] = False
        state['is_image_read_enabled'] = False

        # 3.按白名单判定类别
        if file_ext in self.config.docx_extensions:
            state['is_docx_read_enabled'] = True
            state['docx_path'] = import_file_path

        elif file_ext in self.config.pdf_extensions:
            state['is_pdf_read_enabled'] = True
            state['pdf_path'] = import_file_path

        elif file_ext in self.config.image_extensions:
            state['is_image_read_enabled'] = True
            state['image_path'] = import_file_path

        else:
            # 不在白名单内 → 直接拒绝（SPEC 12：上传类型白名单）
            raise ValidationError(
                message=f'不支持的文件类型 {file_ext or "(无扩展名)"}，'
                        f'仅支持 {sorted(self.config.supported_extensions)}'
            )

        return file_ext

    def _step_3_extract_file_info(self, state: ParseGraphState, import_file_path_obj: Path, file_ext: str) -> None:
        """
        【步骤3】提取文件基础信息
        功能：补齐文件名 / 扩展名 / 文件标题 / 文件字节数
        :param state: 项目状态字典
        :param import_file_path_obj: 已校验的文件 Path 对象
        :param file_ext: 小写扩展名（带点）
        """
        state['file_name'] = import_file_path_obj.name
        state['file_ext'] = file_ext
        state['file_title'] = import_file_path_obj.stem  # 不含扩展名的文件标题
        state['file_size'] = import_file_path_obj.stat().st_size

    def _step_4_print_stats(self, state: ParseGraphState) -> None:
        """
        【步骤4】输出本次判定结果日志
        :param state: 项目状态字典
        """
        self.log_step('文件类型判定', f'file_title={state["file_title"]}')
        self.log_step(
            '类别开关',
            f'docx={state["is_docx_read_enabled"]} '
            f'pdf={state["is_pdf_read_enabled"]} '
            f'image={state["is_image_read_enabled"]}'
        )
        self.log_step('文件大小', f'{state["file_size"]} 字节')


if __name__ == '__main__':
    # 激活日志的全局配置
    setup_logging()

    import tempfile

    # 临时工作目录：由系统生成，测试结束自动随进程清理
    work_dir = Path(tempfile.mkdtemp(prefix='node_entry_test_'))

    def _make_file(suffix: str) -> str:
        """在临时目录里造一个占位文件，仅用于本节点的「路径 + 扩展名」判定测试"""
        file_path = work_dir / f'测试合同{suffix}'
        file_path.write_bytes(b'placeholder')
        return str(file_path)

    # 创建节点对象
    node_entry = NodeEntry()
    passed = 0

    # ---------- 一、正常场景：三类合法格式（含大写扩展名）----------
    logging.getLogger().info('=' * 30 + ' 一、正常场景 ' + '=' * 30)
    ok_cases = [
        ('.docx', 'is_docx_read_enabled', 'docx_path'),
        ('.pdf', 'is_pdf_read_enabled', 'pdf_path'),
        ('.png', 'is_image_read_enabled', 'image_path'),
        ('.PDF', 'is_pdf_read_enabled', 'pdf_path'),  # 大写扩展名也应识别
    ]

    last_result = None
    for suffix, flag_name, path_name in ok_cases:
        file_path = _make_file(suffix)

        # 每个用例都用一份全新的默认状态，模拟真实调用
        init_state = get_default_state()
        init_state['task_id'] = f'task-{suffix.strip(".").lower()}'
        init_state['file_dir'] = str(work_dir)
        init_state['import_file_path'] = file_path

        result = node_entry(init_state)
        last_result = result

        # 断言 1：对应类别的开关被置位，且路径写入正确
        assert result[flag_name] is True, f'{suffix} 应置 {flag_name}=True'
        assert result[path_name] == file_path, f'{suffix} 应把路径写入 {path_name}'

        # 断言 2：三个类别开关必须互斥（有且只有一个为 True）
        flags = [
            result['is_docx_read_enabled'],
            result['is_pdf_read_enabled'],
            result['is_image_read_enabled'],
        ]
        assert sum(flags) == 1, f'{suffix} 三个类别开关应互斥，实际 {flags}'

        # 断言 3：文件基础信息被正确提取
        assert result['file_title'] == '测试合同', f'{suffix} 应提取出不带扩展名的标题'
        assert result['file_ext'] == suffix.lower(), f'{suffix} 扩展名应统一为小写'
        assert result['file_name'] == f'测试合同{suffix}', f'{suffix} 文件名应保留原样'
        assert result['file_size'] > 0, f'{suffix} 应记录文件字节数'

        passed += 1
        logging.getLogger().info(f'  ✅ {suffix:<6} → {flag_name}=True')

    # ---------- 二、异常场景：路径非法 / 类型不支持 ----------
    logging.getLogger().info('=' * 30 + ' 二、异常场景 ' + '=' * 30)
    err_cases = [
        ('空路径', '', StateFieldError),
        ('文件不存在', str(work_dir / '不存在的合同.docx'), FileProcessingError),
        ('路径是目录', str(work_dir), FileProcessingError),
        ('类型不支持', _make_file('.txt'), ValidationError),
    ]

    for case_name, bad_path, expected_exc in err_cases:
        init_state = get_default_state()
        init_state['task_id'] = 'task-error-case'
        init_state['import_file_path'] = bad_path

        try:
            node_entry(init_state)
        except Exception as e:
            # 基类会把子类异常统一包装成 ParseProcessError，原始异常挂在 .cause 上
            assert isinstance(e, ParseProcessError), f'{case_name} 应被基类包装成 ParseProcessError'
            assert isinstance(e.cause, expected_exc), \
                f'{case_name} 应抛出 {expected_exc.__name__}，实际为 {type(e.cause).__name__}'
            passed += 1
            logging.getLogger().info(f'  ✅ {case_name:<6} → 正确抛出 {expected_exc.__name__}')
        else:
            raise AssertionError(f'{case_name} 未抛出异常，用例失败')

    # ---------- 三、打印最终状态（便于人工核对）----------
    logging.getLogger().info('=' * 30 + ' 三、最终状态 ' + '=' * 30)
    logging.getLogger().info(json.dumps(last_result, ensure_ascii=False, indent=4))

    logging.getLogger().info(f'node_entry 自测全部通过 ✅ 共 {passed} 个用例')
