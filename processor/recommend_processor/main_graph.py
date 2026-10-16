'''processor/recommend_processor/main_graph.py'''

'''
目标:
- 构建完整的推荐工作流图
- 定义节点执行顺序
- 提供便捷的测试入口

需求分析
流程结构（纯顺序，无分支）:
1. `node_load_risk`           按 task_id 载入风险项与条款上下文
2. `node_template_recall`     按 (合同类型 + 条款类型) 召回候选示范条款
3. `node_llm_rewrite`         第 P4 层：结合原文 + 成因 + 候选模板生成个性化改写
4. `node_diff_build`          生成「原文 vs 建议」的句子级差异
5. `node_persist_suggestion`  回填 suggestion_text（保护法务人工成果）
6. END

为什么是纯顺序图:
    SPEC 4.4 给的五步是严格的流水线关系（召回 → 改写 → 对比 → 落库），
    步骤之间没有条件分支。唯一的"分支"语义是 LLM 失败降级，
    但按 SPEC 8.5 它**不改变流程走向**（仍然要走到落库，只是内容退化为示范条款），
    所以用状态位 `rewrite_failed` 表达，而不是用条件边。

异常流向:
    本图**不会因为 LLM 故障抛异常**（node_llm_rewrite 内部转成状态位 + 降级）。
    真正的异常只来自：task_id 非法、数据库故障。
    这两类由调用方捕获 —— 推荐是审查完成后的增强动作，
    即使整体失败也不应把已经 completed 的任务拉回 blocked。
'''

import logging

from langgraph.constants import END
from langgraph.graph import StateGraph

from processor.recommend_processor.base import setup_logging
from processor.recommend_processor.nodes.node_diff_build import NodeDiffBuild
from processor.recommend_processor.nodes.node_llm_rewrite import NodeLlmRewrite
from processor.recommend_processor.nodes.node_load_risk import NodeLoadRisk
from processor.recommend_processor.nodes.node_persist_suggestion import NodePersistSuggestion
from processor.recommend_processor.nodes.node_template_recall import NodeTemplateRecall
from processor.recommend_processor.state import RecommendGraphState, get_default_state


class ContractRecommendWorkflow:
    """推荐子图工作流（③ recommend_processor）"""

    def __init__(self, provider=None):
        """
        初始化工作流对象
        :param provider: LLM Provider 覆盖项。默认 None 表示按 .env 配置决定（真实/mock）；
                        传入可注入替身，用于自测与其他需要确定性结果的场景。
        """
        self._provider = provider
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
        构建推荐工作流图
        :return: 编译后的图
        """
        # 1.初始化图（工作流）
        graph = StateGraph(RecommendGraphState)

        # 2.在图中注册节点
        graph.add_node('node_load_risk', NodeLoadRisk())  # 载入风险项与条款上下文
        graph.add_node('node_template_recall', NodeTemplateRecall())  # 模板召回
        graph.add_node('node_llm_rewrite', NodeLlmRewrite(provider=self._provider))  # P4 改写
        graph.add_node('node_diff_build', NodeDiffBuild())  # 差异对比
        graph.add_node('node_persist_suggestion', NodePersistSuggestion())  # 落库

        # 3.设置入口节点
        graph.set_entry_point('node_load_risk')

        # 4.注册顺序边
        graph.add_edge('node_load_risk', 'node_template_recall')
        graph.add_edge('node_template_recall', 'node_llm_rewrite')
        graph.add_edge('node_llm_rewrite', 'node_diff_build')
        graph.add_edge('node_diff_build', 'node_persist_suggestion')
        graph.add_edge('node_persist_suggestion', END)

        # 5.编译工作流
        return graph.compile()

    def run(self, state: RecommendGraphState, stream: bool = False):
        """
        执行推荐工作流
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

    import tempfile
    from pathlib import Path
    from typing import Any, List

    import docx
    from sqlalchemy import select

    from db.models import ContractTask, RiskItem, TemplateClause
    from db.seed import seed_all
    from db.session import get_engine, init_db, init_engine, reset_engine, session_scope
    from processor.parse_processor.main_graph import ContractParseWorkflow
    from processor.parse_processor.state import get_default_state as get_parse_default_state
    from processor.recommend_processor.state import get_default_state as get_rec_default_state
    from processor.review_processor.main_graph import ContractReviewWorkflow
    from processor.review_processor.nodes.node_rule_match import reset_rule_cache
    from processor.review_processor.state import get_default_state as get_review_default_state

    passed = 0

    class _StubProvider:
        """
        自测用的 LLM 替身。

        **为什么必须注入替身**：
        .env 里已经配置了真实 LLM（SiliconFlow）。若自测直接走真实链路：
          1. 每次跑自测都要烧额度、依赖网络，CI 里不可用；
          2. 模型输出不确定，断言会随机失败。
        所以图级自测统一注入本替身，把确定性交还给测试。
        真实 LLM 链路由 service 层与人工验收来验证。
        """

        is_mock = False
        name = 'stub'

        def __init__(self, response: str):
            self._response = response
            self.call_count = 0

        def chat(self, messages, temperature=None, json_mode=False) -> str:
            self.call_count += 1
            return self._response

    # P3（审查）：返回空 items → 只保留规则层风险，测试才有确定性
    REVIEW_STUB = '{"items": []}'

    # P4（改写）：返回一条固定建议，便于断言"确实采纳了 LLM 改写"
    REWRITE_STUB = '''
{"items": [{"suggestion_text": "（改写版）甲方应在验收合格并收到合规发票后 15 个工作日内支付合同款项。",
            "reason": "增加验收前置条件，消除付款起算点的不确定性。",
            "template_used": "付款与验收前置条款（示范）"}]}
'''

    # ---------- 环境准备 ----------
    work_dir = Path(tempfile.mkdtemp(prefix='recommend_graph_test_'))
    reset_engine()
    init_engine(f'sqlite:///{work_dir.as_posix()}/graph_test.db')
    init_db()
    seed_all()
    reset_rule_cache()

    def _make_docx(path: Path) -> None:
        """生成一份含多处风险条款的采购合同"""
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

    def _create_task(name: str) -> int:
        """建一个采购合同任务"""
        with session_scope() as session:
            session.add(ContractTask(
                contract_name=name, biz_type='采购', source_type='upload',
                review_status='pending',
            ))
        with session_scope() as session:
            return session.scalars(
                select(ContractTask).where(ContractTask.contract_name == name)
            ).first().id

    def _run_recommend(task_id: int, provider=None, stream: bool = False) -> Any:
        """跑一遍推荐子图"""
        state = get_rec_default_state()
        state['task_id'] = str(task_id)
        workflow = ContractRecommendWorkflow(provider=provider)
        if stream:
            executed: List[str] = []
            for event in workflow.run(state, stream=True):
                executed.extend(event.keys())
            return executed
        return workflow.run(state)

    # ---------- 一、端到端：解析 → 审查 → 推荐 ----------
    logging.getLogger().info('=' * 16 + ' 一、端到端全链路 ' + '=' * 16)

    docx_path = work_dir / '采购合同.docx'
    _make_docx(docx_path)
    task_id = _create_task('推荐链路测试合同')

    # 1.解析
    parse_state = get_parse_default_state()
    parse_state.update({
        'task_id': str(task_id),
        'source_type': 'upload',
        'import_file_path': str(docx_path),
        'file_dir': str(work_dir / 'files'),
        'file_title': '软件采购合同',
    })
    ContractParseWorkflow().run(parse_state)

    # 2.审查（注入替身，只保留规则层风险）
    review_state = get_review_default_state()
    review_state['task_id'] = str(task_id)
    ContractReviewWorkflow(provider=_StubProvider(REVIEW_STUB)).run(review_state)

    with session_scope() as session:
        risk_rows = session.scalars(
            select(RiskItem).where(RiskItem.task_id == task_id).order_by(RiskItem.id)
        ).all()
        risk_count = len(risk_rows)
        assert risk_count > 0, '审查阶段应产出风险项，否则推荐无从谈起'

    # 3.推荐
    executed = _run_recommend(task_id, provider=_StubProvider(REWRITE_STUB), stream=True)

    expected_order = [
        'node_load_risk',
        'node_template_recall',
        'node_llm_rewrite',
        'node_diff_build',
        'node_persist_suggestion',
    ]
    assert executed == expected_order, f'节点顺序不正确：{executed}'

    with session_scope() as session:
        rows = session.scalars(
            select(RiskItem).where(RiskItem.task_id == task_id).order_by(RiskItem.id)
        ).all()
        assert len(rows) == risk_count, '推荐不应新增或删除风险项（只做原地更新）'
        filled = [r for r in rows if (r.suggestion_text or '').strip()]
        assert len(filled) == risk_count, \
            f'每条风险项都应回填建议，实际 {len(filled)}/{risk_count}'

        # 规则绑定的模板必须被采纳为候选（说明召回链路生效）
        # 用不带标点的核心词做断言：全角括号与半角括号在源码里肉眼几乎分不出来，
        # 拿带标点的字面量做包含判断极易误判（本项目已踩过一次）。
        llm_rows = [r for r in rows if '改写版' in (r.suggestion_text or '')]
        assert llm_rows, '应至少有一条采用 LLM 改写结果'

        # 法务的人工字段不能被推荐流程污染
        assert all(r.adopt_status == 'pending' for r in rows), '推荐不应改动采纳状态'

        task = session.get(ContractTask, task_id)
        assert task.review_status == 'completed', '推荐是增强动作，不应改变主状态机'

    passed += 1
    logging.getLogger().info(
        f'  ✅ 端到端 5 节点顺序正确，{risk_count} 条风险项全部回填建议（含规则绑定模板召回）'
    )

    # ---------- 二、召回确实用到了规则绑定的模板 ----------
    logging.getLogger().info('=' * 16 + ' 二、规则绑定模板 ' + '=' * 16)

    final_state = _run_recommend(task_id, provider=_StubProvider(REWRITE_STUB))

    assert len(final_state['candidates']) == risk_count, '每个风险点都应有一条召回记录'
    bound_hits = [
        c for c in final_state['candidates']
        if c['matched_by'] == 'rule_bound' and c['templates']
    ]
    assert bound_hits, \
        '至少应有一条风险点通过「规则绑定模板」召回（规则库里都配了 template_clause_id）'

    with session_scope() as session:
        template_ids = {t.id for t in session.scalars(select(TemplateClause)).all()}
    for candidate in bound_hits:
        assert candidate['templates'][0]['template_id'] in template_ids, \
            '召回的模板必须来自模板库'

    passed += 1
    logging.getLogger().info(
        f'  ✅ {len(bound_hits)} 条风险点命中规则绑定模板，且模板均来自模板库'
    )

    # ---------- 三、差异对比已生成 ----------
    logging.getLogger().info('=' * 16 + ' 三、差异对比 ' + '=' * 16)

    diffs = final_state['diffs']
    assert len(diffs) == risk_count, '每条风险点都应有差异记录'
    assert [d['index'] for d in diffs] == list(range(risk_count)), 'index 必须是下标'
    compared = [d for d in diffs if not d['skipped']]
    assert compared, '应至少有差异被实际对比'
    assert any(d['stats']['changed'] for d in compared), '改写后应至少有一条产生实质改动'
    passed += 1
    logging.getLogger().info(f'  ✅ 差异对比生成 {len(compared)} 条，其中含实质改动')

    # ---------- 四、重跑幂等 ----------
    logging.getLogger().info('=' * 16 + ' 四、重跑幂等 ' + '=' * 16)

    result = _run_recommend(task_id, provider=_StubProvider(REWRITE_STUB))
    assert result['persisted_count'] == 0, \
        f"内容未变时不应重复写入，实际写入 {result['persisted_count']} 条"

    with session_scope() as session:
        rows = session.scalars(select(RiskItem).where(RiskItem.task_id == task_id)).all()
        assert len(rows) == risk_count, '重跑不应产生重复风险项'
    passed += 1
    logging.getLogger().info('  ✅ 重跑幂等：不重复写入、不产生重复记录')

    # ---------- 五、LLM 降级后链路仍跑通 ----------
    logging.getLogger().info('=' * 16 + ' 五、LLM 降级 ' + '=' * 16)

    degrade_task_id = _create_task('推荐降级合同')
    docx_path2 = work_dir / '采购合同2.docx'
    _make_docx(docx_path2)
    parse_state = get_parse_default_state()
    parse_state.update({
        'task_id': str(degrade_task_id),
        'source_type': 'upload',
        'import_file_path': str(docx_path2),
        'file_dir': str(work_dir / 'files2'),
        'file_title': '软件采购合同',
    })
    ContractParseWorkflow().run(parse_state)
    review_state = get_review_default_state()
    review_state['task_id'] = str(degrade_task_id)
    ContractReviewWorkflow(provider=_StubProvider(REVIEW_STUB)).run(review_state)

    # 用一个永远返回空 items 的替身模拟"LLM 不产出改写"
    degrade_state = _run_recommend(degrade_task_id, provider=_StubProvider('{"items": []}'))

    assert degrade_state['rewrite_failed'] is False, '空 items 不是故障，不应标记失败'
    assert degrade_state['rewrite_fail_count'] == 0
    assert all(s['source'] != 'llm' for s in degrade_state['suggestions']), \
        'LLM 未产出时应全部降级，不得标记为 llm 来源'

    with session_scope() as session:
        rows = session.scalars(
            select(RiskItem).where(RiskItem.task_id == degrade_task_id)
        ).all()
        assert all((r.suggestion_text or '').strip() for r in rows), \
            '降级后仍应给出建议（示范条款或历史建议），不能留空'
        task = session.get(ContractTask, degrade_task_id)
        assert task.review_status == 'completed', '降级不应影响主状态机'
    passed += 1
    logging.getLogger().info('  ✅ LLM 无产出 → 全部降级为示范条款，任务仍 completed')

    # ---------- 六、无风险任务 ----------
    logging.getLogger().info('=' * 16 + ' 六、无风险任务 ' + '=' * 16)

    clean_task_id = _create_task('无风险推荐合同')
    result = _run_recommend(clean_task_id, provider=_StubProvider(REWRITE_STUB))
    assert result['suggestions'] == [], '无风险项时不应产出建议'
    assert result['persisted_count'] == 0
    passed += 1
    logging.getLogger().info('  ✅ 无风险项 → 空建议，不报错')

    reset_engine()
    logging.getLogger().info(f'recommend_processor.main_graph 自测全部通过 ✅ 共 {passed} 个用例')
