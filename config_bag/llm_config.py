'''config_bag/llm_config.py'''

"""
LLM 供应商配置

统一管理 OpenAI 兼容接口的接入参数，全部从 .env 读取。

设计要点:
    未配置时**不在配置层报错** —— 把「降级 mock 还是直接报错」的决定权交给
    utils/llm_utils.py。这样 .env 还没填写时全链路仍能跑通（走 mock），
    与项目「真实接入 + mock 开关」的约定一致。

对应 SPEC 8.1 的配置项：base_url / api_key / model / timeout / max_retries / temperature。
"""

from dataclasses import dataclass, field
from typing import List
import os

from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv(override=True)


def _env_int(name: str, default: int) -> int:
    """读整型环境变量；缺失或非法时返回默认值（配置层不因脏数据崩掉）"""
    raw = os.getenv(name, '').strip()
    try:
        return int(raw)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    """读浮点型环境变量；缺失或非法时返回默认值"""
    raw = os.getenv(name, '').strip()
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    """读布尔型环境变量；只有显式写成 false/0/no 才算关闭"""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() not in ('false', '0', 'no')


@dataclass
class LLMConfig:
    """LLM 供应商配置"""

    # ==================== 必填三项 ====================
    base_url: str = field(
        default_factory=lambda: os.getenv("LLM_BASE_URL", "").strip()
    )
    api_key: str = field(
        default_factory=lambda: os.getenv("LLM_API_KEY", "").strip()
    )
    model: str = field(
        default_factory=lambda: os.getenv("LLM_MODEL", "").strip()
    )

    # ==================== 调用参数 ====================
    temperature: float = field(
        default_factory=lambda: _env_float("LLM_TEMPERATURE", 0.2)
    )
    timeout: int = field(
        default_factory=lambda: _env_int("LLM_TIMEOUT", 60)
    )
    max_retries: int = field(
        default_factory=lambda: _env_int("LLM_MAX_RETRIES", 2)
    )

    # ==================== 降级开关 ====================
    allow_mock: bool = field(
        default_factory=lambda: _env_bool("LLM_ALLOW_MOCK", True)
    )

    @classmethod
    def from_env(cls) -> "LLMConfig":
        """从环境变量加载配置"""
        return cls()

    # ==================== 派生属性 ====================
    @property
    def is_configured(self) -> bool:
        """必填三项是否齐全（齐全才允许真实调用）"""
        return all((self.base_url, self.api_key, self.model))

    @property
    def missing_names(self) -> List[str]:
        """
        列出缺失的必填项「环境变量名」，便于给出可直接照做的报错提示。
        """
        checks = (
            ("LLM_BASE_URL", self.base_url),
            ("LLM_API_KEY", self.api_key),
            ("LLM_MODEL", self.model),
        )
        return [name for name, value in checks if not value]

    @property
    def masked_api_key(self) -> str:
        """脱敏后的密钥，供日志输出使用（绝不打印完整密钥）"""
        if not self.api_key:
            return ''
        if len(self.api_key) <= 8:
            return '*' * len(self.api_key)
        return f'{self.api_key[:4]}{"*" * 8}{self.api_key[-4:]}'


# ==================== 全局单例 ====================
_llm_config: LLMConfig = LLMConfig.from_env()


def get_llm_config() -> LLMConfig:
    """获取 LLM 配置单例"""
    return _llm_config


if __name__ == '__main__':
    # 轻量自测：验证默认值、缺失探测与密钥脱敏
    config = get_llm_config()

    assert isinstance(config.temperature, float), '温度应解析为浮点'
    assert config.timeout > 0, '超时时间应为正数'
    assert config.max_retries >= 0, '重试次数不应为负数'
    assert isinstance(config.allow_mock, bool), 'allow_mock 应解析为布尔'

    # .env 未填写时：必填三项缺失，且能准确列出缺失项名
    if not config.is_configured:
        assert config.missing_names == ['LLM_BASE_URL', 'LLM_API_KEY', 'LLM_MODEL'], \
            f'应列出全部缺失项，实际 {config.missing_names}'
        assert config.masked_api_key == '', '空密钥的脱敏结果应为空字符串'
        print('llm_config 自测通过 ✅（当前 .env 未填写 LLM 配置，走 mock）')
    else:
        assert not config.missing_names, '已配置时不应有缺失项'
        print('llm_config 自测通过 ✅（已检测到 LLM 配置）')

    # 脱敏逻辑（用构造的配置验证，不依赖 .env 实际内容）
    masked = LLMConfig(base_url='x', api_key='sk-1234567890abcdef', model='m').masked_api_key
    assert masked == 'sk-1********cdef', f'脱敏结果异常：{masked}'
    assert '1234567890' not in masked, '脱敏后不应暴露中间段密钥'
    print('密钥脱敏自测通过 ✅')
