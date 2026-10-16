'''processor/review_processor/config.py'''

"""
审查流程配置

集中管理【审查子图】相关配置项，支持环境变量覆盖。

对应 SPEC 第 4.3.2（综合风险与结论映射）、第 8.4（LLM 超时与重试）、
第 9 章（规则引擎）、第 13 章（LLM 按条款并发，限并发 4）。
"""

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple
import os

from dotenv import load_dotenv

from db.constants import RiskLevel

# 加载 .env 文件
load_dotenv(override=True)


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
class ReviewConfig:
    """审查流程配置"""

    # ==================== 规则引擎 ====================
    # 规则热加载：DB 缓存到内存的刷新间隔（秒），0 表示不自动刷新
    rule_cache_ttl_seconds: int = 60
    # 单任务全部规则评估的耗时上限（SPEC 9.3 目标 < 50ms），超时只告警不中断
    rule_eval_warn_ms: int = 50

    # ==================== LLM 审查 ====================
    # 按条款并发调用 LLM 的并发上限（SPEC 13：asyncio.gather 限并发 4）
    max_llm_concurrency: int = field(
        default_factory=lambda: _env_int('REVIEW_LLM_CONCURRENCY', 4)
    )
    # 同一任务 LLM 连续失败达到该次数 → 触发 REVIEW_FAIL（SPEC 8.4）
    llm_fail_threshold: int = field(
        default_factory=lambda: _env_int('REVIEW_LLM_FAIL_THRESHOLD', 3)
    )
    # 是否启用 LLM 语义审查；关闭时只跑规则层（用于压测/回归）
    enable_llm_review: bool = field(
        default_factory=lambda: _env_bool('REVIEW_ENABLE_LLM', True)
    )
    # 是否只审查「未被规则命中」的条款（SPEC 4.3.1 第 2 层的原话）
    #   开启：省 token，但同一条款的其他维度风险可能漏检
    #   关闭：全覆盖，成本更高
    llm_review_only_unmatched: bool = field(
        default_factory=lambda: _env_bool('REVIEW_LLM_ONLY_UNMATCHED', True)
    )
    # 单次送 LLM 的条款正文上限（SPEC 8.3：以 clause 为最小审查单元，通常 < 4K token）
    # 超出则按段落二次切分后分段审查，再合并结果
    llm_max_clause_chars: int = field(
        default_factory=lambda: _env_int('REVIEW_LLM_MAX_CLAUSE_CHARS', 3000)
    )
    # LLM 整体失败时是否把任务置为 blocked（SPEC 8.4：触发 REVIEW_FAIL 进 blocked）
    #   开启：任务进 blocked，管理员可重试；规则层已产出的风险项**仍会落库**（SPEC 明确要求"不阻塞规则层结论"）
    #   关闭：任务照常 completed，仅在 review_note 里提示覆盖不完整
    block_on_llm_failure: bool = field(
        default_factory=lambda: _env_bool('REVIEW_BLOCK_ON_LLM_FAILURE', True)
    )

    # ==================== 综合风险与结论映射（SPEC 4.3.2）====================
    # 风险等级 → 审查结论（SPEC 4.3.2 的映射表）
    # 管理员可调整此映射；SPEC 4.3.2 原文：高风险→建议拒绝/重大整改，中风险→整改后通过，低风险→可通过
    conclusion_map: Dict[str, str] = field(default_factory=lambda: {
        RiskLevel.HIGH: '建议拒绝 / 重大整改后再审',
        RiskLevel.MID: '整改后通过',
        RiskLevel.LOW: '可通过',
        RiskLevel.NONE: '可通过',
    })

    @classmethod
    def from_env(cls) -> "ReviewConfig":
        """从环境变量加载配置"""
        return cls()

    # ==================== 派生方法 ====================
    def conclusion_of(self, risk_level: str) -> str:
        """
        按综合风险等级取审查结论（SPEC 4.3.2 的映射表）
        :param risk_level: 综合风险等级
        :return: 审查结论文案
        """
        return self.conclusion_map.get(risk_level, self.conclusion_map[RiskLevel.NONE])

    @property
    def severity_order(self) -> Tuple[str, ...]:
        """风险严重度从高到低（用于「取最高」的综合风险计算）"""
        return (RiskLevel.HIGH, RiskLevel.MID, RiskLevel.LOW, RiskLevel.NONE)


# ==================== 全局单例 ====================
_config: Optional[ReviewConfig] = None


def get_config() -> ReviewConfig:
    """获取配置单例"""
    global _config
    if _config is None:
        _config = ReviewConfig.from_env()
    return _config


if __name__ == '__main__':
    # 轻量自测
    config = get_config()

    assert config.max_llm_concurrency >= 1, '并发数应至少为 1'
    assert config.llm_fail_threshold >= 1, '失败阈值应至少为 1'
    assert isinstance(config.enable_llm_review, bool)

    # 结论映射：高风险文案要能体现"整改后再审"的语义
    high_conclusion = config.conclusion_of(RiskLevel.HIGH)
    assert '整改' in high_conclusion, f'高风险结论应含"整改"，实际 {high_conclusion}'
    assert config.conclusion_of(RiskLevel.LOW) == '可通过'
    assert config.conclusion_of('不存在的等级') == '可通过', '未知等级应兜底为可通过'

    # 严重度顺序
    assert config.severity_order[0] == RiskLevel.HIGH, '最高严重度应为 high'
    assert config.severity_order[-1] == RiskLevel.NONE

    assert get_config() is config, 'get_config 应返回同一个单例对象'

    print('review_processor.config 自测通过 ✅')
