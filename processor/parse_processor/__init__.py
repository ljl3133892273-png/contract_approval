'''processor/parse_processor/__init__.py'''

'''
解析子图（① parse_processor）

职责：对接入的合同文件完成「类型嗅探 → DOCX / PDF / 图片(OCR) 解析 → 文本预处理
      → 条款切分 → 元数据抽取 → 落库」，输出 Clause[] + ContractMeta + Locator。

对应 SPEC 第 4.2 节「文档解析引擎」。
'''
