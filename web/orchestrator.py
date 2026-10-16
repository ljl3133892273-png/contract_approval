'''web/orchestrator.py'''

"""
子图编排层（接入层的核心）

职责：把 4 个业务子图按 SPEC 的顺序串起来，并负责**状态机迁移与审计事件**。
      SPEC 5.2 要求「状态机由单一组件实现，禁止散落各处直接改状态字段」——
      所以状态推进全部走 `utils.flow_state_machine`，这里不出现裸赋值。

自动链路（SPEC 4.1）:
    CREATE → pending --PARSE_START--> parsing
           → [解析子图] 成功 PARSE_OK → reviewing；失败 PARSE_FAIL → blocked
           → [审查子图] 成功 REVIEW_OK → completed；LLM 失败 REVIEW_FAIL → blocked
           → [推荐子图] **增强环节**：失败只告警，不改变主状态机

人工链路:
    [回写子图] 由「写回审批意见」触发，失败只改 writeback_status，**不回退主状态**
    [报告导出] 由「导出报告」触发，与回写结果解耦

为什么推荐子图失败不改状态:
    SPEC 4.4 把推荐定义为"为每个风险点生成修改建议"的增强动作。
    审查结论已经产出了，建议没生成只是少了个便利，不该让整个任务变成 blocked。
    这一点与"解析失败必须阻塞"是不同的语义，别混。

为什么图内异常要在这一层兜:
    各子图内部**不做阻塞兜底**（节点抛异常直接上抛），
    由接入层决定置 blocked 还是不阻塞 —— 这是 SPEC 5.2 与 11.1 的分工。
"""

import hashlib
import json
import logging
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from config_bag.app_config import get_app_config
from db.constants import ReviewStatus, TaskEvent, WritebackStatus
from db.models import ContractTask, ReviewComment, TaskEventLog
from db.session import session_scope
from processor.parse_processor.config import ParseConfig
from processor.parse_processor.main_graph import ContractParseWorkflow
from processor.parse_processor.state import get_default_state as get_parse_state
from processor.recommend_processor.main_graph import ContractRecommendWorkflow
from processor.recommend_processor.state import get_default_state as get_recommend_state
from processor.review_processor.main_graph import ContractReviewWorkflow
from processor.review_processor.state import get_default_state as get_review_state
from processor.writeback_processor.adapter.approve_adapter import build_adapter
from processor.writeback_processor.config import WritebackConfig
from processor.writeback_processor.main_graph import ContractWritebackWorkflow
from processor.writeback_processor.state import get_default_state as get_writeback_state
from utils.flow_state_machine import (
    MAIN_TRANSITIONS,
    WRITEBACK_TRANSITIONS,
    FlowStateError,
    FlowStateMachine,
)
from utils.task_utils import clear_task
from web.errors import ApiError, ErrorCode

logger = logging.getLogger(__name__)

# 分块读取大小（计算 SHA256 用，避免大文件一次性进内存）
_HASH_CHUNK = 1024 * 1024

# 「管理员取消」的 blocked_reason 统一前缀：
# SPEC 5.1 状态枚举是封闭的（没有 cancelled），取消的任务也落 blocked，
# 只能靠这个前缀把它与"执行失败"区分开 —— prepare_retry 据此拒绝复活已取消的任务。
# 改文案时必须同步修改 prepare_retry 的判断（或改为更稳定的标记位）。
CANCELLED_REASON_PREFIX = '任务已被管理员取消'


# ============================================================
# 状态机 + 审计事件（唯一的"改状态"入口）
# ============================================================
def emit_event(session, task: ContractTask, event: str, operator: str = 'system',
               detail: Optional[Dict[str, Any]] = None,
               writeback: bool = False) -> str:
    """
    推进状态机并写审计事件（**唯一允许改状态的地方**）

    :param session: 数据库会话
    :param task: 任务 ORM 行
    :param event: 事件名（db.constants.TaskEvent）
    :param operator: 操作人
    :param detail: 事件上下文快照
    :param writeback: True 走回写状态机，False 走主状态机
    :return: 迁移后的状态
    :raises ApiError: 迁移非法（INVALID_STATUS_TRANSITION）
    """
    if writeback:
        current = task.writeback_status or WritebackStatus.NOT_WRITTEN
        declared_from = WRITEBACK_TRANSITIONS.get(event, (None, None))[0]
        try:
            target = FlowStateMachine.writeback_transition(current, event)
        except FlowStateError as e:
            raise ApiError(ErrorCode.INVALID_STATUS_TRANSITION, str(e), http_status=409)
        task.writeback_status = target
    else:
        current = task.review_status or ''
        declared_from = MAIN_TRANSITIONS.get(event, (None, None))[0]
        try:
            target = FlowStateMachine.main_transition(current, event)
        except FlowStateError as e:
            # 重复投递（已在目标状态）会被状态机幂等放行，走到这里说明是真的非法迁移
            raise ApiError(ErrorCode.INVALID_STATUS_TRANSITION, str(e), http_status=409)
        task.review_status = target

    # 源状态如实记录；但 CREATE 这类"无前置状态"的事件必须记 NULL ——
    # 新建任务时 ORM 的列默认值已经把 review_status 填成 pending 了，
    # 若直接取当前值，审计里会显示 "pending → pending"，与 SPEC 5.2 的「— → pending」不符。
    log_from = None if declared_from is None else (current or None)

    session.add(TaskEventLog(
        task_id=task.id,
        event=event,
        from_status=log_from,
        to_status=target,
        operator=operator,
        detail_json=json.dumps(detail or {}, ensure_ascii=False),
    ))
    session.flush()
    return target


def get_task_or_404(task_id: int) -> ContractTask:
    """
    取任务，不存在就抛 404
    :param task_id: 任务ID
    :return: 任务 ORM 行（**已脱离会话**，调用方只读使用）
    """
    with session_scope() as session:
        task = session.get(ContractTask, task_id)
        if task is None:
            raise ApiError(ErrorCode.NOT_FOUND, f'任务 {task_id} 不存在', http_status=404)
        session.expunge(task)
        return task


# ============================================================
# 任务创建
# ============================================================
def create_task(*, contract_name: str, source_type: str,
                external_doc_id: Optional[str] = None, applicant: str = '',
                department: Optional[str] = None, biz_type: Optional[str] = None,
                operator: str = 'system') -> int:
    """
    创建任务行并发出 CREATE 事件（— → pending）

    **注意**：`applicant` 存的是**登录用户名**（唯一可比较），不是展示名，
    否则数据权限（applicant=me）在同名用户面前会失效。
    :return: 任务主键
    """
    # 审批单号是回写幂等键，空串必须转 None（唯一索引下多个 '' 会冲突）
    doc_id = (external_doc_id or '').strip() or None

    with session_scope() as session:
        task = ContractTask(
            contract_name=contract_name or '未命名合同',
            source_type=source_type,
            external_doc_id=doc_id,
            applicant=applicant or None,
            department=department or None,
            biz_type=biz_type or None,
        )
        session.add(task)
        session.flush()  # 拿主键

        emit_event(session, task, TaskEvent.CREATE, operator=operator,
                   detail={'source_type': source_type, 'external_doc_id': doc_id})
        task_id = task.id

    logger.info(f'[编排] 创建任务 {task_id}（来源 {source_type}，单据 {doc_id or "无"}）')
    return task_id


def is_duplicated(external_doc_id: str, file_hash: str) -> Optional[int]:
    """
    附件去重判定（SPEC 4.1.1：同指纹 + 同审批单号）

    说明：上传来源没有审批单号，**不参与去重** ——
        同一份文件被不同经办人重新上传时，往往就是想要一次新的审查。
        只有审批来源（推送/拉取）才按"同单据 + 同指纹"判定为重复。
    :return: 命中的已有任务ID；未命中返回 None
    """
    if not external_doc_id or not file_hash:
        return None

    with session_scope() as session:
        row = session.scalars(
            select(ContractTask)
            .where(
                ContractTask.external_doc_id == external_doc_id,
                ContractTask.file_hash == file_hash,
            )
            .order_by(ContractTask.id.desc())
        ).first()
        return row.id if row else None


def save_upload(task_id: int, filename: str, content: bytes) -> Dict[str, Any]:
    """
    落盘上传文件并计算指纹

    校验顺序（接入层先拦一道，避免把明显不合法的文件送进解析子图）:
        空文件 → 扩展名白名单 → 大小上限 → 写盘
    :param task_id: 任务ID
    :param filename: 原始文件名
    :param content: 文件字节
    :return: {'file_path', 'file_type', 'file_hash', 'file_size'}
    :raises ApiError: 1001 空文件 / 1002 格式不支持 / 1003 超限 / 1004 写盘失败
    """
    if not content:
        raise ApiError(ErrorCode.FILE_EMPTY, '上传文件为空')

    app_config = get_app_config()
    suffix = Path(filename or '').suffix.lower()
    if suffix not in app_config.allowed_extensions:
        raise ApiError(
            ErrorCode.UNSUPPORTED_FORMAT,
            f'不支持的文件格式 {suffix or "(无扩展名)"}，仅支持 {app_config.allowed_extensions}',
        )

    if len(content) > app_config.max_file_size_bytes:
        raise ApiError(
            ErrorCode.FILE_TOO_LARGE,
            f'文件大小 {len(content) / 1024 / 1024:.1f}MB 超过上限 {app_config.max_file_size_mb}MB',
        )

    parse_config = ParseConfig()
    target_dir = Path(parse_config.file_store_dir) / f'task_{task_id}'
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        # 文件名做一次清洗，避免 ../ 之类的路径穿越
        safe_name = Path(filename).name or f'upload{suffix}'
        target_path = target_dir / safe_name
        target_path.write_bytes(content)
    except OSError as e:
        raise ApiError(ErrorCode.FILE_SAVE_FAILED, f'文件落盘失败：{e}', http_status=500)

    file_hash = hashlib.sha256(content).hexdigest()
    file_type = suffix.lstrip('.')

    with session_scope() as session:
        task = session.get(ContractTask, task_id)
        if task is not None:
            task.file_path = str(target_path)
            task.file_type = file_type
            task.file_hash = file_hash

    return {
        'file_path': str(target_path),
        'file_type': file_type,
        'file_hash': file_hash,
        'file_size': len(content),
    }


def fetch_approval_doc(doc_id: str) -> Dict[str, Any]:
    """
    从审批系统拉取单据详情（SPEC 4.1.1 的 approve_pull）
    :param doc_id: 审批单号
    :return: 单据字典
    :raises ApiError: 1006 未配置 / 1005 拉取失败
    """
    config = WritebackConfig()
    if not config.is_configured:
        raise ApiError(
            ErrorCode.APPROVAL_NOT_CONFIGURED,
            f'审批系统未配置（缺少 {config.missing_names}），无法拉取单据',
        )
    adapter = build_adapter(config)
    try:
        return adapter.fetch_doc(doc_id)
    except Exception as e:
        raise ApiError(ErrorCode.APPROVAL_FETCH_FAILED, f'拉取审批单据 {doc_id} 失败：{e}')


# 附件类型嗅探表（按文件头判断）—— 审批接口通常只返回字节流，不给扩展名
_ATTACHMENT_SIGNATURES = (
    (b'%PDF-', '.pdf', 'pdf'),
    (b'PK\x03\x04', '.docx', 'docx'),
    (b'\x89PNG\r\n\x1a\n', '.png', 'png'),
    (b'\xff\xd8\xff', '.jpg', 'jpg'),
    (b'BM', '.bmp', 'bmp'),
    (b'II*\x00', '.tiff', 'tiff'),
    (b'MM\x00*', '.tiff', 'tiff'),
)


def _sniff_attachment(path: Path) -> Tuple[str, str]:
    """
    按文件头判断附件真实类型
    :param path: 已落盘的附件
    :return: (扩展名, file_type)；识别不出时返回 ('.bin', 'bin')
    """
    with path.open('rb') as handle:
        header = handle.read(8)

    for signature, suffix, file_type in _ATTACHMENT_SIGNATURES:
        if header.startswith(signature):
            return suffix, file_type
    return '.bin', 'bin'


def download_approval_attachment(doc_id: str, task_id: int) -> Dict[str, Any]:
    """
    下载审批附件到任务目录，识别真实类型并回填任务字段

    为什么要在接入层做:
        解析子图**只处理本地文件**，完全不知道"审批系统"的存在 ——
        换成邮件附件、共享盘等其他来源，也不用改解析代码。

    为什么要嗅探类型:
        审批接口一般只返回字节流，不给文件名/扩展名。
        如果一律按 .docx 存，一个 PDF 附件就会因为扩展名与内容不符在
        `node_detect_type` 里被判为"扩展名与魔数不一致"（虽然能纠正，但会刷告警），
        更糟的是上传/下载出口的文件名会误导人。
    :param doc_id: 审批单号
    :param task_id: 任务ID
    :return: {'file_path', 'file_type', 'file_hash', 'file_size'}
    :raises ApiError: 未配置 / 下载失败
    """
    config = WritebackConfig()
    if not config.is_configured:
        raise ApiError(
            ErrorCode.APPROVAL_NOT_CONFIGURED,
            f'审批系统未配置（缺少 {config.missing_names}），无法下载附件',
        )

    parse_config = ParseConfig()
    task_dir = Path(parse_config.file_store_dir) / f'task_{task_id}'
    # 先落到一个中性文件名，嗅探出类型后再改名
    temp_target = task_dir / f'{doc_id}.download'

    adapter = build_adapter(config)
    try:
        adapter.download_attachment(doc_id, str(temp_target))
    except Exception as e:
        raise ApiError(ErrorCode.APPROVAL_FETCH_FAILED, f'下载审批附件 {doc_id} 失败：{e}')

    if not temp_target.is_file():
        raise ApiError(ErrorCode.APPROVAL_FETCH_FAILED, f'审批附件 {doc_id} 下载后不存在')

    suffix, file_type = _sniff_attachment(temp_target)
    final_path = task_dir / f'{doc_id}{suffix}'
    temp_target.replace(final_path)

    content = final_path.read_bytes()
    file_hash = hashlib.sha256(content).hexdigest()

    with session_scope() as session:
        task = session.get(ContractTask, task_id)
        if task is not None:
            task.file_path = str(final_path)
            task.file_type = file_type
            task.file_hash = file_hash

    logger.info(
        f'[编排] 已下载审批附件 {doc_id} → {final_path.name} '
        f'（识别为 {file_type}，{len(content)} 字节）'
    )
    return {
        'file_path': str(final_path),
        'file_type': file_type,
        'file_hash': file_hash,
        'file_size': len(content),
    }


# ============================================================
# 自动链路：解析 → 审查 → 推荐
# ============================================================
def run_auto_pipeline(task_id: int, operator: str = 'system') -> Dict[str, Any]:
    """
    顺序执行自动链路（供 BackgroundTasks 调用）

    执行顺序与状态流转:
        1. PARSE_START：pending → parsing
        2. 解析子图；异常 → PARSE_FAIL（parsing → blocked）+ blocked_reason
        3. 审查子图；节点内部自行处理 REVIEW_OK / REVIEW_FAIL
        4. 推荐子图；失败**只告警**，不影响主状态

    :param task_id: 任务ID
    :param operator: 操作人
    :return: 各阶段结果摘要（便于日志与测试断言）
    """
    result: Dict[str, Any] = {
        'task_id': task_id, 'parse': 'skipped', 'review': 'skipped',
        'recommend': 'skipped', 'blocked_reason': '',
    }

    # ---------- 前置：取任务并进入 parsing ----------
    with session_scope() as session:
        task = session.get(ContractTask, task_id)
        if task is None:
            logger.error(f'[编排] 任务 {task_id} 不存在，流水线终止')
            result['parse'] = 'task_missing'
            return result

        try:
            emit_event(session, task, TaskEvent.PARSE_START, operator=operator)
        except ApiError as e:
            # 已经完成 / 已在跑的任务不应被重复启动
            logger.warning(f'[编排] 任务 {task_id} 无法启动解析：{e.message}')
            result['parse'] = f'not_started: {e.message}'
            return result

    # ---------- 1. 解析 ----------
    parse_state = get_parse_state()
    parse_state.update({
        'task_id': str(task_id),
        'source_type': _source_type_of(task_id),
        'import_file_path': _file_path_of(task_id),
        'file_dir': str(Path(ParseConfig().file_store_dir) / f'task_{task_id}'),
        'file_title': _contract_name_of(task_id),
    })

    try:
        ContractParseWorkflow().run(parse_state)
        result['parse'] = 'ok'
    except Exception as e:
        reason = f'解析失败：{e}'[:512]
        _mark_blocked(task_id, TaskEvent.PARSE_FAIL, reason, operator)
        result['parse'] = 'failed'
        result['blocked_reason'] = reason
        logger.error(f'[编排] 任务 {task_id} 解析失败：{e}')
        return result

    # ---------- 2. 审查（状态推进由 node_persist_risk 负责）----------
    review_state = get_review_state()
    review_state['task_id'] = str(task_id)
    try:
        review_out = ContractReviewWorkflow().run(review_state)
        result['review'] = 'ok'
        result['overall_risk_level'] = review_out.get('overall_risk_level') or ''
        result['llm_failed'] = bool(review_out.get('llm_failed'))
    except Exception as e:
        reason = f'审查失败：{e}'[:512]
        _mark_blocked(task_id, TaskEvent.REVIEW_FAIL, reason, operator)
        result['review'] = 'failed'
        result['blocked_reason'] = reason
        logger.error(f'[编排] 任务 {task_id} 审查失败：{e}')
        return result

    # ---------- 3. 推荐（增强环节，失败不阻塞）----------
    recommend_state = get_recommend_state()
    recommend_state['task_id'] = str(task_id)
    try:
        recommend_out = ContractRecommendWorkflow().run(recommend_state)
        result['recommend'] = 'ok'
        result['suggestion_count'] = len(recommend_out.get('suggestions') or [])
    except Exception as e:
        # 刻意不改状态：审查结论已经产出，建议没生成只是少了个便利
        result['recommend'] = 'failed'
        logger.warning(
            f'[编排] 任务 {task_id} 的推荐环节失败：{e} —— '
            f'审查结论不受影响，主状态保持（推荐是增强动作，SPEC 4.4）'
        )

    logger.info(f'[编排] 任务 {task_id} 自动链路完成：{result}')
    return result


def _mark_blocked(task_id: int, event: str, reason: str, operator: str) -> None:
    """
    把任务置为 blocked 并记录原因（状态迁移走状态机）
    :param task_id: 任务ID
    :param event: PARSE_FAIL / REVIEW_FAIL
    :param reason: 阻塞原因
    """
    with session_scope() as session:
        task = session.get(ContractTask, task_id)
        if task is None:
            return
        try:
            emit_event(session, task, event, operator=operator, detail={'reason': reason})
        except ApiError as e:
            logger.warning(f'[编排] 任务 {task_id} 置 blocked 失败（状态机拒绝）：{e.message}')
        task.blocked_reason = reason


def _row_field(task_id: int, field: str, default: Any = '') -> Any:
    """读任务某一列的当前值（内部小工具）"""
    with session_scope() as session:
        task = session.get(ContractTask, task_id)
        return getattr(task, field, default) if task is not None else default


def _source_type_of(task_id: int) -> str:
    """任务的接入来源"""
    return _row_field(task_id, 'source_type', 'upload') or 'upload'


def _file_path_of(task_id: int) -> str:
    """任务的文件路径"""
    return _row_field(task_id, 'file_path', '') or ''


def _contract_name_of(task_id: int) -> str:
    """任务的合同名"""
    return _row_field(task_id, 'contract_name', '') or ''


# ============================================================
# 人工链路：回写 / 报告
# ============================================================
def run_writeback(task_id: int, operator: str = 'system', force: bool = False) -> Dict[str, Any]:
    """
    执行回写子图（供 BackgroundTasks 调用）

    **本函数不抛异常给调用方**：回写失败只落 `writeback_status=failed`，
    主状态不动（SPEC 11.1），前端通过轮询接口看到结果。

    :param task_id: 任务ID
    :param operator: 操作人
    :param force: 强制重发。已 success 的任务默认会短路不发请求；
        强制时把本地状态回退到 not_written 再发 ——
        **幂等键不变**，所以审批系统会识别为重复提交并返回原评论 ID，不会产生第二条。
    :return: 回写结果摘要
    """
    summary: Dict[str, Any] = {'task_id': task_id, 'status': '', 'attempts': 0, 'error': ''}

    if force:
        with session_scope() as session:
            task = session.get(ContractTask, task_id)
            if task is not None and task.writeback_status == WritebackStatus.SUCCESS:
                logger.warning(
                    f'[编排] 任务 {task_id} 强制重发：本地回写状态回退到 not_written。'
                    f'幂等键不变，审批系统会识别为重复提交'
                )
                task.writeback_status = WritebackStatus.NOT_WRITTEN

    state = get_writeback_state()
    state['task_id'] = str(task_id)
    state['operator'] = operator

    try:
        out = ContractWritebackWorkflow(config=WritebackConfig()).run(state)
    except Exception as e:
        # 配置错误 / 前置条件不满足会走到这里：如实记录，不伪装成"回写失败"
        summary['status'] = 'error'
        summary['error'] = str(e)[:512]
        logger.error(f'[编排] 任务 {task_id} 回写中断：{e}')
        return summary

    summary.update({
        'status': out.get('writeback_status') or '',
        'attempts': out.get('writeback_attempts') or 0,
        'error': out.get('writeback_error') or '',
        'external_comment_id': out.get('external_comment_id') or '',
        'is_mock': bool(out.get('writeback_is_mock')),
        'duplicated': bool(out.get('writeback_duplicated')),
    })
    logger.info(f'[编排] 任务 {task_id} 回写结果：{summary}')
    return summary


def run_report(task_id: int) -> Dict[str, Any]:
    """
    导出报告（同步执行，通常 1 秒内完成）

    报告与回写**解耦**：即使回写失败，这里照样能导出（内部归档不能少）。
    :param task_id: 任务ID
    :return: {'report_md_path', 'report_pdf_path', 'report_id'}
    """
    from processor.writeback_processor.nodes.node_report_export import NodeReportExport

    # 只跑报告节点，不走整个回写图（避免误触发回写）
    state = get_writeback_state()
    state['task_id'] = str(task_id)
    try:
        out = NodeReportExport(config=WritebackConfig())(state)
    except Exception as e:
        raise ApiError(ErrorCode.PIPELINE_FAILED, f'导出报告失败：{e}', http_status=500)

    summary = {
        'task_id': task_id,
        'report_id': out.get('report_id') or 0,
        'md_path': out.get('report_md_path') or '',
        'pdf_path': out.get('report_pdf_path') or '',
    }
    logger.info(f'[编排] 任务 {task_id} 报告导出完成：{summary}')
    return summary


def prepare_retry(task_id: int, operator: str = 'system') -> None:
    """
    重试前置：blocked → pending（SPEC 11.2）

    SPEC 要求「重试前置：文件重新上传（hash 变化）或参数调整；重试后清空 blocked_reason，
    重设 retry_count」。这里做的是"清空原因 + 计数 +1"，并清掉上一次的节点进度缓存，
    否则前端会一直显示上次跑到哪。
    :param task_id: 任务ID
    :param operator: 操作人
    :raises ApiError: 任务不存在 / 不是 blocked（TASK_NOT_RETRYABLE）
    """
    with session_scope() as session:
        task = session.get(ContractTask, task_id)
        if task is None:
            raise ApiError(ErrorCode.NOT_FOUND, f'任务 {task_id} 不存在', http_status=404)

        if task.review_status != ReviewStatus.BLOCKED:
            raise ApiError(
                ErrorCode.TASK_NOT_RETRYABLE,
                f'只有 blocked 状态的任务可以重试，当前为 {task.review_status!r}',
                http_status=409,
            )

        # 「管理员取消」与「执行失败」共用 blocked（SPEC 5.1 是封闭枚举，没有 cancelled），
        # 只能靠 blocked_reason 前缀区分 —— 取消的任务是人为终态，**不能被重试复活**。
        if (task.blocked_reason or '').startswith(CANCELLED_REASON_PREFIX):
            raise ApiError(
                ErrorCode.TASK_TERMINATED,
                '任务已被管理员取消，不能重试；如需重新审查请重新创建任务',
                http_status=409,
            )

        emit_event(session, task, TaskEvent.RETRY, operator=operator,
                   detail={'previous_reason': task.blocked_reason or ''})
        task.blocked_reason = None
        task.retry_count = (task.retry_count or 0) + 1

    clear_task(str(task_id))
    logger.info(f'[编排] 任务 {task_id} 已重置为可重试（blocked → pending）')


def cancel_task(task_id: int, operator: str = 'system') -> Dict[str, Any]:
    """
    取消任务（SPEC 7.1）

    ⚠️ **能力边界（需知悉）**：演示用 FastAPI BackgroundTasks 跑流水线，
    **没有可中断的进程/任务句柄**，所以无法打断正在跑的节点。因此：
      · `pending`（还没开跑）→ 允许取消，置 blocked + 明确原因
      · `parsing` / `reviewing`（正在跑）→ 拒绝（409），提示稍后重试或等它跑完
      · `completed`（终态）→ 拒绝（2002 任务已终止）
    生产环境换成 Celery / RQ 后，才有真正的"撤销运行中任务"能力（SPEC 3 的演进方向）。

    :raises ApiError: 2002 任务已终止 / 4009 运行中无法取消
    """
    with session_scope() as session:
        task = session.get(ContractTask, task_id)
        if task is None:
            raise ApiError(ErrorCode.NOT_FOUND, f'任务 {task_id} 不存在', http_status=404)

        current = task.review_status
        if current == ReviewStatus.COMPLETED:
            raise ApiError(ErrorCode.TASK_TERMINATED, '任务已完成，无需取消', http_status=409)
        if current in (ReviewStatus.PARSING, ReviewStatus.REVIEWING):
            raise ApiError(
                ErrorCode.CONFLICT,
                f'任务正在执行（{current}），当前实现无法中断运行中的流水线；'
                f'请等待其结束或失败后再处理',
                http_status=409,
            )

        # pending / blocked → 走 PARSE_START 再 PARSE_FAIL（都是真实发生的迁移），
        # 这样既复用了状态机，又能留下清晰的审计轨迹
        emit_event(session, task, TaskEvent.PARSE_START, operator=operator,
                   detail={'action': 'cancel'})
        emit_event(session, task, TaskEvent.PARSE_FAIL, operator=operator,
                   detail={'reason': '管理员取消'})
        task.blocked_reason = CANCELLED_REASON_PREFIX

    clear_task(str(task_id))
    logger.info(f'[编排] 任务 {task_id} 已取消')
    return {'task_id': task_id, 'review_status': ReviewStatus.BLOCKED}


def writeback_status_of(task_id: int) -> Dict[str, Any]:
    """
    回写状态轮询（SPEC 7.2 的 `/write-back/status`）
    :param task_id: 任务ID
    :return: {'writeback_status', 'attempt_count', 'external_comment_id', 'error_message'}
    """
    with session_scope() as session:
        task = session.get(ContractTask, task_id)
        if task is None:
            raise ApiError(ErrorCode.NOT_FOUND, f'任务 {task_id} 不存在', http_status=404)

        comment = session.scalars(
            select(ReviewComment).where(ReviewComment.task_id == task_id)
        ).first()

        return {
            'task_id': task_id,
            'writeback_status': task.writeback_status,
            'review_status': task.review_status,
            'attempt_count': comment.attempt_count if comment else 0,
            'external_comment_id': (comment.external_comment_id if comment else '') or '',
            'error_message': (comment.error_message if comment else '') or '',
            'finished': task.writeback_status in (WritebackStatus.SUCCESS, WritebackStatus.FAILED),
        }


def cleanup_task_files(task_id: int) -> None:
    """
    清理任务的工作目录（任务被删除时调用）
    :param task_id: 任务ID
    """
    task_dir = Path(ParseConfig().file_store_dir) / f'task_{task_id}'
    if task_dir.is_dir():
        shutil.rmtree(task_dir, ignore_errors=True)
    clear_task(str(task_id))


if __name__ == '__main__':
    # 注：本模块的核心流程由 `web/tests` 风格的端到端用例覆盖（见 scripts/run_e2e.py），
    # 这里只做"不依赖 HTTP"的轻量自测：去重判定、取消边界、_row_field 之类的工具函数。
    import logging as _logging
    import tempfile

    from db.constants import SourceType
    from db.seed import seed_all
    from db.session import init_db, init_engine, reset_engine

    _logging.basicConfig(level=_logging.ERROR, format='%(levelname)s - %(message)s')
    passed = 0

    work_dir = Path(tempfile.mkdtemp(prefix='orchestrator_test_'))
    reset_engine()
    init_engine(f'sqlite:///{work_dir.as_posix()}/orchestrator_test.db')
    init_db()
    seed_all()

    # ---------- 一、创建任务与 CREATE 事件 ----------
    task_id = create_task(contract_name='测试合同', source_type=SourceType.UPLOAD,
                          applicant='business', operator='business')
    task = get_task_or_404(task_id)
    assert task.review_status == ReviewStatus.PENDING, '新建任务应为 pending'
    assert task.writeback_status == WritebackStatus.NOT_WRITTEN

    with session_scope() as session:
        events = session.scalars(
            select(TaskEventLog).where(TaskEventLog.task_id == task_id).order_by(TaskEventLog.id)
        ).all()
        assert [e.event for e in events] == [TaskEvent.CREATE], '应写一条 CREATE 事件'
        assert events[0].from_status is None and events[0].to_status == ReviewStatus.PENDING
        assert events[0].operator == 'business', '操作人应记录'
    passed += 1
    print('  ✅ 创建任务：pending + CREATE 事件（含操作人）')

    # ---------- 二、任务不存在 ----------
    try:
        get_task_or_404(999999)
    except ApiError as e:
        assert e.code == ErrorCode.NOT_FOUND and e.http_status == 404
    else:
        raise AssertionError('不存在的任务应抛 404')
    passed += 1
    print('  ✅ 任务不存在 → 404')

    # ---------- 三、去重判定 ----------
    assert is_duplicated('', 'hash-x') is None, '无审批单号不参与去重（上传来源）'
    assert is_duplicated('APV-1', '') is None, '无指纹不参与去重'
    assert is_duplicated('APV-1', 'hash-x') is None, '库里没有时返回 None'

    with session_scope() as session:
        session.add(ContractTask(contract_name='审批来源A', source_type=SourceType.APPROVE_PUSH,
                                 external_doc_id='APV-1', file_hash='hash-x'))
    assert is_duplicated('APV-1', 'hash-x') is not None, '同单据 + 同指纹应判为重复'
    assert is_duplicated('APV-1', 'hash-y') is None, '指纹不同不判重复（附件被换过）'
    assert is_duplicated('APV-2', 'hash-x') is None, '单据不同不判重复'
    passed += 1
    print('  ✅ 去重判定：仅"同审批单号 + 同指纹"命中，上传来源不参与')

    # ---------- 四、上传校验 ----------
    try:
        save_upload(task_id, 'a.txt', b'x')
    except ApiError as e:
        assert e.code == ErrorCode.UNSUPPORTED_FORMAT, f'应报 1002，实际 {e.code}'
    else:
        raise AssertionError('非白名单格式应被拒绝')

    try:
        save_upload(task_id, 'a.docx', b'')
    except ApiError as e:
        assert e.code == ErrorCode.FILE_EMPTY, f'应报 1001，实际 {e.code}'
    else:
        raise AssertionError('空文件应被拒绝')

    saved = save_upload(task_id, '合同.docx', b'PK\x03\x04fake-docx')
    assert saved['file_type'] == 'docx'
    assert saved['file_hash'] == hashlib.sha256(b'PK\x03\x04fake-docx').hexdigest(), \
        '指纹应与独立计算一致'
    assert Path(saved['file_path']).is_file(), '文件应已落盘'
    assert Path(saved['file_path']).parent.name == f'task_{task_id}', '应按任务分目录'
    passed += 1
    print('  ✅ 上传：格式白名单 / 空文件 / 落盘与指纹')

    # 路径穿越防护
    saved2 = save_upload(task_id, '../../evil.docx', b'PK\x03\x04x')
    assert '..' not in saved2['file_path'], f"文件名应被清洗，实际 {saved2['file_path']}"
    assert Path(saved2['file_path']).parent.name == f'task_{task_id}', '不能逃出任务目录'
    passed += 1
    print('  ✅ 上传文件名清洗（路径穿越防护）')

    # ---------- 五、非法状态迁移被拒 ----------
    with session_scope() as session:
        task = session.get(ContractTask, task_id)
        try:
            emit_event(session, task, TaskEvent.REVIEW_OK)  # pending 不能直接 completed
        except ApiError as e:
            assert e.code == ErrorCode.INVALID_STATUS_TRANSITION
            assert e.http_status == 409
        else:
            raise AssertionError('非法迁移应被拒绝')
    passed += 1
    print('  ✅ 非法状态迁移 → 409 / 2006')

    # ---------- 六、取消任务的边界 ----------
    with session_scope() as session:
        task = session.get(ContractTask, task_id)
        task.review_status = ReviewStatus.PARSING
    try:
        cancel_task(task_id)
    except ApiError as e:
        assert e.http_status == 409 and '无法中断' in e.message, \
            '运行中的任务应明确告知无法中断（BackgroundTasks 的能力边界）'
    else:
        raise AssertionError('运行中的任务不应能被取消')

    with session_scope() as session:
        task = session.get(ContractTask, task_id)
        task.review_status = ReviewStatus.PENDING
    result = cancel_task(task_id, operator='admin')
    assert result['review_status'] == ReviewStatus.BLOCKED
    with session_scope() as session:
        task = session.get(ContractTask, task_id)
        assert task.blocked_reason == CANCELLED_REASON_PREFIX
        events = [e.event for e in session.scalars(
            select(TaskEventLog).where(TaskEventLog.task_id == task_id).order_by(TaskEventLog.id)
        ).all()]
        assert events[-2:] == [TaskEvent.PARSE_START, TaskEvent.PARSE_FAIL], \
            f'取消应留下清晰的审计轨迹，实际 {events[-2:]}'

    # 已取消的任务不能被重试复活（取消与失败共用 blocked，靠原因前缀区分）
    try:
        prepare_retry(task_id, operator='admin')
    except ApiError as e:
        assert e.code == ErrorCode.TASK_TERMINATED and e.http_status == 409, \
            f'取消的任务重试应报 2002，实际 {e.code}'
    else:
        raise AssertionError('已取消的任务不应能被重试')

    # 已完成的任务不能再取消
    with session_scope() as session:
        task = session.get(ContractTask, task_id)
        task.review_status = ReviewStatus.COMPLETED
    try:
        cancel_task(task_id)
    except ApiError as e:
        assert e.code == ErrorCode.TASK_TERMINATED, f'应报 2002，实际 {e.code}'
    else:
        raise AssertionError('已完成的任务不应能被取消')
    passed += 1
    print('  ✅ 取消任务：pending 可取消、运行中拒绝、已完成报 2002')

    # ---------- 七、重试前置 ----------
    with session_scope() as session:
        task = session.get(ContractTask, task_id)
        task.review_status = ReviewStatus.COMPLETED
    try:
        prepare_retry(task_id)
    except ApiError as e:
        assert e.code == ErrorCode.TASK_NOT_RETRYABLE, '非 blocked 不可重试'
    else:
        raise AssertionError('completed 任务不应可重试')

    with session_scope() as session:
        task = session.get(ContractTask, task_id)
        task.review_status = ReviewStatus.BLOCKED
        task.blocked_reason = '之前的原因'
        task.retry_count = 0

    prepare_retry(task_id, operator='admin')
    with session_scope() as session:
        task = session.get(ContractTask, task_id)
        assert task.review_status == ReviewStatus.PENDING, 'blocked → pending'
        assert task.blocked_reason is None, 'SPEC 11.2：重试后清空阻塞原因'
        assert task.retry_count == 1, 'SPEC 11.2：重试计数应 +1'
    passed += 1
    print('  ✅ 重试前置：仅 blocked 可重试，清原因 + 计数 +1 + 清进度缓存')

    # ---------- 八、回写状态轮询（无记录时也要能返回）----------
    status = writeback_status_of(task_id)
    assert status['writeback_status'] == WritebackStatus.NOT_WRITTEN
    assert status['attempt_count'] == 0 and status['external_comment_id'] == ''
    assert status['finished'] is False
    passed += 1
    print('  ✅ 回写状态轮询：无评论记录时安全返回默认值')

    reset_engine()
    print(f'web.orchestrator 自测全部通过 ✅ 共 {passed} 个用例')
