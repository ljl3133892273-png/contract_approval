'''processor/writeback_processor/exceptions.py'''

"""
回写流程自定义异常

结构与 parse_processor / review_processor / recommend_processor 的异常层保持一致：
    统一的 WritebackProcessError 风格 —— 携带节点名与底层原因，支持异常链追溯。

异常逐层包装：节点的 process() 抛业务异常 → BaseNode.__call__ 包装成
WritebackProcessError → 接入层捕获后决定返回给前端什么提示。

异常分层（与其他子图一致）:
    `utils.flow_state_machine.FlowStateError` —— 基础设施层，状态迁移非法
    `WritebackProcessError` 及其子类            —— 业务层，本模块自己的语义
    节点捕获 FlowStateError 后包装成自己的 StateTransitionError。
"""


class WritebackProcessError(Exception):
    '''回写流程基础异常'''

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


class StateFieldError(WritebackProcessError):
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


class ConfigurationError(WritebackProcessError):
    '''配置错误：审批系统地址/令牌缺失，且未允许 mock'''
    pass


class ValidationError(WritebackProcessError):
    '''数据验证错误：前置条件不满足（如没有审批单号，无从回写）'''
    pass


class CommentBuildError(WritebackProcessError):
    '''评论构建错误：风险数据无法渲染成 Markdown'''
    pass


class AdapterError(WritebackProcessError):
    """
    审批 Adapter 调用错误：网络异常、超时、非 2xx 响应

    `retryable` 用来区分"重试有意义"与"重试纯属浪费时间"：
        · 5xx / 超时 / 连接失败  → retryable=True（对端可能只是临时抖动）
        · 4xx（除 408/429）      → retryable=False（请求本身有问题：单据不存在、正文非法、令牌错）
    盲目重试一个 404 会白白占用 5s + 30s 的退避时间，还掩盖真正的问题。
    """

    def __init__(self, message: str, node_name: str = '', cause: Exception = None,
                 retryable: bool = True, status_code: int = 0, response_body: str = ''):
        self.retryable = retryable
        self.status_code = status_code
        self.response_body = response_body
        super().__init__(message, node_name=node_name, cause=cause)


class StateTransitionError(WritebackProcessError):
    '''状态迁移错误：回写状态机不允许该事件（包装 FlowStateError）'''
    pass


class ReportExportError(WritebackProcessError):
    '''报告导出错误：Markdown 渲染或 PDF 生成失败'''
    pass


class StorageError(WritebackProcessError):
    '''存储错误：数据库操作失败'''
    pass


if __name__ == '__main__':
    # 轻量自测
    err = StateFieldError(field_name='risk_items', expected_type=list)
    assert 'risk_items' in str(err), '异常信息应包含字段名'
    assert err.field_name == 'risk_items'

    wrapped = WritebackProcessError(message='回写失败', node_name='node_adapter_send', cause=err)
    assert '[node_adapter_send]' in str(wrapped), '应带节点名前缀'
    assert '(原因:' in str(wrapped), '应带原始异常'

    # 异常层级：子类应能被基类捕获（BaseNode 统一包装依赖这一点）
    for sub in (StateFieldError, ConfigurationError, ValidationError, CommentBuildError,
                AdapterError, StateTransitionError, ReportExportError, StorageError):
        assert issubclass(sub, WritebackProcessError), f'{sub.__name__} 应继承 WritebackProcessError'

    print('writeback_processor.exceptions 自测通过 ✅')
