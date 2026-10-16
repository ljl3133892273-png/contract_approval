'''web/__init__.py'''

'''
接入层（Web 层）

职责：把 4 个业务子图暴露成 REST 接口（SPEC 第 7 章），并负责鉴权、参数校验、
      统一响应包装、后台任务编排与状态轮询。

分层（对应 SPEC 2.2 的「接入层」）:
    `web/main.py`        FastAPI 应用装配（路由、异常处理、启动钩子）
    `web/api/*_service.py`  各业务域的路由实现
    `web/orchestrator.py`   子图编排（解析 → 审查 → 推荐；回写；报告）
    `web/deps.py`           依赖注入（当前用户、RBAC、数据权限）
    `web/schemas.py`        请求/响应模型
    `web/security.py`       密码哈希 + JWT
    `web/errors.py`         统一响应包与业务错误码

重要的分层原则:
    接入层**只做编排与转换**，不写业务规则。状态迁移走 `utils.flow_state_machine`，
    业务产物由各 processor 产出 —— 接入层不直接改 `review_status`（SPEC 5.2）。
'''
