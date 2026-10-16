'''web/document_view.py'''

"""
文档预览：把本地合同文件转成前端可渲染的形式（SPEC 7.2 的 `/document`）

    PDF / 图片 → 直接流原文件（前端用 PDF.js / <img> 渲染）
    DOCX       → 转成 HTML（前端塞进 iframe / innerHTML，再叠加高亮层）

为什么服务端转而不是全靠前端 Mammoth.js:
    SPEC 3 的选型里前端用 Mammoth.js 转 DOCX。但那样前端要先拿到二进制再转，
    多一次下载、且断开"服务端已经是唯一解析权威"这条线。
    服务端转的好处是**复用解析阶段的同一套遍历逻辑**（`iter_block_items`），
    保证预览里的段落顺序与条款切分时的顺序完全一致 ——
    如果两边遍历方式不同（比如一边把表格排到最后），高亮就会错位。

关于高亮:
    这里只输出**结构化 HTML**（带 data-seq 段落序号），不注入任何定位逻辑。
    真正的高亮由前端拿 `clause.locator`（start_offset / page_no / paragraph_index）
    与这里输出的序号做映射 —— 保持"服务端出数据、前端做呈现"的分工。
"""

import html
import logging
from pathlib import Path
from typing import Tuple

logger = logging.getLogger(__name__)


def docx_to_html(path: str, title: str = '') -> str:
    """
    把 DOCX 转成简单 HTML
    :param path: DOCX 路径
    :param title: 文档标题（用于 <title> 与页首）
    :return: HTML 字符串
    :raises OSError: 文件不存在或无法读取
    """
    import docx

    # 复用解析阶段的块遍历：顺序必须与条款切分一致，否则高亮会错位
    from processor.parse_processor.nodes.node_parse_docx import iter_block_items

    document = docx.Document(path)
    parts = []
    paragraph_index = 0

    for block in iter_block_items(document):
        # 注意：iter_block_items 产出的是 python-docx 的 Paragraph / Table **对象**，
        # 不是底层 XML 元素 —— 所以要取 `_element.tag` 才能判断块类型
        # （直接 getattr(block, 'tag') 会永远拿到空串，导致一个块都输出不了）。
        tag = getattr(getattr(block, '_element', None), 'tag', '')
        if tag.endswith('}p'):
            text = (block.text or '').strip()
            if not text:
                continue
            # data-paragraph 让前端能把 clause.locator 的 paragraph_index 对上
            parts.append(
                f'<p data-paragraph="{paragraph_index}">{html.escape(text)}</p>'
            )
            paragraph_index += 1
        elif tag.endswith('}tbl'):
            rows_html = []
            for row in block.rows:
                cells = ''.join(
                    f'<td>{html.escape((cell.text or "").strip())}</td>' for cell in row.cells
                )
                rows_html.append(f'<tr>{cells}</tr>')
            parts.append(
                f'<table data-paragraph="{paragraph_index}">'
                f'{"".join(rows_html)}</table>'
            )
            paragraph_index += 1

    safe_title = html.escape(title or Path(path).stem)
    return f'''<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>{safe_title}</title>
<style>
  body {{ font-family: "Microsoft YaHei", "PingFang SC", sans-serif;
         line-height: 1.9; padding: 24px 32px; color: #1f2733; background: #fff; }}
  h1 {{ font-size: 18px; border-bottom: 1px solid #dbe3ee; padding-bottom: 8px; }}
  p {{ margin: 6px 0; font-size: 14px; }}
  table {{ border-collapse: collapse; margin: 10px 0; width: 100%; }}
  td {{ border: 1px solid #cfd8e3; padding: 5px 8px; font-size: 13px; }}
  .hint {{ color: #8a94a6; font-size: 12px; margin-top: 24px; }}
</style>
</head>
<body>
<h1>{safe_title}</h1>
{''.join(parts) or '<p>（文档正文为空）</p>'}
<p class="hint">本页由服务端转换生成，段落序号与审查定位一一对应。</p>
</body>
</html>'''


def resolve_document(task) -> Tuple[str, str, str]:
    """
    判断该任务的文档应该怎么返回
    :param task: 合同任务（需含 file_path / file_type）
    :return: (渲染模式, 绝对路径, MIME 类型)
             渲染模式：'file'（原文件流） / 'html'（DOCX 转 HTML）
    :raises FileNotFoundError: 文件不存在
    """
    raw_path = (task.file_path or '').strip()
    if not raw_path:
        raise FileNotFoundError('任务没有关联的文件路径')

    path = Path(raw_path)
    if not path.is_file():
        raise FileNotFoundError(f'源文件已不存在：{raw_path}')

    suffix = path.suffix.lower()
    if suffix == '.docx':
        return 'html', str(path), 'text/html; charset=utf-8'
    if suffix == '.pdf':
        return 'file', str(path), 'application/pdf'
    if suffix in ('.png', '.jpg', '.jpeg', '.bmp', '.webp', '.tif', '.tiff'):
        mime = 'image/jpeg' if suffix in ('.jpg', '.jpeg') else f'image/{suffix.lstrip(".")}'
        return 'file', str(path), mime
    # 其他类型（理论上上传白名单已挡住）按二进制流返回，至少能下载
    return 'file', str(path), 'application/octet-stream'


if __name__ == '__main__':
    # 自测：真实生成一个 DOCX 再转 HTML
    import tempfile

    import docx

    passed = 0
    work_dir = Path(tempfile.mkdtemp(prefix='document_view_test_'))
    docx_path = work_dir / '测试合同.docx'

    document = docx.Document()
    document.add_paragraph('软件采购合同')
    document.add_paragraph('第一条 合同金额：人民币 100 元。')
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = '甲方'
    table.cell(0, 1).text = '某某科技'
    table.cell(1, 0).text = '乙方'
    table.cell(1, 1).text = '某某软件'
    document.add_paragraph('第二条 付款方式：货到即付全款。')
    document.save(str(docx_path))

    html_text = docx_to_html(str(docx_path), title='测试合同')

    # ---------- 一、段落与表格 ----------
    assert '测试合同' in html_text
    assert '第一条 合同金额' in html_text
    assert '<table' in html_text and '某某科技' in html_text, '表格应被转换'
    assert html_text.count('<p ') >= 3, '三个非空段落都应保留'
    passed += 1
    print('  ✅ 段落与表格都转成了 HTML')

    # ---------- 二、段落顺序与解析阶段一致（关键）----------
    body_start = html_text.index('<h1>')
    positions = [
        html_text.index('软件采购合同'),
        html_text.index('第一条 合同金额'),
        html_text.index('<table'),
        html_text.index('第二条 付款方式'),
    ]
    assert positions == sorted(positions), \
        f'顺序必须与文档一致（表格不能跑到最后），实际 {positions}'
    assert positions[0] > body_start
    passed += 1
    print('  ✅ 段落与表格按文档顺序输出（与条款切分口径一致）')

    # ---------- 三、段落序号 ----------
    assert 'data-paragraph="0"' in html_text
    assert 'data-paragraph="3"' in html_text, '应有 4 个块序号（0~3）'
    passed += 1
    print('  ✅ 输出 data-paragraph 序号，供前端做高亮映射')

    # ---------- 四、XSS 防护 ----------
    xss_path = work_dir / 'xss.docx'
    xss_doc = docx.Document()
    xss_doc.add_paragraph('<script>alert(1)</script> 合同正文 & 其他')
    xss_doc.save(str(xss_path))
    xss_html = docx_to_html(str(xss_path), title='<b>标题</b>')
    assert '<script>' not in xss_html, '正文里的 HTML 必须被转义，否则预览页会执行脚本'
    assert '&lt;script&gt;' in xss_html
    assert '&amp;' in xss_html, '& 也要转义'
    assert '<b>标题</b>' not in xss_html, '标题同样要转义'
    passed += 1
    print('  ✅ HTML 转义生效（防 XSS）')

    # ---------- 五、空文档 ----------
    empty_path = work_dir / 'empty.docx'
    docx.Document().save(str(empty_path))
    empty_html = docx_to_html(str(empty_path))
    assert '（文档正文为空）' in empty_html, '空文档应给占位而不是空白页'
    passed += 1
    print('  ✅ 空文档给出占位提示')

    # ---------- 六、渲染模式判定 ----------
    class _Task:
        def __init__(self, file_path):
            self.file_path = file_path

    mode, resolved, mime = resolve_document(_Task(str(docx_path)))
    assert mode == 'html' and resolved.endswith('.docx') and 'text/html' in mime

    pdf_path = work_dir / 'a.pdf'
    pdf_path.write_bytes(b'%PDF-1.4 fake')
    mode, _, mime = resolve_document(_Task(str(pdf_path)))
    assert mode == 'file' and mime == 'application/pdf', 'PDF 应直接流原文件'

    png_path = work_dir / 'a.png'
    png_path.write_bytes(b'\x89PNG')
    mode, _, mime = resolve_document(_Task(str(png_path)))
    assert mode == 'file' and mime == 'image/png'
    passed += 1
    print('  ✅ 渲染模式判定：DOCX→HTML，PDF/图片→原文件流')

    # ---------- 七、异常 ----------
    for bad in (_Task(''), _Task(str(work_dir / '不存在.docx'))):
        try:
            resolve_document(bad)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError('无路径 / 文件不存在都应抛 FileNotFoundError')
    passed += 1
    print('  ✅ 无路径与文件不存在都正确报错')

    print(f'web.document_view 自测全部通过 ✅ 共 {passed} 个用例')
