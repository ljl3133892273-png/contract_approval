'''processor/writeback_processor/context_loader.py'''

'''
回写上下文载入器

为什么单独成模块:
    `node_build_comment` 与 `node_report_export` 需要的是**同一份**上下文
    （任务快照 / 元数据 / 条款 / 风险项 / 审查结论），只是用途不同 ——
    一个用来渲染评论，一个用来渲染报告。
    如果各写一份取数逻辑，两边对"风险项要带上哪些字段""结论从哪取"的口径迟早会漂移，
    报告与评论就会出现对不上的情况（这类不一致最难排查）。

    所以把取数与结论还原收敛到这里，两个节点都只负责"怎么渲染"。

关于审查结论的还原（重要）:
    `contract_task` 只存了 `risk_level`，没存结论文案。
    结论文案在审查完成时写进了 `REVIEW_OK` / `REVIEW_FAIL` 事件的 `detail_json`，
    那是**当时的权威快照**。所以优先取快照 —— 万一管理员期间改过
    `ReviewConfig.conclusion_map`，报告才不至于与当时给出的结论不一致。
    快照缺失（老数据 / 手工造的数据）才回退到按风险等级现算。
'''

import json
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select

from db.constants import TaskEvent
from db.models import Clause, ContractMeta, ContractTask, RiskItem, TaskEventLog
from db.session import session_scope
from processor.writeback_processor.exceptions import ValidationError


def load_task_context(db_task_id: int) -> Dict[str, Any]:
    """
    载入回写所需的全部上下文
    :param db_task_id: 任务ID
    :return: {'task': 任务快照, 'meta': 元数据, 'clauses': 条款列表, 'risk_items': 风险项列表}
    :raises ValidationError: 任务不存在（无从构建评论或报告）
    """
    with session_scope() as session:
        task_row = session.get(ContractTask, db_task_id)
        if task_row is None:
            raise ValidationError(message=f'任务 {db_task_id} 不存在，无法构建回写内容')

        task = {
            'db_task_id': task_row.id,
            'contract_name': task_row.contract_name or '',
            'external_doc_id': task_row.external_doc_id or '',
            'biz_type': task_row.biz_type or '',
            'source_type': task_row.source_type or '',
            'file_type': task_row.file_type or '',
            'applicant': task_row.applicant or '',
            'department': task_row.department or '',
            'review_status': task_row.review_status or '',
            'writeback_status': task_row.writeback_status or '',
            'risk_level': task_row.risk_level or '',
            'blocked_reason': task_row.blocked_reason or '',
        }

        meta_row = session.scalars(
            select(ContractMeta).where(ContractMeta.task_id == db_task_id)
        ).first()
        meta = meta_to_dict(meta_row)

        clause_rows = session.scalars(
            select(Clause).where(Clause.task_id == db_task_id).order_by(Clause.seq)
        ).all()
        clause_map = {
            row.id: {
                'db_clause_id': row.id,
                'clause_id': f'cl_{row.seq + 1:03d}',
                'clause_type': row.clause_type,
                'title': row.title or '',
                'content': row.content or '',
                'page_no': row.page_no,
                'paragraph_index': row.paragraph_index,
            }
            for row in clause_rows
        }
        clauses = list(clause_map.values())

        risk_rows = session.scalars(
            select(RiskItem).where(RiskItem.task_id == db_task_id).order_by(RiskItem.id)
        ).all()

        risk_items: List[Dict[str, Any]] = []
        for row in risk_rows:
            clause = clause_map.get(row.clause_id) if row.clause_id is not None else None
            risk_items.append({
                'db_risk_id': row.id,
                'db_clause_id': row.clause_id,
                'clause_id': (clause or {}).get('clause_id', ''),
                'clause_title': (clause or {}).get('title', ''),
                'clause_content': (clause or {}).get('content', ''),
                'page_no': (clause or {}).get('page_no', 0),
                'dimension': row.dimension or '',
                'risk_type': row.risk_type or '',
                'risk_level': row.risk_level or '',
                'cause': row.cause or '',
                'legal_basis': row.legal_basis or '',
                'suggestion_text': row.suggestion_text or '',
                'source': row.source or '',
                'adopt_status': row.adopt_status or '',
                'manual_comment': row.manual_comment or '',
            })

    return {'task': task, 'meta': meta, 'clauses': clauses, 'risk_items': risk_items}


def meta_to_dict(meta_row: Optional[ContractMeta]) -> Dict[str, Any]:
    """
    ContractMeta 行 → 字典
    :param meta_row: ORM 行（可为 None）
    :return: 元数据字典；无记录时返回空字典（不因缺元数据而失败）
    """
    if meta_row is None:
        return {}
    return {
        'contract_no': meta_row.contract_no or '',
        'party_a': meta_row.party_a or '',
        'party_b': meta_row.party_b or '',
        'credit_code': meta_row.credit_code or '',
        'amount': float(meta_row.amount) if meta_row.amount is not None else None,
        'currency': meta_row.currency or '',
        'period_start': meta_row.period_start.isoformat() if meta_row.period_start else '',
        'period_end': meta_row.period_end.isoformat() if meta_row.period_end else '',
        'effective_condition': meta_row.effective_condition or '',
    }


def resolve_conclusion(db_task_id: int, risk_level: str = '') -> Tuple[str, str, str]:
    """
    还原审查结论与覆盖说明
    :param db_task_id: 任务ID
    :param risk_level: 综合风险等级（快照缺失时的兜底依据）
    :return: (审查结论文案, 覆盖说明, 来源标记)
             来源标记取值：'event'（事件快照）/ 'mapping'（按等级现算）
    """
    with session_scope() as session:
        event_row = session.scalars(
            select(TaskEventLog)
            .where(
                TaskEventLog.task_id == db_task_id,
                TaskEventLog.event.in_([TaskEvent.REVIEW_OK, TaskEvent.REVIEW_FAIL]),
            )
            .order_by(TaskEventLog.id.desc())
        ).first()

    if event_row is not None and event_row.detail_json:
        try:
            detail = json.loads(event_row.detail_json)
        except (json.JSONDecodeError, TypeError):
            detail = {}
        if isinstance(detail, dict) and detail.get('review_conclusion'):
            return detail['review_conclusion'], detail.get('review_note') or '', 'event'

    # 兜底：按综合风险等级现算（老数据没有事件快照时会走到这里）
    from processor.review_processor.config import get_config as get_review_config

    return get_review_config().conclusion_of(risk_level or ''), '', 'mapping'


def load_review_comment(db_task_id: int) -> Dict[str, Any]:
    """
    读取该任务的回写评论记录
    :param db_task_id: 任务ID
    :return: {'content_md': str, 'status': str, 'attempt_count': int,
              'external_comment_id': str, 'error_message': str}
              无记录时返回空内容与其默认状态
    """
    from db.models import ReviewComment

    with session_scope() as session:
        row = session.scalars(
            select(ReviewComment).where(ReviewComment.task_id == db_task_id)
        ).first()

    if row is None:
        return {
            'content_md': '', 'status': 'not_written', 'attempt_count': 0,
            'external_comment_id': '', 'error_message': '',
        }
    return {
        'content_md': row.content_md or '',
        'status': row.status or 'not_written',
        'attempt_count': row.attempt_count or 0,
        'external_comment_id': row.external_comment_id or '',
        'error_message': row.error_message or '',
    }


if __name__ == '__main__':
    # 轻量自测（需要临时库）
    import logging as _logging
    import tempfile
    from pathlib import Path

    from db.constants import AdoptStatus, ReviewStatus, RiskSource
    from db.models import ReviewComment
    from db.session import init_db, init_engine, reset_engine

    _logging.basicConfig(level=_logging.ERROR, format='%(levelname)s - %(message)s')
    passed = 0

    work_dir = Path(tempfile.mkdtemp(prefix='context_loader_test_'))
    reset_engine()
    init_engine(f'sqlite:///{work_dir.as_posix()}/loader_test.db')
    init_db()

    # 准备：任务 + 元数据 + 2 条款 + 2 风险项 + REVIEW_OK 事件
    with session_scope() as session:
        session.add(ContractTask(contract_name='上下文测试', biz_type='采购',
                                 source_type='approve_push', external_doc_id='APV-1',
                                 file_type='docx', review_status=ReviewStatus.COMPLETED,
                                 risk_level='high'))
    with session_scope() as session:
        task_id = session.scalars(select(ContractTask)).first().id
    with session_scope() as session:
        session.add_all([
            Clause(task_id=task_id, clause_type='payment', title='第二条 付款方式',
                   content='货到即付全款。', page_no=1, paragraph_index=1, seq=0),
            Clause(task_id=task_id, clause_type='ip', title='第三条 知识产权',
                   content='知识产权归乙方。', page_no=2, paragraph_index=2, seq=1),
        ])
        session.add(ContractMeta(task_id=task_id, contract_no='CG-1', party_a='甲',
                                 party_b='乙', amount=1280000, currency='CNY'))
    with session_scope() as session:
        clauses = session.scalars(select(Clause).order_by(Clause.seq)).all()
        session.add_all([
            RiskItem(task_id=task_id, clause_id=clauses[0].id, dimension='payment',
                     risk_type='timing', risk_level='high', cause='付款无验收前置',
                     suggestion_text='验收后付款', source=RiskSource.RULE,
                     adopt_status=AdoptStatus.PENDING, manual_comment='必须改'),
            RiskItem(task_id=task_id, clause_id=None, dimension='subject',
                     risk_type='missing', risk_level='mid', cause='相对方缺失',
                     source=RiskSource.LLM),
        ])
    with session_scope() as session:
        session.add(TaskEventLog(
            task_id=task_id, event=TaskEvent.REVIEW_OK,
            from_status=ReviewStatus.REVIEWING, to_status=ReviewStatus.COMPLETED,
            detail_json=json.dumps({'review_conclusion': '建议拒绝 / 重大整改后再审',
                                    'review_note': 'LLM 降级'}, ensure_ascii=False),
        ))
        session.add(ReviewComment(task_id=task_id, content_md='## 意见',
                                  status='success', attempt_count=1,
                                  external_comment_id='CMT-1'))

    # ---------- 一、完整载入 ----------
    context = load_task_context(task_id)
    assert context['task']['contract_name'] == '上下文测试'
    assert context['task']['external_doc_id'] == 'APV-1'
    assert len(context['clauses']) == 2
    assert len(context['risk_items']) == 2, '应载入 2 条风险项'

    payment = next(i for i in context['risk_items'] if i['dimension'] == 'payment')
    assert payment['clause_title'] == '第二条 付款方式', '风险项应挂上条款标题'
    assert payment['clause_content'] == '货到即付全款。', '风险项应挂上条款原文（报告要用）'
    assert payment['clause_id'] == 'cl_001', '展示编号应按 seq 生成'
    assert payment['page_no'] == 1
    assert payment['manual_comment'] == '必须改'
    assert payment['adopt_status'] == 'pending'

    doc_level = next(i for i in context['risk_items'] if i['dimension'] == 'subject')
    assert doc_level['db_clause_id'] is None
    assert doc_level['clause_title'] == '', '文档级风险没有条款标题'
    assert doc_level['page_no'] == 0
    passed += 1
    print('  ✅ 任务/元数据/条款/风险项全部载入，风险项已挂条款上下文（含文档级）')

    # ---------- 二、元数据字段 ----------
    assert context['meta']['amount'] == 1280000.0, '金额应转成 float'
    assert context['meta']['currency'] == 'CNY'
    assert context['meta']['contract_no'] == 'CG-1'
    passed += 1
    print('  ✅ 元数据金额/币种/编号正确')

    # ---------- 三、结论来自事件快照 ----------
    conclusion, note, source = resolve_conclusion(task_id, risk_level='high')
    assert conclusion == '建议拒绝 / 重大整改后再审', '应取事件快照里的结论'
    assert note == 'LLM 降级', '覆盖说明也应带出'
    assert source == 'event', f'来源应标记为 event，实际 {source}'
    passed += 1
    print('  ✅ 结论优先取事件快照（含覆盖说明），来源标记为 event')

    # ---------- 四、无快照时按等级兜底 ----------
    with session_scope() as session:
        session.add(ContractTask(contract_name='无事件', review_status=ReviewStatus.COMPLETED,
                                 risk_level='low'))
    with session_scope() as session:
        bare_id = session.scalars(
            select(ContractTask).where(ContractTask.contract_name == '无事件')
        ).first().id
    conclusion, note, source = resolve_conclusion(bare_id, risk_level='low')
    assert source == 'mapping', '无快照时应标记为 mapping'
    assert '可通过' in conclusion, f'低风险应兜底为可通过，实际 {conclusion}'
    assert note == ''
    passed += 1
    print('  ✅ 无事件快照 → 按风险等级兜底映射，来源标记为 mapping')

    # 风险等级为空的极端情况也不应报错
    conclusion, _, source = resolve_conclusion(bare_id, risk_level='')
    assert conclusion, '等级为空时也应给出兜底文案'
    passed += 1
    print('  ✅ 风险等级为空也给出兜底文案')

    # ---------- 五、评论记录 ----------
    comment = load_review_comment(task_id)
    assert comment['content_md'] == '## 意见'
    assert comment['status'] == 'success'
    assert comment['attempt_count'] == 1
    assert comment['external_comment_id'] == 'CMT-1'

    empty_comment = load_review_comment(bare_id)
    assert empty_comment['content_md'] == '', '无记录时应返回空内容'
    assert empty_comment['status'] == 'not_written', '无记录时状态应为 not_written'
    passed += 1
    print('  ✅ 评论记录读取正确；无记录时安全返回默认值')

    # ---------- 六、任务不存在 ----------
    from processor.writeback_processor.exceptions import WritebackProcessError
    try:
        load_task_context(999999)
    except WritebackProcessError as e:
        assert '不存在' in str(e)
        assert isinstance(e, ValidationError), '应抛 ValidationError（前置条件不满足）'
    else:
        raise AssertionError('任务不存在时应抛 ValidationError')
    passed += 1
    print('  ✅ 任务不存在 → ValidationError')

    # ---------- 七、无元数据 ----------
    bare_context = load_task_context(bare_id)
    assert bare_context['meta'] == {}, '无 contract_meta 时应返回空字典'
    assert bare_context['risk_items'] == [], '无风险项时应返回空列表'
    assert bare_context['clauses'] == []
    passed += 1
    print('  ✅ 无元数据/无条款/无风险项 → 空结构，不报错')

    reset_engine()
    print(f'context_loader 自测全部通过 ✅ 共 {passed} 个用例')
