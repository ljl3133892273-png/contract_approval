'''mock_approve_service/__init__.py'''

'''
审批系统本地模拟服务

用途：在没有真实审批系统可对接的阶段，提供一个行为完整的替身，让
      `processor/writeback_processor/adapter/approve_adapter.py` 能被真实地端到端验证，
      而不是只靠单元测试里的假对象。

它模拟了真实审批系统上我们真正依赖的四件事：
    1. 单据查询与**附件下载**（SPEC 4.1.1 的 approve_pull / approve_push 来源）
    2. **评论回写**（SPEC 4.5），并支持**幂等键**——同一幂等键重复提交返回同一条评论
    3. **失败注入**（固定失败 / 连续 N 次失败 / 故意超时），用来验证 Adapter 的退避重试
    4. Bearer Token 鉴权，用来验证 Adapter 的令牌透传

启动方式见 `main.py` 文件头。
'''
