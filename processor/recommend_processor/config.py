'''processor/recommend_processor/config.py'''

"""
推荐流程配置

集中管理【推荐子图】相关配置项，支持环境变量覆盖。

对应 SPEC 第 4.4 节（条款推荐引擎）、第 8.4 节（LLM 超时与重试）、
第 13 章（LLM 按条款并发）。
"""

from dataclasses import dataclass, field
from typing import Optional, Tuple
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
class RecommendConfig:
    """推荐流程配置"""

    # ==================== 模板召回 ====================
    # 按 (合同类型 + 条款类型) 召回的候选数量上限（SPEC 4.4：召回候选 → 交给 LLM 改写）
    template_recall_top_k: int = field(
        default_factory=lambda: _env_int('RECOMMEND_TEMPLATE_TOP_K', 3)
    )
    # 合同类型精确匹配的加分（用于候选排序；通配 '*' 模板不含此加分）
    template_contract_type_bonus: int = field(
        default_factory=lambda: _env_int('RECOMMEND_TEMPLATE_TYPE_BONUS', 100)
    )
    # 规则直接绑定的模板得分：远高于类型召回，保证"规则指定的模板"永远排第一
    template_rule_bound_score: int = 1000

    # ==================== 生成范围 ====================
    # 只为「不低于该等级」的风险点生成建议（默认 low = 全部风险点都生成）。
    # 设为 mid 可过滤掉低风险噪音，减少无意义的 LLM 调用。
    min_risk_level: str = field(
        default_factory=lambda: os.getenv('RECOMMEND_MIN_RISK_LEVEL', RiskLevel.LOW).strip()
        or RiskLevel.LOW
    )

    # ==================== LLM 改写 ====================
    # 是否启用 LLM 个性化改写；关闭时直接用召回到的示范条款（纯模板模式，零成本）
    enable_llm_rewrite: bool = field(
        default_factory=lambda: _env_bool('RECOMMEND_ENABLE_LLM', True)
    )
    # 并发上限（SPEC 13：LLM 按条款并发，限并发 4）
    max_llm_concurrency: int = field(
        default_factory=lambda: _env_int('RECOMMEND_LLM_CONCURRENCY', 4)
    )
    # 同一任务 LLM 连续失败达到该次数 → 停止调用，整体退化为模板模式
    llm_fail_threshold: int = field(
        default_factory=lambda: _env_int('RECOMMEND_LLM_FAIL_THRESHOLD', 3)
    )
    # 单次送 LLM 的条款正文上限（超出按段落二次切分，SPEC 8.3）
    llm_max_clause_chars: int = field(
        default_factory=lambda: _env_int('RECOMMEND_LLM_MAX_CLAUSE_CHARS', 3000)
    )
    # 建议条款长度上限（超过截断，避免落库超长文本）
    max_suggestion_chars: int = field(
        default_factory=lambda: _env_int('RECOMMEND_MAX_SUGGESTION_CHARS', 2000)
    )

    # ==================== 差异对比 ====================
    # LCS 单元格上限，超过则退化为整体替换（见 utils/diff_utils.py）
    diff_max_cells: int = field(
        default_factory=lambda: _env_int('RECOMMEND_DIFF_MAX_CELLS', 40000)
    )

    @classmethod
    def from_env(cls) -> "RecommendConfig":
        """从环境变量加载配置"""
        return cls()

    # ==================== 派生方法 ====================
    @property
    def severity_order(self) -> Tuple[str, ...]:
        """风险严重度从高到低（与 review_processor 保持一致）"""
        return (RiskLevel.HIGH, RiskLevel.MID, RiskLevel.LOW, RiskLevel.NONE)

    def is_risk_in_scope(self, risk_level: str) -> bool:
        """
        判断该风险等级是否需要生成建议
        :param risk_level: 风险等级
        :return: True 表示在生成范围内
        """
        threshold = RiskLevel.SEVERITY.get(
            self.min_risk_level, RiskLevel.SEVERITY[RiskLevel.LOW]
        )
        actual = RiskLevel.SEVERITY.get(risk_level or '', RiskLevel.SEVERITY[RiskLevel.NONE])
        return actual >= threshold


# ==================== 全局单例 ====================
_config: Optional[RecommendConfig] = None


def get_config() -> RecommendConfig:
    """获取配置单例"""
    global _config
    if _config is None:
        _config = RecommendConfig.from_env()
    return _config


if __name__ == '__main__':
    # 轻量自测
    config = get_config()

    assert config.template_recall_top_k >= 1, '召回数量应至少为 1'
    assert config.max_llm_concurrency >= 1, '并发数应至少为 1'
    assert config.llm_fail_threshold >= 1, '失败阈值应至少为 1'
    assert config.template_rule_bound_score > config.template_contract_type_bonus, \
        '规则绑定模板的得分必须高于类型召回，否则规则指定的模板会被挤掉'

    # 生成范围过滤
    assert config.is_risk_in_scope(RiskLevel.HIGH) is True
    assert config.is_risk_in_scope(RiskLevel.LOW) is True, '默认应为 low，即全部风险点都生成'
    assert config.is_risk_in_scope('') is False, '未知等级不应在生成范围内'

    strict = RecommendConfig(min_risk_level=RiskLevel.MID)
    assert strict.is_risk_in_scope(RiskLevel.HIGH) is True
    assert strict.is_risk_in_scope(RiskLevel.MID) is True
    assert strict.is_risk_in_scope(RiskLevel.LOW) is False, 'min_risk_level=mid 应过滤低风险'

    assert get_config() is config, 'get_config 应返回同一个单例对象'

    print('recommend_processor.config 自测通过 ✅')
