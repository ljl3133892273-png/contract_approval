'''processor/writeback_processor/adapter/__init__.py'''

'''
回写 Adapter 包（SPEC 4.5）

职责：把"调用审批系统"这件事封装成一个窄接口，让节点只关心业务语义。

    ApproveAdapter      真实 HTTP 实现（httpx，Bearer 令牌，超时可控）
    MockApproveAdapter  未配置审批地址时的降级实现（只记日志，不假装送达成功）

约定（与 utils/llm_utils.py 的 Provider 策略保持一致）:
    · 两个实现都暴露 `name` / `is_mock` 属性，节点据此把"是否真实送达"透传到状态与评论里；
    · Adapter 只负责**一次**调用，重试与退避由节点（`node_adapter_send`）控制 ——
      这样重试策略能统一配置、统一审计，也便于用失败注入做端到端验证；
    · 调用失败一律抛 `AdapterError`，并带 `retryable` 标记（4xx 不可重试）。
'''
