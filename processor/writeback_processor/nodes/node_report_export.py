'''processor/writeback_processor/nodes/node_report_export.py'''

'''
步骤分解
1. 接收状态: 取 `task_id`。
2. 载入上下文: 任务 / 元数据 / 条款 / 风险项 / 审查结论 / 回写评论（复用 context_loader）。
3. 渲染报告: 按 SPEC 10.2 的结构生成 Markdown 正文。
4. 落盘 MD: 写入 `{REPORT_STORE_DIR}/task_{id}/report_{时间戳}.md`。
5. 生成 PDF: Markdown → PDF（reportlab + 内置中文字体）。
6. 落库 report 记录: 按配置决定是否保留历史导出。
7. 写回状态 + 输出统计日志。

需求分析
输入:
- `task_id`
- （可选）`comment_md` / `writeback_status`：同一轮图里刚构建/回写的结果

输出:
- `report_md` / `report_md_path` / `report_pdf_path` / `report_id`

为什么报告导出与回写解耦（重要）:
    SPEC 4.5 把回写与报告都归在"人工触发"，但两者的失败互不牵连：
      · 审批系统挂了 → 报告照样要能导出（内部归档不能少）
      · 报告导出失败 → 不影响已经写回审批系统的意见
    所以本节点**不读 `writeback_status` 来决定要不要干活**，
    只把它作为报告里的一个展示字段。

关于 PDF 生成失败:
    MD 是**内容本体**，PDF 只是它的一个排版格式。所以 PDF 失败时：
    保留 MD、记录一条 md 的 report 行、`report_pdf_path` 留空并告警，**不抛异常**。
    反过来（为了一个渲染格式丢掉整份报告）才是真正的因小失大。
'''

import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from sqlalchemy import delete, select
from sqlalchemy.exc import SQLAlchemyError

from db.constants import ReportFormat
from db.models import Report
from db.session import session_scope
from processor.writeback_processor.base import BaseNode, setup_logging
from processor.writeback_processor.context_loader import (
    load_review_comment,
    load_task_context,
    resolve_conclusion,
)
from processor.writeback_processor.exceptions import (
    ReportExportError,
    StateFieldError,
    StorageError,
)
from processor.writeback_processor.report_template import build_report_markdown
from processor.writeback_processor.state import WritebackGraphState, get_default_state
from utils.md_pdf_utils import md_to_pdf


class NodeReportExport(BaseNode):
    '''报告导出节点：生成 MD + PDF 报告并落库'''
    name = 'node_report_export'

    def process(self, state: WritebackGraphState) -> WritebackGraphState:
        """
        节点：报告导出（node_report_export）
        整体流程：取任务ID → 载入上下文 → 渲染报告 → 落盘 MD → 生成 PDF → 落库 → 统计
        核心目的：产出可归档、可流转的自解释报告（脱离系统也能看懂）
        必要参数：task_id
        更新参数：report_md、report_md_path、report_pdf_path、report_id

        :param state: 工作流状态对象
        :return: 更新后的状态对象
        """
        # =======步骤一:获取并校验任务ID=======
        db_task_id = self._step_1_get_task_id(state)

        try:
            # =======步骤二:载入上下文=======
            context = load_task_context(db_task_id)
            conclusion, review_note, _ = resolve_conclusion(
                db_task_id, context['task'].get('risk_level') or ''
            )
            comment = load_review_comment(db_task_id)
        except SQLAlchemyError as e:
            raise StorageError(message=f'载入报告数据失败：{e}', cause=e)

        comment_md, writeback_status = self._step_2_resolve_comment(state, comment)

        # =======步骤三:渲染报告=======
        try:
            report_md = build_report_markdown(
                task=context['task'],
                meta=context['meta'],
                risk_items=context['risk_items'],
                overall_risk_level=context['task'].get('risk_level') or '',
                review_conclusion=conclusion,
                review_note=review_note,
                review_comment_md=comment_md,
                writeback_status=writeback_status,
                title=self.config.report_title,
            )
        except Exception as e:
            raise ReportExportError(message=f'渲染报告失败：{e}', cause=e)

        # =======步骤四:落盘 MD=======
        md_path, pdf_path = self._step_4_write_files(db_task_id, report_md)

        # =======步骤五:落库 report 记录=======
        report_id = self._step_5_persist(db_task_id, md_path, pdf_path)

        # =======步骤六:写回状态 + 输出统计日志=======
        state['report_md'] = report_md
        state['report_md_path'] = md_path
        state['report_pdf_path'] = pdf_path
        state['report_id'] = report_id
        if not state.get('writeback_status'):
            state['writeback_status'] = writeback_status

        self._step_6_print_stats(db_task_id, report_md, md_path, pdf_path,
                                 len(context['risk_items']))

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

    @staticmethod
    def _step_2_resolve_comment(state: WritebackGraphState,
                                comment: Dict[str, Any]) -> Tuple[str, str]:
        """
        【步骤2附带】确定报告里要附的评论文本与回写状态
        功能：同一轮图里刚构建的 `comment_md` 优先（它就是本次写回的正文，报告与评论必须一致）；
             单独导出报告时回退到库里已保存的评论
        :param state: 项目状态字典
        :param comment: 库里的评论记录
        :return: (评论文本, 回写状态)
        """
        comment_md = (state.get('comment_md') or '').strip() or comment.get('content_md', '')
        writeback_status = (state.get('writeback_status') or '').strip() or comment.get('status', '')
        return comment_md, writeback_status

    def _step_4_write_files(self, db_task_id: int, report_md: str) -> Tuple[str, str]:
        """
        【步骤4（含 5）】落盘 MD 与 PDF
        :param db_task_id: 任务ID
        :param report_md: 报告 Markdown
        :return: (MD 路径, PDF 路径（生成失败时为空字符串）)
        """
        output_dir = Path(self.config.report_dir) / f'task_{db_task_id}'
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        md_path = output_dir / f'report_{timestamp}.md'
        pdf_path = output_dir / f'report_{timestamp}.pdf'

        # 1.Markdown 是内容本体，落盘失败属于硬错误
        try:
            md_path.parent.mkdir(parents=True, exist_ok=True)
            md_path.write_text(report_md, encoding='utf-8')
        except OSError as e:
            raise ReportExportError(message=f'报告 Markdown 落盘失败：{e}', cause=e)

        # 2.PDF 是渲染格式，失败只告警不中断（见文件头说明）
        pdf_result = ''
        try:
            pdf_result = md_to_pdf(
                report_md, str(pdf_path), title=self.config.report_title,
                font_name=self.config.report_font_name,
            )
        except Exception as e:
            self.logger.warning(
                f'PDF 生成失败（{e}）—— Markdown 报告已保存到 {md_path}，'
                f'本次仅记录 md 格式'
            )

        return str(md_path), pdf_result

    def _step_5_persist(self, db_task_id: int, md_path: str, pdf_path: str) -> int:
        """
        【步骤5】落库 report 记录
        功能：写入 md / pdf 两条记录（pdf 未生成时只写 md）；
             按 `REPORT_KEEP_HISTORY` 决定是否先清掉该任务的历史导出记录
        :param db_task_id: 任务ID
        :param md_path: MD 路径
        :param pdf_path: PDF 路径（可为空）
        :return: 最近一条 report 记录的主键
        """
        try:
            with session_scope() as session:
                if not self.config.report_keep_history:
                    removed = session.execute(
                        delete(Report).where(Report.task_id == db_task_id)
                    ).rowcount
                    if removed:
                        self.log_step('清理历史导出', f'{removed} 条（REPORT_KEEP_HISTORY=false）')

                session.add(Report(task_id=db_task_id, format=ReportFormat.MD, file_path=md_path))
                if pdf_path:
                    session.add(Report(
                        task_id=db_task_id, format=ReportFormat.PDF, file_path=pdf_path
                    ))
                session.flush()

                latest = session.scalars(
                    select(Report).where(Report.task_id == db_task_id).order_by(Report.id.desc())
                ).first()
                return latest.id if latest else 0
        except SQLAlchemyError as e:
            raise StorageError(message=f'报告记录落库失败：{e}', cause=e)

    def _step_6_print_stats(self, db_task_id: int, report_md: str, md_path: str,
                            pdf_path: str, risk_count: int) -> None:
        """
        【步骤6】输出统计日志
        """
        self.log_step('任务ID', str(db_task_id))
        self.log_step('风险项', f'{risk_count} 条')
        self.log_step('报告长度', f'{len(report_md)} 字')
        self.log_step('Markdown', md_path)
        self.log_step('PDF', pdf_path or '（未生成）')

        if not pdf_path:
            self.logger.warning('本次报告只产出了 Markdown，缺少 PDF —— 请检查 reportlab 与中文字体配置')


if __name__ == '__main__':
    # 激活日志
    setup_logging()

    import tempfile
    from pathlib import Path as _Path

    from db.constants import ReviewStatus
    from db.models import Clause, ContractMeta, ContractTask, RiskItem
    from db.session import init_db, init_engine, reset_engine, session_scope
    from processor.writeback_processor.config import WritebackConfig

    passed = 0

    work_dir = _Path(tempfile.mkdtemp(prefix='node_report_export_test_'))
    report_dir = work_dir / 'reports'
    reset_engine()
    init_engine(f'sqlite:///{work_dir.as_posix()}/report_test.db')
    init_db()

    def _create_task(name: str, **overrides) -> int:
        """建一个任务 + 1 条条款 + 元数据 + 2 条风险项"""
        fields = {
            'contract_name': name, 'biz_type': '采购', 'source_type': 'approve_push',
            'external_doc_id': f'APV-{name}', 'file_type': 'docx',
            'review_status': ReviewStatus.COMPLETED, 'risk_level': 'high',
        }
        fields.update(overrides)
        with session_scope() as session:
            session.add(ContractTask(**fields))
        with session_scope() as session:
            task_id = session.scalars(
                select(ContractTask).where(ContractTask.contract_name == name)
            ).first().id
        with session_scope() as session:
            session.add(Clause(task_id=task_id, clause_type='payment', title='第二条 付款方式',
                               content='货到即付全款，未设验收条件。', page_no=1,
                               paragraph_index=1, seq=0))
            session.add(ContractMeta(task_id=task_id, contract_no='CG-2026-0001',
                                     party_a='某某科技有限公司', party_b='某某软件股份有限公司',
                                     amount=1280000, currency='CNY'))
        with session_scope() as session:
            clause_id = session.scalars(select(Clause).where(Clause.task_id == task_id)).first().id
            session.add_all([
                RiskItem(task_id=task_id, clause_id=clause_id, dimension='payment',
                         risk_type='timing', risk_level='high', cause='付款未设验收前置条件',
                         legal_basis='《民法典》第626条',
                         suggestion_text='甲方应在验收合格后 15 个工作日内付款。',
                         source='rule', manual_comment='必须改为验收后付款'),
                RiskItem(task_id=task_id, clause_id=None, dimension='subject',
                         risk_type='missing', risk_level='mid', cause='相对方地址缺失',
                         source='llm'),
            ])
        return task_id

    node_export = NodeReportExport(config=WritebackConfig(report_dir=str(report_dir)))

    # ---------- 一、完整导出 ----------
    logging.getLogger().info('=' * 20 + ' 一、完整导出 ' + '=' * 20)

    task_id = _create_task('完整导出')
    state = get_default_state()
    state['task_id'] = str(task_id)
    state['comment_md'] = '## 合同审查意见（系统生成）\n- 风险等级：**高风险**'
    state['writeback_status'] = 'success'
    result = node_export(state)

    assert result['report_md'].startswith(f'# {node_export.config.report_title}'), '报告应以标题开头'
    assert '## 三、风险清单明细' in result['report_md']
    assert '某某科技有限公司' in result['report_md'], '应含合同基本信息'
    assert '必须改为验收后付款' in result['report_md'], '应含法务批注'
    assert '## 合同审查意见（系统生成）' in result['report_md'], '应附上回写的评论'
    assert '回写成功' in result['report_md'], '回写状态应转中文展示'

    md_path = _Path(result['report_md_path'])
    assert md_path.is_file(), 'MD 报告应已落盘'
    assert md_path.read_text(encoding='utf-8') == result['report_md'], '落盘内容应与 state 一致'
    assert md_path.parent.name == f'task_{task_id}', '应按任务分目录存放'
    assert f'task_{task_id}' in str(md_path), '路径应包含任务 ID'

    pdf_path = _Path(result['report_pdf_path'])
    assert pdf_path.is_file(), 'PDF 报告应已生成'
    assert pdf_path.read_bytes().startswith(b'%PDF-'), 'PDF 文件头应合法'
    assert pdf_path.read_bytes().__len__() > 2000, 'PDF 内容不应为空'

    with session_scope() as session:
        rows = session.scalars(select(Report).where(Report.task_id == task_id)).all()
        formats = sorted(r.format for r in rows)
        assert formats == ['md', 'pdf'], f'应落库 md + pdf 两条记录，实际 {formats}'
        assert result['report_id'] == max(r.id for r in rows), 'report_id 应为最近一条'
        for row in rows:
            assert _Path(row.file_path).is_file(), '落库路径必须真实存在'
    passed += 1
    logging.getLogger().info('  ✅ MD + PDF 全部产出并落盘，report 表记录 2 条')

    # ---------- 二、独立导出（无 state 上下文，从库里读）----------
    logging.getLogger().info('=' * 20 + ' 二、独立导出 ' + '=' * 20)

    solo_task_id = _create_task('独立导出')
    solo_state = get_default_state()
    solo_state['task_id'] = str(solo_task_id)
    solo = node_export(solo_state)

    assert solo['report_md_path'], '独立导出也应产出报告'
    assert '尚未回写审批意见' in solo['report_md'], '未回写时应给出占位'
    assert '未回写' in solo['report_md'], '回写状态应为未回写'
    assert solo['writeback_status'] == 'not_written'
    passed += 1
    logging.getLogger().info('  ✅ 独立导出：无 state 上下文也能落报告，未回写时给出占位')

    # ---------- 三、无风险 / 无元数据 ----------
    logging.getLogger().info('=' * 20 + ' 三、干净合同 ' + '=' * 20)

    with session_scope() as session:
        session.add(ContractTask(contract_name='干净合同', source_type='upload',
                                 review_status=ReviewStatus.COMPLETED, risk_level='low'))
    with session_scope() as session:
        clean_id = session.scalars(
            select(ContractTask).where(ContractTask.contract_name == '干净合同')
        ).first().id

    clean_state = get_default_state()
    clean_state['task_id'] = str(clean_id)
    clean = node_export(clean_state)
    assert '未检出风险' in clean['report_md']
    assert clean['report_pdf_path'], '干净合同也应能导出 PDF'
    passed += 1
    logging.getLogger().info('  ✅ 干净合同（无风险/无元数据/无条款）也能正常导出')

    # ---------- 四、历史导出的保留与清理 ----------
    logging.getLogger().info('=' * 20 + ' 四、历史导出策略 ' + '=' * 20)

    # 默认保留历史：再导出一次 → 变成 4 条记录
    node_export(clean_state)
    with session_scope() as session:
        count = len(session.scalars(select(Report).where(Report.task_id == clean_id)).all())
    assert count == 4, f'保留历史时应累积（2 次导出 × 2 格式 = 4 条），实际 {count}'

    # 关闭保留 → 先清后写，只剩 2 条
    no_history = NodeReportExport(config=WritebackConfig(
        report_dir=str(report_dir), report_keep_history=False
    ))
    no_history(clean_state)
    with session_scope() as session:
        rows = session.scalars(select(Report).where(Report.task_id == clean_id)).all()
        assert len(rows) == 2, f'关闭保留历史后应只剩 2 条，实际 {len(rows)}'
    passed += 1
    logging.getLogger().info('  ✅ REPORT_KEEP_HISTORY 生效：true 累积、false 先清后写')

    # ---------- 五、PDF 失败降级 ----------
    logging.getLogger().info('=' * 20 + ' 五、PDF 失败降级 ' + '=' * 20)

    bad_dir = work_dir / 'bad_reports'
    bad_node = NodeReportExport(config=WritebackConfig(
        report_dir=str(bad_dir), report_font_name='不存在的字体名'
    ))
    # 注意：用 `-m` 运行时本文件叫 `__main__`，
    # 若用 `import processor...node_report_export as m` 去补丁，拿到的会是**另一个**
    # 模块实例（与 db/models.py 踩过的双重导入是同一类坑），补丁打不中正在跑的代码。
    # 所以这里直接对当前模块的全局变量下手。
    import sys
    module = sys.modules[__name__]
    original = module.md_to_pdf
    try:
        def _boom(*args, **kwargs):
            raise RuntimeError('模拟 PDF 渲染器崩溃')

        module.md_to_pdf = _boom
        # 用一个**新任务**验证，避免被前几次成功导出留下的 pdf 记录干扰
        pdf_fail_task_id = _create_task('PDF 失败降级')
        pdf_fail_state = get_default_state()
        pdf_fail_state['task_id'] = str(pdf_fail_task_id)

        degraded = bad_node(pdf_fail_state)
        assert degraded['report_md_path'], 'PDF 失败时 MD 仍必须产出'
        assert _Path(degraded['report_md_path']).is_file()
        assert degraded['report_pdf_path'] == '', 'PDF 失败时路径应为空'
        with session_scope() as session:
            formats = [r.format for r in session.scalars(
                select(Report).where(Report.task_id == pdf_fail_task_id)
            ).all()]
            assert formats == ['md'], f'PDF 失败时应只写 md 记录，实际 {formats}'
            for row in session.scalars(
                select(Report).where(Report.task_id == pdf_fail_task_id)
            ).all():
                assert _Path(row.file_path).is_file(), 'md 记录指向的文件必须存在'
    finally:
        module.md_to_pdf = original
    passed += 1
    logging.getLogger().info('  ✅ PDF 失败 → 只告警不中断，MD 报告与记录照常产出')

    # ---------- 六、异常场景 ----------
    logging.getLogger().info('=' * 20 + ' 六、异常场景 ' + '=' * 20)

    for case_name, bad_task_id in (('任务ID为空', ''), ('任务ID非数字', 'uuid-abc')):
        bad = get_default_state()
        bad['task_id'] = bad_task_id
        try:
            node_export(bad)
        except Exception as e:
            assert isinstance(e.cause, StateFieldError), \
                f'{case_name} 应抛 StateFieldError，实际 {type(e.cause).__name__}'
            passed += 1
            logging.getLogger().info(f'  ✅ {case_name:<10} → 正确抛出 StateFieldError')
        else:
            raise AssertionError(f'{case_name} 未抛出异常')

    try:
        orphan = get_default_state()
        orphan['task_id'] = '999999'
        node_export(orphan)
    except Exception as e:
        from processor.writeback_processor.exceptions import ValidationError, WritebackProcessError
        assert isinstance(e, WritebackProcessError)
        assert isinstance(e.cause, ValidationError), '任务不存在应是前置条件问题'
        passed += 1
        logging.getLogger().info('  ✅ 任务不存在 → ValidationError')
    else:
        raise AssertionError('任务不存在时未抛出异常')

    reset_engine()
    logging.getLogger().info(f'node_report_export 自测全部通过 ✅ 共 {passed} 个用例')
