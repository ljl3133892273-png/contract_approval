'''processor/review_processor/prompt/__init__.py'''

'''
审查子图提示词包

命名规范：一类提示词一个文件，与 SPEC 8.2 的 P1~P4 一一对应：
    P1 条款分类    → parse_processor 用（node_clause_split 的辅助策略）
    P2 元数据校验  → parse_processor 用（node_meta_extract 的 LLM 校验）
    P3 条款审查    → 本包 review_prompt.py  ← 当前实现
    P4 条款改写    → recommend_processor 用
'''
