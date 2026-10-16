'''processor/parse_processor/exceptions.py'''

"""
解析流程自定义异常

目标:
- 定义统一的异常层级结构
- 提供清晰的错误信息
- 支持错误溯源

设计原则:
- 继承层级清晰，便于分类捕获
- 每个异常携带上下文信息（节点名、原因）
- 支持异常链追溯

导入方式:
    from processor.parse_processor.exceptions import (
        ParseProcessError, StateFieldError, ValidationError, ...
    )
"""


class ParseProcessError(Exception):  # python 自带的异常类
    '''解析流程基础异常'''

    def __init__(self, message: str, node_name: str = '', cause: Exception = None):
        self.node_name = node_name  # 发生错误的流程节点名称
        self.cause = cause  # 导致该错误的底层原始异常
        super().__init__(message)

    def __str__(self):
        parts = []
        if self.node_name:
            parts.append(f"[{self.node_name}]")
        parts.append(super().__str__())
        if self.cause:
            parts.append(f"(原因: {self.cause})")
        return " ".join(parts)


class StateFieldError(ParseProcessError):
    '''状态字段错误。

    从 state 中获取必需字段时，字段缺失、为空或类型不符时抛出。

    Attributes:
        field_name: 缺失或无效的字段名称。
        expected_type: 期望的字段类型（可选）。
    '''

    def __init__(
            self,
            node_name: str = '',
            field_name: str = '',
            expected_type: type = None,
            message: str = '',
            cause: Exception = None,
    ):
        self.field_name = field_name
        self.expected_type = expected_type
        if not message:
            message = f'状态字段 {field_name} 缺失或无效'
            if expected_type:
                message += f',期望类型:{expected_type.__name__}'
        super().__init__(message, node_name=node_name, cause=cause)


class ConfigurationError(ParseProcessError):
    '''配置错误:环境变量缺失或配置值无效'''
    pass


class FileProcessingError(ParseProcessError):
    '''文件处理错误:文件不存在、不是文件、读写失败'''
    pass


class ValidationError(ParseProcessError):
    '''数据验证错误:输入数据不符合预期（如不支持的文件类型）'''
    pass


class DocumentDetectError(ParseProcessError):
    '''格式嗅探错误:文件头魔数无法识别、文档加密、正文为空'''
    pass


class DocxParseError(ParseProcessError):
    '''DOCX 解析错误:python-docx 读取段落/表格失败'''
    pass


class PdfParseError(ParseProcessError):
    '''PDF 解析错误:PyMuPDF 抽取文本/坐标失败'''
    pass


class OcrError(ParseProcessError):
    '''OCR 识别错误:PaddleOCR 识别失败或超时'''
    pass


class ClauseSplitError(ParseProcessError):
    '''条款切分错误:锚点匹配或 LLM 辅助归类异常'''
    pass


class MetaExtractError(ParseProcessError):
    '''元数据提取错误:主体/金额/期限抽取失败'''
    pass


class LLMError(ParseProcessError):
    '''LLM 调用错误:API 调用失败、响应解析失败'''
    pass


class StorageError(ParseProcessError):
    '''存储错误:数据库操作失败'''
    pass


if __name__ == '__main__':
    # 轻量自测：验证异常信息拼装与字段携带
    err = StateFieldError(field_name='import_file_path', expected_type=str)
    assert 'import_file_path' in str(err), '异常信息应包含出错字段名'
    assert err.field_name == 'import_file_path'

    wrapped = ParseProcessError(message='解析失败', node_name='node_entry', cause=err)
    assert '[node_entry]' in str(wrapped), '异常信息应带节点名前缀'
    assert '(原因:' in str(wrapped), '异常信息应带原始异常原因'
    assert isinstance(wrapped, ParseProcessError)

    print('exceptions 自测通过 ✅')
