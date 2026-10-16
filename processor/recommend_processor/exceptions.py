'''processor/recommend_processor/exceptions.py'''

"""
推荐流程自定义异常

结构与 parse_processor / review_processor 的异常层保持一致：
    统一的 RecommendProcessError 风格 —— 携带节点名与底层原因，支持异常链追溯。

异常逐层包装：节点的 process() 抛业务异常 → BaseNode.__call__ 包装成
RecommendProcessError → 接入层捕获后决定降级还是记录告警。
"""


class RecommendProcessError(Exception):
    '''推荐流程基础异常'''

    def __init__(self, message: str, node_name: str = '', cause: Exception = None):
        self.node_name = node_name
        self.cause = cause
        super().__init__(message)

    def __str__(self):
        parts = []
        if self.node_name:
            parts.append(f"[{self.node_name}]")
        parts.append(super().__str__())
        if self.cause:
            parts.append(f"(原因: {self.cause})")
        return " ".join(parts)


class StateFieldError(RecommendProcessError):
    '''状态字段错误：字段缺失、为空或类型不符'''

    def __init__(self, field_name: str = '', expected_type: type = None,
                 message: str = '', node_name: str = '', cause: Exception = None):
        self.field_name = field_name
        self.expected_type = expected_type
        if not message:
            message = f'状态字段 {field_name} 缺失或无效'
            if expected_type:
                message += f',期望类型:{expected_type.__name__}'
        super().__init__(message, node_name=node_name, cause=cause)


class ConfigurationError(RecommendProcessError):
    '''配置错误：环境变量缺失或配置值无效'''
    pass


class ValidationError(RecommendProcessError):
    '''数据验证错误：输入数据不符合预期'''
    pass


class TemplateRecallError(RecommendProcessError):
    '''模板召回错误：模板库查询失败或候选结构非法'''
    pass


class LLMError(RecommendProcessError):
    '''LLM 调用错误：API 调用失败、响应解析失败'''
    pass


class StorageError(RecommendProcessError):
    '''存储错误：数据库操作失败'''
    pass


if __name__ == '__main__':
    # 轻量自测
    err = StateFieldError(field_name='risk_items', expected_type=list)
    assert 'risk_items' in str(err), '异常信息应包含字段名'
    assert err.field_name == 'risk_items'

    wrapped = RecommendProcessError(message='模板召回异常', node_name='node_template_recall', cause=err)
    assert '[node_template_recall]' in str(wrapped), '应带节点名前缀'
    assert '(原因:' in str(wrapped), '应带原始异常'
    assert isinstance(wrapped, RecommendProcessError)

    # 异常层级：子类应能被基类捕获（节点统一包装依赖这一点）
    for sub in (StateFieldError, ConfigurationError, ValidationError,
                TemplateRecallError, LLMError, StorageError):
        assert issubclass(sub, RecommendProcessError), f'{sub.__name__} 应继承 RecommendProcessError'

    print('recommend_processor.exceptions 自测通过 ✅')
