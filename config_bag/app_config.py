'''config_bag/app_config.py'''

"""
应用级配置（服务监听、鉴权密钥、令牌有效期）

对应 SPEC 第 12 章（安全设计：简单登录 + JWT，access 短期 + refresh 滑动）
与第 3 章的镜像/部署约定。

关于 APP_SECRET_KEY:
    `.env` 里留空时**不会报错**，而是使用一个固定的演示密钥（>= 32 字节，
    因为 PyJWT 对 HMAC 密钥长度有告警阈值）。同时打印醒目警告。
    这样演示环境开箱即用、生产环境也绝不会"悄悄用了个弱密钥" ——
    只要看日志就能发现。
"""

from dataclasses import dataclass, field
from typing import List, Optional
import logging
import os

from dotenv import load_dotenv

load_dotenv(override=True)

logger = logging.getLogger(__name__)

# 演示用密钥：长度 >= 32 字节（PyJWT 对 HMAC-SHA256 的建议下限）
DEMO_SECRET_KEY = 'contract-approval-demo-secret-key-please-change-in-production'

# 环境变量名（供接入层/运维引用）
SECRET_KEY_ENV = 'APP_SECRET_KEY'


def _env_str(name: str, default: str = '') -> str:
    """读字符串环境变量；缺失时返回默认值"""
    raw = os.getenv(name)
    return default if raw is None else raw.strip()


def _env_int(name: str, default: int) -> int:
    """读整型环境变量；缺失或非法时返回默认值"""
    raw = os.getenv(name, '').strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    """读布尔型环境变量；只有显式写成 false/0/no 才算关闭"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ('false', '0', 'no')


@dataclass
class AppConfig:
    """应用级配置"""

    # ==================== 服务监听 ====================
    host: str = field(default_factory=lambda: _env_str('APP_HOST', '127.0.0.1'))
    port: int = field(default_factory=lambda: _env_int('APP_PORT', 8000))
    debug: bool = field(default_factory=lambda: _env_bool('APP_DEBUG', True))

    # ==================== 鉴权（SPEC 12）====================
    secret_key: str = field(default_factory=lambda: _env_str(SECRET_KEY_ENV, ''))
    algorithm: str = 'HS256'
    # access 短期（分钟）
    access_token_minutes: int = field(
        default_factory=lambda: _env_int('ACCESS_TOKEN_MINUTES', 120)
    )
    # refresh 滑动有效期（天）
    refresh_token_days: int = field(
        default_factory=lambda: _env_int('REFRESH_TOKEN_DAYS', 7)
    )
    # 是否强制鉴权。演示阶段可设为 false 直接放行（SPEC 12「演示阶段可 token 直发」）
    auth_enabled: bool = field(
        default_factory=lambda: _env_bool('AUTH_ENABLED', True)
    )
    # 鉴权关闭时使用的默认身份（用于演示）
    demo_username: str = field(
        default_factory=lambda: _env_str('DEMO_USERNAME', 'admin')
    )

    # ==================== 上传 ====================
    # 允许的扩展名（与 ParseConfig 的白名单保持一致，这里用于接口层提前拦截）
    allowed_extensions: List[str] = field(default_factory=lambda: [
        '.docx', '.pdf', '.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.webp',
    ])
    # 单文件大小上限（MB）—— 与 ParseConfig.max_file_size_mb 保持一致
    max_file_size_mb: int = field(
        default_factory=lambda: _env_int('MAX_FILE_SIZE_MB', 50)
    )

    # ==================== CORS ====================
    # 允许的前端来源，逗号分隔；默认 * （演示用）。
    # 生产环境务必收敛成具体域名，否则任意站点都能带着用户的令牌调我们的接口。
    cors_origins: List[str] = field(default_factory=lambda: [
        origin.strip() for origin in _env_str('APP_CORS_ORIGINS', '*').split(',')
        if origin.strip()
    ] or ['*'])

    # ==================== 分页 ====================
    default_page_size: int = 20
    max_page_size: int = 200

    @classmethod
    def from_env(cls) -> "AppConfig":
        """从环境变量加载配置"""
        return cls()

    # ==================== 派生属性 ====================
    @property
    def effective_secret_key(self) -> str:
        """
        实际使用的签名密钥
        未配置时回退到演示密钥（并告警），避免演示环境因为缺一个变量而起不来
        """
        if self.secret_key:
            return self.secret_key
        return DEMO_SECRET_KEY

    @property
    def is_secret_configured(self) -> bool:
        """是否使用了用户配置的密钥（false = 正在用演示密钥）"""
        return bool(self.secret_key)

    def warn_if_demo_secret(self) -> None:
        """若在用演示密钥则打印醒目警告（启动时调用一次）"""
        if self.is_secret_configured:
            return
        logger.warning(
            f'[配置] {SECRET_KEY_ENV} 未配置，正在使用**演示密钥**签名 JWT —— '
            f'生产环境必须配置一个 >= 32 字符的随机密钥，否则任何人都能伪造令牌'
        )
        if len(self.effective_secret_key) < 32:
            logger.warning(
                f'[配置] 当前密钥长度仅 {len(self.effective_secret_key)} 字节，'
                f'PyJWT 会提示 InsecureKeyLengthWarning（建议 >= 32）'
            )

    @property
    def access_token_seconds(self) -> int:
        """access token 有效期（秒）"""
        return max(1, self.access_token_minutes) * 60

    @property
    def refresh_token_seconds(self) -> int:
        """refresh token 有效期（秒）"""
        return max(1, self.refresh_token_days) * 86400

    @property
    def max_file_size_bytes(self) -> int:
        """单文件大小上限（字节）"""
        return max(1, self.max_file_size_mb) * 1024 * 1024


# ==================== 全局单例 ====================
_config: Optional[AppConfig] = None


def get_app_config() -> AppConfig:
    """获取配置单例"""
    global _config
    if _config is None:
        _config = AppConfig.from_env()
    return _config


def reset_app_config() -> None:
    """清空配置单例（测试用）"""
    global _config
    _config = None


if __name__ == '__main__':
    # 轻量自测
    config = get_app_config()

    assert config.host and config.port > 0, '监听地址与端口应有合理默认值'
    assert config.max_file_size_mb == 50, '默认单文件上限应为 50MB（与解析配置一致）'

    # 令牌有效期
    assert config.access_token_seconds == config.access_token_minutes * 60
    assert config.refresh_token_seconds == config.refresh_token_days * 86400
    assert config.refresh_token_seconds > config.access_token_seconds, \
        'refresh 有效期必须长于 access（SPEC 12：access 短期 + refresh 滑动）'

    # 密钥：未配置时回退到演示密钥，且长度达标（否则 PyJWT 会告警）
    assert len(config.effective_secret_key) >= 32, \
        f'密钥长度应 >= 32 字节，实际 {len(config.effective_secret_key)}'
    assert config.effective_secret_key == config.secret_key or not config.secret_key, \
        '配置了密钥就必须用它，未配置才回退'

    # 大小上限换算
    assert config.max_file_size_bytes == 50 * 1024 * 1024

    # CORS 默认放开（演示），但一定不能是空列表（空列表等于拒绝所有跨域请求）
    assert config.cors_origins, 'CORS 来源不能为空'
    assert '*' in config.cors_origins, '默认应放开（演示用）'

    # 白名单应含解析层支持的格式
    for ext in ('.docx', '.pdf', '.png'):
        assert ext in config.allowed_extensions, f'白名单缺少 {ext}'

    assert get_app_config() is config, 'get_app_config 应返回同一个单例对象'

    print('app_config 自测通过 ✅'
          f'（密钥来源：{"环境变量" if config.is_secret_configured else "演示默认"}）')
