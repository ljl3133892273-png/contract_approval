'''web/schemas.py'''

"""
请求 / 响应模型（SPEC 第 7 章的接口契约）

约定:
    1. 请求体用 pydantic 模型，字段缺失/越界由 FastAPI 直接返回 422，
       再由异常处理器统一包成 `code=4001` 的业务响应（SPEC 7.4「校验失败统一 400」）。
    2. 响应体不强制套模型 —— 数据来自各个 processor，结构差异大，
       用 `to_dict()` 平铺更省事，也避免 schema 与领域模型重复定义两份。
    3. 枚举字段一律给出 `Literal` 或显式校验，**不让非法值流进数据库**
       （例如 adopt_status 只能是 pending/adopted/adjusted/rejected）。
"""

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator

from config_bag.app_config import get_app_config
from db.constants import AdoptStatus, ReportFormat

# 分页上限（防止 page_size=100000 把库拖垮）
_APP_CONFIG = get_app_config()


# ============================================================
# 鉴权
# ============================================================
class LoginRequest(BaseModel):
    """登录请求"""

    username: str = Field(min_length=1, max_length=64, description='用户名')
    password: str = Field(min_length=1, max_length=64, description='密码')


class RefreshRequest(BaseModel):
    """刷新令牌请求（access 到期后用 refresh 换新的一对）"""

    refresh_token: str = Field(min_length=1, description='refresh 令牌')


# ============================================================
# 任务
# ============================================================
class TaskCreateFromApprovalRequest(BaseModel):
    """从审批系统创建任务（推送/拉取）"""

    doc_id: str = Field(min_length=1, max_length=64, description='审批单号')
    # SPEC 4.1.1 的三类来源里，上传单独走 /upload
    source_type: str = Field(default='approve_push', description='approve_push / approve_pull')
    # 是否只创建任务、不立刻跑审查（拉取待办时可先攒着）
    run_immediately: bool = Field(default=True, description='是否立即启动审查流水线')

    @field_validator('source_type')
    @classmethod
    def _check_source_type(cls, value: str) -> str:
        allowed = ('approve_push', 'approve_pull')
        if value not in allowed:
            raise ValueError(f'source_type 只能是 {allowed}，收到 {value!r}')
        return value


class TaskRetryRequest(BaseModel):
    """重试请求（SPEC 11.2）"""

    # 重试前置：文件重新上传或参数调整。这里允许覆盖业务类型，便于人工修正
    biz_type: Optional[str] = Field(default=None, max_length=32, description='覆盖合同类型')


class RiskItemUpdateRequest(BaseModel):
    """
    法务调整风险项（SPEC 7.2）

    三个字段都可选：只传一个就只改一个。
    """

    adopt_status: Optional[str] = Field(default=None, description='pending/adopted/adjusted/rejected')
    manual_comment: Optional[str] = Field(default=None, max_length=2000, description='法务批注')
    suggestion_text: Optional[str] = Field(default=None, max_length=5000, description='修改后的建议条款')

    @field_validator('adopt_status')
    @classmethod
    def _check_adopt_status(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        if value not in AdoptStatus.ALL:
            raise ValueError(f'adopt_status 只能是 {AdoptStatus.ALL}，收到 {value!r}')
        return value


class WriteBackRequest(BaseModel):
    """回写请求"""

    operator: str = Field(default='system', max_length=64, description='操作人')
    # 是否强制重新回写（默认 false：已 success 的任务直接复用）
    force: bool = Field(default=False, description='强制重发（忽略已成功状态）')


class ReportExportRequest(BaseModel):
    """报告导出请求"""

    format: str = Field(default=ReportFormat.PDF, description='pdf / md / both')

    @field_validator('format')
    @classmethod
    def _check_format(cls, value: str) -> str:
        allowed = (ReportFormat.PDF, ReportFormat.MD, 'both')
        if value not in allowed:
            raise ValueError(f'format 只能是 {allowed}，收到 {value!r}')
        return value


# ============================================================
# 规则与模板（SPEC 7.3）
# ============================================================
class RuleCreateRequest(BaseModel):
    """新增/编辑规则"""

    code: str = Field(min_length=1, max_length=32, description='规则编号，如 R-09')
    name: str = Field(min_length=1, max_length=64, description='规则名')
    contract_type: str = Field(default='*', max_length=32, description='适用合同类型，* 为通用')
    dimension: Optional[str] = Field(default=None, max_length=32)
    trigger_condition: Dict[str, Any] = Field(description='触发条件（信封 + 条件树）')
    risk_level: str = Field(default='mid', description='high / mid / low')
    legal_basis: Optional[str] = Field(default=None, max_length=512)
    template_clause_id: Optional[int] = None
    enabled: bool = True
    priority: int = Field(default=100, ge=0, le=9999)


class RuleUpdateRequest(BaseModel):
    """编辑规则（全部可选）"""

    name: Optional[str] = Field(default=None, max_length=64)
    contract_type: Optional[str] = Field(default=None, max_length=32)
    dimension: Optional[str] = Field(default=None, max_length=32)
    trigger_condition: Optional[Dict[str, Any]] = None
    risk_level: Optional[str] = Field(default=None, description='high / mid / low')
    legal_basis: Optional[str] = Field(default=None, max_length=512)
    template_clause_id: Optional[int] = None
    enabled: Optional[bool] = None
    priority: Optional[int] = Field(default=None, ge=0, le=9999)


class TemplateCreateRequest(BaseModel):
    """新增/编辑示范条款"""

    clause_type: str = Field(min_length=1, max_length=32)
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(min_length=1)
    contract_type: str = Field(default='*', max_length=32)
    enabled: bool = True


class TemplateUpdateRequest(BaseModel):
    """编辑示范条款（全部可选）"""

    clause_type: Optional[str] = Field(default=None, max_length=32)
    title: Optional[str] = Field(default=None, max_length=255)
    content: Optional[str] = None
    contract_type: Optional[str] = Field(default=None, max_length=32)
    enabled: Optional[bool] = None


# ============================================================
# 查询参数
# ============================================================
class TaskListQuery(BaseModel):
    """任务列表查询参数（对应 SPEC 7.1：分页 + 筛选 status/risk/keyword/biz_type）"""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(
        default=_APP_CONFIG.default_page_size, ge=1, le=_APP_CONFIG.max_page_size
    )
    status: Optional[str] = Field(default=None, description='review_status 精确筛选')
    risk: Optional[str] = Field(default=None, description='risk_level 精确筛选')
    biz_type: Optional[str] = Field(default=None, description='合同类型精确筛选')
    keyword: Optional[str] = Field(default=None, max_length=128, description='合同名/申请人模糊搜索')
    writeback_status: Optional[str] = Field(default=None, description='回写状态筛选')

    @field_validator('status')
    @classmethod
    def _check_status(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        from db.constants import ReviewStatus
        if value not in ReviewStatus.ALL:
            raise ValueError(f'status 只能是 {ReviewStatus.ALL}，收到 {value!r}')
        return value


class ListQuery(BaseModel):
    """通用列表查询（规则 / 模板）"""

    page: int = Field(default=1, ge=1)
    page_size: int = Field(
        default=_APP_CONFIG.default_page_size, ge=1, le=_APP_CONFIG.max_page_size
    )
    contract_type: Optional[str] = Field(default=None, max_length=32)
    keyword: Optional[str] = Field(default=None, max_length=128)


if __name__ == '__main__':
    # 轻量自测：模型必须拦住非法枚举值（不让脏数据流进库）
    passed = 0

    # ---------- 一、登录与刷新 ----------
    request = LoginRequest(username='admin', password='admin123')
    assert request.username == 'admin'
    try:
        LoginRequest(username='', password='x')
    except Exception:
        pass
    else:
        raise AssertionError('空用户名应被拒绝')
    passed += 1
    print('  ✅ LoginRequest 校验生效')

    # ---------- 二、来源类型与报告格式 ----------
    assert TaskCreateFromApprovalRequest(doc_id='A-1').source_type == 'approve_push', '默认应为推送'
    for bad in ('upload', '乱填', ''):
        try:
            TaskCreateFromApprovalRequest(doc_id='A-1', source_type=bad)
        except Exception:
            pass
        else:
            raise AssertionError(f'source_type={bad!r} 应被拒绝（上传走 /upload，不在这里）')

    assert ReportExportRequest().format == 'pdf', '默认导出 PDF'
    for good in ('pdf', 'md', 'both'):
        assert ReportExportRequest(format=good).format == good
    try:
        ReportExportRequest(format='docx')
    except Exception:
        pass
    else:
        raise AssertionError('非法报告格式应被拒绝')
    passed += 1
    print('  ✅ 来源类型与报告格式白名单生效')

    # ---------- 三、风险项更新：枚举必须合法 ----------
    assert RiskItemUpdateRequest(adopt_status='adopted').adopt_status == 'adopted'
    assert RiskItemUpdateRequest().adopt_status is None, '三个字段都应为可选'
    for bad in ('done', 'PENDING', '采纳'):
        try:
            RiskItemUpdateRequest(adopt_status=bad)
        except Exception:
            pass
        else:
            raise AssertionError(f'adopt_status={bad!r} 应被拒绝')
    # 大小写敏感是刻意的：库里存的就是小写枚举值
    try:
        RiskItemUpdateRequest(adopt_status='ADOPTED')
    except Exception:
        pass
    else:
        raise AssertionError('大写枚举应被拒绝（库中存小写）')
    passed += 1
    print('  ✅ adopt_status 只接受 SPEC 定义的 4 个小写取值')

    # ---------- 四、分页上限 ----------
    config = get_app_config()
    query = TaskListQuery()
    assert query.page == 1 and query.page_size == config.default_page_size
    try:
        TaskListQuery(page_size=config.max_page_size + 1)
    except Exception:
        pass
    else:
        raise AssertionError(f'page_size 超过 {config.max_page_size} 应被拒绝')
    try:
        TaskListQuery(page=0)
    except Exception:
        pass
    else:
        raise AssertionError('page 从 1 开始')
    # 非法状态值
    try:
        TaskListQuery(status='乱填')
    except Exception:
        pass
    else:
        raise AssertionError('非法 review_status 应被拒绝')
    assert TaskListQuery(status='blocked').status == 'blocked'
    passed += 1
    print('  ✅ 分页参数与状态筛选校验生效')

    # ---------- 五、规则请求 ----------
    rule = RuleCreateRequest(code='R-09', name='测试规则', trigger_condition={'scope': 'clause'})
    assert rule.contract_type == '*' and rule.risk_level == 'mid' and rule.enabled is True
    try:
        RuleCreateRequest(code='R-09', name='x', trigger_condition={}, priority=10000)
    except Exception:
        pass
    else:
        raise AssertionError('priority 越界应被拒绝')
    # trigger_condition 是必填（没有条件的规则没有意义）
    try:
        RuleCreateRequest(code='R-09', name='x')
    except Exception:
        pass
    else:
        raise AssertionError('trigger_condition 必填')
    passed += 1
    print('  ✅ 规则请求模型：必填项与取值范围校验生效')

    # ---------- 六、模板请求 ----------
    template = TemplateCreateRequest(clause_type='payment', title='标题', content='正文')
    assert template.contract_type == '*'
    for missing in ('clause_type', 'title', 'content'):
        payload = {'clause_type': 'payment', 'title': 't', 'content': 'c'}
        payload.pop(missing)
        try:
            TemplateCreateRequest(**payload)
        except Exception:
            pass
        else:
            raise AssertionError(f'{missing} 缺失应被拒绝')
    passed += 1
    print('  ✅ 模板请求模型：三要素必填')

    print(f'web.schemas 自测全部通过 ✅ 共 {passed} 个用例')
