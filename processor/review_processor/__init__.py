'''processor/review_processor/__init__.py'''

'''
审查子图（② review_processor）

职责：对解析出的 Clause[] + ContractMeta 做两层审查 ——
      第 1 层 规则引擎快速筛查（确定性、低延迟、零成本）
      第 2 层 LLM 语义研判（对未命中规则的条款做深度分析）
      最后组装风险证据链并计算综合风险等级。

对应 SPEC 第 4.3 节「智能审查引擎」与第 9 章「规则引擎设计」。
'''
