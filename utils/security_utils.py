'''utils/security_utils.py'''

"""
密码哈希工具（bcrypt）

为什么单独放在 utils 而不是 web 层:
    两个地方都要用它 ——
      · `web/security.py`    登录时校验密码
      · `db/seed.py`         给演示账号生成初始密码哈希
    如果放在 `web/security.py`，`db/seed.py` 就得反过来 import web 层，
    形成"数据层依赖接入层"的逆向依赖。放到 `utils/` 两边都能用，层次也干净。

关于 bcrypt 的 72 字节上限:
    bcrypt **不会静默截断**，超过 72 字节直接报错。这里显式校验并给出清晰报错 ——
    如果改成静默截断，"两个不同的长密码都能登录"会是真实存在的安全漏洞。
"""

import logging
from typing import Optional

import bcrypt

logger = logging.getLogger(__name__)

# bcrypt 的输入上限（字节）
BCRYPT_MAX_BYTES = 72
# 密码最短长度（接口层与本模块保持一致）
PASSWORD_MIN_LENGTH = 6


def hash_password(plain_password: str) -> str:
    """
    生成密码哈希（bcrypt，随机盐）
    :param plain_password: 明文密码
    :return: 哈希字符串（可直接存进 app_user.password_hash）
    :raises ValueError: 密码为空或超出 72 字节上限
    """
    if not plain_password:
        raise ValueError('密码不能为空')
    raw = plain_password.encode('utf-8')
    if len(raw) > BCRYPT_MAX_BYTES:
        raise ValueError(
            f'密码过长（{len(raw)} 字节），bcrypt 上限为 {BCRYPT_MAX_BYTES} 字节'
        )
    return bcrypt.hashpw(raw, bcrypt.gensalt()).decode('utf-8')


def verify_password(plain_password: str, password_hash: Optional[str]) -> bool:
    """
    校验密码
    :param plain_password: 明文密码
    :param password_hash: 库里的哈希（为空表示该账号未设密码，一律判失败）
    :return: 是否匹配
    """
    if not plain_password or not password_hash:
        return False
    try:
        return bcrypt.checkpw(plain_password.encode('utf-8'), password_hash.encode('utf-8'))
    except (ValueError, TypeError) as e:
        # 哈希格式非法（脏数据）→ 判失败并告警，绝不让异常冒到接口层
        logger.warning(f'[鉴权] 密码哈希格式非法，校验判失败：{e}')
        return False


if __name__ == '__main__':
    # 轻量自测
    passed = 0

    # ---------- 一、基本哈希与校验 ----------
    hashed = hash_password('legal123')
    assert hashed != 'legal123', '绝不能明文存储'
    assert hashed.startswith('$2'), 'bcrypt 哈希应以 $2 开头'
    assert verify_password('legal123', hashed) is True
    assert verify_password('legal1234', hashed) is False, '错误密码必须判失败'
    passed += 1
    print('  ✅ bcrypt 哈希/校验正确')

    # ---------- 二、加盐（同一密码两次哈希不同）----------
    assert hash_password('same-password') != hash_password('same-password'), \
        '应使用随机盐，否则相同密码会产生相同哈希（可被彩虹表批量破解）'
    passed += 1
    print('  ✅ 随机盐生效（同密码两次哈希不同）')

    # ---------- 三、脏数据与空值安全判失败 ----------
    assert verify_password('x', None) is False, '无哈希（未设密码）一律失败'
    assert verify_password('x', '') is False
    assert verify_password('', hashed) is False, '空密码一律失败'
    assert verify_password('x', '不是哈希') is False, '脏哈希应安全判失败，不抛异常'
    passed += 1
    print('  ✅ 空值 / 脏哈希安全判失败，不抛异常')

    # ---------- 四、72 字节边界必须显式拒绝 ----------
    try:
        hash_password('a' * 73)
    except ValueError as e:
        assert '72' in str(e), '报错应说明上限'
    else:
        raise AssertionError('超长密码应被拒绝（静默截断会让两个长密码都能登录）')
    assert verify_password('a' * 72, hash_password('a' * 72)) is True, '恰好 72 字节应可用'
    # 多字节字符按字节数算，不是字符数（中文 3 字节/字）
    assert hash_password('密' * 24) and len('密'.encode('utf-8')) == 3, '中文密码按字节计数'
    try:
        hash_password('密' * 25)  # 75 字节
    except ValueError:
        pass
    else:
        raise AssertionError('中文密码同样受 72 字节限制（25 个中文 = 75 字节）')
    passed += 1
    print('  ✅ 72 字节上限显式拒绝（含多字节字符按字节计数）')

    # ---------- 五、空密码 ----------
    try:
        hash_password('')
    except ValueError:
        pass
    else:
        raise AssertionError('空密码应被拒绝')
    passed += 1
    print('  ✅ 空密码被拒绝')

    # ---------- 六、常量与边界一致 ----------
    assert BCRYPT_MAX_BYTES == 72
    assert PASSWORD_MIN_LENGTH >= 6, '最短长度不应低于 6'

    print(f'security_utils 自测全部通过 ✅ 共 {passed} 个用例')
