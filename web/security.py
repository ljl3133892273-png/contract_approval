'''web/security.py'''

"""
鉴权基础设施：密码哈希 + JWT 签发/校验（SPEC 第 12 章）

设计取舍:
    0. 密码哈希的实现放在 `utils/security_utils.py`，本模块 re-export ——
       `db/seed.py` 也要用它生成演示账号，放这里会让数据层反向依赖接入层。
    1. **bcrypt 存密码**：SPEC 6.2 的 `app_user.password_hash` 注释就写了 bcrypt。
       注意 bcrypt 对输入有 **72 字节上限**（超出会直接报错，不是静默截断），
       所以这里显式校验并在接口层限制密码长度 —— 悄悄截断会让"两个不同的长密码
       都能登录"，是真实存在的安全漏洞。
    2. **access 短期 + refresh 滑动**：两类令牌用 `type` 字段区分，
       校验时必须指定期望类型，避免拿 refresh 当 access 用（refresh 有效期长得多，
       混用等于变相延长了会话）。
    3. **JWT 里带 role**：省掉每次请求查库；代价是改角色后旧令牌仍带旧角色，
       在 access 有效期（默认 2 小时）内有效，这个权衡对演示系统是合适的。
    4. 令牌校验失败统一抛 `TokenError`，由接口层转成 HTTP 401，**不泄露具体原因**
       （过期 / 签名错 / 类型错 对外都是"凭证无效"）。
"""

import logging
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

import jwt

from config_bag.app_config import AppConfig, get_app_config
# 密码哈希下沉在 utils（db/seed.py 也要用，放这里会造成"数据层依赖接入层"）
from utils.security_utils import (  # noqa: F401  （re-export，便于调用方只 import 本模块）
    BCRYPT_MAX_BYTES,
    PASSWORD_MIN_LENGTH,
    hash_password,
    verify_password,
)

logger = logging.getLogger(__name__)

# 令牌类型
TOKEN_TYPE_ACCESS = 'access'
TOKEN_TYPE_REFRESH = 'refresh'

# bcrypt 的输入上限（字节）
BCRYPT_MAX_BYTES = 72
# 密码最短长度（接口层与这里保持一致）
PASSWORD_MIN_LENGTH = 6


class TokenError(Exception):
    """令牌无效（过期 / 签名错误 / 类型不符 / 格式非法）"""

    def __init__(self, message: str, reason: str = ''):
        self.reason = reason
        super().__init__(message)


# ============================================================
# JWT
# ============================================================
def _now() -> datetime:
    """当前 UTC 时间"""
    return datetime.now(timezone.utc)


def create_token(user_id: int, username: str, role: str, token_type: str = TOKEN_TYPE_ACCESS,
                 config: Optional[AppConfig] = None) -> str:
    """
    签发令牌
    :param user_id: 用户主键
    :param username: 用户名
    :param role: 角色（legal / business / admin）
    :param token_type: access / refresh
    :param config: 配置对象，默认取全局单例
    :return: JWT 字符串
    :raises ValueError: token_type 非法
    """
    config = config or get_app_config()
    if token_type not in (TOKEN_TYPE_ACCESS, TOKEN_TYPE_REFRESH):
        raise ValueError(f'不支持的令牌类型：{token_type!r}')

    ttl = (config.access_token_seconds if token_type == TOKEN_TYPE_ACCESS
           else config.refresh_token_seconds)
    issued_at = _now()
    payload: Dict[str, Any] = {
        'sub': str(user_id),
        'username': username,
        'role': role,
        'type': token_type,
        'iat': int(issued_at.timestamp()),
        'exp': int((issued_at + timedelta(seconds=ttl)).timestamp()),
        # jti（JWT ID）：令牌的唯一标识。
        # 必须有它 —— iat / exp 是**整秒**，同一秒内签发的同类令牌会完全一致，
        # 于是"刷新令牌"看起来没换、审计日志里两次请求也是同一个串，无法区分。
        # 将来要做黑名单/吊销时，jti 也是唯一的挂载点。
        'jti': secrets.token_hex(8),
    }
    return jwt.encode(payload, config.effective_secret_key, algorithm=config.algorithm)


def create_token_pair(user_id: int, username: str, role: str,
                      config: Optional[AppConfig] = None) -> Dict[str, Any]:
    """
    签一对令牌（access + refresh）—— 登录与刷新都用它，refresh 滑动续期
    :return: {'access_token':.., 'refresh_token':.., 'token_type':'bearer', 'expires_in':秒}
    """
    config = config or get_app_config()
    return {
        'access_token': create_token(user_id, username, role, TOKEN_TYPE_ACCESS, config),
        'refresh_token': create_token(user_id, username, role, TOKEN_TYPE_REFRESH, config),
        'token_type': 'bearer',
        'expires_in': config.access_token_seconds,
        'role': role,
    }


def decode_token(token: str, expected_type: str = TOKEN_TYPE_ACCESS,
                 config: Optional[AppConfig] = None) -> Dict[str, Any]:
    """
    解析并校验令牌
    :param token: JWT 字符串
    :param expected_type: 期望的令牌类型（access / refresh）；传空字符串表示不校验类型
    :param config: 配置对象，默认取全局单例
    :return: 载荷字典
    :raises TokenError: 令牌非法或类型不符
    """
    config = config or get_app_config()
    if not token:
        raise TokenError('未提供访问令牌', reason='missing')

    try:
        payload = jwt.decode(
            token, config.effective_secret_key, algorithms=[config.algorithm]
        )
    except jwt.ExpiredSignatureError:
        raise TokenError('访问令牌已过期', reason='expired')
    except jwt.InvalidTokenError as e:
        raise TokenError(f'访问令牌无效：{e}', reason='invalid')

    if expected_type and payload.get('type') != expected_type:
        raise TokenError(
            f'令牌类型不符（期望 {expected_type}，实际 {payload.get("type")}）',
            reason='wrong_type',
        )
    return payload


def token_payload_to_user(payload: Dict[str, Any]) -> Dict[str, Any]:
    """
    把令牌载荷转成"当前用户"结构
    :param payload: decode_token 的结果
    :return: {'user_id': int, 'username': str, 'role': str}
    :raises TokenError: sub 不是数字
    """
    try:
        user_id = int(payload.get('sub') or 0)
    except (TypeError, ValueError):
        raise TokenError('访问令牌中的用户标识非法', reason='invalid')
    return {
        'user_id': user_id,
        'username': payload.get('username') or '',
        'role': payload.get('role') or '',
    }


def bearer_token_from_header(authorization: Optional[str]) -> str:
    """
    从 Authorization 头里取出 Bearer 令牌
    :param authorization: 请求头值
    :return: 令牌字符串；格式不对时返回空字符串
    """
    if not authorization:
        return ''
    parts = authorization.split(' ', 1)
    if len(parts) != 2 or parts[0].lower() != 'bearer':
        return ''
    return parts[1].strip()


if __name__ == '__main__':
    # 自测（不依赖数据库）
    passed = 0

    # ---------- 一、密码哈希 ----------
    hashed = hash_password('legal123')
    assert hashed != 'legal123', '绝不能明文存储'
    assert hashed.startswith('$2'), 'bcrypt 哈希应以 $2 开头'
    assert verify_password('legal123', hashed) is True
    assert verify_password('legal1234', hashed) is False, '错误密码必须判失败'
    assert verify_password('legal123', None) is False, '无哈希（未设密码）一律失败'
    assert verify_password('legal123', '不是哈希') is False, '脏哈希应安全判失败，不抛异常'
    # 同一密码两次哈希不同（加了随机盐）
    assert hash_password('legal123') != hashed, '应使用随机盐'
    passed += 1
    print('  ✅ bcrypt 哈希/校验正确，脏数据与空哈希安全判失败')

    # 长度边界：72 字节上限必须显式拒绝，而不是静默截断
    try:
        hash_password('a' * 73)
    except ValueError as e:
        assert '72' in str(e), '报错应说明上限'
    else:
        raise AssertionError('超长密码应被拒绝（静默截断会让两个长密码都能登录）')
    assert verify_password('a' * 72, hash_password('a' * 72)) is True, '恰好 72 字节应可用'
    try:
        hash_password('')
    except ValueError:
        pass
    else:
        raise AssertionError('空密码应被拒绝')
    passed += 1
    print('  ✅ 72 字节边界显式拒绝，空密码拒绝')

    # ---------- 二、JWT 签发与校验 ----------
    config = get_app_config()
    pair = create_token_pair(7, 'zhangsan', 'legal')
    for key in ('access_token', 'refresh_token', 'token_type', 'expires_in', 'role'):
        assert key in pair, f'令牌对缺少字段 {key}'
    assert pair['token_type'] == 'bearer'
    assert pair['expires_in'] == config.access_token_seconds
    passed += 1
    print('  ✅ 令牌对字段齐全（access / refresh / expires_in）')

    payload = decode_token(pair['access_token'], TOKEN_TYPE_ACCESS)
    assert payload['sub'] == '7' and payload['username'] == 'zhangsan' and payload['role'] == 'legal'
    assert payload['exp'] > payload['iat']
    # jti 必须存在且唯一（否则同一秒内签发的令牌完全相同，刷新等于没刷新）
    assert payload.get('jti'), '令牌必须带 jti'
    same_second = [create_token(7, 'zhangsan', 'legal') for _ in range(3)]
    assert len(set(same_second)) == 3, '同一秒内签发的令牌也必须互不相同（靠 jti 保证）'
    assert len({decode_token(t, TOKEN_TYPE_ACCESS)['jti'] for t in same_second}) == 3
    user = token_payload_to_user(payload)
    assert user == {'user_id': 7, 'username': 'zhangsan', 'role': 'legal'}
    passed += 1
    print('  ✅ 载荷解析正确，sub/username/role 可还原')

    # ---------- 三、类型不符必须拒绝（关键）----------
    try:
        decode_token(pair['refresh_token'], TOKEN_TYPE_ACCESS)
    except TokenError as e:
        assert e.reason == 'wrong_type', f'应识别为类型不符，实际 {e.reason}'
        assert 'access' in str(e)
    else:
        raise AssertionError('refresh 令牌不能当 access 用（有效期长得多，等于变相延长会话）')
    # 反过来 refresh 端点应只收 refresh
    assert decode_token(pair['refresh_token'], TOKEN_TYPE_REFRESH)['type'] == 'refresh'
    passed += 1
    print('  ✅ refresh 不能当 access 用，反之亦然')

    # ---------- 四、过期 / 篡改 / 缺失 ----------
    from config_bag.app_config import AppConfig as _AppConfig

    # 注意：不能用 access_token_minutes=-1 来造过期令牌 —— 有效期会被夹到最小 1 分钟，
    # 那其实是"60 秒后才过期"。这里直接手工签一个 exp 已过去的令牌。
    expired_secret = 'x' * 40
    past = _now() - timedelta(hours=1)
    expired = jwt.encode(
        {
            'sub': '1', 'username': 'u', 'role': 'legal', 'type': TOKEN_TYPE_ACCESS,
            'iat': int((past - timedelta(hours=1)).timestamp()),
            'exp': int(past.timestamp()),
        },
        expired_secret, algorithm='HS256',
    )
    try:
        decode_token(expired, TOKEN_TYPE_ACCESS, _AppConfig(secret_key=expired_secret))
    except TokenError as e:
        assert e.reason == 'expired', f'应识别为过期，实际 {e.reason}'
    else:
        raise AssertionError('过期令牌应被拒绝')

    # 有效期下限：负值会被夹到 1 分钟（避免签出"立即失效"的令牌）
    clamped = _AppConfig(access_token_minutes=-5)
    assert clamped.access_token_seconds == 60,         f'负值应夹到最小 1 分钟，实际 {clamped.access_token_seconds} 秒'

    try:
        decode_token(pair['access_token'], TOKEN_TYPE_ACCESS,
                     _AppConfig(secret_key='y' * 40))
    except TokenError as e:
        assert e.reason == 'invalid', f'换密钥后应识别为非法，实际 {e.reason}'
    else:
        raise AssertionError('换密钥后原令牌必须失效')

    try:
        decode_token('', TOKEN_TYPE_ACCESS)
    except TokenError as e:
        assert e.reason == 'missing'
    else:
        raise AssertionError('空令牌应被拒绝')

    try:
        decode_token('abc.def.ghi', TOKEN_TYPE_ACCESS)
    except TokenError as e:
        assert e.reason == 'invalid'
    else:
        raise AssertionError('格式非法应被拒绝')
    passed += 1
    print('  ✅ 过期 / 换密钥 / 缺失 / 格式非法 均正确拒绝')

    # ---------- 五、Authorization 头解析 ----------
    assert bearer_token_from_header('Bearer abc') == 'abc'
    assert bearer_token_from_header('bearer abc') == 'abc', '大小写不敏感'
    assert bearer_token_from_header('bearer   abc  ') == 'abc', '应去空白'
    for bad in (None, '', 'abc', 'Basic abc', 'Bearer'):
        assert bearer_token_from_header(bad) == '', f'{bad!r} 应解析为空'
    passed += 1
    print('  ✅ Bearer 头解析（大小写、空白、非法格式）')

    # ---------- 六、密钥长度 ----------
    demo = _AppConfig(secret_key='')
    assert len(demo.effective_secret_key) >= 32, '演示密钥必须 >= 32 字节，否则 PyJWT 告警'
    assert demo.is_secret_configured is False
    assert _AppConfig(secret_key='k' * 40).is_secret_configured is True
    passed += 1
    print('  ✅ 演示密钥长度达标（规避 PyJWT InsecureKeyLengthWarning）')

    print(f'web.security 自测全部通过 ✅ 共 {passed} 个用例')
