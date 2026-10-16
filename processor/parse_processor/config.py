'''processor/parse_processor/config.py'''

"""
解析流程配置管理模块

集中管理【解析子图】相关配置项，支持环境变量覆盖

注意:
    1. 本模块只负责解析相关配置；LLM / 审批系统对接等配置由各自 processor 管理。
    2. 需要 .env 的配置项（如 OCR_DEVICE / FILE_STORE_DIR）未配置时保持空值或默认值，
       绝不在配置层直接报错 —— 由各节点自行决定「降级」还是「mock」策略。

对应 SPEC 第 3 章（技术选型）、第 12 章（安全设计：类型白名单 + 大小上限）。
"""

from dataclasses import dataclass, field
from typing import Set, Optional
import os

from dotenv import load_dotenv

# 加载 .env 文件
load_dotenv(override=True)


@dataclass
class ParseConfig:
    """解析流程配置"""

    # ==================== 格式白名单 ====================
    # SPEC 12：上传类型白名单 docx / pdf / png / jpg
    docx_extensions: Set[str] = field(
        default_factory=lambda: {'.docx'}
    )
    pdf_extensions: Set[str] = field(
        default_factory=lambda: {'.pdf'}
    )
    image_extensions: Set[str] = field(
        default_factory=lambda: {'.png', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff', '.webp'}
    )

    # ==================== 文件大小限制 ====================
    max_file_size_mb: int = 50  # 单文件大小上限（SPEC 12 默认 50MB）

    # ==================== 存储目录 ====================
    file_store_dir: str = field(
        default_factory=lambda: os.getenv("FILE_STORE_DIR", "")
    )

    # ==================== PDF 解析配置（PyMuPDF）====================
    # 全文可提取文本量低于该字符数 → 判定为「无文本层」（扫描件），转 OCR
    pdf_min_text_chars: int = 20
    # 无文本层 PDF 转 OCR 时的页面渲染 DPI（越高越清晰，也越慢）
    pdf_ocr_dpi: int = 200

    # ==================== 文本预处理配置（node_clean_text）====================
    # 页眉页脚判定：短于该字符数、且在多个不同页重复出现的块 → 判为页眉/页脚删除
    clean_repeat_max_chars: int = 50
    # 是否删除「纯数字行」（绝大多数情况是页码；如合同正文确有纯数字行可关闭）
    clean_drop_pure_number_lines: bool = True

    # ==================== OCR 配置（PaddleOCR）====================
    ocr_device: str = field(
        default_factory=lambda: os.getenv("OCR_DEVICE", "cpu")
    )
    ocr_lang: str = field(
        default_factory=lambda: os.getenv("OCR_LANG", "ch")
    )
    ocr_timeout_seconds: int = 300  # OCR 进程超时（SPEC 11.1：> 300s 判定阻塞）
    ocr_min_confidence: float = 0.6  # 整篇平均置信度低于该值 → 判「严重模糊」→ blocked
    # 未安装 PaddleOCR 时是否允许降级为 mock 结果（保证演示链路可跑通）
    # 设为 false 则直接报错进入 blocked，绝不产出假数据
    ocr_allow_mock: bool = field(
        default_factory=lambda: os.getenv("OCR_ALLOW_MOCK", "true").lower() != "false"
    )

    # ==================== LLM 辅助配置（P1 条款分类 / P2 元数据归一化，SPEC 8.2）====================
    # 解析层的 LLM 是**辅助**角色：规则路径是主线，LLM 只做兜底与归一化。
    # 关闭后 P1/P2 不发起任何调用，行为与接入前完全一致（归 other / 保留规则原值）。
    enable_llm_assist: bool = field(
        default_factory=lambda: os.getenv("PARSE_ENABLE_LLM_ASSIST", "true").lower() != "false"
    )
    # P1 置信度阈值：低于该值一律归 other（SPEC 4.2.2「置信度低则归入 other」）
    llm_classify_min_confidence: float = field(
        default_factory=lambda: float(os.getenv("PARSE_LLM_MIN_CONFIDENCE", "0.6"))
    )
    # P1 调用超时（秒）：解析层辅助调用不该拖慢主链路，单独收紧
    llm_assist_timeout_seconds: int = field(
        default_factory=lambda: int(os.getenv("PARSE_LLM_TIMEOUT", "30"))
    )

    @classmethod
    def from_env(cls) -> "ParseConfig":
        """从环境变量加载配置"""
        return cls()

    # ==================== 派生属性 ====================
    @property
    def supported_extensions(self) -> Set[str]:
        """全部支持的扩展名合集（用于上传白名单校验）"""
        return self.docx_extensions | self.pdf_extensions | self.image_extensions

    def is_supported(self, suffix: str) -> bool:
        """判断某个扩展名是否在白名单内（入参请传小写、带点的形式，如 .docx）"""
        return suffix in self.supported_extensions


# ==================== 全局单例 ====================
_config: Optional[ParseConfig] = None


def get_config() -> ParseConfig:
    """获取配置单例"""
    global _config
    if _config is None:
        _config = ParseConfig.from_env()
    return _config


if __name__ == '__main__':
    # 轻量自测：白名单判定与单例
    cfg = get_config()
    assert cfg.is_supported('.docx'), '.docx 应在白名单内'
    assert cfg.is_supported('.pdf'), '.pdf 应在白名单内'
    assert cfg.is_supported('.png'), '.png 应在白名单内'
    assert not cfg.is_supported('.txt'), '.txt 不应在白名单内'
    assert get_config() is cfg, 'get_config 应返回同一个单例对象'
    assert cfg.max_file_size_mb == 50

    print('parse_processor.config 自测通过 ✅')
