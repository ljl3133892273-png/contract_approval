'''processor/recommend_processor/nodes/node_persist_suggestion.py'''

'''
步骤分解
1. 接收状态: 取 `task_id` 与 `suggestions`（node_llm_rewrite 产出）。
2. 开启事务: 全部写操作包在一个事务里，任何一步失败整体回滚。
3. 回填建议: 按 `db_risk_id` 精确更新 risk_item.suggestion_text。
4. 保护人工成果: 法务已采纳/调整/否决或已写批注的条目**跳过回填**，不覆盖。
5. 组装说明文案（suggestion_note），供工作台提示。
6. 输出统计日志。

需求分析
输入:
- `task_id`：contract_task.id
- `suggestions`：建议列表（含 db_risk_id / suggestion_text / source）

输出（写库）:
- risk_item.suggestion_text

为什么必须保护法务的人工成果（与 node_persist_risk 同一原则）:
    法务在工作台会编辑建议条款并把它采纳进合同。如果重跑推荐时无条件覆盖
    suggestion_text，法务改过的条款会被系统建议**冲掉** —— 这是不可接受的数据丢失。
    所以判据是：`adopt_status != pending` 或 `manual_comment` 非空 → 视为"人工已处理"，
    直接跳过。

    注意本节点的判据比 node_persist_risk 更严：
    node_persist_risk 是"保留人工字段、更新系统字段"（因为它写的是风险判定结果，
    是权威结论）；而推荐建议是**可编辑产物本身**，一旦被人工改过就应完全尊重，
    连"要不要再生成"都不该由系统决定。

为什么不写 task_event_log:
    SPEC 5.2 的事件表（CREATE / PARSE_* / REVIEW_* / RETRY / WRITE_*）是封闭枚举，
    里面**没有推荐相关的状态迁移** —— 因为推荐属于审查完成后的增强动作，
    不改变主状态机（任务仍是 completed）。
    所以本节点不写事件日志，避免往状态机里塞一个规格外的迁移。
    若后续需要审计"谁在什么时候重算过建议"，应新增一个独立的事件码（另议）。

幂等性:
    按 db_risk_id 精确更新，重跑只会把 suggestion_text 更新为同样的值，
    不会产生重复行 —— 推荐是"原地更新"而不是"追加"。
'''

import logging
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from db.constants import AdoptStatus
from db.models import RiskItem
from db.session import session_scope
from processor.recommend_processor.base import BaseNode, setup_logging
from processor.recommend_processor.exceptions import (
    RecommendProcessError,
    StateFieldError,
    StorageError,
)
from processor.recommend_processor.state import RecommendGraphState, get_default_state


class NodePersistSuggestion(BaseNode):
    '''建议落库节点：把推荐结果回填到 risk_item.suggestion_text'''
    name = 'node_persist_suggestion'

    def process(self, state: RecommendGraphState) -> RecommendGraphState:
        """
        节点：建议落库（node_persist_suggestion）
        整体流程：取输入 → 开事务 → 逐条回填 → 保护人工成果 → 组装说明 → 统计日志
        核心目的：让工作台从 risk_item 就能直接读到建议条款，无需回查推荐结果
        必要参数：task_id
        更新参数：persisted_count、skipped_count、suggestion_note

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取并校验输入=======
        db_task_id = self._step_1_get_task_id(state)
        suggestions = state.get('suggestions') or []

        try:
            with session_scope() as session:
                # =======步骤二:逐条回填（事务内）=======
                stats = self._step_2_persist(session, db_task_id, suggestions)

        except SQLAlchemyError as e:
            raise StorageError(message=f'推荐建议落库失败：{e}', cause=e)

        # =======步骤三:组装说明文案=======
        note = self._step_3_build_note(state, stats)

        # =======步骤四:写回状态 + 输出统计日志=======
        state['persisted_count'] = stats['persisted']
        state['skipped_count'] = stats['skipped_manual']
        state['suggestion_note'] = note

        self._step_4_print_stats(db_task_id, suggestions, stats, note)

        return state

    def _step_1_get_task_id(self, state: RecommendGraphState) -> int:
        """
        【步骤1】获取并校验任务ID
        :param state: 项目状态字典（RecommendGraphState）
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

    def _step_2_persist(self, session, db_task_id: int,
                        suggestions: List[Dict[str, Any]]) -> Dict[str, int]:
        """
        【步骤2】逐条回填建议
        功能：按 db_risk_id 定位记录并更新 suggestion_text；
             跳过"未生成建议"、"无主键"、"跨任务"、"人工已处理"、"内容未变"的条目
        :param session: 数据库会话
        :param db_task_id: 任务ID
        :param suggestions: 建议列表
        :return: 统计 {'persisted', 'unchanged', 'skipped_manual', 'skipped_other'}
        """
        stats = {'persisted': 0, 'unchanged': 0, 'skipped_manual': 0, 'skipped_other': 0}

        # 1.一次性载入本任务的全部风险项，避免 N 次查询
        rows = session.scalars(
            select(RiskItem).where(RiskItem.task_id == db_task_id)
        ).all()
        rows_by_id = {row.id: row for row in rows}

        for suggestion in suggestions:
            db_risk_id = suggestion.get('db_risk_id')

            # 2.没有主键 → 无法定位记录（如状态传入的临时风险项）
            if db_risk_id is None:
                stats['skipped_other'] += 1
                self.logger.warning(
                    f'建议缺少 db_risk_id（风险点 {suggestion.get("clause_id") or "文档级"}），'
                    f'无法回填'
                )
                continue

            # 3.本节点只回填"有建议正文"的条目
            if not (suggestion.get('suggestion_text') or '').strip():
                stats['skipped_other'] += 1
                continue

            row = rows_by_id.get(db_risk_id)
            if row is None:
                # 跨任务或记录已被删除 → 绝不写入别的任务
                stats['skipped_other'] += 1
                self.logger.warning(
                    f'风险项 {db_risk_id} 不属于任务 {db_task_id} 或已不存在，跳过回填'
                )
                continue

            # 4.人工已处理 → 完全尊重，不覆盖（关键保护）
            if self._is_handled_by_human(row):
                stats['skipped_manual'] += 1
                self.logger.warning(
                    f'风险项 {db_risk_id} 已被法务处理'
                    f'（adopt_status={row.adopt_status}），跳过回填以免冲掉人工意见'
                )
                continue

            # 5.内容未变 → 不写库（避免无意义的 updated_at 抖动）
            new_text = suggestion['suggestion_text'].strip()
            if (row.suggestion_text or '') == new_text:
                stats['unchanged'] += 1
                continue

            row.suggestion_text = new_text
            stats['persisted'] += 1

        session.flush()
        return stats

    @staticmethod
    def _is_handled_by_human(row: RiskItem) -> bool:
        """
        【辅助函数】判断该风险项是否已被法务处理
        判据（任一成立即视为已处理）：
        - adopt_status 不是 pending（已采纳 / 已调整 / 已否决）
        - manual_comment 非空（法务写了批注）
        :param row: risk_item 记录
        :return: True 表示人工已处理，不应被系统覆盖
        """
        if (row.adopt_status or AdoptStatus.PENDING) != AdoptStatus.PENDING:
            return True
        return bool((row.manual_comment or '').strip())

    def _step_3_build_note(self, state: RecommendGraphState,
                           stats: Dict[str, int]) -> str:
        """
        【步骤3】组装说明文案
        功能：把"降级情况 + 回填情况 + 人工跳过情况"拼成一句话，供工作台提示
        :param state: 项目状态字典
        :param stats: 落库统计
        :return: 说明文案（无异常情况时为空字符串）
        """
        parts: List[str] = []

        if state.get('rewrite_is_mock'):
            parts.append('推荐改写使用了 mock 供应商，建议可能来自示范条款库而非个性化改写，不可用于生产')
        elif state.get('rewrite_failed'):
            parts.append(
                f"个性化改写未完成（LLM 失败 {state.get('rewrite_fail_count') or 0} 次），"
                f'已降级为示范条款'
            )

        if stats['skipped_manual']:
            parts.append(f"{stats['skipped_manual']} 条建议因法务已处理而未回填")

        if stats['skipped_other']:
            parts.append(f"{stats['skipped_other']} 条建议未能回填（无对应风险记录或无建议内容）")

        return '；'.join(parts)

    def _step_4_print_stats(self, db_task_id: int, suggestions: List[Dict[str, Any]],
                            stats: Dict[str, int], note: str) -> None:
        """
        【步骤4】输出统计日志
        """
        self.log_step('任务ID', str(db_task_id))
        self.log_step('建议总数', str(len(suggestions)))
        self.log_step(
            '回填结果',
            f"写入 {stats['persisted']} / 未变 {stats['unchanged']} / "
            f"人工已处理跳过 {stats['skipped_manual']} / 其他跳过 {stats['skipped_other']}"
        )

        if note:
            self.log_step('推荐说明', note)

        if stats['persisted']:
            self.log_step('完成', f"已回填 {stats['persisted']} 条建议条款")

        if not stats['persisted'] and suggestions:
            self.logger.warning('本次没有任何建议被写入（全部未变化或被跳过）')


if __name__ == '__main__':
    # 激活日志的全局配置
    setup_logging()

    import tempfile
    from pathlib import Path

    from db.models import Clause, ContractTask
    from db.session import init_db, init_engine, reset_engine

    passed = 0

    # 临时库
    work_dir = Path(tempfile.mkdtemp(prefix='node_persist_suggestion_test_'))
    reset_engine()
    init_engine(f'sqlite:///{work_dir.as_posix()}/suggestion_test.db')
    init_db()

    def _create_task(name: str) -> Tuple[int, int]:
        """建一个任务 + 一条条款，返回 (task_id, clause_id)"""
        with session_scope() as session:
            session.add(ContractTask(contract_name=name, source_type='upload',
                                     review_status='completed'))
        with session_scope() as session:
            task_id = session.scalars(
                select(ContractTask).where(ContractTask.contract_name == name)
            ).first().id
        with session_scope() as session:
            session.add(Clause(task_id=task_id, clause_type='payment',
                               title='第二条 付款方式', content='货到即付全款。', seq=0))
        with session_scope() as session:
            clause_id = session.scalars(select(Clause).where(Clause.task_id == task_id)).first().id
        return task_id, clause_id

    def _add_risk(task_id: int, clause_id: Optional[int], **overrides) -> int:
        """建一条风险项，返回其主键"""
        with session_scope() as session:
            session.add(RiskItem(task_id=task_id, clause_id=clause_id, dimension='payment',
                                 risk_type='timing', risk_level='high', cause='付款无验收前置',
                                 suggestion_text=overrides.pop('suggestion_text', '审查阶段的建议'),
                                 **overrides))
        with session_scope() as session:
            return session.scalars(
                select(RiskItem).where(RiskItem.task_id == task_id).order_by(RiskItem.id.desc())
            ).first().id

    def _suggestion(risk_id: Optional[int], text: str, **overrides) -> Dict[str, Any]:
        """构造一条建议"""
        item = {
            'index': 0,
            'db_risk_id': risk_id,
            'db_clause_id': 1,
            'clause_id': 'cl_001',
            'suggestion_text': text,
            'reason': '理由',
            'source': 'llm',
            'has_suggestion': bool(text),
        }
        item.update(overrides)
        return item

    def _run(task_id: int, suggestions: List[Dict[str, Any]], **overrides) -> Dict[str, Any]:
        """跑一遍落库节点"""
        state = get_default_state()
        state['task_id'] = str(task_id)
        state['suggestions'] = suggestions
        state.update(overrides)
        return NodePersistSuggestion()(state)

    # ---------- 一、正常回填 ----------
    logging.getLogger().info('=' * 18 + ' 一、正常回填 ' + '=' * 18)

    task_id, clause_id = _create_task('正常回填')
    risk_id = _add_risk(task_id, clause_id)

    result = _run(task_id, [_suggestion(risk_id, '甲方应在验收合格后 15 个工作日内付款。')])

    with session_scope() as session:
        row = session.get(RiskItem, risk_id)
        assert row.suggestion_text == '甲方应在验收合格后 15 个工作日内付款。', \
            f'建议应被回填，实际 {row.suggestion_text!r}'
        assert row.adopt_status == 'pending', '回填不应改动采纳状态'
    assert result['persisted_count'] == 1, f"应写入 1 条，实际 {result['persisted_count']}"
    assert result['skipped_count'] == 0
    passed += 1
    logging.getLogger().info('  ✅ 建议正确回填到 risk_item.suggestion_text')

    # ---------- 二、重跑幂等 ----------
    logging.getLogger().info('=' * 18 + ' 二、重跑幂等 ' + '=' * 18)

    result = _run(task_id, [_suggestion(risk_id, '甲方应在验收合格后 15 个工作日内付款。')])
    assert result['persisted_count'] == 0, '内容未变时不应重复写入'
    with session_scope() as session:
        rows = session.scalars(select(RiskItem).where(RiskItem.task_id == task_id)).all()
        assert len(rows) == 1, '推荐是原地更新，不应产生新记录'
    passed += 1
    logging.getLogger().info('  ✅ 重跑幂等：内容未变不写库，不产生新记录')

    # ---------- 三、保护法务的人工成果（关键）----------
    logging.getLogger().info('=' * 18 + ' 三、保护人工成果 ' + '=' * 18)

    # 3.1 法务已采纳 → 跳过
    adopt_task_id, adopt_clause_id = _create_task('法务已采纳')
    adopt_risk_id = _add_risk(adopt_task_id, adopt_clause_id, adopt_status='adopted',
                              suggestion_text='法务采纳的版本')
    result = _run(adopt_task_id, [_suggestion(adopt_risk_id, '系统新生成的建议')])

    with session_scope() as session:
        row = session.get(RiskItem, adopt_risk_id)
        assert row.suggestion_text == '法务采纳的版本', \
            f'法务已采纳的建议绝不能被覆盖，实际 {row.suggestion_text!r}'
    assert result['skipped_count'] == 1, '应计入人工跳过'
    assert result['persisted_count'] == 0
    assert '法务已处理' in result['suggestion_note'], '说明文案应提示跳过原因'
    passed += 1
    logging.getLogger().info('  ✅ 法务已采纳 → 跳过回填，人工版本完整保留')

    # 3.2 只写了批注（未改采纳状态）→ 同样跳过
    comment_task_id, comment_clause_id = _create_task('法务已批注')
    comment_risk_id = _add_risk(comment_task_id, comment_clause_id,
                                manual_comment='法务意见：必须改为验收后付款')
    result = _run(comment_task_id, [_suggestion(comment_risk_id, '系统新生成的建议')])

    with session_scope() as session:
        row = session.get(RiskItem, comment_risk_id)
        assert row.suggestion_text == '审查阶段的建议', \
            '有批注的条目不应被系统建议覆盖'
        assert row.manual_comment == '法务意见：必须改为验收后付款'
    assert result['skipped_count'] == 1
    passed += 1
    logging.getLogger().info('  ✅ 有法务批注（即使仍是 pending）→ 同样跳过回填')

    # ---------- 四、跳过条件 ----------
    logging.getLogger().info('=' * 18 + ' 四、跳过条件 ' + '=' * 18)

    skip_task_id, skip_clause_id = _create_task('跳过条件')
    skip_risk_id = _add_risk(skip_task_id, skip_clause_id)

    result = _run(skip_task_id, [
        _suggestion(None, '没有主键的建议'),           # 无 db_risk_id
        _suggestion(skip_risk_id, ''),                 # 无建议正文
        _suggestion(999999, '不存在的风险项'),          # 记录不存在
    ])

    with session_scope() as session:
        row = session.get(RiskItem, skip_risk_id)
        assert row.suggestion_text == '审查阶段的建议', '跳过时不应改动任何内容'
    assert result['persisted_count'] == 0
    passed += 1
    logging.getLogger().info('  ✅ 无主键 / 无正文 / 记录不存在 → 全部安全跳过')

    # ---------- 五、跨任务写入防护 ----------
    logging.getLogger().info('=' * 18 + ' 五、跨任务防护 ' + '=' * 18)

    other_task_id, other_clause_id = _create_task('别的任务')
    other_risk_id = _add_risk(other_task_id, other_clause_id)

    result = _run(skip_task_id, [_suggestion(other_risk_id, '来自别的任务建议')])
    with session_scope() as session:
        row = session.get(RiskItem, other_risk_id)
        assert row.suggestion_text == '审查阶段的建议', \
            '绝不能把建议写到别的任务的风险项上'
    assert result['persisted_count'] == 0
    passed += 1
    logging.getLogger().info('  ✅ 跨任务的风险项主键 → 拒绝写入（不越权改别的任务）')

    # ---------- 六、降级说明文案 ----------
    logging.getLogger().info('=' * 18 + ' 六、降级说明 ' + '=' * 18)

    note_task_id, note_clause_id = _create_task('降级说明')
    note_risk_id = _add_risk(note_task_id, note_clause_id)

    result = _run(note_task_id, [_suggestion(note_risk_id, '降级生成的建议')],
                  rewrite_is_mock=True)
    assert 'mock' in result['suggestion_note'], f"mock 时应提示，实际 {result['suggestion_note']!r}"
    assert result['persisted_count'] == 1, '降级产出的建议同样要落库'

    result = _run(note_task_id, [_suggestion(note_risk_id, '又一条建议')],
                  rewrite_failed=True, rewrite_fail_count=3)
    assert '降级为示范条款' in result['suggestion_note'], \
        f"LLM 失败时应提示降级，实际 {result['suggestion_note']!r}"
    passed += 1
    logging.getLogger().info('  ✅ 降级情况写入 suggestion_note，建议照常落库')

    # ---------- 七、无建议 ----------
    logging.getLogger().info('=' * 18 + ' 七、无建议 ' + '=' * 18)

    empty_task_id, empty_clause_id = _create_task('无建议')
    empty_risk_id = _add_risk(empty_task_id, empty_clause_id)
    result = _run(empty_task_id, [])
    assert result['persisted_count'] == 0 and result['skipped_count'] == 0
    with session_scope() as session:
        row = session.get(RiskItem, empty_risk_id)
        assert row.suggestion_text == '审查阶段的建议', '无建议时不应改动'
    passed += 1
    logging.getLogger().info('  ✅ 无建议 → 不做任何改动，不报错')

    # ---------- 八、异常场景 ----------
    logging.getLogger().info('=' * 18 + ' 八、异常场景 ' + '=' * 18)

    for case_name, bad_task_id in (('任务ID为空', ''), ('任务ID非数字', 'uuid-abc')):
        state = get_default_state()
        state['task_id'] = bad_task_id
        try:
            NodePersistSuggestion()(state)
        except Exception as e:
            assert isinstance(e, RecommendProcessError), f'{case_name} 应被包装成 RecommendProcessError'
            assert isinstance(e.cause, StateFieldError), \
                f'{case_name} 应抛 StateFieldError，实际 {type(e.cause).__name__}'
            passed += 1
            logging.getLogger().info(f'  ✅ {case_name:<10} → 正确抛出 StateFieldError')
        else:
            raise AssertionError(f'{case_name} 未抛出异常，用例失败')

    reset_engine()
    logging.getLogger().info(f'node_persist_suggestion 自测全部通过 ✅ 共 {passed} 个用例')
