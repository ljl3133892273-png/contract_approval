'''web/deps.py'''

"""
依赖注入：当前用户、RBAC 鉴权、数据权限（SPEC 第 12 章）

三层权限模型:
    1. **认证**（get_current_user）：解析 Bearer 令牌 → 当前用户。
       演示阶段可用 `AUTH_ENABLED=false` 直接放行（SPEC 12「演示阶段可 token 直发」），
       此时使用 `.env` 里 DEMO_USERNAME 指定的账号身份。
    2. **功能鉴权**（require_roles）：接口级角色白名单，对应 SPEC 7 章各接口的「权限」列。
    3. **数据权限**（ensure_task_visible）：业务角色只能看**自己提交**的任务
       （SPEC 12「业务仅可查本人任务（applicant=me 过滤）」）。

关于 `contract_task.applicant` 存什么:
    存**登录用户名**（唯一、可比较），不存展示名 ——
    否则"张三"和另一个部门同名的"张三"就无法区分，数据权限会失效。
    展示名通过 `GET /api/auth/me` 或任务详情里的 `applicant_display_name` 给出。
"""

import logging
from typing import Any, Callable, Dict, List, Optional

from fastapi import Depends
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from config_bag.app_config import get_app_config
from db.constants import UserRole
from db.models import AppUser, ContractTask
from db.session import session_scope
from web.errors import ErrorCode, ApiError
from web.security import (
    TOKEN_TYPE_ACCESS,
    TokenError,
    bearer_token_from_header,
    decode_token,
    token_payload_to_user,
)

logger = logging.getLogger(__name__)


def load_user_by_username(username: str) -> Optional[Dict[str, Any]]:
    """
    按用户名查用户
    :param username: 登录名
    :return: 用户字典；不存在返回 None
    """
    if not username:
        return None

    from sqlalchemy import select

    with session_scope() as session:
        row = session.scalars(select(AppUser).where(AppUser.username == username)).first()
        if row is None:
            return None
        return {
            'user_id': row.id,
            'username': row.username,
            'role': row.role or UserRole.BUSINESS,
            'display_name': row.display_name or row.username,
        }


# 用 HTTPBearer 而不是裸 Header：这样 /docs 会出现 "Authorize" 按钮，
# OpenAPI 里也才有标准的安全方案（前端 SDK 生成器据此识别鉴权方式）。
# auto_error=False：令牌缺失时由我们自己抛 ApiError，保证走统一响应包（而不是框架的 403 裸响应）。
_BEARER_SCHEME = HTTPBearer(auto_error=False, description='登录后获取的 access_token')


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(_BEARER_SCHEME),
) -> Dict[str, Any]:
    """
    解析当前用户（FastAPI 依赖）

    - 鉴权开启：必须带合法 access 令牌
    - 鉴权关闭（演示）：用 `DEMO_USERNAME` 指定的账号身份，找不到就合成一个 admin
    :param credentials: HTTPBearer 解析出的凭证
    :return: {'user_id', 'username', 'role', 'display_name'}
    :raises ApiError: 未认证 / 令牌非法
    """
    config = get_app_config()

    if not config.auth_enabled:
        demo = load_user_by_username(config.demo_username)
        if demo is not None:
            return demo
        logger.warning(
            f'[鉴权] AUTH_ENABLED=false 且演示账号 {config.demo_username!r} 不存在，'
            f'已合成一个 admin 身份（仅演示用）'
        )
        return {
            'user_id': 0,
            'username': config.demo_username or 'demo',
            'role': UserRole.ADMIN,
            'display_name': '演示身份',
        }

    token = (credentials.credentials if credentials else '') or ''
    try:
        payload = decode_token(token, TOKEN_TYPE_ACCESS, config)
    except TokenError as e:
        # 对外不区分"过期/签名错/类型错"，避免给攻击者额外信息
        raise ApiError(ErrorCode.UNAUTHORIZED, '未认证或凭证已失效', http_status=401)

    user = token_payload_to_user(payload)
    user['display_name'] = payload.get('username') or ''
    return user


def require_roles(*roles: str) -> Callable[..., Dict[str, Any]]:
    """
    生成"要求指定角色"的依赖

    用法:
        @router.post('/tasks/upload', dependencies=[Depends(require_roles(UserRole.BUSINESS, UserRole.ADMIN))])

    :param roles: 允许的角色（SPEC 12 的 legal / business / admin）
    :return: FastAPI 依赖函数
    """
    allowed: List[str] = [r for r in roles if r]
    if not allowed:
        raise ValueError('require_roles 至少要指定一个角色')

    def _checker(current_user: Dict[str, Any] = Depends(get_current_user)) -> Dict[str, Any]:
        if current_user.get('role') not in allowed:
            raise ApiError(
                ErrorCode.FORBIDDEN,
                f"当前角色 {current_user.get('role')!r} 无权访问，需要 {allowed}",
                http_status=403,
            )
        return current_user

    return _checker


def is_admin(user: Dict[str, Any]) -> bool:
    """是否管理员"""
    return (user or {}).get('role') == UserRole.ADMIN


def ensure_task_visible(user: Dict[str, Any], task: ContractTask) -> None:
    """
    数据权限校验：业务角色只能访问自己提交的任务

    SPEC 12「数据权限：法务可查全部；业务仅可查本人任务（applicant=me 过滤）」。
    admin 与 legal 不受限。
    :param user: 当前用户
    :param task: 任务 ORM 行
    :raises ApiError: 无权访问（返回 404 而非 403 —— 不泄露"这个任务存在"）
    """
    role = (user or {}).get('role')
    if role in (UserRole.ADMIN, UserRole.LEGAL):
        return
    if (task.applicant or '') != (user or {}).get('username', ''):
        # 刻意用 404：403 会让业务用户知道"这个 id 确实存在一个不属于他的任务"
        raise ApiError(ErrorCode.NOT_FOUND, f'任务 {task.id} 不存在', http_status=404)


def applicant_filter(user: Dict[str, Any]) -> Optional[str]:
    """
    列表查询的数据权限过滤条件
    :param user: 当前用户
    :return: 需要按 applicant 过滤时返回用户名，否则返回 None（不过滤）
    """
    if (user or {}).get('role') == UserRole.BUSINESS:
        return (user or {}).get('username') or ''
    return None


if __name__ == '__main__':
    # 自测：不依赖 FastAPI 请求对象的部分直接调；依赖注入部分用轻量替身验证角色判断
    import logging as _logging
    import tempfile
    from pathlib import Path

    from db.constants import ReviewStatus
    from db.seed import seed_all
    from db.session import init_db, init_engine, reset_engine, session_scope

    _logging.basicConfig(level=_logging.ERROR, format='%(levelname)s - %(message)s')
    passed = 0

    work_dir = Path(tempfile.mkdtemp(prefix='deps_test_'))
    reset_engine()
    init_engine(f'sqlite:///{work_dir.as_posix()}/deps_test.db')
    init_db()
    seed_all()

    # ---------- 一、按用户名查用户 ----------
    legal = load_user_by_username('legal')
    assert legal is not None and legal['role'] == 'legal'
    assert legal['display_name'] == '法务审查人', '应带展示名'
    assert load_user_by_username('不存在的人') is None
    assert load_user_by_username('') is None
    passed += 1
    print('  ✅ 按用户名查用户（含不存在与空串）')

    # ---------- 二、require_roles 的角色判断 ----------
    admin_user = load_user_by_username('admin')
    checker = require_roles(UserRole.ADMIN)
    assert checker(admin_user)['username'] == 'admin', '依赖函数应把用户透传出去'
    for wrong in (legal, {'user_id': 1, 'username': 'x', 'role': UserRole.BUSINESS}):
        try:
            checker(wrong)
        except ApiError as e:
            assert e.code == ErrorCode.FORBIDDEN and e.http_status == 403
            assert '无权' in e.message
        else:
            raise AssertionError(f"角色 {wrong.get('role')} 不应通过 admin 白名单")

    multi = require_roles(UserRole.LEGAL, UserRole.ADMIN)
    assert multi(legal)['role'] == 'legal'
    assert multi({'role': UserRole.ADMIN})['role'] == 'admin', '多角色白名单应放行任一'
    try:
        multi({'role': UserRole.BUSINESS})
    except ApiError:
        pass
    else:
        raise AssertionError('不在白名单内的角色应被拒绝')
    passed += 1
    print('  ✅ require_roles：单/多角色白名单与 403')

    try:
        require_roles()
    except ValueError:
        pass
    else:
        raise AssertionError('不指定角色应直接报错（否则等于没鉴权）')
    passed += 1
    print('  ✅ require_roles 必须指定角色，否则拒绝构造')

    # ---------- 三、数据权限 ----------
    with session_scope() as session:
        session.add_all([
            ContractTask(contract_name='业务A的合同', applicant='business',
                         source_type='upload', review_status=ReviewStatus.PENDING),
            ContractTask(contract_name='业务B的合同', applicant='other_user',
                         source_type='upload', review_status=ReviewStatus.PENDING),
        ])

    from sqlalchemy import select

    with session_scope() as session:
        own = session.scalars(
            select(ContractTask).where(ContractTask.contract_name == '业务A的合同')
        ).first()
        others = session.scalars(
            select(ContractTask).where(ContractTask.contract_name == '业务B的合同')
        ).first()

    business = {'user_id': 2, 'username': 'business', 'role': UserRole.BUSINESS}

    ensure_task_visible(business, own)  # 不抛异常即通过
    try:
        ensure_task_visible(business, others)
    except ApiError as e:
        assert e.http_status == 404, '刻意用 404 而不是 403（不泄露任务存在）'
    else:
        raise AssertionError('业务用户不应看到他人任务')

    # legal / admin 不受限
    ensure_task_visible({'role': UserRole.LEGAL}, others)
    ensure_task_visible({'role': UserRole.ADMIN}, others)
    passed += 1
    print('  ✅ 数据权限：business 只能看自己的，legal/admin 不受限（越权用 404）')

    # ---------- 四、列表过滤条件 ----------
    assert applicant_filter(business) == 'business', '业务角色应带 applicant 过滤'
    assert applicant_filter({'role': UserRole.LEGAL}) is None, '法务不过滤'
    assert applicant_filter({'role': UserRole.ADMIN}) is None, '管理员不过滤'
    passed += 1
    print('  ✅ 列表数据权限过滤条件正确')

    # ---------- 五、is_admin ----------
    assert is_admin({'role': UserRole.ADMIN}) is True
    assert is_admin({'role': UserRole.LEGAL}) is False
    assert is_admin({}) is False
    passed += 1
    print('  ✅ is_admin 判断正确')

    reset_engine()
    print(f'web.deps 自测全部通过 ✅ 共 {passed} 个用例')
