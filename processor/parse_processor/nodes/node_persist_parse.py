'''processor/parse_processor/nodes/node_persist_parse.py'''

'''
步骤分解
1. 接收状态: 取 task_id（必须是 contract_task.id）与全部解析产物。
2. 开启事务: 整条链路放在一个事务里，任何一步失败全部回滚，不留半截数据。
3. 落库任务: upsert contract_task（文件信息、来源、金额、状态）。
4. 落库元数据: upsert contract_meta（含各字段 Locator 的 raw_json）。
5. 重建条款: 先删该任务的旧条款再批量插入 —— 保证 retry 重跑时幂等不重复。
6. 写审计日志: 追加 task_event_log（PARSE_OK）。
7. 输出统计日志。

需求分析
输入:
- `task_id`：**contract_task.id**（数字字符串，由接入层建任务后传入）
- `fragments` / `clauses` / `meta` / `stored_file_path` / `file_hash` / `source_type` /
  `external_doc_id` / `page_count` 等

输出（写库）:
- contract_task：文件信息 + review_status 置 reviewing
- contract_meta：合同元数据
- clause：条款列表
- task_event_log：PARSE_OK 事件

状态约定（重要）:
    SPEC 5.2 的事件表规定 PARSE_OK 使 parsing → reviewing。
    本节点是解析子图的最后一个节点，所以离开时任务状态应为 **reviewing**
    （即「解析完成，等待/进入审查」），而不是停在 parsing。

关于外部单据号:
    `external_doc_id` 在 contract_task 上是**唯一索引**。
    多个 NULL 可以共存，多个空字符串会冲突 —— 所以这里必须把 '' 转成 None。

关于 retry:
    SPEC 11.2 要求阻塞任务可重跑。本节点对 clause 采用「先删后插」，
    对 task/meta 采用 upsert，因此整条链路可重复执行且结果一致。
'''

import json
import logging
from datetime import date
from decimal import Decimal
from typing import Any, Dict, Optional

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from db.constants import FileType, ReviewStatus, TaskEvent
from db.models import Clause, ContractMeta, ContractTask, TaskEventLog
from db.session import session_scope
from processor.parse_processor.base import BaseNode, setup_logging
from processor.parse_processor.exceptions import (
    ParseProcessError,
    StateFieldError,
    StorageError,
)
from processor.parse_processor.state import ParseGraphState, get_default_state
from utils.flow_state_machine import FlowStateMachine


class NodePersistParse(BaseNode):
    '''解析结果落库节点：把解析产物写入 contract_task / contract_meta / clause'''
    name = 'node_persist_parse'

    def process(self, state: ParseGraphState) -> ParseGraphState:
        """
        节点：解析结果落库（node_persist_parse）
        整体流程：取输入 → 开事务 → 落任务 → 落元数据 → 重建条款 → 写事件日志 → 统计
        核心目的：把内存里的解析产物固化成可查询、可审计的库记录，
                 并把任务状态推进到 reviewing（发 PARSE_OK 事件）
        必要参数：task_id（contract_task.id）
        更新参数：无（写库为主；会回填 task_db_id 便于上层核对）

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取并校验输入=======
        db_task_id = self._step_1_get_task_id(state)

        try:
            # 单个事务包住全部写操作：任何一步失败整体回滚，不留半截数据
            with session_scope() as session:
                # =======步骤二:落库任务=======
                task, status_from = self._step_2_upsert_task(session, db_task_id, state)

                # =======步骤三:落库元数据=======
                self._step_3_upsert_meta(session, db_task_id, state)

                # =======步骤四:重建条款（先删后插，保证 retry 幂等）=======
                clause_count = self._step_4_rebuild_clauses(session, db_task_id, state)

                # =======步骤五:写审计事件日志=======
                self._step_5_log_event(session, db_task_id, task, status_from)

            # =======步骤六:输出统计日志=======
            self._step_6_print_stats(db_task_id, state, clause_count)

        except SQLAlchemyError as e:
            raise StorageError(message=f'解析结果落库失败：{e}', cause=e)

        state['task_db_id'] = db_task_id
        return state

    def _step_1_get_task_id(self, state: ParseGraphState) -> int:
        """
        【步骤1】获取并校验任务ID
        功能：task_id 必须是 contract_task.id（数字），因为库里主键是自增 BIGINT
        :param state: 项目状态字典（ParseGraphState）
        :return: 整型任务ID
        """
        raw_task_id = state.get('task_id')
        if raw_task_id in (None, ''):
            raise StateFieldError(
                field_name='task_id',
                message='任务ID不能为空',
                expected_type=str
            )

        try:
            return int(raw_task_id)
        except (TypeError, ValueError):
            raise StateFieldError(
                field_name='task_id',
                message=f'task_id 必须是 contract_task.id（数字），当前值为 {raw_task_id!r}。'
                        f'接入层需先建任务，再把数据库主键作为 task_id 传入。',
                expected_type=int
            )

    def _step_2_upsert_task(self, session, db_task_id: int,
                            state: ParseGraphState):
        """
        【步骤2】落库任务（有则更新，无则创建）
        功能：写入文件信息与来源，并把 review_status 推进到 reviewing
        :param session: 数据库会话
        :param db_task_id: 任务ID
        :param state: 项目状态字典
        :return: (ContractTask 实例, 迁移前的 review_status)
        """
        task = session.get(ContractTask, db_task_id)
        is_new_task = task is None
        if is_new_task:
            # 独立运行 / 测试场景：任务行还不存在，按传入的主键补建
            task = ContractTask(id=db_task_id)
            session.add(task)
            self.log_step('创建任务', f'id={db_task_id}')

        meta = state.get('meta') or {}

        # 1.基础信息
        task.contract_name = state.get('file_title') or task.contract_name or ''
        task.source_type = state.get('source_type') or task.source_type
        # 2.外部单据号：**状态里没带就不要动**。
        #   审批单号通常由接入层建任务时写入（来自审批系统），解析状态里不一定有；
        #   如果无条件覆盖，就会把审批单号冲成 NULL，导致后续回写找不到目标单据。
        raw_doc_id = (state.get('external_doc_id') or '').strip()
        if raw_doc_id:
            task.external_doc_id = raw_doc_id
        elif is_new_task:
            # 新建任务且状态里没有单据号 → 显式置 NULL
            # （唯一索引下多个 '' 会冲突，多个 NULL 才允许共存）
            task.external_doc_id = None
        # 3.文件信息
        task.file_path = state.get('stored_file_path') or state.get('import_file_path') or task.file_path
        task.file_type = self._detect_file_type(state)
        task.file_hash = state.get('file_hash') or task.file_hash
        # 4.金额币种（来自元数据抽取）
        task.amount = self._to_decimal(meta.get('amount'))
        task.currency = meta.get('currency') or task.currency
        # 5.状态推进：PARSE_OK → reviewing（SPEC 5.2）
        #    **不直接赋值** —— SPEC 5.2 要求状态机由单一组件实现、禁止散落各处改状态字段。
        #    用宽松版：接入层没来得及发 PARSE_START（任务还在 pending）时也允许补齐，
        #    这样解析子图能被单独重跑；blocked / 终态仍会被严格拒绝。
        status_from = task.review_status or ''
        task.review_status = FlowStateMachine.resolve_main_transition(
            status_from, TaskEvent.PARSE_OK
        )
        task.blocked_reason = None  # 成功跑完解析，清掉上次的阻塞原因
        session.flush()
        return task, status_from

    def _step_3_upsert_meta(self, session, db_task_id: int, state: ParseGraphState) -> None:
        """
        【步骤3】落库元数据（有则更新，无则创建）
        :param session: 数据库会话
        :param db_task_id: 任务ID
        :param state: 项目状态字典
        """
        meta = state.get('meta')
        if not meta:
            # 元数据可能整体抽不到（前端不阻塞），此时不必建空记录
            self.logger.info('[落库] 无元数据可写，跳过 contract_meta')
            return

        record = session.scalars(
            select(ContractMeta).where(ContractMeta.task_id == db_task_id)
        ).first()
        if record is None:
            record = ContractMeta(task_id=db_task_id)
            session.add(record)

        record.contract_no = meta.get('contract_no') or None
        record.party_a = meta.get('party_a') or None
        record.party_b = meta.get('party_b') or None
        record.credit_code = meta.get('credit_code') or None
        record.amount = self._to_decimal(meta.get('amount'))
        record.currency = meta.get('currency') or None
        record.period_start = self._parse_date(meta.get('period_start'))
        record.period_end = self._parse_date(meta.get('period_end'))
        record.effective_condition = meta.get('effective_condition') or None
        # raw_json 里含各字段的 Locator，供工作台「元数据高亮」使用
        record.raw_json = meta.get('raw_json')
        session.flush()

    def _step_4_rebuild_clauses(self, session, db_task_id: int,
                                state: ParseGraphState) -> int:
        """
        【步骤4】重建条款（先删后插）
        功能：保证 retry 重跑时不会重复累积条款
        :param session: 数据库会话
        :param db_task_id: 任务ID
        :param state: 项目状态字典
        :return: 写入的条款数
        """
        clauses = state.get('clauses') or []

        # 1.先清掉该任务的旧条款（同时会级联清掉挂在条款上的风险项）
        session.execute(delete(Clause).where(Clause.task_id == db_task_id))

        # 2.批量插入新条款
        records = [
            Clause(
                task_id=db_task_id,
                clause_type=clause.get('clause_type') or 'other',
                title=(clause.get('title') or '')[:255],
                content=clause.get('content') or '',
                start_offset=clause.get('start_offset') or 0,
                end_offset=clause.get('end_offset') or 0,
                page_no=clause.get('page_no') or 0,
                paragraph_index=clause.get('paragraph_index') or 0,
                seq=clause.get('seq') or 0,
            )
            for clause in clauses
        ]
        if records:
            session.add_all(records)
        session.flush()

        return len(records)

    def _step_5_log_event(self, session, db_task_id: int, task: ContractTask,
                          status_from: str = '') -> None:
        """
        【步骤5】写审计事件日志
        功能：记录 PARSE_OK 事件（→ reviewing），全流程可追溯
        :param session: 数据库会话
        :param db_task_id: 任务ID
        :param task: 任务实例
        :param status_from: 迁移前的 review_status（接入层可能没发 PARSE_START，不能写死 parsing）
        """
        session.add(TaskEventLog(
            task_id=db_task_id,
            event=TaskEvent.PARSE_OK,
            from_status=status_from or ReviewStatus.PARSING,
            to_status=task.review_status,
            operator='system',
            detail_json=json.dumps(
                {'node': self.name, 'to_status': task.review_status},
                ensure_ascii=False
            ),
        ))
        session.flush()

    def _step_6_print_stats(self, db_task_id: int, state: ParseGraphState,
                            clause_count: int) -> None:
        """
        【步骤6】输出统计日志
        """
        meta = state.get('meta') or {}
        self.log_step('任务ID', str(db_task_id))
        self.log_step('条款落库', f'{clause_count} 条')
        self.log_step('元数据', '已写入' if meta else '无')
        self.log_step('任务状态', ReviewStatus.REVIEWING)
        if state.get('ocr_is_mock'):
            self.log_step('注意', 'OCR 结果为 mock 占位数据，不可用于真实审查')

    # ==================== 字段转换辅助 ====================

    def _detect_file_type(self, state: ParseGraphState) -> Optional[str]:
        """
        【辅助函数】由类别开关推导文件类型
        说明：权威判定来自 node_detect_type 置位的三个互斥开关
        :param state: 项目状态字典
        :return: docx / pdf / image；无法判定返回 None
        """
        if state.get('is_docx_read_enabled'):
            return FileType.DOCX
        if state.get('is_pdf_read_enabled'):
            return FileType.PDF
        if state.get('is_image_read_enabled'):
            return FileType.IMAGE
        return None

    @staticmethod
    def _to_decimal(value: Any) -> Optional[Decimal]:
        """
        【辅助函数】数值转 Decimal
        说明：金额列是 DECIMAL(20,2)，用 float 直接入库会有精度误差
        :param value: 原始数值
        :return: Decimal 或 None
        """
        if value in (None, ''):
            return None
        try:
            return Decimal(str(value))
        except Exception:
            return None

    @staticmethod
    def _parse_date(value: Any) -> Optional[date]:
        """
        【辅助函数】'YYYY-MM-DD' 字符串转 date
        :param value: 日期字符串
        :return: date 或 None（空值或格式非法都返回 None，不因元数据脏而整单失败）
        """
        if not value:
            return None
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            return None


if __name__ == '__main__':
    # 激活日志的全局配置
    setup_logging()

    import tempfile
    from pathlib import Path

    from sqlalchemy import inspect

    from db.session import drop_all, get_engine, init_db, init_engine, reset_engine, session_scope

    from processor.parse_processor.nodes.node_clean_text import NodeCleanText
    from processor.parse_processor.nodes.node_clause_split import NodeClauseSplit
    from processor.parse_processor.nodes.node_meta_extract import NodeMetaExtract

    node_persist_parse = NodePersistParse()
    passed = 0

    # 临时库
    temp_dir = Path(tempfile.mkdtemp(prefix='node_persist_parse_test_'))
    reset_engine()
    init_engine(f'sqlite:///{temp_dir.as_posix()}/persist_test.db')
    init_db()
    assert 'contract_task' in inspect(get_engine()).get_table_names()

    def _build_ready_state(task_id: str) -> ParseGraphState:
        """串起 clean_text → clause_split → meta_extract，产出可落库的完整状态"""
        state = get_default_state()
        state.update({
            'task_id': task_id,
            'source_type': 'upload',
            'file_dir': str(temp_dir / f'task-{task_id}'),
            'stored_file_path': str(temp_dir / f'task-{task_id}' / '软件采购合同.docx'),
            'file_hash': 'a' * 64,
            'is_docx_read_enabled': True,
            'fragments': [
                {'seq': 0, 'page_no': 1, 'paragraph_index': 0, 'kind': 'paragraph', 'bbox': None,
                 'text': '软件采购合同\n合同编号：CG-2026-0918-001'},
                {'seq': 1, 'page_no': 1, 'paragraph_index': 1, 'kind': 'paragraph', 'bbox': None,
                 'text': '甲方：某某科技有限公司'},
                {'seq': 2, 'page_no': 1, 'paragraph_index': 2, 'kind': 'paragraph', 'bbox': None,
                 'text': '乙方：某某软件股份有限公司\n统一社会信用代码：91310000MA1FL1234X'},
                {'seq': 3, 'page_no': 1, 'paragraph_index': 3, 'kind': 'paragraph', 'bbox': None,
                 'text': '第一条 合同金额：人民币 1,280,000.00 元（大写：壹佰贰拾捌万元整）。'},
                {'seq': 4, 'page_no': 2, 'paragraph_index': 4, 'kind': 'paragraph', 'bbox': None,
                 'text': '第二条 履行期限：自2026年1月1日起至2026年12月31日止。'},
                {'seq': 5, 'page_no': 2, 'paragraph_index': 5, 'kind': 'paragraph', 'bbox': None,
                 'text': '第三条 生效条件：本合同自双方签字盖章之日起生效。'},
            ],
        })
        state = NodeCleanText()(state)
        state = NodeClauseSplit()(state)
        return NodeMetaExtract()(state)

    # ---------- 一、正常落库 ----------
    logging.getLogger().info('=' * 20 + ' 一、正常落库 ' + '=' * 20)

    with session_scope() as session:
        pre_task = ContractTask(contract_name='待解析任务', external_doc_id='CG-2026-0918-001')
        session.add(pre_task)
    with session_scope() as session:
        db_task_id = session.scalars(
            select(ContractTask).where(ContractTask.external_doc_id == 'CG-2026-0918-001')
        ).first().id

    state = _build_ready_state(str(db_task_id))
    state['file_title'] = '软件采购合同'
    state = node_persist_parse(state)

    with session_scope() as session:
        task = session.get(ContractTask, db_task_id)
        assert task.contract_name == '软件采购合同', f"任务名称未更新：{task.contract_name}"
        assert task.review_status == 'reviewing', f"状态应推进到 reviewing，实际 {task.review_status}"
        assert task.file_type == 'docx', f"文件类型错误：{task.file_type}"
        assert task.file_hash == 'a' * 64, '文件指纹未写入'
        assert task.blocked_reason is None, '成功落库后应清空阻塞原因'
        assert task.amount == Decimal('1280000.00'), f"金额错误：{task.amount}"
        assert task.currency == 'CNY', f"币种错误：{task.currency}"
        assert task.updated_at is not None

        meta = session.scalars(
            select(ContractMeta).where(ContractMeta.task_id == db_task_id)
        ).one()
        assert meta.contract_no == 'CG-2026-0918-001'
        assert meta.party_a == '某某科技有限公司'
        assert meta.party_b == '某某软件股份有限公司'
        assert meta.credit_code == '91310000MA1FL1234X'
        assert meta.period_start == date(2026, 1, 1), f'期限起错误：{meta.period_start}'
        assert meta.period_end == date(2026, 12, 31), f'期限止错误：{meta.period_end}'
        assert meta.raw_json is not None, 'raw_json 未写入'
        # raw_json 里应含 Locator（供工作台元数据高亮）
        assert 'locators' in json.loads(meta.raw_json), 'raw_json 未包含 locators'

        clauses = session.scalars(
            select(Clause).where(Clause.task_id == db_task_id).order_by(Clause.seq)
        ).all()
        assert len(clauses) == len(state['clauses']), \
            f"条款数不一致：库 {len(clauses)} / 状态 {len(state['clauses'])}"
        assert clauses[0].content == state['clauses'][0]['content'], '条款内容不一致'
        assert clauses[0].page_no == state['clauses'][0]['page_no'], 'Locator 页码未写入'

        events = session.scalars(
            select(TaskEventLog).where(TaskEventLog.task_id == db_task_id)
        ).all()
        assert len(events) == 1 and events[0].event == 'PARSE_OK', 'PARSE_OK 事件未写入'
        assert events[0].to_status == 'reviewing'
        # from_status 必须是**真实的迁移前状态**，不能写死。
        # 本用例的任务建出来是 pending（接入层没发 PARSE_START），
        # 所以事件里就应如实记 pending —— 谎报成 parsing 会在排查状态问题时误导人。
        assert events[0].from_status == 'pending', \
            f"from_status 应是真实的前置状态 pending，实际 {events[0].from_status!r}"

    passed += 1
    logging.getLogger().info('  ✅ 任务/元数据/条款/事件日志全部落库，状态推进到 reviewing')

    # ---------- 一点五、真实 parsing 状态下 from_status 应为 parsing ----------
    logging.getLogger().info('=' * 20 + ' 一点五、from_status 真实性 ' + '=' * 20)

    with session_scope() as session:
        session.add(ContractTask(contract_name='解析中任务', source_type='upload',
                                 review_status=ReviewStatus.PARSING))
    with session_scope() as session:
        parsing_task_id = session.scalars(
            select(ContractTask).where(ContractTask.contract_name == '解析中任务')
        ).first().id

    node_persist_parse(_build_ready_state(str(parsing_task_id)))
    with session_scope() as session:
        event = session.scalars(
            select(TaskEventLog).where(TaskEventLog.task_id == parsing_task_id)
        ).first()
        assert event.from_status == 'parsing', \
            f'正常链路（pending→parsing→reviewing）下应为 parsing，实际 {event.from_status!r}'
        assert event.to_status == 'reviewing'
    passed += 1
    logging.getLogger().info('  ✅ from_status 如实记录：pending→reviewing 记 pending，parsing→reviewing 记 parsing')

    # ---------- 一点六、非法起点必须被状态机拒绝 ----------
    logging.getLogger().info('=' * 20 + ' 一点六、非法起点 ' + '=' * 20)

    with session_scope() as session:
        session.add(ContractTask(contract_name='已阻塞任务', source_type='upload',
                                 review_status=ReviewStatus.BLOCKED))
    with session_scope() as session:
        blocked_task_id = session.scalars(
            select(ContractTask).where(ContractTask.contract_name == '已阻塞任务')
        ).first().id

    try:
        node_persist_parse(_build_ready_state(str(blocked_task_id)))
    except Exception as e:
        assert 'blocked' in str(e), f'报错应说明 blocked 不能直接推进，实际 {e}'
    else:
        raise AssertionError('blocked 任务必须先 RETRY，解析节点不应放行')
    passed += 1
    logging.getLogger().info('  ✅ blocked 任务被状态机拒绝（必须先 RETRY）')

    # ---------- 二、幂等：重跑不重复累积 ----------
    logging.getLogger().info('=' * 20 + ' 二、重跑幂等 ' + '=' * 20)

    before_count = len(state['clauses'])
    state_again = _build_ready_state(str(db_task_id))
    state_again['file_title'] = '软件采购合同'
    node_persist_parse(state_again)

    with session_scope() as session:
        clause_count = len(session.scalars(
            select(Clause).where(Clause.task_id == db_task_id)
        ).all())
        meta_count = len(session.scalars(
            select(ContractMeta).where(ContractMeta.task_id == db_task_id)
        ).all())
        assert clause_count == before_count, \
            f'重跑后条款数应不变（{before_count}），实际 {clause_count}'
        assert meta_count == 1, f'重跑后 contract_meta 应仍为 1 条，实际 {meta_count}'
    passed += 1
    logging.getLogger().info('  ✅ 重跑幂等：条款与元数据未重复累积')

    # ---------- 三、空 external_doc_id 必须存 NULL ----------
    logging.getLogger().info('=' * 20 + ' 三、上传来源单据号处理 ' + '=' * 20)

    with session_scope() as session:
        upload_tasks = [ContractTask(contract_name=f'上传合同{i}') for i in range(2)]
        session.add_all(upload_tasks)
    with session_scope() as session:
        upload_ids = [t.id for t in session.scalars(
            select(ContractTask).where(ContractTask.contract_name.like('上传合同%'))
        ).all()]

    for upload_id in upload_ids:
        upload_state = _build_ready_state(str(upload_id))
        upload_state['external_doc_id'] = ''  # 上传来源没有单据号，传空串
        upload_state['file_title'] = f'上传合同{upload_id}'
        node_persist_parse(upload_state)

    with session_scope() as session:
        for upload_id in upload_ids:
            task = session.get(ContractTask, upload_id)
            assert task.external_doc_id is None, \
                f"空串必须落成 NULL（唯一索引下多个 '' 会冲突），实际 {task.external_doc_id!r}"
    passed += 1
    logging.getLogger().info('  ✅ 空单据号落成 NULL，多个上传任务可共存')

    # 3.2 审批来源：状态里没带单据号时，**绝不能冲掉接入层已写入的值**
    #     （这是端到端测试暴露的真实 bug：回写在审批单号为 NULL 时直接报"无从回写"）
    with session_scope() as session:
        session.add(ContractTask(contract_name='审批来源合同', source_type='approve_push',
                                 external_doc_id='APV-KEEP-001'))
    with session_scope() as session:
        keep_id = session.scalars(
            select(ContractTask).where(ContractTask.contract_name == '审批来源合同')
        ).first().id

    keep_state = _build_ready_state(str(keep_id))
    keep_state['source_type'] = 'approve_push'
    keep_state.pop('external_doc_id', None)  # 解析状态里没带单据号
    node_persist_parse(keep_state)

    with session_scope() as session:
        task = session.get(ContractTask, keep_id)
        assert task.external_doc_id == 'APV-KEEP-001', \
            f'接入层写入的审批单号不能被解析节点冲掉，实际 {task.external_doc_id!r}'

    # 反过来：状态里带了新单据号 → 应更新
    update_state = _build_ready_state(str(keep_id))
    update_state['external_doc_id'] = 'APV-UPDATED-002'
    node_persist_parse(update_state)
    with session_scope() as session:
        assert session.get(ContractTask, keep_id).external_doc_id == 'APV-UPDATED-002', \
            '状态里提供了单据号时应更新'
    passed += 1
    logging.getLogger().info('  ✅ 状态未提供单据号时保留接入层的值；提供了则更新')

    # ---------- 四、无元数据时不建空记录 ----------
    logging.getLogger().info('=' * 20 + ' 四、无元数据 ' + '=' * 20)

    with session_scope() as session:
        bare_task = ContractTask(contract_name='无元数据合同')
        session.add(bare_task)
    with session_scope() as session:
        bare_id = session.scalars(
            select(ContractTask).where(ContractTask.contract_name == '无元数据合同')
        ).first().id

    bare_state = get_default_state()
    bare_state.update({
        'task_id': str(bare_id),
        'file_title': '无元数据合同',
        'clauses': [{'seq': 0, 'clause_type': 'other', 'title': '全文',
                     'content': '正文内容', 'start_offset': 0, 'end_offset': 4,
                     'page_no': 1, 'paragraph_index': 0}],
    })
    node_persist_parse(bare_state)

    with session_scope() as session:
        assert session.scalars(
            select(ContractMeta).where(ContractMeta.task_id == bare_id)
        ).first() is None, '无元数据时不应建空的 contract_meta'
        assert len(session.scalars(
            select(Clause).where(Clause.task_id == bare_id)
        ).all()) == 1, '条款仍应正常落库'
    passed += 1
    logging.getLogger().info('  ✅ 无元数据时跳过 contract_meta，条款仍正常落库')

    # ---------- 五、异常场景 ----------
    logging.getLogger().info('=' * 20 + ' 五、异常场景 ' + '=' * 20)

    err_cases = [
        ('任务ID为空', '', StateFieldError),
        ('任务ID非数字', 'uuid-abc-123', StateFieldError),
    ]
    for case_name, bad_task_id, expected_exc in err_cases:
        bad_state = get_default_state()
        bad_state['task_id'] = bad_task_id
        try:
            node_persist_parse(bad_state)
        except Exception as e:
            assert isinstance(e, ParseProcessError), f'{case_name} 应被基类包装成 ParseProcessError'
            assert isinstance(e.cause, expected_exc), \
                f'{case_name} 应抛出 {expected_exc.__name__}，实际 {type(e.cause).__name__}'
            passed += 1
            logging.getLogger().info(f'  ✅ {case_name:<12} → 正确抛出 {expected_exc.__name__}')
        else:
            raise AssertionError(f'{case_name} 未抛出异常，用例失败')

    reset_engine()
    logging.getLogger().info(f'node_persist_parse 自测全部通过 ✅ 共 {passed} 个用例')
