'''processor/writeback_processor/config.py'''

"""
回写流程配置

集中管理【回写子图】相关配置项，支持环境变量覆盖。

对应 SPEC 第 4.5 节（审批回写：幂等键、退避重试 5s/30s、超时 30s）、
第 5.3 节（回写状态机）、第 10 章（评论模板与报告结构）、第 11.1 节（回写失败不回退主状态）。

降级约定（与 LLM / OCR 一致）:
    `APPROVE_API_BASE` / `APPROVE_TOKEN` 未配置时：
      · `WRITEBACK_ALLOW_MOCK_ADAPTER=true`（默认）→ 使用只记日志的 mock Adapter，
        回写状态按"成功"落地但标记 `writeback_is_mock`，链路可跑通、前端能提示；
      · `WRITEBACK_ALLOW_MOCK_ADAPTER=false` → 直接抛 ConfigurationError，绝不假装成功。
    与 LLM 的 mock 策略保持同一个哲学：**降级可以，谎报不行**。
"""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple
import os

from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv(override=True)


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


def _env_backoff(name: str, default: Tuple[int, ...]) -> Tuple[int, ...]:
    """
    读退避序列环境变量，形如 "5,30"
    :param name: 变量名
    :param default: 默认序列
    :return: 秒数元组；解析不出任何数字时返回默认值
    """
    raw = os.getenv(name, '').strip()
    if not raw:
        return default
    values: List[int] = []
    for piece in raw.split(','):
        piece = piece.strip()
        if not piece:
            continue
        try:
            seconds = int(float(piece))
        except ValueError:
            continue
        if seconds >= 0:
            values.append(seconds)
    return tuple(values) if values else default


@dataclass
class WritebackConfig:
    """回写流程配置"""

    # ==================== 审批系统对接（SPEC 4.5）====================
    approve_api_base: str = field(
        default_factory=lambda: _env_str('APPROVE_API_BASE', '')
    )
    approve_token: str = field(
        default_factory=lambda: _env_str('APPROVE_TOKEN', '')
    )
    # 接口超时（SPEC 4.5：默认 30s，可配）
    approve_timeout: int = field(
        default_factory=lambda: _env_int('APPROVE_TIMEOUT', 30)
    )
    # 单次失败的自动重试次数（SPEC 4.5：默认重试 2 次，共 3 次尝试）
    max_retry_attempts: int = field(
        default_factory=lambda: _env_int('WRITEBACK_MAX_RETRY', 2)
    )
    # 退避序列（SPEC 4.5：退避 5s / 30s）
    retry_backoff: Tuple[int, ...] = field(
        default_factory=lambda: _env_backoff('WRITEBACK_RETRY_BACKOFF', (5, 30))
    )
    # 未配置审批地址时是否允许降级为 mock Adapter（false 则直接报错）
    allow_mock_adapter: bool = field(
        default_factory=lambda: _env_bool('WRITEBACK_ALLOW_MOCK_ADAPTER', True)
    )
    # 是否强制要求 external_doc_id（上传来源的任务没有审批单号，本就无从回写）
    require_external_doc_id: bool = field(
        default_factory=lambda: _env_bool('WRITEBACK_REQUIRE_DOC_ID', True)
    )

    # ==================== 评论模板（SPEC 10.1）====================
    # 评论风险明细表最多列多少条（防止评论过长，超出部分只给统计）
    comment_max_risks: int = field(
        default_factory=lambda: _env_int('WRITEBACK_COMMENT_MAX_RISKS', 50)
    )
    # 「重点关注事项」条数（SPEC 10.1 模板的最后一段）
    comment_highlight_top_n: int = field(
        default_factory=lambda: _env_int('WRITEBACK_HIGHLIGHT_TOP_N', 3)
    )
    # 评论风险明细是否包含低风险
    comment_include_low_risk: bool = field(
        default_factory=lambda: _env_bool('WRITEBACK_INCLUDE_LOW', True)
    )

    # ==================== 报告导出（SPEC 10.2）====================
    report_dir: str = field(
        default_factory=lambda: _env_str('REPORT_STORE_DIR', './data/reports')
    )
    report_font_name: str = field(
        default_factory=lambda: _env_str('REPORT_FONT', 'STSong-Light')
    )
    report_title: str = field(
        default_factory=lambda: _env_str('REPORT_TITLE', '合同审查报告')
    )
    # 是否保留历史导出记录：true 每次导出新增一条（可追溯历次结论）；
    # false 则先删掉该任务的历史记录再写，避免 report 表与磁盘文件无限堆积
    report_keep_history: bool = field(
        default_factory=lambda: _env_bool('REPORT_KEEP_HISTORY', True)
    )

    @classmethod
    def from_env(cls) -> "WritebackConfig":
        """从环境变量加载配置"""
        return cls()

    # ==================== 派生属性 ====================
    @property
    def is_configured(self) -> bool:
        """审批系统地址与令牌是否都已配置"""
        return bool(self.approve_api_base and self.approve_token)

    @property
    def missing_names(self) -> List[str]:
        """列出缺失的配置项名（用于错误提示与日志）"""
        return [
            name for name, value in (
                ('APPROVE_API_BASE', self.approve_api_base),
                ('APPROVE_TOKEN', self.approve_token),
            ) if not value
        ]

    @property
    def masked_token(self) -> str:
        """脱敏令牌：只留首尾各 4 位，日志里只能打这个"""
        token = self.approve_token or ''
        if len(token) <= 8:
            return '*' * len(token)
        return f'{token[:4]}{"*" * 8}{token[-4:]}'

    @property
    def total_attempts(self) -> int:
        """总尝试次数 = 首次 + 重试次数（SPEC 4.5：1 + 2 = 3）"""
        return 1 + max(0, self.max_retry_attempts)

    def backoff_for(self, attempt_index: int) -> int:
        """
        取第 attempt_index 次失败后的退避秒数
        :param attempt_index: 第几次失败（从 0 开始）
        :return: 退避秒数；超出序列长度时沿用最后一个值
        """
        if not self.retry_backoff:
            return 0
        return self.retry_backoff[min(attempt_index, len(self.retry_backoff) - 1)]


# ==================== 全局单例 ====================
_config: Optional[WritebackConfig] = None


def get_config() -> WritebackConfig:
    """获取配置单例"""
    global _config
    if _config is None:
        _config = WritebackConfig.from_env()
    return _config


if __name__ == '__main__':
    # 轻量自测
    config = get_config()

    assert config.total_attempts == config.max_retry_attempts + 1, '总尝试次数应为重试数 +1'
    assert config.total_attempts >= 1

    # 退避序列：默认 5s / 30s（SPEC 4.5）
    assert config.retry_backoff == (5, 30), f'默认退避应为 (5, 30)，实际 {config.retry_backoff}'
    assert config.backoff_for(0) == 5
    assert config.backoff_for(1) == 30
    assert config.backoff_for(9) == 30, '超出序列时沿用最后一个值'

    # 令牌脱敏
    masked = WritebackConfig(approve_token='mock-token-123456').masked_token
    assert '123456' not in masked.replace(masked[-4:], ''), '脱敏后不应泄露中间段'
    assert WritebackConfig(approve_token='short').masked_token == '*****', '短令牌应整体打码'
    assert WritebackConfig(approve_token='').masked_token == ''

    # 配置齐全性判断
    assert WritebackConfig(approve_api_base='', approve_token='').is_configured is False
    assert WritebackConfig(approve_api_base='', approve_token='t').missing_names == ['APPROVE_API_BASE']
    assert WritebackConfig(approve_api_base='http://x', approve_token='t').is_configured is True

    # 是否允许 mock：当前 .env 未填时应为 True（链路可跑通）
    assert isinstance(config.allow_mock_adapter, bool)
    assert isinstance(config.require_external_doc_id, bool)

    assert get_config() is config, 'get_config 应返回同一个单例对象'

    print('writeback_processor.config 自测通过 ✅')
