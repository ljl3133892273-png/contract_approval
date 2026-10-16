'''processor/recommend_processor/prompt/__init__.py'''

'''
推荐子图提示词包

命名规范：一类提示词一个文件，与 SPEC 8.2 的 P1~P4 一一对应：
    P1 条款分类    → parse_processor 用
    P2 元数据校验  → parse_processor 用
    P3 条款审查    → review_processor/prompt/review_prompt.py
    P4 条款改写    → 本包 rewrite_prompt.py  ← 当前实现
'''
