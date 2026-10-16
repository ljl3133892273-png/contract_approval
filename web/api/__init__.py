'''web/api/__init__.py'''

'''
接入层路由包

命名规范：一个业务域一个文件，文件名 `*_service.py`，
         模块内导出 `router: APIRouter`，由 `web/main.py` 统一 include。

    auth_service.py      /api/auth/*      登录、刷新令牌、当前用户
    contract_service.py  /api/tasks/*     任务、工作台、回写、报告（SPEC 7.1 / 7.2）
    rule_service.py      /api/rules/*     规则与示范条款（SPEC 7.3）
'''
