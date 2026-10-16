'''processor/recommend_processor/__init__.py'''

'''
推荐子图（③ recommend_processor）

职责：为审查阶段产出的每一个风险点，生成**可落地的修改示范条款**。

    node_load_risk          按 task_id 载入风险项与对应条款原文
    node_template_recall    按 (合同类型 + 条款类型) 从 template_clause 库召回候选
    node_llm_rewrite        结合原文 + 风险成因 + 候选模板，调 LLM 生成个性化改写（P4）
    node_diff_build         生成「原文 vs 建议」的句子级差异对比
    node_persist_suggestion 把建议回填到 risk_item.suggestion_text（保护法务人工成果）

对应 SPEC 第 4.4 节「条款推荐引擎（Recommend）」与第 8.2 节的 P4 提示词。

与审查子图的一致性:
    本子图同样是**独立子图**，只依赖 task_id 从库取数（web 层顺序调用），
    且同样遵循 SPEC 8.5 的降级策略 —— LLM 不可用时链路照常跑完，
    退化为「直接用召回到的示范条款」，绝不因为 LLM 故障丢掉整个推荐结果。
'''
