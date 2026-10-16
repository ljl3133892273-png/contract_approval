'''processor/writeback_processor/nodes/node_adapter_send.py'''

'''
步骤分解
1. 接收状态: 取 `comment_md`、`idempotency_key`、`external_doc_id`、`task_id`。
2. 解析 Adapter: 有注入用注入，否则按配置决定真实 / mock。
3. 置"回写中": 事务内把 review_comment 落成 writing、推进回写状态机（WRITE_START）、写审计事件。
4. 发送 + 退避重试: 最多 `1 + WRITEBACK_MAX_RETRY` 次，退避 5s / 30s（SPEC 4.5）。
5. 判定结果: 成功 → success；重试耗尽或遇到不可重试错误 → failed。
6. 写回状态 + 输出统计日志。

需求分析
输入:
- `comment_md` / `idempotency_key` / `external_doc_id`（node_build_comment 产出）

输出:
- `writeback_status` / `writeback_is_mock` / `external_comment_id`
- `writeback_attempts` / `writeback_error` / `writeback_duplicated`

两条必须严格区分的错误路径（重要）:
    A. **配置错误**（审批地址没配、却把 mock 开关关了）
       → 抛 `ConfigurationError`，**让问题立刻暴露**。这种错误重试一万次也不会好，
         而且假装"回写失败"会让人以为是网络抖动，反而掩盖真因。
    B. **调用失败**（超时 / 5xx / 单据不存在）
       → **绝不抛异常给图**，记录 `writeback_status=failed` 后照常往下走。
       SPEC 11.1 明确要求「回写接口失败 → 回写状态 failed，**不回退主状态**」，
       而且报告导出是与回写解耦的：回写失败不代表这次审查白做了。

关于"不可重试"的 4xx:
    `AdapterError.retryable=False`（如 404 单据不存在、401 令牌错、422 正文非法）
    表示请求本身有问题，重试只会白等 5s + 30s。所以遇到它立即停止重试。
    日志里会明确写出"不可重试"，避免运维误以为是偶发故障。

关于重复写回:
    回写状态机里 `success` 是终态。若任务已经回写成功，本次直接短路返回成功态，
    **不再发起请求**（幂等键虽然能防住对端重复，但本地也没必要再打一次网络）。
'''

import json
import logging
import time
from typing import Any, Dict, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from db.constants import TaskEvent, WritebackStatus
from db.models import ContractTask, ReviewComment, TaskEventLog
from db.session import session_scope
from processor.writeback_processor.adapter.approve_adapter import build_adapter
from processor.writeback_processor.base import BaseNode, setup_logging
from processor.writeback_processor.exceptions import (
    AdapterError,
    ConfigurationError,
    StateFieldError,
    StateTransitionError,
    StorageError,
)
from processor.writeback_processor.state import WritebackGraphState, get_default_state
from utils.flow_state_machine import FlowStateError, FlowStateMachine


class NodeAdapterSend(BaseNode):
    '''回写发送节点：调审批系统评论接口（幂等 + 退避重试 + 状态推进）'''
    name = 'node_adapter_send'

    def __init__(self, config=None, adapter=None):
        """
        初始化节点
        :param config: 配置对象，默认使用全局配置
        :param adapter: 审批 Adapter 覆盖项（自测/联调时注入替身，避免真的打网络）
        """
        super().__init__(config)
        self._adapter_override = adapter

    def _get_adapter(self):
        """
        获取审批 Adapter
        :return: Adapter 实例
        :raises ConfigurationError: 未配置且不允许 mock
        """
        if self._adapter_override is not None:
            return self._adapter_override
        try:
            return build_adapter(self.config)
        except AdapterError as e:
            # 配置类问题：让问题立刻暴露，不要伪装成"回写失败"
            raise ConfigurationError(message=str(e), cause=e)

    def process(self, state: WritebackGraphState) -> WritebackGraphState:
        """
        节点：回写发送（node_adapter_send）
        整体流程：取输入 → 解析 Adapter → 置"回写中" → 发送重试 → 判定结果 → 写回状态
        核心目的：把审查意见真正送到审批系统，并把"送达结果"如实记录
        必要参数：comment_md / idempotency_key / external_doc_id
        更新参数：writeback_from_status、writeback_status、writeback_is_mock、
                 external_comment_id、writeback_attempts、writeback_error、writeback_duplicated

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取输入=======
        db_task_id = self._step_1_get_task_id(state)
        comment_md = (state.get('comment_md') or '').strip()
        idempotency_key = state.get('idempotency_key') or ''
        doc_id = state.get('external_doc_id') or ''
        operator = state.get('operator') or 'system'

        if not comment_md:
            raise StateFieldError(
                field_name='comment_md',
                message='评论正文为空，无法回写（应先执行 node_build_comment）',
                expected_type=str,
            )
        if not doc_id:
            raise StateFieldError(
                field_name='external_doc_id',
                message='审批单号为空，无法回写（应先执行 node_build_comment）',
                expected_type=str,
            )

        # =======短路：已经回写成功过，不再重复发起 =======
        already = self._check_already_success(db_task_id)
        if already is not None:
            self.logger.warning(
                f'任务 {db_task_id} 的回写状态已是 success，本次不再发起请求'
                f'（幂等键 {idempotency_key}）'
            )
            state.update({
                'writeback_from_status': WritebackStatus.SUCCESS,
                'writeback_status': WritebackStatus.SUCCESS,
                'writeback_is_mock': False,
                'external_comment_id': already.get('external_comment_id') or '',
                'writeback_attempts': 0,
                'writeback_error': '',
                'writeback_duplicated': True,
            })
            self._step_6_print_stats(db_task_id, doc_id, idempotency_key, state, 0)
            return state

        # =======步骤二:解析 Adapter =======
        adapter = self._get_adapter()
        is_mock = bool(getattr(adapter, 'is_mock', False))
        if is_mock:
            self.logger.warning(
                '审批系统当前为 mock Adapter，评论只会写进日志，'
                '**不会真正送达审批系统**（SPEC 8.5 的降级形态）'
            )

        # =======步骤三:置"回写中" =======
        from_status = self._step_3_mark_writing(db_task_id, comment_md)

        # =======步骤四~五:发送 + 退避重试 =======
        result, attempts, error = self._step_4_send_with_retry(
            adapter, doc_id, comment_md, idempotency_key, db_task_id, operator
        )
        status = WritebackStatus.SUCCESS if error == '' else WritebackStatus.FAILED

        # =======步骤六:写回状态 + 输出统计日志=======
        state.update({
            'writeback_from_status': from_status,
            'writeback_status': status,
            'writeback_is_mock': is_mock,
            'external_comment_id': (result or {}).get('comment_id', ''),
            'writeback_attempts': attempts,
            'writeback_error': error,
            'writeback_duplicated': bool((result or {}).get('duplicated')),
        })

        self._step_6_print_stats(db_task_id, doc_id, idempotency_key, state, attempts)

        return state

    # ==================== 步骤实现 ====================
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

    def _check_already_success(self, db_task_id: int) -> Optional[Dict[str, Any]]:
        """
        【辅助函数】检查任务是否已经回写成功
        :param db_task_id: 任务ID
        :return: 已成功时返回评论记录摘要；否则 None
        """
        with session_scope() as session:
            task = session.get(ContractTask, db_task_id)
            if task is None:
                raise StateFieldError(
                    field_name='task_id',
                    message=f'任务 {db_task_id} 不存在，无法回写',
                )
            if task.writeback_status != WritebackStatus.SUCCESS:
                return None

            comment = session.scalars(
                select(ReviewComment).where(ReviewComment.task_id == db_task_id)
            ).first()
            return {
                'external_comment_id': (comment.external_comment_id if comment else '') or '',
            }

    def _step_3_mark_writing(self, db_task_id: int, comment_md: str) -> str:
        """
        【步骤3】把回写状态置为 writing
        功能：事务内完成三件事 —— 落评论正文、推进回写状态机、写 WRITE_START 审计事件。
             先置"回写中"再发请求，前端轮询才能看到"正在写回"这个中间态。
        :param db_task_id: 任务ID
        :param comment_md: 评论正文
        :return: 迁移前的 writeback_status
        :raises StorageError: 数据库操作失败
        :raises StateTransitionError: 回写状态机不允许该迁移事件
        """
        try:
            with session_scope() as session:
                task = session.get(ContractTask, db_task_id)
                if task is None:
                    raise StateFieldError(
                        field_name='task_id', message=f'任务 {db_task_id} 不存在，无法回写'
                    )

                from_status = task.writeback_status or WritebackStatus.NOT_WRITTEN
                # 进入 writing 的事件取决于当前状态（SPEC 5.3）：
                #   not_written / writing（上次中断）→ WRITE_START
                #   failed（管理员点"重试"）        → RETRY_WRITE
                event = (TaskEvent.RETRY_WRITE if from_status == WritebackStatus.FAILED
                         else TaskEvent.WRITE_START)
                try:
                    to_status = FlowStateMachine.writeback_transition(from_status, event)
                except FlowStateError as e:
                    raise StateTransitionError(message=str(e), cause=e)

                task.writeback_status = to_status

                # 评论记录 1:1 —— 有则更新，无则创建
                comment = session.scalars(
                    select(ReviewComment).where(ReviewComment.task_id == db_task_id)
                ).first()
                if comment is None:
                    comment = ReviewComment(task_id=db_task_id)
                    session.add(comment)
                comment.content_md = comment_md
                comment.status = to_status
                comment.error_message = None

                session.add(TaskEventLog(
                    task_id=db_task_id,
                    event=event,
                    from_status=from_status,
                    to_status=to_status,
                    operator='system',
                    detail_json=_snapshot({'node': self.name, 'content_length': len(comment_md)}),
                ))
                session.flush()

            if event == TaskEvent.RETRY_WRITE:
                self.log_step('重试写回', f'{from_status} → {to_status}（RETRY_WRITE）')

            return from_status
        except SQLAlchemyError as e:
            raise StorageError(message=f'置回写状态失败：{e}', cause=e)

    def _step_4_send_with_retry(self, adapter, doc_id: str, comment_md: str,
                                idempotency_key: str, db_task_id: int, operator: str
                                ) -> Tuple[Optional[Dict[str, Any]], int, str]:
        """
        【步骤4~5】发送并退避重试
        :param adapter: 审批 Adapter
        :param doc_id: 审批单号
        :param comment_md: 评论正文
        :param idempotency_key: 幂等键
        :param db_task_id: 任务ID
        :param operator: 操作人
        :return: (成功结果或 None, 实际尝试次数, 错误信息（成功时为空字符串）)
        """
        total_attempts = max(1, self.config.total_attempts)
        last_error = ''

        for attempt in range(total_attempts):
            try:
                result = adapter.send_comment(
                    doc_id=doc_id, content_md=comment_md,
                    idempotency_key=idempotency_key,
                    task_id=str(db_task_id), operator=operator,
                )
                self.logger.info(
                    f'[回写] 第 {attempt + 1}/{total_attempts} 次尝试成功，'
                    f'评论 ID={result.get("comment_id") or "(对端未返回)"}'
                    f'{"（对端判定为重复提交）" if result.get("duplicated") else ""}'
                )
                return result, attempt + 1, ''

            except AdapterError as e:
                last_error = str(e)
                self.logger.warning(f'[回写] 第 {attempt + 1}/{total_attempts} 次尝试失败：{e}')

                # 不可重试（4xx：单据不存在 / 令牌错 / 正文非法）→ 立刻停，别白等退避
                if not e.retryable:
                    self.logger.error(
                        '[回写] 该错误标记为不可重试（对端 %s），立即停止重试', e.status_code or 'N/A'
                    )
                    return None, attempt + 1, last_error

                if attempt < total_attempts - 1:
                    delay = self.config.backoff_for(attempt)
                    self.logger.warning(f'[回写] {delay}s 后重试')
                    if delay > 0:
                        time.sleep(delay)

            except Exception as e:
                # 非 AdapterError（例如注入的替身抛了别的异常）—— 按可重试处理并记录
                last_error = str(e)
                self.logger.warning(f'[回写] 第 {attempt + 1}/{total_attempts} 次尝试异常：{e}')
                if attempt < total_attempts - 1:
                    delay = self.config.backoff_for(attempt)
                    if delay > 0:
                        time.sleep(delay)

        self.logger.error(
            f'[回写] {total_attempts} 次尝试全部失败，回写状态将置为 failed'
            f'（**主状态不受影响**，SPEC 11.1）'
        )
        return None, total_attempts, last_error

    def _step_6_print_stats(self, db_task_id: int, doc_id: str, idempotency_key: str,
                            state: WritebackGraphState, attempts: int) -> None:
        """
        【步骤6】输出统计日志
        """
        status = state.get('writeback_status') or ''
        self.log_step('任务ID', str(db_task_id))
        self.log_step('审批单号', doc_id)
        self.log_step('幂等键', idempotency_key)
        self.log_step('尝试次数', str(attempts))
        self.log_step('回写状态', status)

        if state.get('external_comment_id'):
            self.log_step('评论ID', state['external_comment_id'])
        if state.get('writeback_duplicated'):
            self.log_step('幂等', '对端判定为重复提交（该幂等键此前已成功写回）')

        if status == WritebackStatus.FAILED:
            self.logger.warning(
                f"回写失败：{state.get('writeback_error') or '未知原因'} —— "
                f"可稍后重试（RETRY_WRITE），审查结果与报告不受影响"
            )
        elif status == WritebackStatus.SUCCESS and state.get('writeback_is_mock'):
            self.logger.warning(
                '回写"成功"但走的是 mock 通道 —— 审批系统实际上没有收到这条意见'
            )


def _snapshot(payload: Dict[str, Any]) -> str:
    """把上下文快照序列化成 JSON（保证中文可读）"""
    return json.dumps(payload, ensure_ascii=False)


if __name__ == '__main__':
    # 激活日志
    setup_logging()

    import tempfile
    from pathlib import Path

    from db.constants import ReviewStatus
    from db.session import init_db, init_engine, reset_engine
    from processor.writeback_processor.adapter.approve_adapter import MockApproveAdapter
    from processor.writeback_processor.config import WritebackConfig

    passed = 0

    work_dir = Path(tempfile.mkdtemp(prefix='node_adapter_send_test_'))
    reset_engine()
    init_engine(f'sqlite:///{work_dir.as_posix()}/adapter_send_test.db')
    init_db()

    def _create_task(name: str, **overrides) -> int:
        """建一个任务"""
        fields = {
            'contract_name': name, 'source_type': 'approve_push',
            'external_doc_id': f'APV-{name}', 'review_status': ReviewStatus.COMPLETED,
            'risk_level': 'high', 'writeback_status': WritebackStatus.NOT_WRITTEN,
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
        state['comment_md'] = '## 合同审查意见（系统生成）\n- 风险等级：**高风险**'
        state['external_doc_id'] = f'APV-{task_id}'
        state['idempotency_key'] = f'{task_id}:APV-{task_id}'
        state['meta'] = {}
        state['risk_items'] = []
        state.update(overrides)
        return state

    # 退避置零，避免自测真的睡 5s / 30s
    fast_config = WritebackConfig(retry_backoff=(0, 0), max_retry_attempts=2)

    class _FlakyAdapter:
        """替身：前 N 次失败，之后成功"""

        name = 'flaky'
        is_mock = False

        def __init__(self, fail_times: int, retryable: bool = True):
            self.fail_times = fail_times
            self.retryable = retryable
            self.calls = 0

        def send_comment(self, doc_id, content_md, idempotency_key='', task_id='', operator='system'):
            self.calls += 1
            if self.calls <= self.fail_times:
                raise AdapterError(message=f'第 {self.calls} 次模拟失败',
                                   retryable=self.retryable, status_code=503 if self.retryable else 404)
            return {'comment_id': f'CMT-{self.calls}', 'duplicated': False, 'raw': {}}

    # ---------- 一、一次成功 ----------
    logging.getLogger().info('=' * 20 + ' 一、一次成功 ' + '=' * 20)

    task_id = _create_task('一次成功')
    adapter = _FlakyAdapter(fail_times=0)
    result = NodeAdapterSend(config=fast_config, adapter=adapter)(_state(task_id))

    assert result['writeback_status'] == 'success', f"应成功，实际 {result['writeback_status']}"
    assert result['writeback_attempts'] == 1
    assert result['external_comment_id'] == 'CMT-1'
    assert result['writeback_error'] == ''
    assert result['writeback_from_status'] == 'not_written'
    assert result['writeback_is_mock'] is False

    with session_scope() as session:
        task = session.get(ContractTask, task_id)
        assert task.writeback_status == 'writing', \
            '本节点只推进到 writing；success 由 node_writeback_log 落地'
        comment = session.scalars(
            select(ReviewComment).where(ReviewComment.task_id == task_id)
        ).first()
        assert comment.status == 'writing'
        assert '合同审查意见' in comment.content_md, '评论正文应先落库，前端才能轮询到'
        events = session.scalars(
            select(TaskEventLog).where(TaskEventLog.task_id == task_id)
        ).all()
        assert [e.event for e in events] == ['WRITE_START'], '应写 WRITE_START 审计事件'
        assert events[0].from_status == 'not_written' and events[0].to_status == 'writing'
    passed += 1
    logging.getLogger().info('  ✅ 一次成功：状态置 writing、评论落库、WRITE_START 事件写入')

    # ---------- 二、退避重试后成功 ----------
    logging.getLogger().info('=' * 20 + ' 二、重试后成功 ' + '=' * 20)

    retry_task_id = _create_task('重试成功')
    flaky = _FlakyAdapter(fail_times=2)
    result = NodeAdapterSend(config=fast_config, adapter=flaky)(_state(retry_task_id))

    assert result['writeback_status'] == 'success', '重试后应成功'
    assert result['writeback_attempts'] == 3, f'应尝试 3 次，实际 {result["writeback_attempts"]}'
    assert flaky.calls == 3
    passed += 1
    logging.getLogger().info('  ✅ 连续 2 次失败后第 3 次成功（退避重试生效）')

    # ---------- 三、不可重试错误 → 立即停止 ----------
    logging.getLogger().info('=' * 20 + ' 三、不可重试 ' + '=' * 20)

    no_retry_task_id = _create_task('不可重试')
    not_retryable = _FlakyAdapter(fail_times=99, retryable=False)
    result = NodeAdapterSend(config=fast_config, adapter=not_retryable)(
        _state(no_retry_task_id)
    )

    assert result['writeback_status'] == 'failed'
    assert result['writeback_attempts'] == 1, \
        f'不可重试错误应立即停止（不消耗 5s+30s 退避），实际尝试 {result["writeback_attempts"]} 次'
    assert not_retryable.calls == 1
    assert '模拟失败' in result['writeback_error']
    passed += 1
    logging.getLogger().info('  ✅ 4xx 类不可重试错误 → 只试 1 次即落 failed')

    # ---------- 四、重试耗尽 → failed，且不抛异常 ----------
    logging.getLogger().info('=' * 20 + ' 四、重试耗尽 ' + '=' * 20)

    fail_task_id = _create_task('重试耗尽')
    always_fail = _FlakyAdapter(fail_times=99)
    result = NodeAdapterSend(config=fast_config, adapter=always_fail)(_state(fail_task_id))

    assert result['writeback_status'] == 'failed', '重试耗尽应落 failed'
    assert result['writeback_attempts'] == 3, f'应尝试 3 次，实际 {result["writeback_attempts"]}'
    assert result['writeback_error'], '应记录失败原因'
    with session_scope() as session:
        task = session.get(ContractTask, fail_task_id)
        assert task.review_status == 'completed', \
            '回写失败绝不能回退主状态（SPEC 11.1）'
        assert task.writeback_status == 'writing', 'failed 由 node_writeback_log 落地'
    passed += 1
    logging.getLogger().info('  ✅ 重试耗尽 → failed 且不抛异常，主状态保持 completed')

    # ---------- 五、已经成功过 → 短路，不再发起请求 ----------
    logging.getLogger().info('=' * 20 + ' 五、已成功短路 ' + '=' * 20)

    done_task_id = _create_task('已回写成功', writeback_status=WritebackStatus.SUCCESS)
    with session_scope() as session:
        session.add(ReviewComment(task_id=done_task_id, content_md='旧评论',
                                  status=WritebackStatus.SUCCESS,
                                  external_comment_id='CMT-OLD'))
    never_called = _FlakyAdapter(fail_times=0)
    result = NodeAdapterSend(config=fast_config, adapter=never_called)(_state(done_task_id))

    assert never_called.calls == 0, '已成功的任务不应再次发起请求'
    assert result['writeback_status'] == 'success'
    assert result['external_comment_id'] == 'CMT-OLD', '应沿用已有的评论 ID'
    assert result['writeback_attempts'] == 0
    assert result['writeback_duplicated'] is True, '应标记为重复（无需再写）'
    passed += 1
    logging.getLogger().info('  ✅ 已回写成功 → 短路返回，零网络请求')

    # ---------- 六、从 failed 重试（RETRY_WRITE 路径）----------
    logging.getLogger().info('=' * 20 + ' 六、失败后重试 ' + '=' * 20)

    retry_again_task_id = _create_task('失败后重试', writeback_status=WritebackStatus.FAILED)
    recovering = _FlakyAdapter(fail_times=0)
    result = NodeAdapterSend(config=fast_config, adapter=recovering)(
        _state(retry_again_task_id)
    )
    assert result['writeback_from_status'] == 'failed'
    assert result['writeback_status'] == 'success'
    with session_scope() as session:
        task = session.get(ContractTask, retry_again_task_id)
        assert task.writeback_status == 'writing', 'failed → writing 应被状态机放行'
    passed += 1
    logging.getLogger().info('  ✅ failed → writing 的重试路径被状态机放行')

    # ---------- 七、mock Adapter ----------
    logging.getLogger().info('=' * 20 + ' 七、mock 通道 ' + '=' * 20)

    mock_task_id = _create_task('mock 通道')
    result = NodeAdapterSend(config=fast_config, adapter=MockApproveAdapter(reason='测试'))(
        _state(mock_task_id)
    )
    assert result['writeback_is_mock'] is True, 'mock 必须如实标记'
    assert result['writeback_status'] == 'success'
    assert result['external_comment_id'].startswith('MOCK-CMT-')
    passed += 1
    logging.getLogger().info('  ✅ mock 通道：状态成功但如实标记 writeback_is_mock')

    # ---------- 八、配置错误 → 抛出（不伪装成回写失败）----------
    logging.getLogger().info('=' * 20 + ' 八、配置错误 ' + '=' * 20)

    cfg_task_id = _create_task('配置错误')
    try:
        NodeAdapterSend(
            config=WritebackConfig(approve_api_base='', approve_token='',
                                  allow_mock_adapter=False),
        )(_state(cfg_task_id))
    except Exception as e:
        from processor.writeback_processor.exceptions import WritebackProcessError
        assert isinstance(e, WritebackProcessError), '应被基类包装'
        assert isinstance(e.cause, ConfigurationError), \
            f'应抛 ConfigurationError，实际 {type(e.cause).__name__}'
        assert 'WRITEBACK_ALLOW_MOCK_ADAPTER' in str(e), '报错应指明开关名'
    else:
        raise AssertionError('配置错误应立刻抛出，而不是记录成 failed')
    with session_scope() as session:
        task = session.get(ContractTask, cfg_task_id)
        assert task.writeback_status == 'not_written', \
            '配置错误发生在置 writing 之前，不应弄脏状态'
    passed += 1
    logging.getLogger().info('  ✅ 配置错误 → 立刻抛出 ConfigurationError，不弄脏回写状态')

    # ---------- 九、缺前置数据 ----------
    logging.getLogger().info('=' * 20 + ' 九、缺前置数据 ' + '=' * 20)

    for case_name, overrides, field_name in (
        ('评论为空', {'comment_md': ''}, 'comment_md'),
        ('单号为空', {'external_doc_id': ''}, 'external_doc_id'),
    ):
        missing_state = _state(_create_task(f'缺{field_name}'), **overrides)
        try:
            NodeAdapterSend(config=fast_config, adapter=_FlakyAdapter(0))(missing_state)
        except Exception as e:
            assert isinstance(e.cause, StateFieldError), f'{case_name} 应抛 StateFieldError'
            assert e.cause.field_name == field_name
            passed += 1
            logging.getLogger().info(f'  ✅ {case_name:<8} → 正确抛出 StateFieldError')
        else:
            raise AssertionError(f'{case_name} 未抛出异常')

    # 任务不存在
    try:
        NodeAdapterSend(config=fast_config, adapter=_FlakyAdapter(0))(_state(999999))
    except Exception as e:
        assert isinstance(e.cause, StateFieldError)
        assert '不存在' in str(e)
        passed += 1
        logging.getLogger().info('  ✅ 任务不存在 → StateFieldError')
    else:
        raise AssertionError('任务不存在时未抛出异常')

    reset_engine()
    logging.getLogger().info(f'node_adapter_send 自测全部通过 ✅ 共 {passed} 个用例')
