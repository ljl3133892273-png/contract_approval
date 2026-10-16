'''processor/review_processor/exceptions.py'''

"""
审查流程自定义异常

结构与 parse_processor 的异常层保持一致：
    统一的 ParseProcessError 风格 —— 携带节点名与底层原因，支持异常链追溯。

异常逐层包装：节点的 process() 抛业务异常 → BaseNode.__call__ 包装成
ReviewProcessError → 接入层捕获后按 SPEC 5.2 / 11.1 决定置 blocked 还是降级。
"""


class ReviewProcessError(Exception):
    '''审查流程基础异常'''

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


class StateFieldError(ReviewProcessError):
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


class ConfigurationError(ReviewProcessError):
    '''配置错误：环境变量缺失或配置值无效'''
    pass


class ValidationError(ReviewProcessError):
    '''数据验证错误：输入数据不符合预期'''
    pass


class RuleEngineError(ReviewProcessError):
    '''规则引擎错误：规则条件非法、算子不支持、字段引用错误'''
    pass


class RiskAggregateError(ReviewProcessError):
    '''综合风险计算错误：风险等级取值非法'''
    pass


class LLMError(ReviewProcessError):
    '''LLM 调用错误：API 调用失败、响应解析失败'''
    pass


class StorageError(ReviewProcessError):
    '''存储错误：数据库操作失败'''
    pass


if __name__ == '__main__':
    # 轻量自测
    err = StateFieldError(field_name='clauses', expected_type=list)
    assert 'clauses' in str(err), '异常信息应包含字段名'
    assert err.field_name == 'clauses'

    wrapped = ReviewProcessError(message='规则引擎异常', node_name='node_rule_match', cause=err)
    assert '[node_rule_match]' in str(wrapped), '应带节点名前缀'
    assert '(原因:' in str(wrapped), '应带原始异常'
    assert isinstance(wrapped, ReviewProcessError)

    print('review_processor.exceptions 自测通过 ✅')
