'''web/main.py'''

"""
FastAPI 应用装配（接入层入口）

启动:
    cd F:/Contract_approval/contract_approval
    export PYTHONPATH="F:/Contract_approval/contract_approval"
    ./.venv/Scripts/python.exe -m uvicorn web.main:app --host 127.0.0.1 --port 8000
    或者直接  ./.venv/Scripts/python.exe main.py

装配内容:
    · 路由：auth / tasks（含工作台）/ rules（含模板）/ health / 根路径
    · 全局异常处理：统一响应包（web.errors.register_exception_handlers）
    · CORS：默认放开（演示），生产应收敛到具体域名
    · 启动钩子：建表 + 写种子数据 + 冒烟检查规则引擎
      （演示阶段从简：SPEC 3 说明确「演示：SQLite + create_all」，生产建议引 Alembic）

启动钩子为什么做"种子 + 冒烟":
    空库起来的话，规则表是空的 → 审查永远不产出任何风险 → 看起来像"审查没生效"。
    所以启动时顺手把 R-01~R-08 + 示范条款 + 演示账号写进去，并跑一条规则求值确认引擎可用。
    这一步是**幂等**的，重复启动不会重复写入。
"""

import logging
import time
from contextlib import asynccontextmanager
from typing import Any, Dict

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from config_bag.app_config import get_app_config
from db.seed import RULES, seed_all
from db.session import init_db
from web.api import auth_service, contract_service, rule_service
from web.errors import ok, register_exception_handlers

logger = logging.getLogger(__name__)

APP_TITLE = '合同审批审查系统 API'
APP_VERSION = '1.0.0'
APP_DESCRIPTION = '''
智能合同审查后端：解析 → 审查（规则 + LLM 语义）→ 建议推荐 → 审批回写 → 报告导出。

**鉴权**：除 `/health` 外均需 `Authorization: Bearer <access_token>`；
用 `POST /api/auth/login` 换取（演示账号见 `db/seed.py`）。
若 `.env` 里 `AUTH_ENABLED=false`，则以 `DEMO_USERNAME` 的身份直接放行（SPEC 12 的"token 直发"）。

**响应约定**（SPEC 7.4）：`{"code": 0, "message": "ok", "data": ...}`
'''


def _bootstrap() -> None:
    """
    启动引导：建表 + 种子数据 + 规则引擎冒烟

    幂等：`init_db()` 是 `create_all`，`seed_all()` 按业务键去重。
    """
    started = time.time()

    # 1.建表（演示从简；生产用 Alembic 迁移）
    init_db()

    # 2.种子数据（规则 + 示范条款 + 演示账号）
    stats = seed_all()

    # 3.规则引擎冒烟：确认规则可加载（空规则表会导致"审查永远没风险"这种静默故障）
    try:
        from processor.review_processor.nodes.node_rule_match import (
            load_enabled_rules,
            reset_rule_cache,
        )

        reset_rule_cache()
        total = load_enabled_rules()
        enabled = [rule for rule in total if getattr(rule, 'enabled', True)]
        if not enabled:
            logger.warning(
                f'[启动] 规则表里没有启用的规则（种子应有 {len(RULES)} 条）—— '
                f'审查将只依赖 LLM 语义层，规则层不会产出任何风险'
            )
        else:
            logger.info(f'[启动] 规则引擎就绪，启用规则 {len(enabled)} 条')
    except Exception as e:  # 冒烟失败不应阻止服务启动，但必须让人看见
        logger.warning(f'[启动] 规则引擎冒烟失败（服务仍会启动）：{e}')

    logger.info(f'[启动] 引导完成，用时 {time.time() - started:.2f}s，种子统计 {stats}')


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期：启动时引导，关闭时只记日志（连接由引擎池自行回收）"""
    config = get_app_config()
    config.warn_if_demo_secret()

    _bootstrap()

    logger.info(
        f'[启动] {APP_TITLE} v{APP_VERSION} 就绪：'
        f'http://{config.host}:{config.port}  '
        f'（鉴权 {"开启" if config.auth_enabled else "关闭（演示）"}，'
        f'文档 /docs）'
    )
    yield
    logger.info('[关闭] 服务已停止')


def create_app() -> FastAPI:
    """
    创建应用实例（工厂函数，便于测试里造独立实例）
    :return: FastAPI 应用
    """
    config = get_app_config()

    app = FastAPI(
        title=APP_TITLE,
        version=APP_VERSION,
        description=APP_DESCRIPTION,
        lifespan=lifespan,
    )

    # 统一异常处理：必须早于路由挂载，否则业务异常会变成裸 500
    register_exception_handlers(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=config.cors_origins,
        allow_credentials=False,   # 与 allow_origins=['*'] 不能同时为 True
        allow_methods=['*'],
        allow_headers=['*'],
    )

    # 业务路由
    app.include_router(auth_service.router)
    app.include_router(contract_service.router)
    app.include_router(rule_service.router)

    @app.get('/health', tags=['运维'], summary='健康检查（无需鉴权）')
    def health() -> Dict[str, Any]:
        """探活用；顺带把关键配置状态暴露出来，便于排查"为什么审查没生效" """
        from processor.writeback_processor.config import WritebackConfig
        from utils.llm_utils import get_llm_provider

        llm = get_llm_provider()
        writeback_config = WritebackConfig()
        return ok({
            'status': 'ok',
            'version': APP_VERSION,
            'auth_enabled': config.auth_enabled,
            'llm': {
                'provider': getattr(llm, 'name', ''),
                'is_mock': bool(getattr(llm, 'is_mock', False)),
            },
            'approval': {
                'configured': writeback_config.is_configured,
                'base_url': writeback_config.approve_api_base or '',
                'allow_mock': writeback_config.allow_mock_adapter,
            },
        })

    @app.get('/', tags=['运维'], summary='服务信息')
    def index() -> Dict[str, Any]:
        """根路径给个导航，免得打开就 404"""
        return ok({
            'name': APP_TITLE,
            'version': APP_VERSION,
            'docs': '/docs',
            'health': '/health',
            'modules': {
                'auth': '/api/auth/login',
                'tasks': '/api/tasks',
                'rules': '/api/rules',
                'templates': '/api/templates',
            },
        })

    return app


app = create_app()


if __name__ == '__main__':
    # 自测：装配完整性 + 健康检查 + 路由清单
    import logging as _logging
    import tempfile
    from pathlib import Path

    from fastapi.testclient import TestClient

    from db.session import init_engine, reset_engine

    _logging.basicConfig(level=_logging.ERROR, format='%(levelname)s - %(message)s')
    passed = 0

    # 用临时库，避免污染演示库
    work_dir = Path(tempfile.mkdtemp(prefix='web_main_test_'))
    reset_engine()
    init_engine(f'sqlite:///{work_dir.as_posix()}/main_test.db')

    with TestClient(app) as client:
        # ---------- 一、健康检查（无需鉴权）----------
        response = client.get('/health')
        assert response.status_code == 200, response.text
        body = response.json()
        assert body['code'] == 0 and body['data']['status'] == 'ok'
        assert 'llm' in body['data'] and 'approval' in body['data'], \
            '健康检查应暴露 LLM / 审批的关键配置状态（排查"为什么没生效"）'
        passed += 1
        print('  ✅ /health 可用且无需鉴权，含 LLM/审批配置状态')

        # ---------- 二、根路径信息 ----------
        response = client.get('/')
        assert response.status_code == 200
        assert response.json()['data']['docs'] == '/docs'
        passed += 1
        print('  ✅ / 返回服务导航')

        # ---------- 三、启动引导写入了种子数据 ----------
        from sqlalchemy import func, select

        from db.models import AppUser, Rule, TemplateClause
        from db.session import session_scope

        with session_scope() as session:
            rule_count = session.scalar(select(func.count()).select_from(Rule)) or 0
            template_count = session.scalar(select(func.count()).select_from(TemplateClause)) or 0
            user_count = session.scalar(select(func.count()).select_from(AppUser)) or 0
        assert rule_count == len(RULES), f'启动应写入 {len(RULES)} 条规则，实际 {rule_count}'
        assert template_count > 0, '启动应写入示范条款'
        assert user_count == 3, '启动应写入 3 个演示账号'
        passed += 1
        print(f'  ✅ 启动引导写入 {rule_count} 规则 / {template_count} 模板 / {user_count} 账号')

        # ---------- 四、需要鉴权的接口在未登录时返回统一包 ----------
        response = client.get('/api/tasks')
        assert response.status_code == 401
        assert response.json()['code'] == 3001, '未认证应返回 3001'
        passed += 1
        print('  ✅ 受保护接口未登录 → 401 / 3001')

        # ---------- 五、登录后可访问 ----------
        login = client.post('/api/auth/login', json={'username': 'legal', 'password': 'legal123'})
        token = login.json()['data']['access_token']
        headers = {'Authorization': f'Bearer {token}'}

        response = client.get('/api/tasks', headers=headers)
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload['code'] == 0 and payload['data'] == []
        assert payload['total'] == 0 and payload['page'] == 1, '空列表也要带分页元信息'
        passed += 1
        print('  ✅ 登录后可访问任务列表，空库返回 total=0 且带分页元信息')

        # ---------- 六、路由清单完整（SPEC 7 章的接口都在）----------
        # 用 OpenAPI 契约来核对路径清单：
        # 新版 Starlette 的 app.routes 里子路由被包在 _IncludedRouter 里、不再摊平，
        # 直接遍历拿不到真实路径；而 OpenAPI 是**对外承诺的契约**，拿它核对更贴合本意。
        paths = set(app.openapi()['paths'].keys())
        expected = {
            '/api/auth/login', '/api/auth/refresh', '/api/auth/me',
            '/api/tasks/upload', '/api/tasks', '/api/tasks/{task_id}',
            '/api/tasks/{task_id}/risk-items', '/api/tasks/{task_id}/clauses',
            '/api/tasks/{task_id}/document', '/api/tasks/{task_id}/retry',
            '/api/tasks/{task_id}/cancel', '/api/tasks/{task_id}/write-back',
            '/api/tasks/{task_id}/write-back/status', '/api/tasks/{task_id}/report/export',
            '/api/tasks/{task_id}/progress', '/api/tasks/from-approval',
            '/api/risk-items/{risk_id}', '/api/reports/{report_id}/download',
            '/api/rules', '/api/rules/{rule_id}', '/api/templates',
            '/api/templates/{template_id}', '/health', '/',
        }
        missing = expected - paths
        assert not missing, f'缺少接口：{sorted(missing)}'
        passed += 1
        print(f'  ✅ SPEC 第 7 章的 {len(expected)} 个接口全部注册')

        # ---------- 七、OpenAPI 能生成（契约可交付前端）----------
        schema = client.get('/openapi.json').json()
        assert schema['info']['title'] == APP_TITLE
        assert len(schema['paths']) >= len(expected)
        security_schemes = schema.get('components', {}).get('securitySchemes', {})
        assert 'HTTPBearer' in security_schemes, \
            f'鉴权应以标准安全方案出现在 OpenAPI 里（/docs 的 Authorize 按钮靠它），实际 {list(security_schemes)}'
        passed += 1
        print(f'  ✅ OpenAPI 契约可生成（{len(schema["paths"])} 个路径）')

    reset_engine()
    print(f'web.main 自测全部通过 ✅ 共 {passed} 个用例')
