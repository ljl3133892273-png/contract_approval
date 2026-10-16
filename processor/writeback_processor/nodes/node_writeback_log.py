'''processor/writeback_processor/nodes/node_writeback_log.py'''

'''
步骤分解
1. 接收状态: 取 `task_id` 与 `writeback_status`（node_adapter_send 的结论）。
2. 开启事务: 全部写操作包在一个事务里，任何一步失败整体回滚。
3. 推进回写状态机: success → WRITE_OK 到终态；失败 → WRITE_FAIL 到 failed（可重试）。
4. 落库评论记录: review_comment 的 status / attempt_count / external_comment_id / error_message。
5. 写审计事件: WRITE_OK / WRITE_FAIL，附上下文快照。
6. 输出统计日志。

需求分析
输入:
- `writeback_status`（adapter_send 判定）/ `writeback_attempts` / `external_comment_id` / `writeback_error`
- `writeback_duplicated` / `writeback_is_mock`

输出（写库）:
- contract_task.writeback_status
- review_comment 的最终状态
- task_event_log：WRITE_OK / WRITE_FAIL

为什么把"置 writing"和"落最终状态"拆成两个节点:
    回写是一个耗时的外部调用（最坏 3 次 × 30s 超时）。
    如果只在最后写一次状态，前端在整个回写期间看到的都是 `not_written`，
    既没法区分"还没点过写回"和"正在写回"，也无法给用户进度反馈。
    所以 `node_adapter_send` 先落 `writing`，本节点再落终态 ——
    这也正是 SPEC 5.3 把 `writing` 单独列为一个状态的意义。

关于主状态:
    **本节点绝不触碰 `review_status`**。SPEC 11.1 明确：回写失败不回退主状态。
    审查结果本身是有效的，只是没能送到审批系统而已。
'''

import json
import logging
from typing import Dict, Tuple

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from db.constants import TaskEvent, WritebackStatus
from db.models import ContractTask, ReviewComment, TaskEventLog
from db.session import session_scope
from processor.writeback_processor.base import BaseNode, setup_logging
from processor.writeback_processor.exceptions import (
    StateFieldError,
    StateTransitionError,
    StorageError,
)
from processor.writeback_processor.state import WritebackGraphState, get_default_state
from utils.flow_state_machine import FlowStateError, FlowStateMachine


class NodeWritebackLog(BaseNode):
    '''回写落库节点：固化回写终态 + 评论记录 + 审计事件'''
    name = 'node_writeback_log'

    def process(self, state: WritebackGraphState) -> WritebackGraphState:
        """
        节点：回写落库（node_writeback_log）
        整体流程：取输入 → 开事务 → 推进状态机 → 落评论记录 → 写审计事件 → 统计
        核心目的：把"这次到底送出去了没有"如实固化成可查询、可审计的记录
        必要参数：task_id
        更新参数：writeback_status（以库中落地值为准）

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取并校验输入=======
        db_task_id = self._step_1_get_task_id(state)
        final_status = self._step_1_resolve_status(state)

        try:
            with session_scope() as session:
                # =======步骤二~三:推进状态机 + 落评论记录=======
                from_status, to_status = self._step_2_finalize(
                    session, db_task_id, final_status, state
                )

                # =======步骤四:写审计事件=======
                event = self._step_3_log_event(session, db_task_id, from_status, to_status, state)

        except SQLAlchemyError as e:
            raise StorageError(message=f'回写结果落库失败：{e}', cause=e)

        # =======步骤五:写回状态 + 输出统计日志=======
        state['writeback_from_status'] = from_status
        state['writeback_status'] = to_status
        self._step_4_print_stats(db_task_id, to_status, event, state)

        return state

    def _step_1_get_task_id(self, state: WritebackGraphState) -> int:
        """
        【步骤1】获取并校验任务ID
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

    @staticmethod
    def _step_1_resolve_status(state: WritebackGraphState) -> str:
        """
        【步骤1附带】确定本次要落地的回写终态
        功能：优先用 adapter_send 写入的 writeback_status；
             单独跑本节点时（状态里没有）按"有无错误"推导
        :param state: 项目状态字典
        :return: success / failed
        """
        status = (state.get('writeback_status') or '').strip()
        if status in (WritebackStatus.SUCCESS, WritebackStatus.FAILED):
            return status
        if status == WritebackStatus.WRITING:
            # 状态里是 writing（未走到 adapter_send 的收尾）→ 按是否有错误推导
            return WritebackStatus.FAILED if state.get('writeback_error') else WritebackStatus.SUCCESS
        # 完全没有信息：有尝试次数且无错误视为成功，否则失败
        if state.get('writeback_attempts'):
            return WritebackStatus.FAILED if state.get('writeback_error') else WritebackStatus.SUCCESS
        return WritebackStatus.FAILED

    def _step_2_finalize(self, session, db_task_id: int, final_status: str,
                         state: WritebackGraphState) -> Tuple[str, str]:
        """
        【步骤2~3】推进回写状态机并落评论记录
        :param session: 数据库会话
        :param db_task_id: 任务ID
        :param final_status: 目标终态（success / failed）
        :param state: 项目状态字典
        :return: (迁移前状态, 迁移后状态)
        """
        task = session.get(ContractTask, db_task_id)
        if task is None:
            raise StateFieldError(
                field_name='task_id', message=f'任务 {db_task_id} 不存在，无法落库回写结果'
            )

        from_status = task.writeback_status or WritebackStatus.NOT_WRITTEN
        event = (TaskEvent.WRITE_OK if final_status == WritebackStatus.SUCCESS
                 else TaskEvent.WRITE_FAIL)

        try:
            to_status = FlowStateMachine.writeback_transition(from_status, event)
        except FlowStateError as e:
            raise StateTransitionError(message=str(e), cause=e)

        task.writeback_status = to_status
        # 注意：**不碰 review_status** —— 回写失败不回退主状态（SPEC 11.1）

        # 评论记录：终态 + 尝试次数 + 对端评论 ID + 错误信息
        comment = session.scalars(
            select(ReviewComment).where(ReviewComment.task_id == db_task_id)
        ).first()
        if comment is None:
            # 理论上 adapter_send 已建过；单独跑本节点时兜底
            comment = ReviewComment(task_id=db_task_id)
            session.add(comment)

        comment.status = to_status
        comment.attempt_count = int(state.get('writeback_attempts') or 0)
        comment.external_comment_id = (state.get('external_comment_id') or '') or None
        comment.error_message = (state.get('writeback_error') or '')[:512] or None

        session.flush()
        return from_status, to_status

    def _step_3_log_event(self, session, db_task_id: int, from_status: str,
                          to_status: str, state: WritebackGraphState) -> str:
        """
        【步骤4】写审计事件
        :param session: 数据库会话
        :param db_task_id: 任务ID
        :param from_status: 迁移前状态
        :param to_status: 迁移后状态
        :param state: 项目状态字典
        :return: 本次事件名
        """
        event = (TaskEvent.WRITE_OK if to_status == WritebackStatus.SUCCESS
                 else TaskEvent.WRITE_FAIL)

        session.add(TaskEventLog(
            task_id=db_task_id,
            event=event,
            from_status=from_status,
            to_status=to_status,
            operator=state.get('operator') or 'system',
            detail_json=json.dumps({
                'node': self.name,
                'external_doc_id': state.get('external_doc_id') or '',
                'idempotency_key': state.get('idempotency_key') or '',
                'external_comment_id': state.get('external_comment_id') or '',
                'attempt_count': state.get('writeback_attempts') or 0,
                'duplicated': bool(state.get('writeback_duplicated')),
                'is_mock': bool(state.get('writeback_is_mock')),
                'risk_count': state.get('risk_count') or 0,
                'error': state.get('writeback_error') or '',
            }, ensure_ascii=False),
        ))
        session.flush()
        return event

    def _step_4_print_stats(self, db_task_id: int, status: str, event: str,
                            state: WritebackGraphState) -> None:
        """
        【步骤5】输出统计日志
        """
        self.log_step('任务ID', str(db_task_id))
        self.log_step('回写状态', status)
        self.log_step('事件', event)
        self.log_step('尝试次数', str(state.get('writeback_attempts') or 0))

        if status == WritebackStatus.SUCCESS:
            if state.get('writeback_is_mock'):
                self.logger.warning(
                    '回写标记为成功，但走的是 mock 通道 —— 审批系统并未真正收到该意见'
                )
            if state.get('writeback_duplicated'):
                self.log_step('幂等', '对端判定为重复提交（同幂等键此前已成功）')
        else:
            self.logger.warning(
                f"回写失败已落库（原因：{state.get('writeback_error') or '未知'}），"
                f"主状态保持不变，可发起 RETRY_WRITE 重试"
            )


if __name__ == '__main__':
    # 激活日志
    setup_logging()

    import tempfile
    from pathlib import Path

    from db.constants import ReviewStatus
    from db.session import init_db, init_engine, reset_engine

    passed = 0

    work_dir = Path(tempfile.mkdtemp(prefix='node_writeback_log_test_'))
    reset_engine()
    init_engine(f'sqlite:///{work_dir.as_posix()}/writeback_log_test.db')
    init_db()

    def _create_task(name: str, **overrides) -> int:
        """建一个任务"""
        fields = {
            'contract_name': name, 'source_type': 'approve_push',
            'external_doc_id': f'APV-{name}', 'review_status': ReviewStatus.COMPLETED,
            'risk_level': 'high', 'writeback_status': WritebackStatus.WRITING,
        }
        fields.update(overrides)
        with session_scope() as session:
            session.add(ContractTask(**fields))
        with session_scope() as session:
            return session.scalars(
                select(ContractTask).where(ContractTask.contract_name == name)
            ).first().id

    def _state(task_id: int, **overrides) -> WritebackGraphState:
        """构造状态"""
        state = get_default_state()
        state['task_id'] = str(task_id)
        state['external_doc_id'] = f'APV-T-{task_id}'
        state['idempotency_key'] = f'{task_id}:APV-T-{task_id}'
        state['comment_md'] = '## 合同审查意见（系统生成）'
        state.update(overrides)
        return state

    def _run(task_id: int, **overrides) -> WritebackGraphState:
        """跑一遍落库节点"""
        return NodeWritebackLog()(_state(task_id, **overrides))

    # ---------- 一、成功落库 ----------
    logging.getLogger().info('=' * 18 + ' 一、成功落库 ' + '=' * 18)

    task_id = _create_task('成功落库')
    with session_scope() as session:
        session.add(ReviewComment(task_id=task_id, content_md='评论正文',
                                  status=WritebackStatus.WRITING))
    result = _run(task_id, writeback_status='success', writeback_attempts=2,
                  external_comment_id='CMT-xyz', writeback_error='')

    assert result['writeback_status'] == 'success'
    assert result['writeback_from_status'] == 'writing'
    with session_scope() as session:
        task = session.get(ContractTask, task_id)
        assert task.writeback_status == 'success', f'实际 {task.writeback_status}'
        assert task.review_status == 'completed', '本节点绝不应触碰主状态'
        comment = session.scalars(
            select(ReviewComment).where(ReviewComment.task_id == task_id)
        ).first()
        assert comment.status == 'success'
        assert comment.attempt_count == 2
        assert comment.external_comment_id == 'CMT-xyz'
        assert comment.error_message is None, '成功时应清空错误信息'
        events = session.scalars(
            select(TaskEventLog).where(TaskEventLog.task_id == task_id)
        ).all()
        assert [e.event for e in events] == ['WRITE_OK']
        assert events[0].from_status == 'writing' and events[0].to_status == 'success'
        detail = json.loads(events[0].detail_json)
        assert detail['external_comment_id'] == 'CMT-xyz'
        assert detail['attempt_count'] == 2
    passed += 1
    logging.getLogger().info('  ✅ 成功：状态机 writing → success，评论与事件全部落库')

    # ---------- 二、失败落库（不回退主状态）----------
    logging.getLogger().info('=' * 18 + ' 二、失败落库 ' + '=' * 18)

    fail_task_id = _create_task('失败落库')
    with session_scope() as session:
        session.add(ReviewComment(task_id=fail_task_id, content_md='评论正文',
                                  status=WritebackStatus.WRITING))
    result = _run(fail_task_id, writeback_status='failed', writeback_attempts=3,
                  writeback_error='审批接口返回 503：service unavailable')

    assert result['writeback_status'] == 'failed'
    with session_scope() as session:
        task = session.get(ContractTask, fail_task_id)
        assert task.writeback_status == 'failed'
        assert task.review_status == 'completed', \
            '回写失败绝不能回退主状态（SPEC 11.1）—— 审查结果本身是有效的'
        comment = session.scalars(
            select(ReviewComment).where(ReviewComment.task_id == fail_task_id)
        ).first()
        assert comment.status == 'failed'
        assert comment.attempt_count == 3
        assert '503' in comment.error_message, '错误信息应落库，便于运维排查'
        events = session.scalars(
            select(TaskEventLog).where(TaskEventLog.task_id == fail_task_id)
        ).all()
        assert [e.event for e in events] == ['WRITE_FAIL']
        assert events[0].to_status == 'failed'
    passed += 1
    logging.getLogger().info('  ✅ 失败：WRITE_FAIL 落库，主状态保持 completed')

    # ---------- 三、failed → writing → success 的重试闭环 ----------
    logging.getLogger().info('=' * 18 + ' 三、重试闭环 ' + '=' * 18)

    # 先失败
    _run(fail_task_id, writeback_status='failed', writeback_attempts=3,
         writeback_error='重试前')
    # 再模拟 adapter_send 把状态推回 writing（RETRY_WRITE）
    from db.constants import TaskEvent as _TE
    from utils.flow_state_machine import FlowStateMachine as _FSM
    with session_scope() as session:
        task = session.get(ContractTask, fail_task_id)
        task.writeback_status = _FSM.writeback_transition(task.writeback_status, _TE.RETRY_WRITE)
    # 再成功
    result = _run(fail_task_id, writeback_status='success', writeback_attempts=1,
                  external_comment_id='CMT-RETRY')
    with session_scope() as session:
        task = session.get(ContractTask, fail_task_id)
        assert task.writeback_status == 'success', '重试后应落到 success'
        comment = session.scalars(
            select(ReviewComment).where(ReviewComment.task_id == fail_task_id)
        ).first()
        assert comment.external_comment_id == 'CMT-RETRY'
        assert comment.error_message is None, '重试成功后应清空上一次的错误信息'
    passed += 1
    logging.getLogger().info('  ✅ 失败 → 重试 → 成功 的完整闭环走通')

    # ---------- 四、状态推导（单独跑本节点时）----------
    logging.getLogger().info('=' * 18 + ' 四、状态推导 ' + '=' * 18)

    derived_task_id = _create_task('状态推导')
    with session_scope() as session:
        session.add(ReviewComment(task_id=derived_task_id, content_md='x',
                                  status=WritebackStatus.WRITING))
    # 没有 writeback_status，但有 attempts 且无 error → 推导为成功
    result = _run(derived_task_id, writeback_attempts=1, writeback_error='')
    assert result['writeback_status'] == 'success', f"应推导为成功，实际 {result['writeback_status']}"

    derived_fail_id = _create_task('状态推导失败')
    with session_scope() as session:
        session.add(ReviewComment(task_id=derived_fail_id, content_md='x',
                                  status=WritebackStatus.WRITING))
    result = _run(derived_fail_id, writeback_attempts=1, writeback_error='有错误')
    assert result['writeback_status'] == 'failed'

    # 完全没有信息 → 保守判失败（宁可为 failed 也不能谎报成功）
    bare_id = _create_task('状态推导缺失')
    with session_scope() as session:
        session.add(ReviewComment(task_id=bare_id, content_md='x',
                                  status=WritebackStatus.WRITING))
    result = _run(bare_id)
    assert result['writeback_status'] == 'failed', \
        '信息缺失时应保守判 failed，绝不能谎报成功'
    passed += 1
    logging.getLogger().info('  ✅ 状态推导：有错误→failed、无错误→success、无信息→保守 failed')

    # ---------- 五、非法迁移被拒 ----------
    logging.getLogger().info('=' * 18 + ' 五、非法迁移 ' + '=' * 18)

    # 已经 success 的任务再落一次 WRITE_OK → 幂等（状态机放行）
    result = _run(task_id, writeback_status='success', writeback_attempts=1,
                  external_comment_id='CMT-xyz')
    assert result['writeback_status'] == 'success', '已是 success 时重复落库应幂等'

    # 从 not_written 直接落成功 → 非法（没经历过 writing）
    illegal_id = _create_task('非法迁移', writeback_status=WritebackStatus.NOT_WRITTEN)
    with session_scope() as session:
        session.add(ReviewComment(task_id=illegal_id, content_md='x',
                                  status=WritebackStatus.NOT_WRITTEN))
    try:
        _run(illegal_id, writeback_status='success', writeback_attempts=1)
    except Exception as e:
        from processor.writeback_processor.exceptions import WritebackProcessError
        assert isinstance(e, WritebackProcessError)
        assert isinstance(e.cause, StateTransitionError), \
            f'应抛 StateTransitionError，实际 {type(e.cause).__name__}'
        assert '回写状态机' in str(e), '报错应标明是回写状态机'
    else:
        raise AssertionError('not_written 不能直接跳到 success，应被状态机拒绝')
    passed += 1
    logging.getLogger().info('  ✅ 重复落库幂等；not_written 直接跳 success 被拒绝')

    # ---------- 六、mock 提示 ----------
    logging.getLogger().info('=' * 18 + ' 六、mock 标记 ' + '=' * 18)

    mock_task_id = _create_task('mock 落库')
    with session_scope() as session:
        session.add(ReviewComment(task_id=mock_task_id, content_md='x',
                                  status=WritebackStatus.WRITING))
    result = _run(mock_task_id, writeback_status='success', writeback_attempts=1,
                  external_comment_id='MOCK-CMT-1', writeback_is_mock=True)
    with session_scope() as session:
        event = session.scalars(
            select(TaskEventLog).where(TaskEventLog.task_id == mock_task_id)
        ).first()
        assert json.loads(event.detail_json)['is_mock'] is True, \
            '事件快照应记录是否走 mock，便于事后审计'
    passed += 1
    logging.getLogger().info('  ✅ 事件快照记录 is_mock，事后可审计')

    # ---------- 七、异常场景 ----------
    logging.getLogger().info('=' * 18 + ' 七、异常场景 ' + '=' * 18)

    for case_name, bad_task_id in (('任务ID为空', ''), ('任务ID非数字', 'uuid-abc')):
        bad = get_default_state()
        bad['task_id'] = bad_task_id
        try:
            NodeWritebackLog()(bad)
        except Exception as e:
            assert isinstance(e.cause, StateFieldError), \
                f'{case_name} 应抛 StateFieldError，实际 {type(e.cause).__name__}'
            passed += 1
            logging.getLogger().info(f'  ✅ {case_name:<10} → 正确抛出 StateFieldError')
        else:
            raise AssertionError(f'{case_name} 未抛出异常')

    try:
        _run(999999, writeback_status='success', writeback_attempts=1)
    except Exception as e:
        assert isinstance(e.cause, StateFieldError)
        assert '不存在' in str(e)
        passed += 1
        logging.getLogger().info('  ✅ 任务不存在 → StateFieldError')
    else:
        raise AssertionError('任务不存在时未抛出异常')

    reset_engine()
    logging.getLogger().info(f'node_writeback_log 自测全部通过 ✅ 共 {passed} 个用例')
