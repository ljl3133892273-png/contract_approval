'''web/api/auth_service.py'''

'''
鉴权接口（SPEC 12）

    POST /api/auth/login     用户名密码换令牌对
    POST /api/auth/refresh   用 refresh 换新令牌对（滑动续期）
    GET  /api/auth/me        当前用户信息
    GET  /api/auth/users     用户列表（admin，便于演示时查看有哪几个账号）

关于"演示阶段可 token 直发":
    SPEC 12 允许演示阶段直接发令牌。本实现的做法是提供 `AUTH_ENABLED=false` 开关 ——
    关掉后所有接口都以 `.env` 里 DEMO_USERNAME 指定的身份放行，不需要登录。
    比"硬编码一个万能令牌"更可控：至少身份是显式的、可审计的。
'''

import logging
from typing import Any, Dict, List

from fastapi import APIRouter, Depends
from sqlalchemy import select

from config_bag.app_config import get_app_config
from db.constants import UserRole
from db.models import AppUser
from db.session import session_scope
from web.deps import get_current_user, load_user_by_username, require_roles
from web.errors import ApiError, ErrorCode, ok, register_exception_handlers
from web.schemas import LoginRequest, RefreshRequest
from web.security import (
    TOKEN_TYPE_REFRESH,
    TokenError,
    create_token_pair,
    decode_token,
    token_payload_to_user,
    verify_password,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/api/auth', tags=['鉴权'])


@router.post('/login', summary='登录（用户名密码换令牌）')
def login(payload: LoginRequest) -> Dict[str, Any]:
    """
    登录并签发令牌对

    安全细节：用户名不存在与密码错误返回**同一个错误码与文案**（3003 用户名或密码错误），
    否则攻击者可以靠错误信息差异枚举出有哪些账号。
    """
    with session_scope() as session:
        user = session.scalars(
            select(AppUser).where(AppUser.username == payload.username)
        ).first()
        if user is None or not verify_password(payload.password, user.password_hash):
            logger.warning(f'[鉴权] 登录失败：{payload.username!r}')
            raise ApiError(ErrorCode.LOGIN_FAILED, '用户名或密码错误', http_status=401)

        tokens = create_token_pair(user.id, user.username, user.role)
        result = {
            **tokens,
            'user': {
                'user_id': user.id,
                'username': user.username,
                'role': user.role,
                'display_name': user.display_name or user.username,
            },
        }

    logger.info(f'[鉴权] 用户 {payload.username!r} 登录成功（角色 {tokens["role"]}）')
    return ok(result, message='登录成功')


@router.post('/refresh', summary='刷新令牌（滑动续期）')
def refresh(payload: RefreshRequest) -> Dict[str, Any]:
    """
    用 refresh 令牌换一对新令牌

    为什么要把 refresh 也换新（而不是只换 access）:
        SPEC 12 写的是「access 短期 + refresh 滑动」—— 滑动就是只要用户在活跃期内
        持续使用，有效期就往后延。所以每次刷新都签发新的 refresh。
    """
    try:
        decoded = decode_token(payload.refresh_token, TOKEN_TYPE_REFRESH)
    except TokenError:
        raise ApiError(ErrorCode.UNAUTHORIZED, '刷新凭证无效或已过期', http_status=401)

    user = token_payload_to_user(decoded)

    # 复核用户是否仍存在（账号被删后旧 refresh 不应还能换出新令牌）
    current = load_user_by_username(user['username'])
    if current is None:
        raise ApiError(ErrorCode.UNAUTHORIZED, '账号不存在或已停用', http_status=401)

    tokens = create_token_pair(current['user_id'], current['username'], current['role'])
    return ok({**tokens, 'user': current}, message='刷新成功')


@router.get('/me', summary='当前用户信息')
def me(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
    """查看当前登录身份（前端用来决定按钮显隐）"""
    config = get_app_config()
    return ok({
        **current_user,
        'auth_enabled': config.auth_enabled,
        'permissions': _permissions_of(current_user.get('role')),
    })


@router.get('/users', summary='用户列表（admin）')
def list_users(_: Dict[str, Any] = Depends(require_roles(UserRole.ADMIN))) -> Dict[str, Any]:
    """列出账号，便于演示时知道有哪些身份可切换（不返回任何密码信息）"""
    with session_scope() as session:
        rows = session.scalars(select(AppUser).order_by(AppUser.id)).all()
        users: List[Dict[str, Any]] = [
            {
                'user_id': row.id,
                'username': row.username,
                'role': row.role,
                'display_name': row.display_name or row.username,
                'created_at': row.created_at.isoformat() if row.created_at else '',
            }
            for row in rows
        ]
    return ok(users)


def _permissions_of(role: str) -> List[str]:
    """
    角色能力清单（给前端做按钮级控制；**真正的拦截仍在服务端**）
    :param role: legal / business / admin
    :return: 能力标识列表
    """
    mapping = {
        UserRole.ADMIN: ['task:create', 'task:read', 'task:retry', 'task:cancel',
                         'risk:write', 'writeback:trigger', 'report:export', 'rule:write'],
        UserRole.LEGAL: ['task:read', 'risk:write', 'writeback:trigger', 'report:export'],
        UserRole.BUSINESS: ['task:create', 'task:read'],
    }
    return mapping.get(role, [])


if __name__ == '__main__':
    # 自测：用 TestClient 跑接口（临时库 + 种子账号）
    import logging as _logging
    import tempfile
    from pathlib import Path

    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from db.seed import seed_all
    from db.session import init_db, init_engine, reset_engine

    _logging.basicConfig(level=_logging.ERROR, format='%(levelname)s - %(message)s')
    passed = 0

    work_dir = Path(tempfile.mkdtemp(prefix='auth_service_test_'))
    reset_engine()
    init_engine(f'sqlite:///{work_dir.as_posix()}/auth_test.db')
    init_db()
    seed_all()

    app = FastAPI()
    register_exception_handlers(app)   # 否则 ApiError 不会被转成响应包
    app.include_router(router)
    client = TestClient(app)

    # ---------- 一、登录 ----------
    response = client.post('/api/auth/login', json={'username': 'legal', 'password': 'legal123'})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body['code'] == 0
    data = body['data']
    for key in ('access_token', 'refresh_token', 'token_type', 'expires_in', 'user'):
        assert key in data, f'登录响应缺少 {key}'
    assert data['user']['role'] == 'legal'
    assert data['user']['display_name'] == '法务审查人'
    access = data['access_token']
    refresh = data['refresh_token']
    passed += 1
    print('  ✅ 登录成功，返回令牌对与用户信息')

    # ---------- 二、登录失败 ----------
    for bad in ({'username': 'legal', 'password': 'wrong'},
                {'username': '不存在的账号', 'password': 'x'}):
        response = client.post('/api/auth/login', json=bad)
        assert response.status_code == 401, f'{bad} 应 401'
        payload = response.json()
        assert payload['code'] == 3003
        assert payload['message'] == '用户名或密码错误', \
            '不存在的账号与密码错误必须返回同一文案，否则可枚举账号'
    passed += 1
    print('  ✅ 登录失败统一 3003/401，不泄露账号是否存在')

    # ---------- 三、当前用户 ----------
    response = client.get('/api/auth/me', headers={'Authorization': f'Bearer {access}'})
    assert response.status_code == 200, response.text
    me = response.json()['data']
    assert me['username'] == 'legal' and me['role'] == 'legal'
    assert 'risk:write' in me['permissions'], '法务应有风险项写入权限'
    assert 'rule:write' not in me['permissions'], '法务不应有规则编辑权限'
    passed += 1
    print('  ✅ /me 返回身份与能力清单')

    # 未带令牌 / 令牌非法
    assert client.get('/api/auth/me').status_code == 401
    # 用 ASCII 的非法令牌：HTTP 头只能放 latin-1，中文会在客户端编码阶段就报错
    assert client.get('/api/auth/me',
                      headers={'Authorization': 'Bearer not-a-token'}).status_code == 401
    # refresh 不能当 access 用
    assert client.get('/api/auth/me',
                      headers={'Authorization': f'Bearer {refresh}'}).status_code == 401, \
        'refresh 令牌不能用于访问接口（有效期长得多）'
    passed += 1
    print('  ✅ 未认证 / 非法令牌 / refresh 冒用 均 401')

    # ---------- 四、刷新（滑动续期）----------
    response = client.post('/api/auth/refresh', json={'refresh_token': refresh})
    assert response.status_code == 200, response.text
    refreshed = response.json()['data']
    assert refreshed['access_token'] and refreshed['refresh_token']
    assert refreshed['refresh_token'] != refresh, '滑动续期应签发新的 refresh'
    # 新 access 可用
    assert client.get('/api/auth/me',
                      headers={'Authorization': f"Bearer {refreshed['access_token']}"}
                      ).status_code == 200
    # access 拿来刷新应被拒
    response = client.post('/api/auth/refresh', json={'refresh_token': access})
    assert response.status_code == 401, 'access 不能用来刷新'
    passed += 1
    print('  ✅ 刷新：签发新令牌对，且 access 不能用于刷新')

    # ---------- 五、用户列表权限 ----------
    admin_login = client.post('/api/auth/login',
                              json={'username': 'admin', 'password': 'admin123'}).json()['data']
    response = client.get('/api/auth/users',
                          headers={'Authorization': f"Bearer {admin_login['access_token']}"})
    assert response.status_code == 200
    users = response.json()['data']
    assert len(users) == 3, f'应有 3 个演示账号，实际 {len(users)}'
    assert {u['username'] for u in users} == {'legal', 'business', 'admin'}
    assert all('password_hash' not in u for u in users), '绝不能把密码哈希返回给前端'
    passed += 1
    print('  ✅ 用户列表：admin 可见 3 个账号，且不含任何密码信息')

    response = client.get('/api/auth/users', headers={'Authorization': f'Bearer {access}'})
    assert response.status_code == 403, '法务不应能看用户列表'
    assert response.json()['code'] == 3002
    passed += 1
    print('  ✅ 用户列表越权 → 403 / 3002')

    reset_engine()
    print(f'web.api.auth_service 自测全部通过 ✅ 共 {passed} 个用例')
