'''processor/writeback_processor/__init__.py'''

'''
回写子图（④ writeback_processor）

职责：把审查结论写回审批系统，并导出归档报告。**由人工触发**，不接在自动链路末端。

    node_build_comment   组装 Markdown 审查意见（SPEC 10.1 模板）+ 计算幂等键
    node_adapter_send    调审批系统评论接口（幂等 + 退避重试），状态置 writing
    node_writeback_log   落库评论记录与状态机（success / failed）+ 审计事件
    node_report_export   导出审查报告（MD + PDF，SPEC 10.2 结构）

对应 SPEC 第 4.5 节（审批回写模块）、第 5.3 节（回写状态机）、第 10 章（业务模板）。

与其他子图的关键差别:
    1. **人工触发**：由「写回审批意见」/「导出报告」按钮驱动，不接自动链路。
    2. **失败不回退主状态**（SPEC 11.1）：回写失败只把 `writeback_status` 置 failed，
       `review_status` 保持 completed —— 审查结果本身是有效的，只是没送出去。
    3. **报告导出与回写结果解耦**：即使回写失败（或审批系统不可用），
       报告照样能导出——它是给内部归档用的。
'''
