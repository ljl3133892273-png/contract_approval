'''processor/writeback_processor/nodes/node_build_comment.py'''

'''
步骤分解
1. 接收状态: 取 `task_id`（contract_task.id）。
2. 载入数据: 任务、元数据、条款、风险项（风险项挂上条款信息，供「涉及条款/页码/建议」列使用）。
3. 还原结论: 从**最近一次 REVIEW_OK / REVIEW_FAIL 事件**的快照里取审查结论与覆盖说明；
             取不到时按 task.risk_level 走结论映射表兜底。
4. 校验可回写性: 取 `external_doc_id`（审批单号），缺失时按开关报错。
5. 渲染评论: 按 SPEC 10.1 模板生成 Markdown，并算出重点关注事项。
6. 生成幂等键: SPEC 4.5 —— `task_id + 审批单号`。
7. 写回状态 + 输出统计日志。

需求分析
输入:
- `task_id`：contract_task.id

输出:
- `comment_md` / `highlights` / `idempotency_key`
- `task` / `meta` / `clauses` / `risk_items` / `external_doc_id` / `risk_count`

为什么结论要从事件快照里取而不是重新算:
    `task` 表只存了 `risk_level`，没存结论文案。
    结论文案在审查完成时就已经写进 `REVIEW_OK` 事件的 `detail_json` 里了 ——
    那是**当时的权威快照**。重新按映射表算一次虽然结果通常一样，
    但万一管理员期间改过 `conclusion_map`，报告就会与当时给出的结论不一致。
    所以优先用快照，快照缺失（如老数据）才回退到现算。

关于「没有审批单号」:
    `external_doc_id` 是审批单号，也是回写的目标地址。上传来源的任务没有它，
    回写根本无从发起。此时按 `WRITEBACK_REQUIRE_DOC_ID`（默认 true）直接报错，
    给出可操作的提示；关掉开关则用 `{task_id}:manual` 作为幂等键，允许写进 mock 通道，
    方便演示与联调。

**本节点不做任何写库操作** —— 它只负责"把该说的话组织好"。
'''

import logging
from typing import Any, Dict, List, Tuple

from sqlalchemy.exc import SQLAlchemyError

from processor.writeback_processor.adapter.approve_adapter import build_adapter
from processor.writeback_processor.base import BaseNode, setup_logging
from processor.writeback_processor.comment_template import build_comment_markdown
from processor.writeback_processor.context_loader import (
    load_task_context,
    resolve_conclusion,
)
from processor.writeback_processor.exceptions import (
    CommentBuildError,
    StateFieldError,
    StorageError,
    ValidationError,
    WritebackProcessError,
)
from processor.writeback_processor.state import WritebackGraphState, get_default_state


class NodeBuildComment(BaseNode):
    '''评论构建节点：把审查结果组织成 SPEC 10.1 的 Markdown 审查意见'''
    name = 'node_build_comment'

    def __init__(self, config=None, adapter=None):
        """
        初始化节点
        :param config: 配置对象，默认使用全局配置
        :param adapter: 审批 Adapter 覆盖项（仅用于预判是否会走 mock 通道）
        """
        super().__init__(config)
        self._adapter_override = adapter

    def process(self, state: WritebackGraphState) -> WritebackGraphState:
        """
        节点：评论构建（node_build_comment）
        整体流程：取任务ID → 载入数据 → 还原结论 → 校验可回写性 → 渲染评论 → 幂等键 → 日志
        核心目的：把"要回写什么"定下来，且与"能不能送出去"解耦
        必要参数：task_id
        更新参数：task、meta、clauses、risk_items、external_doc_id、risk_count、
                 comment_md、highlights、idempotency_key

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取并校验任务ID=======
        db_task_id = self._step_1_get_task_id(state)

        try:
            # =======步骤二:载入数据=======
            task, meta, clauses, risk_items = self._step_2_load_data(db_task_id)

            # =======步骤三:还原审查结论与覆盖说明=======
            conclusion, review_note = self._step_3_resolve_conclusion(db_task_id, task)

        except SQLAlchemyError as e:
            raise StorageError(message=f'载入回写数据失败：{e}', cause=e)

        # =======步骤四:校验可回写性（审批单号）=======
        external_doc_id = self._step_4_resolve_doc_id(task, db_task_id)

        # =======步骤五:渲染评论=======
        try:
            comment = build_comment_markdown(
                risk_items=risk_items,
                overall_risk_level=task.get('risk_level') or '',
                review_conclusion=conclusion,
                max_risks=self.config.comment_max_risks,
                include_low=self.config.comment_include_low_risk,
                top_n=self.config.comment_highlight_top_n,
                review_note=review_note,
                writeback_is_mock=self._predict_is_mock(),
            )
        except Exception as e:
            raise CommentBuildError(message=f'渲染回写评论失败：{e}', cause=e)

        # =======步骤六:生成幂等键（SPEC 4.5：task_id + 审批单号）=======
        idempotency_key = f'{db_task_id}:{external_doc_id}'

        # =======步骤七:写回状态 + 输出统计日志=======
        state['task'] = task
        state['meta'] = meta
        state['clauses'] = clauses
        state['risk_items'] = risk_items
        state['external_doc_id'] = external_doc_id
        state['risk_count'] = len(risk_items)
        state['comment_md'] = comment['markdown']
        state['highlights'] = comment['highlights']
        state['idempotency_key'] = idempotency_key

        self._step_7_print_stats(db_task_id, task, risk_items, external_doc_id,
                                 idempotency_key, comment['highlights'],
                                 len(comment['markdown']))

        return state

    # ==================== 步骤实现 ====================
    def _step_1_get_task_id(self, state: WritebackGraphState) -> int:
        """
        【步骤1】获取并校验任务ID
        :param state: 项目状态字典（WritebackGraphState）
        :return: 整型任务ID
        """
        raw_task_id = state.get('task_id')
        if raw_task_id in (None, ''):
            raise StateFieldError(field_name='task_id', message='任务ID不能为空', expected_type=str)

        try:
            return int(raw_task_id)
        except (TypeError, ValueError):
            raise StateFieldError(
                field_name='task_id',
                message=f'task_id 必须是 contract_task.id（数字），当前值为 {raw_task_id!r}',
                expected_type=int
            )

    def _step_2_load_data(self, db_task_id: int) -> Tuple[
            Dict[str, Any], Dict[str, Any], List[Dict[str, Any]], List[Dict[str, Any]]]:
        """
        【步骤2】载入任务、元数据、条款与风险项
        说明：取数逻辑与 node_report_export 共用 `writeback_processor.context_loader` ——
             两处口径必须一致，否则评论与报告会对不上（这类不一致最难排查）。
        :param db_task_id: 任务ID
        :return: (任务快照, 元数据, 条款列表, 风险项列表)
        """
        context = load_task_context(db_task_id)
        return context['task'], context['meta'], context['clauses'], context['risk_items']

    def _step_3_resolve_conclusion(self, db_task_id: int,
                                   task: Dict[str, Any]) -> Tuple[str, str]:
        """
        【步骤3】还原审查结论与覆盖说明
        功能：优先用最近一次 REVIEW_OK / REVIEW_FAIL 事件的快照（当时的权威结论）；
             快照缺失时按 task.risk_level 走结论映射表兜底
        :param db_task_id: 任务ID
        :param task: 任务快照
        :return: (审查结论文案, 覆盖说明)
        """
        conclusion, note, source = resolve_conclusion(db_task_id, task.get('risk_level') or '')
        self.log_step(
            '结论文案来源',
            '事件快照（审查完成时的权威结论）' if source == 'event'
            else '按风险等级映射（未找到事件快照）'
        )
        return conclusion, note

    def _step_4_resolve_doc_id(self, task: Dict[str, Any], db_task_id: int) -> str:
        """
        【步骤4】取审批单号并校验可回写性
        :param task: 任务快照
        :param db_task_id: 任务ID
        :return: 审批单号（开关关闭时可能返回占位值）
        :raises ValidationError: 没有审批单号且开关要求必须有
        """
        external_doc_id = (task.get('external_doc_id') or '').strip()

        if external_doc_id:
            return external_doc_id

        source_type = task.get('source_type') or ''
        if self.config.require_external_doc_id:
            raise ValidationError(
                message=f'任务 {db_task_id} 没有审批单号（来源 {source_type or "未知"}），'
                        f'无法回写审批意见。'
                        f'本地上传的合同请改用「导出报告」；'
                        f'确实需要回写请让接入层补 external_doc_id，'
                        f'或把 WRITEBACK_REQUIRE_DOC_ID 设为 false（仅演示用）'
            )

        # 开关关闭 → 用占位单号，允许写进 mock 通道（便于演示）
        placeholder = f'MANUAL-{db_task_id}'
        self.logger.warning(
            f'任务 {db_task_id} 无审批单号，按 WRITEBACK_REQUIRE_DOC_ID=false 用占位单号 '
            f'{placeholder} 继续（仅演示用，真实审批系统会拒收）'
        )
        return placeholder

    def _predict_is_mock(self) -> bool:
        """
        【辅助函数】预判本次回写是否会走 mock 通道
        功能：评论正文里需要写一句"未真正送达审批系统"的提示，而这句提示取决于
             Adapter 是真实还是 mock。Adapter 的选择由配置决定（`build_adapter`），
             这里用同一套规则预判，保证与 `node_adapter_send` 实际用的 Adapter 一致。
        说明：配置非法（如关闭了 mock 开关却没配地址）时回写注定失败，
             但**评论本身照样要能构建出来**，所以这里吞掉异常并返回 False。
        :return: 是否 mock
        """
        if self._adapter_override is not None:
            return bool(getattr(self._adapter_override, 'is_mock', False))
        try:
            return bool(getattr(build_adapter(self.config), 'is_mock', False))
        except Exception:
            return False

    def _step_7_print_stats(self, db_task_id: int, task: Dict[str, Any],
                            risk_items: List[Dict[str, Any]], external_doc_id: str,
                            idempotency_key: str, highlights: List[str],
                            comment_length: int) -> None:
        """
        【步骤7】输出统计日志
        """
        self.log_step('任务ID', str(db_task_id))
        self.log_step('合同名称', task.get('contract_name') or '(未命名)')
        self.log_step('审批单号', external_doc_id)
        self.log_step('综合风险', task.get('risk_level') or '未评估')
        self.log_step('风险项', f'{len(risk_items)} 条')
        self.log_step('评论长度', f'{comment_length} 字')
        self.log_step('幂等键', idempotency_key)

        if highlights:
            self.log_step('重点关注', f'{len(highlights)} 条')

        if not risk_items:
            self.logger.info(
                '本次审查未检出风险 —— 评论仍会照常回写'
                '（告知审批方"已审查"这件事本身有意义）'
            )


if __name__ == '__main__':
    # 激活日志
    setup_logging()

    import json
    import tempfile
    from pathlib import Path

    from sqlalchemy import select

    from db.constants import ReviewStatus, TaskEvent
    from db.models import Clause, ContractMeta, ContractTask, RiskItem, TaskEventLog
    from db.session import init_db, init_engine, reset_engine, session_scope
    from processor.writeback_processor.config import WritebackConfig

    node_build = NodeBuildComment()
    passed = 0

    work_dir = Path(tempfile.mkdtemp(prefix='node_build_comment_test_'))
    reset_engine()
    init_engine(f'sqlite:///{work_dir.as_posix()}/build_comment_test.db')
    init_db()

    def _create_task(name: str, **overrides) -> int:
        """建一个任务"""
        fields = {
            'contract_name': name, 'biz_type': '采购', 'source_type': 'approve_push',
            'external_doc_id': f'APV-{name}', 'file_type': 'docx',
            'review_status': ReviewStatus.COMPLETED, 'risk_level': 'high',
        }
        fields.update(overrides)
        with session_scope() as session:
            session.add(ContractTask(**fields))
        with session_scope() as session:
            return session.scalars(
                select(ContractTask).where(ContractTask.contract_name == name)
            ).first().id

    def _seed_task_content(task_id: int) -> None:
        """给任务补上条款、元数据、风险项与审查事件"""
        with session_scope() as session:
            session.add(Clause(task_id=task_id, clause_type='payment', title='第二条 付款方式',
                               content='货到即付全款，未设验收条件。', page_no=1,
                               paragraph_index=1, seq=0))
            session.add(ContractMeta(task_id=task_id, contract_no='CG-2026-0001',
                                     party_a='某某科技有限公司', party_b='某某软件股份有限公司',
                                     credit_code='91310000MA1FL5678Y', amount=1280000,
                                     currency='CNY'))
        with session_scope() as session:
            clause_id = session.scalars(select(Clause).where(Clause.task_id == task_id)).first().id
            session.add_all([
                RiskItem(task_id=task_id, clause_id=clause_id, dimension='payment',
                         risk_type='timing', risk_level='high', cause='付款未设验收前置条件',
                         legal_basis='《民法典》第626条',
                         suggestion_text='甲方应在验收合格后 15 个工作日内付款。',
                         source='rule', adopt_status='adopted',
                         manual_comment='必须改为验收后付款'),
                RiskItem(task_id=task_id, clause_id=None, dimension='subject',
                         risk_type='missing', risk_level='mid', cause='相对方地址缺失',
                         source='llm'),
            ])
            session.add(TaskEventLog(
                task_id=task_id, event=TaskEvent.REVIEW_OK,
                from_status=ReviewStatus.REVIEWING, to_status=ReviewStatus.COMPLETED,
                operator='system',
                detail_json=json.dumps({
                    'review_conclusion': '建议拒绝 / 重大整改后再审',
                    'review_note': '深度语义审查未完成（LLM 降级）',
                    'overall_risk_level': 'high',
                }, ensure_ascii=False),
            ))

    # ---------- 一、正常构建 ----------
    logging.getLogger().info('=' * 20 + ' 一、正常构建 ' + '=' * 20)

    task_id = _create_task('正常构建', external_doc_id='APV-T-001')
    _seed_task_content(task_id)

    state = get_default_state()
    state['task_id'] = str(task_id)
    # 注入 mock adapter，避免依赖 .env 里是否有审批地址
    from processor.writeback_processor.adapter.approve_adapter import MockApproveAdapter
    result = NodeBuildComment(adapter=MockApproveAdapter(reason='测试'))(state)

    assert result['comment_md'].startswith('## 合同审查意见（系统生成）'), '应以 SPEC 10.1 的标题开头'
    assert '建议拒绝 / 重大整改后再审' in result['comment_md'], '结论应取自事件快照'
    assert result['risk_count'] == 2, f"应载入 2 条风险项，实际 {result['risk_count']}"
    assert result['external_doc_id'] == 'APV-T-001'
    assert result['idempotency_key'] == f'{task_id}:APV-T-001', '幂等键应为 task_id + 审批单号'
    assert result['task']['review_status'] == 'completed'
    assert result['meta']['party_a'] == '某某科技有限公司'
    assert len(result['clauses']) == 1
    # 风险项应挂上条款信息
    payment_item = next(i for i in result['risk_items'] if i['dimension'] == 'payment')
    assert payment_item['clause_title'] == '第二条 付款方式'
    assert payment_item['page_no'] == 1
    assert payment_item['manual_comment'] == '必须改为验收后付款'
    # 结论与覆盖说明都应进评论
    assert any('覆盖提示' in h and 'LLM 降级' in h for h in result['highlights']), \
        '审查阶段的覆盖说明必须带进评论'
    passed += 1
    logging.getLogger().info('  ✅ 评论按 SPEC 10.1 构建，结论取自事件快照，幂等键正确')

    # ---------- 二、当前评论长度统计（用于日志）----------
    logging.getLogger().info('=' * 20 + ' 二、评论长度 ' + '=' * 20)

    assert len(result['comment_md']) > 200, '评论应包含明细表，长度不可能太短'
    assert '| 等级 | 风险点 | 涉及条款 | 页码 | 建议 |' in result['comment_md'], '缺少风险明细表头'
    assert 'P1' in result['comment_md'], '应带页码'
    assert '整份合同' in result['comment_md'], '文档级风险应显示"整份合同"'
    passed += 1
    logging.getLogger().info('  ✅ 评论含风险明细表、页码与文档级风险标注')

    # ---------- 三、无审批单号 → 前置条件不满足 ----------
    logging.getLogger().info('=' * 20 + ' 三、无审批单号 ' + '=' * 20)

    upload_task_id = _create_task('本地上传', source_type='upload', external_doc_id=None)
    _seed_task_content(upload_task_id)

    bad_state = get_default_state()
    bad_state['task_id'] = str(upload_task_id)
    try:
        node_build(bad_state)
    except Exception as e:
        assert isinstance(e, WritebackProcessError), '应被基类包装'
        assert isinstance(e.cause, ValidationError), f'应抛 ValidationError，实际 {type(e.cause).__name__}'
        assert 'external_doc_id' in str(e) or '审批单号' in str(e), '报错应说明缺什么'
        assert '导出报告' in str(e), '报错应给出替代做法（上传来源改走报告导出）'
    else:
        raise AssertionError('无审批单号时不应继续')
    passed += 1
    logging.getLogger().info('  ✅ 无审批单号 → ValidationError 并给出替代做法')

    # 关掉开关后应放行，用占位单号
    loose_state = get_default_state()
    loose_state['task_id'] = str(upload_task_id)
    loose = NodeBuildComment(
        config=WritebackConfig(require_external_doc_id=False),
        adapter=MockApproveAdapter(reason='测试'),
    )(loose_state)
    assert loose['external_doc_id'] == f'MANUAL-{upload_task_id}'
    assert loose['idempotency_key'] == f'{upload_task_id}:MANUAL-{upload_task_id}'
    passed += 1
    logging.getLogger().info('  ✅ 关闭开关后允许占位单号（仅演示用）')

    # ---------- 四、任务不存在 ----------
    logging.getLogger().info('=' * 20 + ' 四、任务不存在 ' + '=' * 20)

    orphan_state = get_default_state()
    orphan_state['task_id'] = '999999'
    try:
        node_build(orphan_state)
    except Exception as e:
        assert isinstance(e.cause, ValidationError), '任务不存在应抛 ValidationError'
        assert '不存在' in str(e)
    else:
        raise AssertionError('任务不存在时不应继续')
    passed += 1
    logging.getLogger().info('  ✅ 任务不存在 → ValidationError')

    # ---------- 五、无风险 / 无元数据 ----------
    logging.getLogger().info('=' * 20 + ' 五、无风险与无元数据 ' + '=' * 20)

    clean_task_id = _create_task('干净合同', external_doc_id='APV-T-CLEAN',
                                 risk_level='low')
    clean_state = get_default_state()
    clean_state['task_id'] = str(clean_task_id)
    clean = NodeBuildComment(adapter=MockApproveAdapter(reason='测试'))(clean_state)

    assert clean['risk_count'] == 0
    assert '未检出风险' in clean['comment_md'], '无风险时应明确写出'
    assert any('未检出风险不等于无风险' in h for h in clean['highlights']), \
        '无风险时必须提醒人工复核'
    assert clean['meta'] == {}, '无元数据时应为空字典，不报错'
    # 没有事件快照 → 结论按风险等级兜底映射
    assert '可通过' in clean['comment_md'], f"低风险结论应兜底为可通过：{clean['comment_md']}"
    passed += 1
    logging.getLogger().info('  ✅ 无风险 + 无元数据 + 无事件快照 → 正常构建，结论兜底映射')

    # ---------- 六、mock 通道提示 ----------
    logging.getLogger().info('=' * 20 + ' 六、mock 通道提示 ' + '=' * 20)

    assert any('mock' in h for h in result['highlights']), \
        '注入 mock Adapter 时，评论里必须提示"未真正送达审批系统"'
    passed += 1
    logging.getLogger().info('  ✅ mock 通道会在评论里明确提示未真正送达')

    # ---------- 七、异常场景 ----------
    logging.getLogger().info('=' * 20 + ' 七、异常场景 ' + '=' * 20)

    for case_name, bad_task_id in (('任务ID为空', ''), ('任务ID非数字', 'uuid-abc')):
        bad = get_default_state()
        bad['task_id'] = bad_task_id
        try:
            node_build(bad)
        except Exception as e:
            assert isinstance(e.cause, StateFieldError), \
                f'{case_name} 应抛 StateFieldError，实际 {type(e.cause).__name__}'
            passed += 1
            logging.getLogger().info(f'  ✅ {case_name:<10} → 正确抛出 StateFieldError')
        else:
            raise AssertionError(f'{case_name} 未抛出异常')

    reset_engine()
    logging.getLogger().info(f'node_build_comment 自测全部通过 ✅ 共 {passed} 个用例')
