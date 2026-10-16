'''processor/recommend_processor/nodes/__init__.py'''

'''
推荐子图节点包

命名规范：一个节点一个文件，文件名 node_<动作>.py，
         类名 Node<动作>（大驼峰），类属性 name = 'node_<动作>'（与 LangGraph add_node 一致）。

执行顺序（SPEC 4.4）:
    node_load_risk → node_template_recall → node_llm_rewrite → node_diff_build → node_persist_suggestion

节点分工:
    node_load_risk          载入风险项 + 条款上下文（唯一入口，只依赖 task_id）
    node_template_recall    模板召回（按规则绑定 / 合同类型 + 条款类型）
    node_llm_rewrite        P4 个性化改写（可降级为纯模板模式）
    node_diff_build         句子级差异对比
    node_persist_suggestion 回填 suggestion_text（保护法务人工成果）
'''
