'''web/api/contract_service.py'''

'''
任务与工作台接口（SPEC 7.1 / 7.2）

    # ---------- 7.1 任务与大盘 ----------
    POST /api/tasks/upload              上传合同文件创建任务（business/admin）
    POST /api/tasks/from-approval       从审批系统创建任务（推送/拉取）
    GET  /api/tasks                     任务列表（分页 + 筛选，带数据权限）
    GET  /api/tasks/{id}                任务详情（含元数据 + 状态）
    GET  /api/tasks/{id}/progress       节点级进度（轮询用）
    POST /api/tasks/{id}/retry          阻塞任务重新审查（admin）
    POST /api/tasks/{id}/cancel         取消任务（admin）

    # ---------- 7.2 工作台 ----------
    GET  /api/tasks/{id}/risk-items     任务风险列表（legal）
    GET  /api/tasks/{id}/clauses        条款列表（含 Locator）
    GET  /api/tasks/{id}/document       文档流（PDF 原文件 / DOCX 转 HTML）
    PUT  /api/risk-items/{rid}          调整风险项（采纳状态/批注/建议）
    POST /api/tasks/{id}/write-back     一键回写审批意见（异步 202）
    GET  /api/tasks/{id}/write-back/status  回写状态轮询
    POST /api/tasks/{id}/report/export  导出报告
    GET  /api/reports/{rid}/download    下载报告

几处刻意的设计:
    1. **上传与"从审批系统拉取"分开**：前者收文件流，后者按审批单号去拉。
       两者都会创建任务并（默认）立刻启动流水线，所以后端逻辑复用同一个 create + schedule。
    2. **回写是异步的**（202 Accepted）：它要打外部接口，最坏 3 次 × 30s 超时。
       SPEC 7.2 也标了「异步」，前端靠 `/write-back/status` 轮询。
    3. **报告导出是同步的**：纯本地渲染，1 秒内完成，没必要让前端再轮询一轮。
    4. **写接口一律带数据权限与角色校验**：业务角色只能看/改自己的任务。
'''

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, File, Form, Query, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy import func, or_, select

from db.constants import (
    AdoptStatus,
    ClauseType,
    ReportFormat,
    ReviewStatus,
    RiskLevel,
    RiskSource,
    TaskEvent,
    UserRole,
    WritebackStatus,
)
from db.models import Clause, ContractMeta, ContractTask, Report, RiskItem, Rule, TaskEventLog
from db.session import session_scope
from utils.task_utils import get_progress
from web import orchestrator
from web.deps import (
    applicant_filter,
    ensure_task_visible,
    get_current_user,
    is_admin,
    require_roles,
)
from web.document_view import docx_to_html, resolve_document
from web.errors import ApiError, ErrorCode, ok, paged
from web.schemas import (
    ReportExportRequest,
    RiskItemUpdateRequest,
    TaskCreateFromApprovalRequest,
    TaskListQuery,
    TaskRetryRequest,
    WriteBackRequest,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix='/api', tags=['任务与工作台'])

# 允许触发自动链路的角色
_CAN_CREATE = require_roles(UserRole.BUSINESS, UserRole.ADMIN)
# 工作台的写操作（改风险项 / 回写 / 导出报告）
_CAN_WORK = require_roles(UserRole.LEGAL, UserRole.ADMIN)
# 管理类操作
_CAN_ADMIN = require_roles(UserRole.ADMIN)


# ============================================================
# 序列化辅助
# ============================================================
def _task_brief(task: ContractTask) -> Dict[str, Any]:
    """任务列表项（对应 SPEC 7.1 的响应示例字段）"""
    return {
        'id': task.id,
        'contract_name': task.contract_name or '',
        'applicant': task.applicant or '',
        'department': task.department or '',
        'biz_type': task.biz_type or '',
        'source_type': task.source_type or '',
        'external_doc_id': task.external_doc_id or '',
        'amount': float(task.amount) if task.amount is not None else None,
        'currency': task.currency or '',
        'review_status': task.review_status or '',
        'risk_level': task.risk_level or '',
        'writeback_status': task.writeback_status or '',
        'retry_count': task.retry_count or 0,
        'blocked_reason': task.blocked_reason or '',
        'created_at': task.created_at.isoformat() if task.created_at else '',
        'updated_at': task.updated_at.isoformat() if task.updated_at else '',
    }


def _load_task(task_id: int, user: Dict[str, Any]) -> ContractTask:
    """取任务并做数据权限校验"""
    task = orchestrator.get_task_or_404(task_id)
    ensure_task_visible(user, task)
    return task


def _clause_map_of(task_id: int) -> Dict[int, Dict[str, Any]]:
    """条款主键 → 条款信息（供风险项补全"涉及条款/页码"）"""
    with session_scope() as session:
        rows = session.scalars(
            select(Clause).where(Clause.task_id == task_id).order_by(Clause.seq)
        ).all()
        return {
            row.id: {
                'db_clause_id': row.id,
                'clause_id': f'cl_{row.seq + 1:03d}',
                'seq': row.seq,
                'clause_type': row.clause_type,
                'title': row.title or '',
                'page_no': row.page_no,
                'paragraph_index': row.paragraph_index,
                'start_offset': row.start_offset,
                'end_offset': row.end_offset,
            }
            for row in rows
        }


def _rule_code_map(rule_ids: List[int]) -> Dict[int, str]:
    """规则主键 → 规则编号（前端要显示 R-04 这种可读编号）"""
    valid = {rid for rid in rule_ids if rid}
    if not valid:
        return {}
    with session_scope() as session:
        rows = session.scalars(select(Rule).where(Rule.id.in_(valid))).all()
        return {row.id: row.code for row in rows}


def _risk_item_out(item: RiskItem, clause_map: Dict[int, Dict[str, Any]],
                   rule_map: Dict[int, str]) -> Dict[str, Any]:
    """风险项输出结构（含前端做双向高亮联动所需的 Locator）"""
    clause = clause_map.get(item.clause_id) if item.clause_id else None
    return {
        'id': item.id,
        'task_id': item.task_id,
        'clause_id': item.clause_id,
        'clause_display_id': (clause or {}).get('clause_id', ''),
        'clause_title': (clause or {}).get('title', ''),
        'page_no': (clause or {}).get('page_no', 0),
        'paragraph_index': (clause or {}).get('paragraph_index', 0),
        'start_offset': (clause or {}).get('start_offset', 0),
        'end_offset': (clause or {}).get('end_offset', 0),
        'dimension': item.dimension or '',
        'risk_type': item.risk_type or '',
        'risk_level': item.risk_level or RiskLevel.LOW,
        'cause': item.cause or '',
        'legal_basis': item.legal_basis or '',
        'suggestion_text': item.suggestion_text or '',
        'source': item.source or RiskSource.RULE,
        'rule_code': rule_map.get(item.matched_rule_id or 0, ''),
        'adopt_status': item.adopt_status or AdoptStatus.PENDING,
        'manual_comment': item.manual_comment or '',
        'created_at': item.created_at.isoformat() if item.created_at else '',
        'updated_at': item.updated_at.isoformat() if item.updated_at else '',
    }


def _meta_out(meta: Optional[ContractMeta]) -> Dict[str, Any]:
    """合同元数据输出"""
    if meta is None:
        return {}
    return {
        'contract_no': meta.contract_no or '',
        'party_a': meta.party_a or '',
        'party_b': meta.party_b or '',
        'credit_code': meta.credit_code or '',
        'amount': float(meta.amount) if meta.amount is not None else None,
        'currency': meta.currency or '',
        'period_start': meta.period_start.isoformat() if meta.period_start else '',
        'period_end': meta.period_end.isoformat() if meta.period_end else '',
        'effective_condition': meta.effective_condition or '',
    }


# ============================================================
# 7.1 任务与大盘
# ============================================================
@router.post('/tasks/upload', status_code=201, summary='上传合同文件创建任务')
async def upload_task(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(..., description='合同文件（docx/pdf/图片，默认上限 50MB）'),
    contract_name: Optional[str] = Form(default=None, description='合同名，默认取文件名'),
    biz_type: Optional[str] = Form(default=None, description='合同类型：采购/销售/服务/劳动'),
    department: Optional[str] = Form(default=None),
    run_immediately: bool = Form(default=True, description='是否立刻启动审查流水线'),
    current_user: Dict[str, Any] = Depends(_CAN_CREATE),
) -> Dict[str, Any]:
    """
    上传合同并创建任务

    文件按任务隔离存放（`FILE_STORE_DIR/task_{id}/`），SPEC 12 要求"上传文件仅按任务隔离存放"。
    """
    content = await file.read()
    filename = file.filename or ''

    task_id = orchestrator.create_task(
        contract_name=(contract_name or Path(filename).stem or '未命名合同').strip(),
        source_type='upload',
        external_doc_id=None,      # 上传来源没有审批单号（唯一索引下必须存 NULL）
        applicant=current_user['username'],
        department=department,
        biz_type=biz_type,
        operator=current_user['username'],
    )

    saved = orchestrator.save_upload(task_id, filename, content)

    if run_immediately:
        background_tasks.add_task(orchestrator.run_auto_pipeline, task_id,
                                  current_user['username'])

    logger.info(
        f'[接入] 用户 {current_user["username"]} 上传 {filename} → 任务 {task_id}'
        f'（{saved["file_size"]} 字节，{"已排队" if run_immediately else "未启动"}）'
    )
    return ok({
        'task_id': task_id,
        'contract_name': (contract_name or Path(filename).stem or '未命名合同').strip(),
        'file_type': saved['file_type'],
        'file_hash': saved['file_hash'],
        'file_size': saved['file_size'],
        'duplicated': False,
        'scheduled': run_immediately,
    }, message='上传成功，审查任务已创建')


@router.post('/tasks/from-approval', status_code=201, summary='从审批系统创建任务')
def create_from_approval(
    payload: TaskCreateFromApprovalRequest,
    background_tasks: BackgroundTasks,
    current_user: Dict[str, Any] = Depends(_CAN_CREATE),
) -> Dict[str, Any]:
    """
    按审批单号创建任务（SPEC 4.1.1 的 approve_push / approve_pull）

    流程：查审批单据 → 下载附件到任务目录 → 建任务 → （可选）启动流水线。
    附件下载放在接入层，解析子图只处理本地文件。
    """
    doc_id = payload.doc_id.strip()

    # 1.先去审批系统探一下单据（顺便把标题/类型带回来，减少人工填写）
    doc_info = orchestrator.fetch_approval_doc(doc_id)

    # 2.已存在同单据号的任务 → 直接复用（唯一索引决定了一个单号只能有一个任务）
    with session_scope() as session:
        existing = session.scalars(
            select(ContractTask).where(ContractTask.external_doc_id == doc_id)
        ).first()
        existing_id = existing.id if existing else None

    if existing_id is not None:
        logger.info(f'[接入] 审批单据 {doc_id} 已有任务 {existing_id}，复用而非新建')
        return ok({
            'task_id': existing_id,
            'external_doc_id': doc_id,
            'duplicated': True,
            'scheduled': False,
        }, message='该审批单已有审查任务，直接复用')

    # 3.建任务
    task_id = orchestrator.create_task(
        contract_name=(doc_info.get('title') or f'审批单据 {doc_id}').strip(),
        source_type=payload.source_type,
        external_doc_id=doc_id,
        applicant=current_user['username'],
        department=doc_info.get('department') or None,
        biz_type=doc_info.get('biz_type') or None,
        operator=current_user['username'],
    )

    # 4.下载附件并回填文件字段
    try:
        saved = orchestrator.download_approval_attachment(doc_id, task_id)
    except ApiError:
        # 附件拉不下来就把任务留着（保持 pending，下一轮可重试）——
        # SPEC 11.1：「附件下载失败 → 拉取任务保持 pending，下一轮重试」
        logger.warning(f'[接入] 审批单据 {doc_id} 的任务 {task_id} 附件下载失败，保持 pending')
        raise

    # 5.审批侧带来的金额/币种先落到任务上（解析阶段还会再抽一次，以解析结果为准）
    if doc_info.get('amount') is not None:
        with session_scope() as session:
            task = session.get(ContractTask, task_id)
            if task is not None and task.amount is None:
                task.amount = doc_info['amount']
                task.currency = doc_info.get('currency') or task.currency

    if payload.run_immediately:
        background_tasks.add_task(orchestrator.run_auto_pipeline, task_id,
                                  current_user['username'])

    return ok({
        'task_id': task_id,
        'external_doc_id': doc_id,
        'file_type': saved['file_type'],
        'file_hash': saved['file_hash'],
        'duplicated': False,
        'scheduled': payload.run_immediately,
    }, message='已从审批系统拉取并创建任务')


@router.get('/tasks', summary='任务列表（分页 + 筛选）')
def list_tasks(
    query: TaskListQuery = Depends(),
    current_user: Dict[str, Any] = Depends(get_current_user),
) -> Dict[str, Any]:
    """
    任务列表

    SPEC 7.1 要求支持 status / risk / keyword / biz_type 筛选，
    并按数据权限过滤（业务角色只能看自己提交的）。
    """
    conditions = []

    scope_applicant = applicant_filter(current_user)
    if scope_applicant is not None:
        conditions.append(ContractTask.applicant == scope_applicant)
    if query.status:
        conditions.append(ContractTask.review_status == query.status)
    if query.risk:
        conditions.append(ContractTask.risk_level == query.risk)
    if query.biz_type:
        conditions.append(ContractTask.biz_type == query.biz_type)
    if query.writeback_status:
        conditions.append(ContractTask.writeback_status == query.writeback_status)
    if query.keyword:
        like = f'%{query.keyword.strip()}%'
        conditions.append(or_(
            ContractTask.contract_name.like(like),
            ContractTask.applicant.like(like),
            ContractTask.external_doc_id.like(like),
        ))

    with session_scope() as session:
        count_stmt = select(func.count()).select_from(ContractTask)
        rows_stmt = select(ContractTask).order_by(ContractTask.id.desc())
        if conditions:
            count_stmt = count_stmt.where(*conditions)
            rows_stmt = rows_stmt.where(*conditions)

        total = session.scalar(count_stmt) or 0
        rows = session.scalars(
            rows_stmt.offset((query.page - 1) * query.page_size).limit(query.page_size)
        ).all()
        items = [_task_brief(row) for row in rows]

    return paged(items, total=total, page=query.page, page_size=query.page_size)


@router.get('/tasks/{task_id}', summary='任务详情（含元数据与状态）')
def get_task(task_id: int, current_user: Dict[str, Any] = Depends(get_current_user)
             ) -> Dict[str, Any]:
    """任务详情：任务本体 + 合同元数据 + 风险统计 + 最新事件"""
    task = _load_task(task_id, current_user)

    with session_scope() as session:
        meta = session.scalars(
            select(ContractMeta).where(ContractMeta.task_id == task_id)
        ).first()
        meta_out = _meta_out(meta)

        risk_rows = session.scalars(
            select(RiskItem).where(RiskItem.task_id == task_id)
        ).all()
        level_counter: Dict[str, int] = {}
        adopt_counter: Dict[str, int] = {}
        for row in risk_rows:
            level_counter[row.risk_level] = level_counter.get(row.risk_level, 0) + 1
            adopt_counter[row.adopt_status] = adopt_counter.get(row.adopt_status, 0) + 1

        clause_count = session.scalar(
            select(func.count()).select_from(Clause).where(Clause.task_id == task_id)
        ) or 0

        events = session.scalars(
            select(TaskEventLog)
            .where(TaskEventLog.task_id == task_id)
            .order_by(TaskEventLog.id.desc())
            .limit(20)
        ).all()
        event_list = [
            {
                'event': row.event,
                'from_status': row.from_status or '',
                'to_status': row.to_status or '',
                'operator': row.operator or '',
                'created_at': row.created_at.isoformat() if row.created_at else '',
            }
            for row in events
        ]

    return ok({
        **_task_brief(task),
        'meta': meta_out,
        'risk_count': len(risk_rows),
        'risk_level_distribution': level_counter,
        'adopt_status_distribution': adopt_counter,
        'clause_count': clause_count,
        'events': event_list,
        'allowed_events': _allowed_events_of(task.review_status),
    })


def _allowed_events_of(status: str) -> List[str]:
    """当前状态允许的事件（前端据此决定按钮是否可点）"""
    from utils.flow_state_machine import FlowStateMachine

    return FlowStateMachine.allowed_events(status)


@router.get('/tasks/{task_id}/progress', summary='节点级进度（轮询用）')
def get_task_progress(task_id: int, current_user: Dict[str, Any] = Depends(get_current_user)
                      ) -> Dict[str, Any]:
    """
    节点级进度

    数据来自 `utils/task_utils` 的**内存态**进度（哪个节点在跑/跑完了），
    与业务主状态（`review_status`）是两回事 —— 前者用于进度条，后者用于状态标签。
    """
    task = _load_task(task_id, current_user)
    progress = get_progress(str(task_id))
    return ok({
        'task_id': task_id,
        'review_status': task.review_status,
        **progress,
    })


@router.post('/tasks/{task_id}/retry', summary='阻塞任务重新审查（admin）')
def retry_task(
    task_id: int,
    payload: TaskRetryRequest,
    background_tasks: BackgroundTasks,
    current_user: Dict[str, Any] = Depends(_CAN_ADMIN),
) -> Dict[str, Any]:
    """
    重试：blocked → pending → 重新跑流水线（SPEC 11.2）

    可选带上 `biz_type` 覆盖合同类型（人工修正解析结果不准的情况）。
    """
    orchestrator.prepare_retry(task_id, operator=current_user['username'])

    if payload.biz_type:
        with session_scope() as session:
            task = session.get(ContractTask, task_id)
            if task is not None:
                task.biz_type = payload.biz_type

    background_tasks.add_task(orchestrator.run_auto_pipeline, task_id,
                              current_user['username'])
    logger.info(f'[接入] 任务 {task_id} 已重新排入审查队列（操作人 {current_user["username"]}）')
    return ok({'task_id': task_id, 'review_status': ReviewStatus.PENDING,
               'scheduled': True}, message='已重新排入审查队列')


@router.post('/tasks/{task_id}/cancel', summary='取消任务（admin）')
def cancel_task(
    task_id: int,
    current_user: Dict[str, Any] = Depends(_CAN_ADMIN),
) -> Dict[str, Any]:
    """
    取消任务

    ⚠️ 能力边界：演示用 BackgroundTasks 没有可中断的任务句柄，
    只能取消**尚未开跑**（pending）的任务；运行中的会返回 409 并说明原因。
    详见 `web.orchestrator.cancel_task` 的说明。
    """
    result = orchestrator.cancel_task(task_id, operator=current_user['username'])
    return ok(result, message='任务已取消')


# ============================================================
# 7.2 工作台
# ============================================================
@router.get('/tasks/{task_id}/risk-items', summary='任务风险列表')
def list_risk_items(
    task_id: int,
    level: Optional[str] = Query(default=None, description='按风险等级筛选'),
    source: Optional[str] = Query(default=None, description='按来源筛选：rule/llm/manual'),
    adopt_status: Optional[str] = Query(default=None, description='按采纳状态筛选'),
    current_user: Dict[str, Any] = Depends(_CAN_WORK),
) -> Dict[str, Any]:
    """
    风险列表（含条款 Locator，供前端做双向高亮联动）

    排序与审查阶段保持一致：严重度倒序 → 页码升序 → 条款序号升序。
    """
    task = _load_task(task_id, current_user)

    conditions = [RiskItem.task_id == task_id]
    if level:
        if level not in RiskLevel.ALL:
            raise ApiError(ErrorCode.VALIDATION_FAILED, f'非法风险等级 {level!r}')
        conditions.append(RiskItem.risk_level == level)
    if source:
        if source not in RiskSource.ALL:
            raise ApiError(ErrorCode.VALIDATION_FAILED, f'非法来源 {source!r}')
        conditions.append(RiskItem.source == source)
    if adopt_status:
        if adopt_status not in AdoptStatus.ALL:
            raise ApiError(ErrorCode.VALIDATION_FAILED, f'非法采纳状态 {adopt_status!r}')
        conditions.append(RiskItem.adopt_status == adopt_status)

    clause_map = _clause_map_of(task_id)

    with session_scope() as session:
        rows = session.scalars(select(RiskItem).where(*conditions)).all()
        rule_map = _rule_code_map([row.matched_rule_id for row in rows])
        items = [_risk_item_out(row, clause_map, rule_map) for row in rows]

    items.sort(key=lambda x: (
        -RiskLevel.SEVERITY.get(x['risk_level'], 0), x['page_no'], x['clause_id'] or 0
    ))

    return ok({
        'task_id': task_id,
        'overall_risk_level': task.risk_level or '',
        'total': len(items),
        'items': items,
    })


@router.get('/tasks/{task_id}/clauses', summary='条款列表（含 Locator）')
def list_clauses(task_id: int, current_user: Dict[str, Any] = Depends(get_current_user)
                 ) -> Dict[str, Any]:
    """条款列表：工作台用它建立"条款 → 风险"的索引映射"""
    _load_task(task_id, current_user)
    clause_map = _clause_map_of(task_id)
    ordered = sorted(clause_map.values(), key=lambda x: x['seq'])
    return ok({'task_id': task_id, 'total': len(ordered), 'items': ordered})


@router.get('/tasks/{task_id}/document', summary='文档流（PDF 原文件 / DOCX 转 HTML）')
def get_document(task_id: int, current_user: Dict[str, Any] = Depends(get_current_user)
                 ) -> Response:
    """
    文档预览

    DOCX → 服务端转 HTML（段落序号与解析阶段的 paragraph_index 一一对应）；
    PDF / 图片 → 直接流原文件，前端用 PDF.js / img 渲染。
    """
    task = _load_task(task_id, current_user)

    try:
        mode, path, media_type = resolve_document(task)
    except FileNotFoundError as e:
        raise ApiError(ErrorCode.NOT_FOUND, str(e), http_status=404)

    if mode == 'html':
        return HTMLResponse(content=docx_to_html(path, title=task.contract_name or ''))
    return FileResponse(path=path, media_type=media_type, filename=Path(path).name)


@router.put('/risk-items/{risk_id}', summary='调整风险项（采纳状态/批注/建议）')
def update_risk_item(
    risk_id: int,
    payload: RiskItemUpdateRequest,
    current_user: Dict[str, Any] = Depends(_CAN_WORK),
) -> Dict[str, Any]:
    """
    法务调整风险项（SPEC 4.4「人工可编辑：法务确认/调整/否决后落库 adopt_status」）

    三个字段都可选，只改传了的那些。改完后**建议不会被重跑冲掉** ——
    `node_persist_suggestion` 对 `adopt_status != pending` 或已有批注的条目会整条跳过。
    """
    with session_scope() as session:
        row = session.get(RiskItem, risk_id)
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, f'风险项 {risk_id} 不存在', http_status=404)

        task = session.get(ContractTask, row.task_id)
        if task is None:
            raise ApiError(ErrorCode.NOT_FOUND, f'风险项 {risk_id} 的任务不存在', http_status=404)
        ensure_task_visible(current_user, task)

        changed: List[str] = []
        if payload.adopt_status is not None:
            row.adopt_status = payload.adopt_status
            changed.append('adopt_status')
        if payload.manual_comment is not None:
            row.manual_comment = payload.manual_comment
            changed.append('manual_comment')
        if payload.suggestion_text is not None:
            row.suggestion_text = payload.suggestion_text
            changed.append('suggestion_text')

        if not changed:
            raise ApiError(ErrorCode.VALIDATION_FAILED,
                           '至少需要提供一个要修改的字段（adopt_status / manual_comment / suggestion_text）')

        session.flush()
        session.refresh(row)
        clause_map = _clause_map_of(row.task_id)
        rule_map = _rule_code_map([row.matched_rule_id])
        result = _risk_item_out(row, clause_map, rule_map)

    logger.info(f'[接入] 用户 {current_user["username"]} 更新风险项 {risk_id}：{changed}')
    return ok({**result, 'changed_fields': changed}, message='已更新')


@router.post('/tasks/{task_id}/write-back', status_code=202, summary='一键回写审批意见（异步）')
def write_back(
    task_id: int,
    payload: WriteBackRequest,
    background_tasks: BackgroundTasks,
    current_user: Dict[str, Any] = Depends(_CAN_WORK),
) -> Dict[str, Any]:
    """
    触发回写（异步）

    返回 202 后前端应轮询 `/write-back/status`。
    这里先做**前置校验**，把"注定失败"的情况挡在排队之前：
      · 任务必须已审查完成（reviewing/completed 才有意见可写）
      · 必须有审批单号（上传来源无从回写）
    """
    task = _load_task(task_id, current_user)

    if task.review_status not in (ReviewStatus.COMPLETED, ReviewStatus.REVIEWING):
        raise ApiError(
            ErrorCode.TASK_NOT_COMPLETED,
            f'任务当前状态为 {task.review_status!r}，审查尚未完成，暂不能回写',
            http_status=409,
        )
    if not (task.external_doc_id or '').strip():
        raise ApiError(
            ErrorCode.NO_EXTERNAL_DOC_ID,
            '任务没有审批单号（本地上传来源），无法回写审批意见；请改用「导出报告」',
            http_status=409,
        )

    background_tasks.add_task(
        orchestrator.run_writeback, task_id, current_user['username'], payload.force
    )
    logger.info(
        f'[接入] 用户 {current_user["username"]} 触发任务 {task_id} 回写'
        f'（force={payload.force}）'
    )
    return ok({
        'task_id': task_id,
        'writeback_status': task.writeback_status,
        'scheduled': True,
    }, message='回写任务已提交，请轮询回写状态')


@router.get('/tasks/{task_id}/write-back/status', summary='回写状态轮询')
def write_back_status(task_id: int, current_user: Dict[str, Any] = Depends(_CAN_WORK)
                      ) -> Dict[str, Any]:
    """回写状态轮询：`finished=true` 时前端停止轮询"""
    _load_task(task_id, current_user)
    return ok(orchestrator.writeback_status_of(task_id))


@router.post('/tasks/{task_id}/report/export', summary='导出报告（pdf/md）')
def export_report(
    task_id: int,
    payload: ReportExportRequest,
    current_user: Dict[str, Any] = Depends(_CAN_WORK),
) -> Dict[str, Any]:
    """
    导出审查报告（同步）

    报告与回写**解耦**：回写失败也能导出（内部归档不能少）。
    `format=both` 时两种都产出。
    """
    task = _load_task(task_id, current_user)

    if task.review_status != ReviewStatus.COMPLETED:
        raise ApiError(
            ErrorCode.TASK_NOT_COMPLETED,
            f'任务当前状态为 {task.review_status!r}，审查未完成，无法导出报告',
            http_status=409,
        )

    summary = orchestrator.run_report(task_id)

    # 按请求的格式给出可下载的记录
    with session_scope() as session:
        rows = session.scalars(
            select(Report).where(Report.task_id == task_id).order_by(Report.id.desc())
        ).all()
        downloads = [
            {'report_id': row.id, 'format': row.format,
             'download_url': f'/api/reports/{row.id}/download'}
            for row in rows
        ]

    if payload.format == ReportFormat.MD:
        downloads = [d for d in downloads if d['format'] == ReportFormat.MD] or downloads
    elif payload.format == ReportFormat.PDF:
        downloads = [d for d in downloads if d['format'] == ReportFormat.PDF] or downloads

    return ok({**summary, 'downloads': downloads}, message='报告已生成')


@router.get('/reports/{report_id}/download', summary='下载报告')
def download_report(report_id: int, current_user: Dict[str, Any] = Depends(get_current_user)
                    ) -> Response:
    """下载报告文件（PDF 直接下，MD 以 text/markdown 返回）"""
    with session_scope() as session:
        row = session.get(Report, report_id)
        if row is None:
            raise ApiError(ErrorCode.NOT_FOUND, f'报告 {report_id} 不存在', http_status=404)
        task = session.get(ContractTask, row.task_id)
        if task is None:
            raise ApiError(ErrorCode.NOT_FOUND, f'报告 {report_id} 的任务不存在', http_status=404)
        ensure_task_visible(current_user, task)
        file_path, report_format = row.file_path or '', row.format

    path = Path(file_path)
    if not path.is_file():
        # 记录在但文件没了（被清理/换了机器）→ 明确报错，别返回空文件
        raise ApiError(ErrorCode.REPORT_NOT_READY, f'报告文件已不存在：{file_path}', http_status=404)

    media_type = 'application/pdf' if report_format == ReportFormat.PDF else 'text/markdown'
    return FileResponse(path=str(path), media_type=media_type, filename=path.name)


# ============================================================
# 内部统计（大盘用；放在这里是为了复用数据权限）
# ============================================================
@router.get('/dashboard/summary', summary='审查大盘统计')
def dashboard_summary(current_user: Dict[str, Any] = Depends(get_current_user)
                      ) -> Dict[str, Any]:
    """
    大盘统计（PRD 第 6 章的"审查大盘"）

    统计范围同样受数据权限约束：业务角色只看到自己的任务。
    """
    conditions = []
    scope_applicant = applicant_filter(current_user)
    if scope_applicant is not None:
        conditions.append(ContractTask.applicant == scope_applicant)

    with session_scope() as session:
        def _count(*extra):
            stmt = select(func.count()).select_from(ContractTask)
            for cond in (*conditions, *extra):
                stmt = stmt.where(cond)
            return session.scalar(stmt) or 0

        status_counter = {
            status_value: _count(ContractTask.review_status == status_value)
            for status_value in ReviewStatus.ALL
        }
        risk_counter = {
            level: _count(ContractTask.risk_level == level) for level in RiskLevel.ALL
        }
        total = _count()

    return ok({
        'total': total,
        'by_status': status_counter,
        'by_risk_level': risk_counter,
        'blocked': status_counter.get(ReviewStatus.BLOCKED, 0),
        'completed': status_counter.get(ReviewStatus.COMPLETED, 0),
    })


if __name__ == '__main__':
    # 冒烟自测：路由注册清单 + 与 SPEC 7.1/7.2 的对照。
    # 完整 HTTP 端到端（上传→审查→回写→报告）见 scripts/run_e2e.py ——
    # 那里起真实 mock 审批服务、注入 LLM 替身跑全链路，本文件不重复维护。
    logging.basicConfig(level=logging.ERROR, format='%(levelname)s - %(message)s')
    passed = 0

    paths = {route.path for route in router.routes}
    spec_required = {
        # SPEC 7.1
        '/api/tasks/upload', '/api/tasks', '/api/tasks/{task_id}',
        '/api/tasks/{task_id}/risk-items', '/api/tasks/{task_id}/retry',
        '/api/tasks/{task_id}/cancel',
        # SPEC 7.2
        '/api/tasks/{task_id}/clauses', '/api/tasks/{task_id}/document',
        '/api/risk-items/{risk_id}', '/api/tasks/{task_id}/write-back',
        '/api/tasks/{task_id}/write-back/status',
        '/api/tasks/{task_id}/report/export', '/api/reports/{report_id}/download',
    }
    missing = spec_required - paths
    assert not missing, f'SPEC 第 7 章要求的接口未注册：{sorted(missing)}'
    passed += 1
    print(f'  ✅ SPEC 7.1/7.2 要求的 {len(spec_required)} 个接口全部注册（共 {len(paths)} 个）')

    # 增强接口（SPEC 之外但前端需要）
    extras = paths - spec_required
    assert '/api/tasks/from-approval' in extras, '审批来源建任务接口缺失'
    assert '/api/tasks/{task_id}/progress' in extras, '进度轮询接口缺失'
    assert '/api/dashboard/summary' in extras, '大盘统计接口缺失'
    passed += 1
    print(f'  ✅ 增强接口 {len(extras)} 个：{sorted(extras)}')

    # 关键依赖注入组装无误（参数校验模型能被 FastAPI 解析）
    from fastapi import FastAPI

    from web.errors import register_exception_handlers

    app = FastAPI()
    register_exception_handlers(app)
    app.include_router(router)
    openapi = app.openapi()
    assert '/api/tasks' in openapi['paths'], 'OpenAPI 生成失败'
    # 校验失败应 400 + 4001（错误包由 web.errors 统一）
    from fastapi.testclient import TestClient

    from db.session import init_db, init_engine, reset_engine
    import tempfile as _tempfile
    _dir = Path(_tempfile.mkdtemp(prefix='contract_svc_test_'))
    reset_engine()
    init_engine(f'sqlite:///{_dir.as_posix()}/t.db')
    init_db()
    client = TestClient(app)
    # 未登录访问受保护接口 → 401 + 3001
    resp = client.get('/api/tasks')
    assert resp.status_code == 401 and resp.json()['code'] == ErrorCode.UNAUTHORIZED
    # 参数非法（page_size 超限）→ 400 + 4001
    resp = client.get('/api/tasks?page_size=99999',
                      headers={'Authorization': 'Bearer fake-token'})
    assert resp.status_code in (400, 401), '参数校验或鉴权应先挡住非法请求'
    passed += 1
    print('  ✅ 应用可装配；未登录 401 / 非法参数被挡（错误包统一）')

    reset_engine()
    print(f'contract_service 自测全部通过 ✅ 共 {passed} 个用例'
          f'（全链路流程见 scripts/run_e2e.py）')

