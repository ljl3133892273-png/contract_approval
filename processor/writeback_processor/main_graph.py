'''processor/writeback_processor/main_graph.py'''

'''
目标:
- 构建完整的回写工作流图
- 定义节点执行顺序
- 提供便捷的测试入口（起真实 mock 审批服务做端到端验证）

需求分析
流程结构（纯顺序，无分支）:
1. `node_build_comment`    组装 SPEC 10.1 的 Markdown 审查意见 + 幂等键
2. `node_adapter_send`     调审批系统评论接口（幂等 + 退避重试），状态置 writing
3. `node_writeback_log`    固化终态 success / failed + 审计事件
4. `node_report_export`    导出 MD + PDF 报告并落库
5. END

为什么是纯顺序图:
    SPEC 4.5 的四步是严格流水线（组装 → 发送 → 落状态 → 出报告）。
    回写失败的"分支"语义（SPEC 11.1）**不改变流程走向** ——
    失败后仍要落状态、仍要出报告，所以用 `writeback_status` 状态位表达，不用条件边。

为什么报告导出排在最后而不是接在成功分支上:
    报告是**内部归档物**，与"审批系统收没收到"无关。
    接在失败分支之外（即无条件执行）才能保证：审批系统挂了，审查成果照样能归档。

异常流向:
    本图会抛异常的只有两类 —— 前置条件不满足（无审批单号、任务不存在）与
    配置错误（审批地址没配却关了 mock 开关）。这两类由调用方捕获后提示管理员。
    网络/接口类故障**不会**抛异常，而是落 `failed` 并继续走完剩余节点。
'''

import logging

from langgraph.constants import END
from langgraph.graph import StateGraph

from processor.writeback_processor.base import setup_logging
from processor.writeback_processor.nodes.node_adapter_send import NodeAdapterSend
from processor.writeback_processor.nodes.node_build_comment import NodeBuildComment
from processor.writeback_processor.nodes.node_report_export import NodeReportExport
from processor.writeback_processor.nodes.node_writeback_log import NodeWritebackLog
from processor.writeback_processor.state import WritebackGraphState, get_default_state


class ContractWritebackWorkflow:
    """回写子图工作流（④ writeback_processor）"""

    def __init__(self, adapter=None, config=None):
        """
        初始化工作流对象
        :param adapter: 审批 Adapter 覆盖项。默认 None 表示按 .env 配置决定（真实/mock）；
                        传入可注入替身或指定配置的 Adapter，用于自测与联调。
        :param config: 配置对象，默认使用全局配置
        """
        self._adapter = adapter
        self._config = config
        self._compiled_graph = None

    @property
    def graph(self):
        """
        延迟加载（懒加载）：只在第一次使用工作流对象的时候编译图
        :return: 编译后的图
        """
        if self._compiled_graph is None:
            self._compiled_graph = self.build_graph()
        return self._compiled_graph

    def build_graph(self):
        """
        构建回写工作流图
        :return: 编译后的图
        """
        # 1.初始化图（工作流）
        graph = StateGraph(WritebackGraphState)

        # 2.在图中注册节点
        #    注意：build_comment 与 adapter_send 必须拿到**同一个** Adapter 实例，
        #    否则评论里"是否走 mock 通道"的判断会与实际发送用的 Adapter 不一致。
        graph.add_node('node_build_comment',
                       NodeBuildComment(config=self._config, adapter=self._adapter))
        graph.add_node('node_adapter_send',
                       NodeAdapterSend(config=self._config, adapter=self._adapter))
        graph.add_node('node_writeback_log', NodeWritebackLog(config=self._config))
        graph.add_node('node_report_export', NodeReportExport(config=self._config))

        # 3.设置入口节点
        graph.set_entry_point('node_build_comment')

        # 4.注册顺序边
        graph.add_edge('node_build_comment', 'node_adapter_send')
        graph.add_edge('node_adapter_send', 'node_writeback_log')
        graph.add_edge('node_writeback_log', 'node_report_export')
        graph.add_edge('node_report_export', END)

        # 5.编译工作流
        return graph.compile()

    def run(self, state: WritebackGraphState, stream: bool = False):
        """
        执行回写工作流
        :param state: 初始状态（只需 task_id）
        :param stream: True 时以流式返回每个节点的执行结果
        :return: 最终状态，或流式迭代器
        """
        if stream:
            return self.graph.stream(state)
        return self.graph.invoke(state)


if __name__ == '__main__':
    # 激活全局日志
    setup_logging()

    import json
    import socket
    import tempfile
    import threading
    import time
    from pathlib import Path
    from typing import Any, List

    import docx
    import httpx
    import uvicorn
    from sqlalchemy import select

    from db.constants import ReviewStatus
    from db.models import ContractTask, Report, ReviewComment, RiskItem, TaskEventLog
    from db.seed import seed_all
    from db.session import init_db, init_engine, reset_engine, session_scope
    from mock_approve_service.main import STORE, app as mock_app
    from processor.parse_processor.main_graph import ContractParseWorkflow
    from processor.parse_processor.state import get_default_state as get_parse_default_state
    from processor.recommend_processor.main_graph import ContractRecommendWorkflow
    from processor.recommend_processor.state import get_default_state as get_recommend_default_state
    from processor.review_processor.main_graph import ContractReviewWorkflow
    from processor.review_processor.nodes.node_rule_match import reset_rule_cache
    from processor.review_processor.state import get_default_state as get_review_default_state
    from processor.writeback_processor.adapter.approve_adapter import ApproveAdapter, MockApproveAdapter
    from processor.writeback_processor.config import WritebackConfig
    from processor.writeback_processor.state import get_default_state as get_writeback_default_state

    passed = 0

    class _StubProvider:
        """LLM 替身：审查返回空 items（只留规则层风险），改写返回固定建议"""

        is_mock = False
        name = 'stub'

        def __init__(self, response: str):
            self._response = response
            self.call_count = 0

        def chat(self, messages, temperature=None, json_mode=False) -> str:
            self.call_count += 1
            return self._response

    REVIEW_STUB = '{"items": []}'
    REWRITE_STUB = ('{"items": [{"suggestion_text": "甲方应在验收合格并收到合规发票后 '
                    '15 个工作日内支付合同款项。", "reason": "增加验收前置条件"}]}')

    # ==================== 起真实 mock 审批服务 ====================
    def _free_port() -> int:
        """取一个空闲端口"""
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            return sock.getsockname()[1]

    port = _free_port()
    server = uvicorn.Server(uvicorn.Config(
        mock_app, host='127.0.0.1', port=port, log_level='error'
    ))
    threading.Thread(target=server.run, daemon=True).start()

    # 等服务起来（最多 15 秒）
    base_url = f'http://127.0.0.1:{port}'
    for _ in range(150):
        try:
            if httpx.get(f'{base_url}/health', timeout=0.5).status_code == 200:
                break
        except httpx.HTTPError:
            time.sleep(0.1)
    else:
        raise RuntimeError('mock 审批服务启动超时')

    print(f'  （mock 审批服务已启动：{base_url}）')

    # mock 服务会读 .env 的 MOCK_APPROVE_TOKEN，因此这些调用必须带上令牌
    # —— 顺带把"令牌是否正确透传"也一并验证了。
    AUTH_HEADERS = {'Authorization': 'Bearer mock-token'}

    # ==================== 环境准备 ====================
    work_dir = Path(tempfile.mkdtemp(prefix='writeback_graph_test_'))
    report_dir = work_dir / 'reports'
    reset_engine()
    init_engine(f'sqlite:///{work_dir.as_posix()}/writeback_graph_test.db')
    init_db()
    seed_all()
    reset_rule_cache()

    # 退避置零，避免自测真的睡 5s / 30s
    http_config = WritebackConfig(
        approve_api_base=f'{base_url}/mock-approve',
        approve_token='mock-token',
        approve_timeout=10,
        retry_backoff=(0, 0),
        max_retry_attempts=2,
        report_dir=str(report_dir),
    )
    http_adapter = ApproveAdapter(http_config)

    def _make_contract(path: Path) -> None:
        """生成一份含风险条款的采购合同 DOCX"""
        document = docx.Document()
        for text in [
            '软件采购合同',
            '合同编号：CG-2026-0001',
            '甲方：某某科技有限公司',
            '乙方：某某软件股份有限公司\n统一社会信用代码：91310000MA1FL5678Y',
            '第一条 合同金额：人民币 1,280,000.00 元。',
            '第二条 付款方式：货到即付全款，未设验收条件。',
            '第三条 违约责任：甲方承担全部损失，乙方不承担任何违约责任。',
            '第四条 知识产权：本合同产生的知识产权归乙方所有。',
        ]:
            document.add_paragraph(text)
        document.save(str(path))

    def _create_task(name: str, doc_id: str) -> int:
        """建一个审批来源的任务（回写需要审批单号）"""
        with session_scope() as session:
            session.add(ContractTask(
                contract_name=name, biz_type='采购', source_type='approve_push',
                external_doc_id=doc_id, review_status=ReviewStatus.PENDING,
            ))
        with session_scope() as session:
            return session.scalars(
                select(ContractTask).where(ContractTask.contract_name == name)
            ).first().id

    def _run_pipeline_to_completed(task_id: int, name: str) -> None:
        """解析 → 审查 → 推荐：把任务推到 completed（回写的前置条件）"""
        docx_path = work_dir / f'{name}.docx'
        _make_contract(docx_path)

        parse_state = get_parse_default_state()
        parse_state.update({
            'task_id': str(task_id), 'source_type': 'approve_push',
            'import_file_path': str(docx_path), 'file_dir': str(work_dir / f'files_{name}'),
            'file_title': name,
        })
        ContractParseWorkflow().run(parse_state)

        review_state = get_review_default_state()
        review_state['task_id'] = str(task_id)
        ContractReviewWorkflow(provider=_StubProvider(REVIEW_STUB)).run(review_state)

        recommend_state = get_recommend_default_state()
        recommend_state['task_id'] = str(task_id)
        ContractRecommendWorkflow(provider=_StubProvider(REWRITE_STUB)).run(recommend_state)

    def _run_writeback(task_id: int, adapter, config=None, stream: bool = False) -> Any:
        """跑一遍回写子图"""
        state = get_writeback_default_state()
        state['task_id'] = str(task_id)
        workflow = ContractWritebackWorkflow(adapter=adapter, config=config)
        if stream:
            executed: List[str] = []
            for event in workflow.run(state, stream=True):
                executed.extend(event.keys())
            return executed
        return workflow.run(state)

    # ---------- 一、端到端：真实 HTTP 回写 ----------
    logging.getLogger().info('=' * 16 + ' 一、端到端真实 HTTP 回写 ' + '=' * 16)

    task_id = _create_task('回写链路合同', 'APV-WB-001')
    _run_pipeline_to_completed(task_id, 'd1')

    # 在 mock 审批侧建好对应单据
    response = httpx.post(f'{base_url}/mock-approve/docs', json={
        'doc_id': 'APV-WB-001', 'title': '软件采购合同', 'applicant': '张三',
        'department': '技术部', 'biz_type': '采购', 'amount': 1280000,
    }, headers=AUTH_HEADERS, timeout=5)
    assert response.status_code == 200, f'建单据失败：{response.text}'

    executed = _run_writeback(task_id, http_adapter, config=http_config, stream=True)
    assert executed == ['node_build_comment', 'node_adapter_send',
                        'node_writeback_log', 'node_report_export'], \
        f'节点顺序不正确：{executed}'

    # 回写结果
    comments = httpx.get(f'{base_url}/mock-approve/docs/APV-WB-001/comments',
                         headers=AUTH_HEADERS, timeout=5).json()
    assert comments['total'] == 1, f'审批系统应收到 1 条评论，实际 {comments["total"]}'
    remote_comment = comments['data'][0]
    assert remote_comment['task_id'] == str(task_id)

    with session_scope() as session:
        task = session.get(ContractTask, task_id)
        assert task.writeback_status == 'success', f'实际 {task.writeback_status}'
        assert task.review_status == 'completed', '回写不应改变主状态'
        assert task.blocked_reason is None

        comment = session.scalars(
            select(ReviewComment).where(ReviewComment.task_id == task_id)
        ).first()
        assert comment.status == 'success'
        assert comment.attempt_count == 1
        assert comment.external_comment_id == remote_comment['comment_id'], \
            '本地记录的对端评论 ID 应与审批系统一致'
        assert '合同审查意见' in comment.content_md
        assert '风险明细' in comment.content_md, '评论应含风险明细表'
        # 断言用不依赖规则文案的稳定字段：合同里实际存在的条款标题
        assert '第二条 付款方式' in comment.content_md, '评论应列出实际检出的风险条款'
        assert '| 高 |' in comment.content_md, '评论明细表应有高风险行'

        events = [e.event for e in session.scalars(
            select(TaskEventLog).where(TaskEventLog.task_id == task_id).order_by(TaskEventLog.id)
        ).all()]
    assert 'PARSE_OK' in events and 'REVIEW_OK' in events, '前置链路的事件应在'
    assert 'WRITE_START' in events and 'WRITE_OK' in events, f'回写事件链不完整：{events}'

    # 报告
    with session_scope() as session:
        reports = session.scalars(select(Report).where(Report.task_id == task_id)).all()
        assert sorted(r.format for r in reports) == ['md', 'pdf'], \
            f'应落库 md + pdf，实际 {[r.format for r in reports]}'
        for row in reports:
            assert Path(row.file_path).is_file(), f'{row.format} 文件不存在：{row.file_path}'
        pdf_row = next(r for r in reports if r.format == 'pdf')
        assert Path(pdf_row.file_path).read_bytes().startswith(b'%PDF-')

    passed += 1
    logging.getLogger().info(
        '  ✅ 真实 HTTP 回写成功：审批系统收到评论、本地状态 success、MD+PDF 报告产出'
    )

    # ---------- 二、幂等键防重复写回 ----------
    logging.getLogger().info('=' * 16 + ' 二、幂等键防重复 ' + '=' * 16)

    # 模拟"本地状态被重置"（比如运维手工改了库），再回写一次
    with session_scope() as session:
        task = session.get(ContractTask, task_id)
        task.writeback_status = 'not_written'

    again = _run_writeback(task_id, http_adapter, config=http_config)
    assert again['writeback_status'] == 'success'
    assert again['writeback_duplicated'] is True, \
        '审批系统应识别出这是同一幂等键的重复提交（SPEC 4.5 的核心保障）'
    assert again['external_comment_id'] == remote_comment['comment_id'], \
        '重复提交应返回**同一条**评论 ID'

    comments = httpx.get(f'{base_url}/mock-approve/docs/APV-WB-001/comments',
                         headers=AUTH_HEADERS, timeout=5).json()
    assert comments['total'] == 1, \
        f'重复回写绝不能在审批系统里产生第二条评论，实际 {comments["total"]} 条'
    passed += 1
    logging.getLogger().info('  ✅ 同一幂等键重复回写：对端识别为重复，评论数仍为 1')

    # ---------- 三、失败注入：重试耗尽 → failed，报告照出 ----------
    logging.getLogger().info('=' * 16 + ' 三、重试耗尽落 failed ' + '=' * 16)

    fail_task_id = _create_task('失败注入合同', 'APV-WB-FAIL')
    _run_pipeline_to_completed(fail_task_id, 'd2')
    httpx.post(f'{base_url}/mock-approve/docs',
               json={'doc_id': 'APV-WB-FAIL', 'title': '失败注入'},
               headers=AUTH_HEADERS, timeout=5)

    # 让审批侧一直返回 503
    httpx.post(f'{base_url}/mock-approve/_control/failure',
               json={'mode': 'always', 'status_code': 503},
               headers=AUTH_HEADERS, timeout=5)
    try:
        fail_result = _run_writeback(fail_task_id, http_adapter, config=http_config)
    finally:
        httpx.delete(f'{base_url}/mock-approve/_control/failure',
                     headers=AUTH_HEADERS, timeout=5)

    assert fail_result['writeback_status'] == 'failed', \
        f"重试耗尽应落 failed，实际 {fail_result['writeback_status']}"
    assert fail_result['writeback_attempts'] == 3, \
        f'应尝试 3 次（1 次 + 重试 2 次），实际 {fail_result["writeback_attempts"]} 次'
    assert '503' in fail_result['writeback_error'], '失败原因应记录状态码'

    with session_scope() as session:
        task = session.get(ContractTask, fail_task_id)
        assert task.writeback_status == 'failed'
        assert task.review_status == 'completed', \
            'SPEC 11.1：回写失败绝不回退主状态 —— 审查结果本身是有效的'
        comment = session.scalars(
            select(ReviewComment).where(ReviewComment.task_id == fail_task_id)
        ).first()
        assert comment.status == 'failed'
        assert comment.attempt_count == 3
        assert comment.error_message, '失败原因应落库，便于运维排查'
        events = [e.event for e in session.scalars(
            select(TaskEventLog).where(TaskEventLog.task_id == fail_task_id).order_by(TaskEventLog.id)
        ).all()]
        assert 'WRITE_FAIL' in events
        # 报告仍然产出（与回写解耦）
        reports = session.scalars(select(Report).where(Report.task_id == fail_task_id)).all()
        assert reports, '回写失败时报告**仍然必须**产出（内部归档与审批系统无关）'

    assert fail_result['report_pdf_path'], '回写失败不应影响报告导出'
    assert '回写失败' in fail_result['report_md'], '报告里应如实写出回写失败'
    passed += 1
    logging.getLogger().info('  ✅ 503 注入：3 次尝试全失败 → failed，主状态 completed，报告照出')

    # ---------- 四、退避重试最终成功 ----------
    logging.getLogger().info('=' * 16 + ' 四、退避重试后成功 ' + '=' * 16)

    retry_task_id = _create_task('重试后成功合同', 'APV-WB-RETRY')
    _run_pipeline_to_completed(retry_task_id, 'd3')
    httpx.post(f'{base_url}/mock-approve/docs',
               json={'doc_id': 'APV-WB-RETRY', 'title': '重试测试'},
               headers=AUTH_HEADERS, timeout=5)

    # 连续 2 次失败后放行
    httpx.post(f'{base_url}/mock-approve/_control/failure',
               json={'mode': 'next_n', 'remaining': 2, 'status_code': 500},
               headers=AUTH_HEADERS, timeout=5)
    retry_result = _run_writeback(retry_task_id, http_adapter, config=http_config)

    assert retry_result['writeback_status'] == 'success', \
        f"第 3 次应成功，实际 {retry_result['writeback_status']}"
    assert retry_result['writeback_attempts'] == 3
    comments = httpx.get(f'{base_url}/mock-approve/docs/APV-WB-RETRY/comments',
                         headers=AUTH_HEADERS, timeout=5).json()
    assert comments['total'] == 1, '重试成功后评论数应为 1'
    passed += 1
    logging.getLogger().info('  ✅ 连续 2 次 500 后第 3 次成功，评论数仍为 1')

    # ---------- 五、不可重试错误（单据不存在）----------
    logging.getLogger().info('=' * 16 + ' 五、不可重试错误 ' + '=' * 16)

    missing_task_id = _create_task('单据不存在合同', 'APV-WB-NOTFOUND')
    _run_pipeline_to_completed(missing_task_id, 'd4')
    # 刻意**不**在审批侧建单据 → 应收到 404
    missing_result = _run_writeback(missing_task_id, http_adapter, config=http_config)

    assert missing_result['writeback_status'] == 'failed'
    assert missing_result['writeback_attempts'] == 1, \
        (f'404 属于不可重试错误，应只尝试 1 次（不该白等 5s+30s 退避），'
         f'实际 {missing_result["writeback_attempts"]} 次')
    assert '404' in missing_result['writeback_error']
    passed += 1
    logging.getLogger().info('  ✅ 单据不存在（404）→ 只试 1 次即落 failed，不做无意义重试')

    # ---------- 六、mock Adapter 降级 ----------
    logging.getLogger().info('=' * 16 + ' 六、mock 通道降级 ' + '=' * 16)

    mock_task_id = _create_task('mock 通道合同', 'APV-WB-MOCK')
    _run_pipeline_to_completed(mock_task_id, 'd5')
    mock_config = WritebackConfig(report_dir=str(report_dir), retry_backoff=(0, 0))
    mock_result = _run_writeback(mock_task_id, MockApproveAdapter(reason='测试'), config=mock_config)

    assert mock_result['writeback_is_mock'] is True
    assert mock_result['writeback_status'] == 'success'
    assert mock_result['external_comment_id'].startswith('MOCK-CMT-')
    assert '未真正送达' in mock_result['comment_md'], \
        '走 mock 通道时，评论正文里必须明确写出"审批系统并未收到"'
    passed += 1
    logging.getLogger().info('  ✅ mock 通道：状态成功但如实标记，评论里明确提示未真正送达')

    # ---------- 七、无审批单号 → 前置条件不满足 ----------
    logging.getLogger().info('=' * 16 + ' 七、无审批单号 ' + '=' * 16)

    with session_scope() as session:
        session.add(ContractTask(contract_name='本地上传合同', source_type='upload',
                                 review_status=ReviewStatus.COMPLETED, risk_level='low'))
    with session_scope() as session:
        upload_id = session.scalars(
            select(ContractTask).where(ContractTask.contract_name == '本地上传合同')
        ).first().id

    from processor.writeback_processor.exceptions import ValidationError, WritebackProcessError
    try:
        _run_writeback(upload_id, http_adapter, config=http_config)
    except Exception as e:
        assert isinstance(e, WritebackProcessError), '应被基类包装'
        assert isinstance(e.cause, ValidationError), \
            f'应抛 ValidationError，实际 {type(e.cause).__name__}'
        assert '导出报告' in str(e), '报错应给出替代做法'
    else:
        raise AssertionError('无审批单号时不应继续回写')
    passed += 1
    logging.getLogger().info('  ✅ 无审批单号（上传来源）→ ValidationError 并提示改用报告导出')

    # ---------- 八、空风险合同也能回写 ----------
    logging.getLogger().info('=' * 16 + ' 八、无风险合同 ' + '=' * 16)

    clean_task_id = _create_task('无风险合同', 'APV-WB-CLEAN')
    with session_scope() as session:
        task = session.get(ContractTask, clean_task_id)
        task.review_status = ReviewStatus.COMPLETED
        task.risk_level = 'low'
    httpx.post(f'{base_url}/mock-approve/docs',
               json={'doc_id': 'APV-WB-CLEAN', 'title': '无风险合同'},
               headers=AUTH_HEADERS, timeout=5)

    clean_result = _run_writeback(clean_task_id, http_adapter, config=http_config)
    assert clean_result['writeback_status'] == 'success', '无风险也应回写（告知审批方"已审查"）'
    assert clean_result['risk_count'] == 0
    assert '未检出风险' in clean_result['comment_md']
    assert any('未检出风险不等于无风险' in h for h in clean_result['highlights'])
    passed += 1
    logging.getLogger().info('  ✅ 无风险合同照常回写，并在评论中提醒人工复核')

    # ---------- 收尾 ----------
    server.should_exit = True
    reset_engine()
    logging.getLogger().info(f'writeback_processor.main_graph 自测全部通过 ✅ 共 {passed} 个用例')
