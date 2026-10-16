'''processor/review_processor/nodes/node_persist_risk.py'''

'''
步骤分解
1. 接收状态: 取 `task_id` 与合并后的 `risk_items`、综合风险与结论。
2. 开启事务: 全部写操作包在一个事务里，任何一步失败整体回滚。
3. 落库风险项: **按 (条款, 维度, 风险类型) 对账**：命中的更新、新增的插入、消失的删除。
4. 落库综合结论: 更新 contract_task 的 risk_level 与 review_status。
5. 写审计日志: LLM 正常 → REVIEW_OK（reviewing → completed）；
             LLM 整体失败 → REVIEW_FAIL（reviewing → blocked），并落阻塞原因。
6. 输出统计日志。

需求分析
输入:
- `task_id`：contract_task.id
- `risk_items`：node_risk_aggregate 合并去重后的风险项
- `overall_risk_level` / `review_conclusion` / `review_note`
- `llm_failed`：LLM 是否整体失败

输出（写库）:
- risk_item：风险项明细
- contract_task：risk_level + review_status（completed 或 blocked）
- task_event_log：REVIEW_OK / REVIEW_FAIL

为什么风险项要「对账」而不是「先删后插」（重要）:
    法务在工作台会**采纳/调整/否决**风险建议并写批注，这些落在
    risk_item.adopt_status 与 manual_comment 上。
    如果重跑审查时粗暴地"先删后插"，法务已经录入的所有意见会**全部丢失**。
    所以这里按业务键对账：命中的保留人工字段只更新系统字段，
    新增的插入，消失的才删除。

关于 LLM 失败与 blocked（SPEC 8.4）:
    "同一任务若 LLM 连续 3 次失败 → 触发 REVIEW_FAIL 事件进入 blocked，
     并落原因日志，**不阻塞规则层已产出的结论**"。
    所以这种情况下：风险项**照样落库**（规则层结果不能丢），但任务状态置 blocked，
    管理员可在列表页发起重试。
'''

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from db.constants import (
    AdoptStatus,
    ReviewStatus,
    RiskLevel,
    RiskSource,
    TaskEvent,
)
from db.models import ContractTask, RiskItem, TaskEventLog
from db.session import session_scope
from processor.review_processor.base import BaseNode, setup_logging
from processor.review_processor.exceptions import (
    ReviewProcessError,
    StateFieldError,
    StorageError,
)
from processor.review_processor.state import ReviewGraphState, get_default_state
from utils.flow_state_machine import FlowStateMachine


class NodePersistRisk(BaseNode):
    '''风险结果落库节点：风险项对账 + 综合结论回填 + 状态推进'''
    name = 'node_persist_risk'

    def process(self, state: ReviewGraphState) -> ReviewGraphState:
        """
        节点：风险结果落库（node_persist_risk）
        整体流程：取输入 → 开事务 → 风险项对账 → 回填综合结论 → 写事件日志 → 统计
        核心目的：把审查结果固化下来，并把任务推进到终态（completed）或 blocked
        必要参数：task_id
        更新参数：无（写库为主；回填 task_db_id 便于上层核对）

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取并校验输入=======
        db_task_id = self._step_1_get_task_id(state)
        risk_items = state.get('risk_items') or []
        overall_level = state.get('overall_risk_level') or RiskLevel.LOW
        llm_failed = bool(state.get('llm_failed'))

        # 是否因 LLM 失败而阻塞（SPEC 8.4）
        should_block = llm_failed and self.config.block_on_llm_failure

        try:
            with session_scope() as session:
                # =======步骤二:风险项对账=======
                stats = self._step_2_reconcile_risk_items(session, db_task_id, risk_items)

                # =======步骤三:回填综合结论与任务状态=======
                status_from = self._step_3_update_task(
                    session, db_task_id, overall_level, should_block, state
                )

                # =======步骤四:写审计事件日志=======
                event = self._step_4_log_event(
                    session, db_task_id, overall_level, should_block, state, status_from
                )

        except SQLAlchemyError as e:
            raise StorageError(message=f'审查结果落库失败：{e}', cause=e)

        # =======步骤五:输出统计日志=======
        self._step_5_print_stats(db_task_id, risk_items, stats, overall_level, event, should_block)

        state['task_db_id'] = db_task_id
        return state

    def _step_1_get_task_id(self, state: ReviewGraphState) -> int:
        """
        【步骤1】获取并校验任务ID
        :param state: 项目状态字典（ReviewGraphState）
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

    def _step_2_reconcile_risk_items(self, session, db_task_id: int,
                                     risk_items: List[Dict[str, Any]]) -> Dict[str, int]:
        """
        【步骤2】风险项对账
        功能：按 (条款, 维度, 风险类型) 与库中现有记录对账
             - 命中 → 更新系统字段，**保留人工字段（采纳状态 / 法务批注）**
             - 未命中现有 → 插入
             - 现有但本次未出现 → 删除
        :param session: 数据库会话
        :param db_task_id: 任务ID
        :param risk_items: 本次合并后的风险项
        :return: 对账统计 {'inserted':.., 'updated':.., 'deleted':..}
        """
        # 1.取现有记录并按业务键建索引
        existing_rows = session.scalars(
            select(RiskItem).where(RiskItem.task_id == db_task_id)
        ).all()
        existing_by_key: Dict[Tuple, RiskItem] = {}
        for row in existing_rows:
            existing_by_key[self._business_key(row.clause_id, row.dimension, row.risk_type)] = row

        inserted, updated = 0, 0
        matched_row_ids = set()

        for item in risk_items:
            clause_id = item.get('db_clause_id')
            dimension = item.get('dimension') or None
            risk_type = item.get('risk_type') or None
            key = self._business_key(clause_id, dimension, risk_type)

            row = existing_by_key.get(key)
            if row is not None and id(row) not in matched_row_ids:
                # 命中 → 只更新系统字段，人工字段原样保留
                self._apply_system_fields(row, item)
                matched_row_ids.add(id(row))
                updated += 1
            else:
                if row is not None:
                    # 同一业务键在本次结果里出现多次（极端情况），只保留第一条
                    self.logger.warning(
                        f'风险项业务键重复 {key}，已跳过重复条目（保留首条）'
                    )
                    continue
                session.add(self._build_new_row(db_task_id, item))
                inserted += 1

        # 2.删除本次未出现的旧记录（法务已处理的那些如果风险没了，也应清掉）
        stale_rows = [row for row in existing_rows if id(row) not in matched_row_ids]
        for row in stale_rows:
            session.delete(row)

        session.flush()
        return {'inserted': inserted, 'updated': updated, 'deleted': len(stale_rows)}

    @staticmethod
    def _business_key(clause_id: Optional[int], dimension: Optional[str],
                      risk_type: Optional[str]) -> Tuple:
        """
        【辅助函数】风险项业务键
        说明：**不含 risk_level** —— 等级变化不应该被当成"新风险"，
             否则法务已录入的采纳状态与批注会随着等级调整而丢失。
        """
        return (clause_id or 0, dimension or '', risk_type or '')

    def _apply_system_fields(self, row: RiskItem, item: Dict[str, Any]) -> None:
        """
        【辅助函数】把本次结果里的系统字段写到已有记录上
        注意：**不碰 adopt_status 与 manual_comment** —— 那是法务的人工成果。
        """
        row.risk_level = item.get('risk_level') or RiskLevel.LOW
        row.cause = (item.get('cause') or '')[:512] or None
        row.legal_basis = (item.get('legal_basis') or '')[:512] or None
        row.suggestion_text = item.get('suggestion') or row.suggestion_text
        row.matched_rule_id = item.get('matched_rule_id') or row.matched_rule_id
        row.source = item.get('source') or row.source

    def _build_new_row(self, db_task_id: int, item: Dict[str, Any]) -> RiskItem:
        """
        【辅助函数】由风险项字典构造新的 ORM 记录
        :param db_task_id: 任务ID
        :param item: 风险项字典
        :return: RiskItem 实例
        """
        return RiskItem(
            task_id=db_task_id,
            # 文档级风险（如 R-01 主体缺失）挂不到具体条款上，clause_id 为 NULL
            clause_id=item.get('db_clause_id'),
            dimension=item.get('dimension') or None,
            risk_type=item.get('risk_type') or None,
            risk_level=item.get('risk_level') or RiskLevel.LOW,
            cause=(item.get('cause') or '')[:512] or None,
            legal_basis=(item.get('legal_basis') or '')[:512] or None,
            suggestion_text=item.get('suggestion') or None,
            matched_rule_id=item.get('matched_rule_id'),
            source=item.get('source') or RiskSource.RULE,
            adopt_status=AdoptStatus.PENDING,
        )

    def _step_3_update_task(self, session, db_task_id: int, overall_level: str,
                            should_block: bool, state: ReviewGraphState) -> str:
        """
        【步骤三】回填综合结论与任务状态
        功能：写 risk_level；LLM 失败时置 blocked 并落原因，否则置 completed
        :param session: 数据库会话
        :param db_task_id: 任务ID
        :param overall_level: 综合风险等级
        :param should_block: 是否因 LLM 失败阻塞
        :param state: 项目状态字典
        :return: 迁移前的 review_status（供审计事件如实记录）
        """
        task = session.get(ContractTask, db_task_id)
        if task is None:
            # 独立运行 / 测试场景：按传入主键补建任务行
            task = ContractTask(id=db_task_id)
            session.add(task)
            self.log_step('创建任务', f'id={db_task_id}')

        task.risk_level = overall_level

        # 状态迁移统一走 FlowStateMachine（SPEC 5.2：状态机由单一组件实现，
        # 禁止散落各处直接改状态字段）。终态/blocked 的非法迁移会被它挡下来。
        event = TaskEvent.REVIEW_FAIL if should_block else TaskEvent.REVIEW_OK
        status_from = task.review_status or ''
        task.review_status = FlowStateMachine.resolve_main_transition(status_from, event)

        if should_block:
            task.blocked_reason = (
                f'LLM 深度语义审查失败（连续 {state.get("llm_fail_count") or 0} 次），'
                f'规则层结果已保留，可重试'
            )[:512]
        else:
            task.blocked_reason = None

        session.flush()
        return status_from

    def _step_4_log_event(self, session, db_task_id: int, overall_level: str,
                          should_block: bool, state: ReviewGraphState,
                          status_from: str = '') -> str:
        """
        【步骤4】写审计事件日志
        :param session: 数据库会话
        :param db_task_id: 任务ID
        :param overall_level: 综合风险等级
        :param should_block: 是否因 LLM 失败阻塞
        :param state: 项目状态字典
        :param status_from: 迁移前的 review_status（如实记录，不写死 reviewing）
        :return: 本次写入的事件名
        """
        event = TaskEvent.REVIEW_FAIL if should_block else TaskEvent.REVIEW_OK
        to_status = ReviewStatus.BLOCKED if should_block else ReviewStatus.COMPLETED

        session.add(TaskEventLog(
            task_id=db_task_id,
            event=event,
            from_status=status_from or ReviewStatus.REVIEWING,
            to_status=to_status,
            operator='system',
            detail_json=json.dumps(
                {
                    'node': self.name,
                    'overall_risk_level': overall_level,
                    'risk_item_count': len(state.get('risk_items') or []),
                    'rule_risk_count': len(state.get('rule_risk_items') or []),
                    'llm_risk_count': len(state.get('llm_risk_items') or []),
                    'llm_failed': bool(state.get('llm_failed')),
                    'llm_fail_count': state.get('llm_fail_count') or 0,
                    'llm_is_mock': bool(state.get('llm_is_mock')),
                    'review_conclusion': state.get('review_conclusion') or '',
                    'review_note': state.get('review_note') or '',
                },
                ensure_ascii=False
            ),
        ))
        session.flush()
        return event

    def _step_5_print_stats(self, db_task_id: int, risk_items: List[Dict[str, Any]],
                            stats: Dict[str, int], overall_level: str,
                            event: str, should_block: bool) -> None:
        """
        【步骤5】输出统计日志
        """
        self.log_step('任务ID', str(db_task_id))
        self.log_step(
            '风险项对账',
            f"新增 {stats['inserted']} / 更新 {stats['updated']} / 删除 {stats['deleted']}"
        )
        self.log_step('综合风险', overall_level)
        self.log_step('事件', event)

        if should_block:
            self.logger.warning(
                'LLM 审查失败 → 任务置为 blocked；规则层风险项已照常落库（SPEC 8.4）'
            )
        else:
            self.log_step('任务状态', ReviewStatus.COMPLETED)

        if stats['deleted']:
            self.logger.warning(
                f"有 {stats['deleted']} 条历史风险项在本次审查中不再出现，已删除"
                f"（对应的法务批注也一并丢失，如需保留请改为人工调整采纳状态）"
            )


if __name__ == '__main__':
    # 激活日志的全局配置
    setup_logging()

    import tempfile
    from pathlib import Path

    from sqlalchemy import inspect

    from db.models import Clause
    from db.session import get_engine, init_db, init_engine, reset_engine
    from processor.review_processor.config import ReviewConfig

    passed = 0

    # 临时库
    work_dir = Path(tempfile.mkdtemp(prefix='node_persist_risk_test_'))
    reset_engine()
    init_engine(f'sqlite:///{work_dir.as_posix()}/risk_test.db')
    init_db()
    assert 'risk_item' in inspect(get_engine()).get_table_names()

    def _create_task_with_clause(name: str) -> Tuple[int, int]:
        """建一个任务 + 一条条款，返回 (task_id, clause_id)"""
        with session_scope() as session:
            session.add(ContractTask(contract_name=name, source_type='upload',
                                     review_status='reviewing'))
        with session_scope() as session:
            task_id = session.scalars(
                select(ContractTask).where(ContractTask.contract_name == name)
            ).first().id
        with session_scope() as session:
            session.add(Clause(task_id=task_id, clause_type='payment', title='第二条 付款方式',
                               content='货到即付全款。', seq=0))
        with session_scope() as session:
            clause_id = session.scalars(
                select(Clause).where(Clause.task_id == task_id)
            ).first().id
        return task_id, clause_id

    def _risk(clause_id: Optional[int], dimension: str, risk_type: str, level: str,
              source: str = RiskSource.RULE, suggestion: str = '建议条款') -> Dict[str, Any]:
        """构造一条风险项"""
        return {
            'rule_code': 'R-04' if source == RiskSource.RULE else '',
            'matched_rule_id': None,
            'clause_id': f'cl_{clause_id:03d}' if clause_id else '',
            'db_clause_id': clause_id,
            'page_no': 1,
            'paragraph_index': 0,
            'dimension': dimension,
            'risk_type': risk_type,
            'risk_level': level,
            'cause': f'{dimension}-{risk_type} 风险',
            'evidence': '货到即付全款。',
            'legal_basis': '《民法典》',
            'suggestion': suggestion,
            'source': source,
        }

    def _run(task_id: int, **overrides) -> Dict[str, Any]:
        """跑一遍落库节点"""
        state = get_default_state()
        state['task_id'] = str(task_id)
        state.update(overrides)
        return NodePersistRisk()(state)

    # ---------- 一、正常落库 ----------
    logging.getLogger().info('=' * 18 + ' 一、正常落库 ' + '=' * 18)

    task_id, clause_id = _create_task_with_clause('正常落库')
    result = _run(
        task_id,
        risk_items=[
            _risk(clause_id, 'payment', 'timing', 'high'),
            _risk(None, 'subject', 'missing', 'high', source=RiskSource.RULE),  # 文档级
        ],
        overall_risk_level='high',
        review_conclusion='建议拒绝 / 重大整改后再审',
    )

    with session_scope() as session:
        rows = session.scalars(select(RiskItem).where(RiskItem.task_id == task_id)).all()
        assert len(rows) == 2, f'应落库 2 条风险项，实际 {len(rows)}'

        clause_row = next(r for r in rows if r.clause_id is not None)
        assert clause_row.clause_id == clause_id, '条款外键应指向数据库主键'
        assert clause_row.risk_level == 'high'
        assert clause_row.adopt_status == 'pending', '新风险项默认未处理'
        assert clause_row.source == 'rule'

        doc_row = next(r for r in rows if r.clause_id is None)
        assert doc_row.dimension == 'subject', '文档级风险项应允许 clause_id 为 NULL'

        task = session.get(ContractTask, task_id)
        assert task.review_status == 'completed', f'应置 completed，实际 {task.review_status}'
        assert task.risk_level == 'high', f'综合风险应回填，实际 {task.risk_level}'
        assert task.blocked_reason is None

        events = session.scalars(select(TaskEventLog).where(TaskEventLog.task_id == task_id)).all()
        assert len(events) == 1 and events[0].event == 'REVIEW_OK', '应写 REVIEW_OK 事件'
        assert events[0].from_status == 'reviewing' and events[0].to_status == 'completed'
    passed += 1
    logging.getLogger().info('  ✅ 风险项/综合风险/状态/事件全部落库（含文档级 NULL 外键）')

    # ---------- 二、重跑幂等 ----------
    logging.getLogger().info('=' * 18 + ' 二、重跑幂等 ' + '=' * 18)

    _run(task_id, risk_items=[_risk(clause_id, 'payment', 'timing', 'high')],
         overall_risk_level='high')
    with session_scope() as session:
        rows = session.scalars(select(RiskItem).where(RiskItem.task_id == task_id)).all()
        # 本次只报了 1 条 → 文档级那条应被删除
        assert len(rows) == 1, f'重跑后应只剩 1 条，实际 {len(rows)}'
        assert rows[0].clause_id == clause_id
    passed += 1
    logging.getLogger().info('  ✅ 重跑对账：消失的风险项被清理，无重复累积')

    # ---------- 三、保留法务的人工成果（关键）----------
    logging.getLogger().info('=' * 18 + ' 三、保留法务批注 ' + '=' * 18)

    # 模拟法务已经采纳并写了批注
    with session_scope() as session:
        row = session.scalars(select(RiskItem).where(RiskItem.task_id == task_id)).one()
        row.adopt_status = 'adopted'
        row.manual_comment = '法务意见：必须改为验收后付款'

    # 重跑审查（等级从 high 调整为 mid，suggestion 也变了）
    _run(task_id,
         risk_items=[_risk(clause_id, 'payment', 'timing', 'mid', suggestion='新的建议条款')],
         overall_risk_level='mid')

    with session_scope() as session:
        rows = session.scalars(select(RiskItem).where(RiskItem.task_id == task_id)).all()
        assert len(rows) == 1, f'业务键相同应更新而非新增，实际 {len(rows)} 条'
        row = rows[0]
        assert row.risk_level == 'mid', '系统字段（风险等级）应被更新'
        assert row.suggestion_text == '新的建议条款', '系统字段（建议）应被更新'
        # 关键断言：人工字段必须保留
        assert row.adopt_status == 'adopted', \
            f'法务的采纳状态绝不能被重跑冲掉，实际 {row.adopt_status}'
        assert row.manual_comment == '法务意见：必须改为验收后付款', '法务批注绝不能被冲掉'
    passed += 1
    logging.getLogger().info('  ✅ 等级调整时更新系统字段，法务采纳状态与批注完整保留')

    # ---------- 四、LLM 失败 → blocked，但风险项照常落库 ----------
    logging.getLogger().info('=' * 18 + ' 四、LLM 失败阻塞 ' + '=' * 18)

    block_task_id, block_clause_id = _create_task_with_clause('LLM 失败任务')
    _run(
        block_task_id,
        risk_items=[_risk(block_clause_id, 'payment', 'timing', 'high')],
        overall_risk_level='high',
        review_conclusion='建议拒绝 / 重大整改后再审',
        llm_failed=True,
        llm_fail_count=3,
        llm_is_mock=False,
    )

    with session_scope() as session:
        task = session.get(ContractTask, block_task_id)
        assert task.review_status == 'blocked', \
            f'LLM 失败应置 blocked（SPEC 8.4），实际 {task.review_status}'
        assert task.blocked_reason and 'LLM' in task.blocked_reason, '应记录阻塞原因'
        assert task.risk_level == 'high', '综合风险仍应回填'

        # 关键：规则层结果不能被丢（SPEC「不阻塞规则层已产出的结论」）
        rows = session.scalars(select(RiskItem).where(RiskItem.task_id == block_task_id)).all()
        assert len(rows) == 1, 'LLM 失败时风险项仍必须落库'

        events = session.scalars(
            select(TaskEventLog).where(TaskEventLog.task_id == block_task_id)
        ).all()
        assert events[0].event == 'REVIEW_FAIL', '应写 REVIEW_FAIL 事件'
        assert events[0].to_status == 'blocked'
    passed += 1
    logging.getLogger().info('  ✅ LLM 失败 → blocked + REVIEW_FAIL，规则层风险项仍落库')

    # 关闭阻塞开关后应照常完成（供不希望因 LLM 故障卡住流程的部署使用）
    loose_task_id, loose_clause_id = _create_task_with_clause('LLM 失败但不阻塞')
    loose_state = get_default_state()
    loose_state['task_id'] = str(loose_task_id)
    loose_state.update({
        'risk_items': [_risk(loose_clause_id, 'payment', 'timing', 'high')],
        'overall_risk_level': 'high',
        'llm_failed': True,
        'llm_fail_count': 3,
    })
    NodePersistRisk(config=ReviewConfig(block_on_llm_failure=False))(loose_state)

    with session_scope() as session:
        task = session.get(ContractTask, loose_task_id)
        assert task.review_status == 'completed', \
            f'关闭阻塞开关后应照常 completed，实际 {task.review_status}'
        assert task.blocked_reason is None, '不阻塞时不应留阻塞原因'
        rows = session.scalars(select(RiskItem).where(RiskItem.task_id == loose_task_id)).all()
        assert len(rows) == 1, '风险项仍应落库'
        events = session.scalars(
            select(TaskEventLog).where(TaskEventLog.task_id == loose_task_id)
        ).all()
        assert events[0].event == 'REVIEW_OK', '不阻塞时应发 REVIEW_OK'
    passed += 1
    logging.getLogger().info('  ✅ block_on_llm_failure=false → 照常 completed 并发 REVIEW_OK')

    # ---------- 五、无风险项 ----------
    logging.getLogger().info('=' * 18 + ' 五、无风险项 ' + '=' * 18)

    clean_task_id, _ = _create_task_with_clause('无风险任务')
    _run(clean_task_id, risk_items=[], overall_risk_level='low', review_conclusion='可通过')

    with session_scope() as session:
        task = session.get(ContractTask, clean_task_id)
        assert task.review_status == 'completed', '无风险项也应正常完成'
        assert task.risk_level == 'low'
        rows = session.scalars(select(RiskItem).where(RiskItem.task_id == clean_task_id)).all()
        assert rows == [], '无风险项时不应落库任何记录'
    passed += 1
    logging.getLogger().info('  ✅ 无风险项 → 任务 completed、risk_level=low、无风险记录')

    # ---------- 六、事件日志带上下文快照 ----------
    logging.getLogger().info('=' * 18 + ' 六、事件日志快照 ' + '=' * 18)

    with session_scope() as session:
        event = session.scalars(
            select(TaskEventLog).where(TaskEventLog.task_id == block_task_id)
        ).first()
        detail = json.loads(event.detail_json)
        assert detail['llm_failed'] is True
        assert detail['llm_fail_count'] == 3
        assert detail['overall_risk_level'] == 'high'
        assert detail['node'] == 'node_persist_risk'
    passed += 1
    logging.getLogger().info('  ✅ 事件日志含 llm_failed / 风险计数 / 结论等上下文快照')

    # ---------- 七、异常场景 ----------
    logging.getLogger().info('=' * 18 + ' 七、异常场景 ' + '=' * 18)

    for case_name, bad_task_id in (('任务ID为空', ''), ('任务ID非数字', 'uuid-abc')):
        state = get_default_state()
        state['task_id'] = bad_task_id
        try:
            NodePersistRisk()(state)
        except Exception as e:
            assert isinstance(e, ReviewProcessError), f'{case_name} 应被包装成 ReviewProcessError'
            assert isinstance(e.cause, StateFieldError), \
                f'{case_name} 应抛 StateFieldError，实际 {type(e.cause).__name__}'
            passed += 1
            logging.getLogger().info(f'  ✅ {case_name:<10} → 正确抛出 StateFieldError')
        else:
            raise AssertionError(f'{case_name} 未抛出异常，用例失败')

    reset_engine()
    logging.getLogger().info(f'node_persist_risk 自测全部通过 ✅ 共 {passed} 个用例')
