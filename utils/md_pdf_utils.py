'''utils/md_pdf_utils.py'''

"""
Markdown → PDF 工具（供报告导出使用，SPEC 10.2）

为什么不用 markdown + weasyprint:
    SPEC 10.2 给了两条路：「markdown + weasyprint」或「直接用 reportlab」。
    weasyprint 依赖 GTK/Pango 等系统库，在 Windows 上装起来非常折腾；
    而 reportlab 是纯 Python 轮子，并且**内置中文 CID 字体 `STSong-Light`**，
    不需要额外下载字体文件（中文 PDF 最大的坑就是字体）。
    所以这里选 reportlab，并把 Markdown 解析收敛成本文件自己的实现。

为什么不用 reportlab 的 RML/HTML 解析器:
    reportlab 的 `Paragraph` 只支持一个很小的 HTML 子集（<b> <i> <br/> <font> 等），
    喂完整 HTML 进去会报错。所以这里手写一个**只覆盖我们报告用到的那几类语法**的
    行级解析器（标题 / 段落 / 有序无序列表 / 表格 / 引用），反而更可控、更好排错。

支持的 Markdown 子集（够用即可，不追求完整兼容）:
    # ~ ######      标题
    - / * / 1.      无序 / 有序列表
    >               引用块
    | a | b |       表格（含 --- 分隔行）
    **粗体**  `代码`  行内样式
    空行             段落分隔

安全:
    所有文本都会先做 XML 转义再送进 reportlab（它内部按 XML 解析），
    否则文本里出现 `&` 或 `<` 会直接抛异常。
"""

import logging
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    KeepTogether,
    ListFlowable,
    ListItem,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger(__name__)

# 内置中文字体（reportlab 自带 CID 字体，无需字体文件）
DEFAULT_CJK_FONT = 'STSong-Light'

_FONT_REGISTERED: Dict[str, bool] = {}


def register_cjk_font(font_name: str = DEFAULT_CJK_FONT) -> str:
    """
    注���中文 CID 字体（幂等，重复调用只注册一次）
    :param font_name: 字体名
    :return: 实际可用的字体名；注册失败时回退到 Helvetica（会显示不出中文，但不会崩）
    """
    if _FONT_REGISTERED.get(font_name):
        return font_name
    try:
        pdfmetrics.registerFont(UnicodeCIDFont(font_name))
        _FONT_REGISTERED[font_name] = True
        return font_name
    except Exception as e:  # 理论上不会失败，兜底避免整个导出挂掉
        logger.warning(f'[PDF] 注册中文字体 {font_name} 失败（{e}），回退 Helvetica，中文可能显示为方块')
        _FONT_REGISTERED[font_name] = False
        return 'Helvetica'


# ============================================================
# 文本处理
# ============================================================
_XML_ESCAPE = {'&': '&amp;', '<': '&lt;', '>': '&gt;'}


def escape_xml(text: str) -> str:
    """
    XML 转义（reportlab 的 Paragraph 按 XML 解析，文本里的 & < > 必须转义）
    :param text: 原始文本
    :return: 转义后的文本
    """
    for raw, safe in _XML_ESCAPE.items():
        text = text.replace(raw, safe)
    return text


def inline_markdown_to_markup(text: str) -> str:
    """
    把行内 Markdown 语法转成 reportlab Paragraph 认识的标记
    支持：`**粗体**`、`*斜体*`（单星号）、`` `代码` ``
    :param text: 原始文本（未转义）
    :return: 可直接喂给 Paragraph 的标记文本
    """
    escaped = escape_xml(text)
    # 先处理行内代码，避免里面的星号被当成强调
    escaped = re.sub(r'`([^`]+)`',
                     r'<font face="Courier">\1</font>', escaped)
    # 粗体（** 或 __）
    escaped = re.sub(r'\*\*([^*]+)\*\*', r'<b>\1</b>', escaped)
    escaped = re.sub(r'__([^_]+)__', r'<b>\1</b>', escaped)
    # 斜体（单星号，且不与粗体冲突）
    escaped = re.sub(r'(?<!\*)\*([^*\n]+)\*(?!\*)', r'<i>\1</i>', escaped)
    return escaped


def strip_markdown(text: str) -> str:
    """
    去掉 Markdown 标记，得到纯文本（用于文件名、PDF 元信息等）
    :param text: 原始 Markdown
    :return: 纯文本
    """
    if not text:
        return ''
    plain = re.sub(r'`{1,3}', '', text)
    plain = re.sub(r'\*\*|__|\*|_', '', plain)
    plain = re.sub(r'^\s{0,3}#{1,6}\s*', '', plain, flags=re.MULTILINE)
    plain = re.sub(r'^\s{0,3}>\s?', '', plain, flags=re.MULTILINE)
    plain = re.sub(r'^\s{0,3}[-*+]\s+', '', plain, flags=re.MULTILINE)
    return plain.strip()


# ============================================================
# 样式表
# ============================================================
def build_styles(font_name: str) -> Dict[str, ParagraphStyle]:
    """
    构建报告用样式表
    :param font_name: 中文字体名
    :return: {样式名: ParagraphStyle}
    """
    base = getSampleStyleSheet()
    return {
        'title': ParagraphStyle(
            'ReportTitle', parent=base['Title'], fontName=font_name,
            fontSize=20, leading=28, alignment=TA_CENTER, spaceAfter=6,
        ),
        'meta': ParagraphStyle(
            'ReportMeta', parent=base['Normal'], fontName=font_name,
            fontSize=8.5, leading=13, alignment=TA_CENTER,
            textColor=colors.HexColor('#666666'), spaceAfter=10,
        ),
        'h1': ParagraphStyle(
            'ReportH1', parent=base['Heading1'], fontName=font_name,
            fontSize=15, leading=22, spaceBefore=12, spaceAfter=6,
            textColor=colors.HexColor('#1a3d6d'),
        ),
        'h2': ParagraphStyle(
            'ReportH2', parent=base['Heading2'], fontName=font_name,
            fontSize=12.5, leading=19, spaceBefore=10, spaceAfter=5,
            textColor=colors.HexColor('#20456f'),
        ),
        'h3': ParagraphStyle(
            'ReportH3', parent=base['Heading3'], fontName=font_name,
            fontSize=11, leading=17, spaceBefore=8, spaceAfter=4,
            textColor=colors.HexColor('#333333'),
        ),
        'body': ParagraphStyle(
            'ReportBody', parent=base['Normal'], fontName=font_name,
            fontSize=9.5, leading=15, spaceAfter=4,
        ),
        'bullet': ParagraphStyle(
            'ReportBullet', parent=base['Normal'], fontName=font_name,
            fontSize=9.5, leading=15, leftIndent=10, bulletIndent=2,
        ),
        'quote': ParagraphStyle(
            'ReportQuote', parent=base['Normal'], fontName=font_name,
            fontSize=9, leading=14, leftIndent=12, textColor=colors.HexColor('#555555'),
        ),
        'cell': ParagraphStyle(
            'ReportCell', parent=base['Normal'], fontName=font_name,
            fontSize=8.5, leading=12,
        ),
        'cell_header': ParagraphStyle(
            'ReportCellHeader', parent=base['Normal'], fontName=font_name,
            fontSize=8.5, leading=12, textColor=colors.white,
        ),
    }


# ============================================================
# 行级解析
# ============================================================
_TABLE_SEPARATOR = re.compile(r'^\s*\|?[\s:|-]+\|[\s:|-]*$')
_HEADING = re.compile(r'^(#{1,6})\s+(.*)$')
_BULLET = re.compile(r'^\s{0,3}[-*+]\s+(.*)$')
_ORDERED = re.compile(r'^\s{0,3}(\d+)[.)]\s+(.*)$')
_QUOTE = re.compile(r'^\s{0,3}>\s?(.*)$')


def _split_table_row(line: str) -> List[str]:
    """把 `| a | b |` 拆成 ['a', 'b']"""
    stripped = line.strip()
    if stripped.startswith('|'):
        stripped = stripped[1:]
    if stripped.endswith('|'):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split('|')]


def markdown_to_flowables(markdown_text: str, styles: Dict[str, ParagraphStyle],
                          content_width: float) -> List[Any]:
    """
    Markdown → reportlab Flowable 列表
    :param markdown_text: Markdown 文本
    :param styles: build_styles() 的结果
    :param content_width: 可用内容宽度（用于表格列宽分配）
    :return: Flowable 列表
    """
    flowables: List[Any] = []
    lines = (markdown_text or '').split('\n')
    index = 0

    # 缓冲：连续的列表项攒到一起，交给 ListFlowable 排
    bullet_buffer: List[str] = []
    ordered_buffer: List[str] = []

    def flush_bullets() -> None:
        """把攒着的无序列表项落成 Flowable"""
        nonlocal bullet_buffer
        if not bullet_buffer:
            return
        flowables.append(ListFlowable(
            [ListItem(Paragraph(item, styles['bullet']), leftIndent=14)
             for item in bullet_buffer],
            bulletType='bullet', bulletFontName=styles['bullet'].fontName,
            bulletFontSize=8, leftIndent=10, spaceAfter=4,
        ))
        bullet_buffer = []

    def flush_ordered() -> None:
        """把攒着的有序列表项落成 Flowable"""
        nonlocal ordered_buffer
        if not ordered_buffer:
            return
        flowables.append(ListFlowable(
            [ListItem(Paragraph(item, styles['bullet']), leftIndent=14)
             for item in ordered_buffer],
            bulletType='1', bulletFontName=styles['bullet'].fontName,
            bulletFontSize=9, leftIndent=10, spaceAfter=4,
        ))
        ordered_buffer = []

    def flush_all() -> None:
        """两种列表都落掉"""
        flush_bullets()
        flush_ordered()

    while index < len(lines):
        line = lines[index]
        stripped = line.strip()

        # 1.空行 → 段落分隔
        if not stripped:
            flush_all()
            index += 1
            continue

        # 2.表格：当前行含 | 且下一行是分隔行
        if ('|' in stripped and index + 1 < len(lines)
                and _TABLE_SEPARATOR.match(lines[index + 1] or '')):
            flush_all()
            header_cells = _split_table_row(stripped)
            rows: List[List[str]] = [header_cells]
            index += 2  # 跳过表头与分隔行
            while index < len(lines) and '|' in (lines[index] or ''):
                rows.append(_split_table_row(lines[index]))
                index += 1

            column_count = max(len(row) for row in rows)
            for row in rows:
                while len(row) < column_count:
                    row.append('')
            # 列宽：按内容长度加权，避免某一列把字挤成竖排
            weights = []
            for column in range(column_count):
                longest = max(len(row[column]) for row in rows)
                weights.append(max(6, min(longest, 30)))
            total = sum(weights)
            widths = [content_width * weight / total for weight in weights]

            table_data = [
                [Paragraph(inline_markdown_to_markup(cell), styles['cell_header']) for cell in rows[0]]
            ] + [
                [Paragraph(inline_markdown_to_markup(cell), styles['cell']) for cell in row]
                for row in rows[1:]
            ]
            table = Table(table_data, colWidths=widths, repeatRows=1, hAlign='LEFT')
            table.setStyle(TableStyle([
                ('BACKGROUND', (0, 0), (-1, 0), colors.HexColor('#2c5c94')),
                ('TEXTCOLOR', (0, 0), (-1, 0), colors.white),
                ('GRID', (0, 0), (-1, -1), 0.4, colors.HexColor('#b8c4d4')),
                ('VALIGN', (0, 0), (-1, -1), 'TOP'),
                ('LEFTPADDING', (0, 0), (-1, -1), 4),
                ('RIGHTPADDING', (0, 0), (-1, -1), 4),
                ('TOPPADDING', (0, 0), (-1, -1), 3),
                ('BOTTOMPADDING', (0, 0), (-1, -1), 3),
                ('ROWBACKGROUNDS', (0, 1), (-1, -1),
                 [colors.white, colors.HexColor('#f4f7fb')]),
            ]))
            flowables.append(table)
            flowables.append(Spacer(1, 6))
            continue

        # 3.标题
        heading_match = _HEADING.match(stripped)
        if heading_match:
            flush_all()
            level = len(heading_match.group(1))
            text = inline_markdown_to_markup(heading_match.group(2).strip())
            if level == 1:
                style = styles['h1']
            elif level == 2:
                style = styles['h2']
            else:
                style = styles['h3']
            flowables.append(Paragraph(text, style))
            index += 1
            continue

        # 4.引用
        quote_match = _QUOTE.match(line)
        if quote_match:
            flush_all()
            flowables.append(Paragraph(
                inline_markdown_to_markup(quote_match.group(1)), styles['quote']
            ))
            index += 1
            continue

        # 5.有序列表
        ordered_match = _ORDERED.match(line)
        if ordered_match:
            flush_bullets()
            ordered_buffer.append(inline_markdown_to_markup(ordered_match.group(2)))
            index += 1
            continue

        # 6.无序列表
        bullet_match = _BULLET.match(line)
        if bullet_match:
            flush_ordered()
            bullet_buffer.append(inline_markdown_to_markup(bullet_match.group(1)))
            index += 1
            continue

        # 7.普通段落（连续非空行合并成一段）
        flush_all()
        paragraph: List[str] = [stripped]
        index += 1
        while index < len(lines):
            nxt = (lines[index] or '').strip()
            if (not nxt or _HEADING.match(nxt) or _QUOTE.match(lines[index])
                    or _BULLET.match(lines[index]) or _ORDERED.match(lines[index])
                    or ('|' in nxt and index + 1 < len(lines)
                        and _TABLE_SEPARATOR.match(lines[index + 1] or ''))):
                break
            paragraph.append(nxt)
            index += 1
        flowables.append(Paragraph(
            inline_markdown_to_markup(' '.join(paragraph)), styles['body']
        ))

    flush_all()
    return flowables


# ============================================================
# PDF 导出
# ============================================================
def md_to_pdf(markdown_text: str, output_path: str, title: str = '',
              author: str = '合同审批审查系统', font_name: str = DEFAULT_CJK_FONT,
              subtitle: str = '') -> str:
    """
    Markdown → PDF 落盘
    :param markdown_text: Markdown 正文
    :param output_path: 输出 PDF 路径（父目录会自动创建）
    :param title: 文档标题（会作为页眉首行渲染）
    :param author: PDF 元信息作者
    :param font_name: 中文字体名
    :param subtitle: 副标题（渲染在标题下方的小字）
    :return: 输出路径
    """
    actual_font = register_cjk_font(font_name)
    styles = build_styles(actual_font)

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)

    document = SimpleDocTemplate(
        str(target), pagesize=A4,
        leftMargin=18 * mm, rightMargin=18 * mm,
        topMargin=16 * mm, bottomMargin=16 * mm,
        title=strip_markdown(title) or '合同审查报告',
        author=author,
    )
    content_width = document.width

    story: List[Any] = []
    if title:
        story.append(Paragraph(inline_markdown_to_markup(title), styles['title']))
    if subtitle:
        story.append(Paragraph(inline_markdown_to_markup(subtitle), styles['meta']))
    story.append(Spacer(1, 4))

    flowables = markdown_to_flowables(markdown_text, styles, content_width)
    if flowables:
        story.extend(flowables)
    else:
        story.append(Paragraph('（报告正文为空）', styles['body']))

    # 页码：在每页底部居中打印 "第 X 页"
    def _draw_page_number(canvas, doc_) -> None:
        canvas.saveState()
        canvas.setFont(actual_font, 8)
        canvas.setFillColor(colors.HexColor('#888888'))
        canvas.drawCentredString(A4[0] / 2, 10 * mm, f'第 {canvas.getPageNumber()} 页')
        canvas.restoreState()

    document.build(story, onFirstPage=_draw_page_number, onLaterPages=_draw_page_number)
    return str(target)


if __name__ == '__main__':
    # 自测：真实生成一个含中文 + 表格 + 列表的 PDF，再校验文件有效
    import logging as _logging
    import tempfile

    _logging.basicConfig(level=_logging.WARNING, format='%(levelname)s - %(message)s')
    passed = 0

    # ---------- 一、字体注册 ----------
    font = register_cjk_font()
    assert font == DEFAULT_CJK_FONT, f'应注册成功并使用内置中文字体，实际 {font}'
    assert register_cjk_font() == font, '重复注册应幂等'
    passed += 1
    print(f'  ✅ 内置中文字体 {font} 注册成功（无需外部字体文件）')

    # ---------- 二、纯文本化与行内标记 ----------
    assert strip_markdown('# 标题\n- **要点**一\n> 引用') == '标题\n要点一\n引用'
    markup = inline_markdown_to_markup('**粗体** 与 `代码` 与 a<b>c&d')
    assert '<b>粗体</b>' in markup, '粗体应转成 <b>'
    assert '<font face="Courier">代码</font>' in markup, '行内代码应转成 Courier'
    assert 'a&lt;b&gt;c&amp;d' in markup, 'XML 特殊字符必须转义（否则 reportlab 会抛异常）'
    passed += 1
    print('  ✅ 纯文本化与行内标记转换正确，XML 特殊字符已转义')

    # ---------- 三、解析块类型 ----------
    styles = build_styles(font)
    sample_md = '''# 一级标题

这是一段**正文**，包含中文与 English mixed。

## 二级标题

| 等级 | 风险点 | 页码 |
| --- | --- | --- |
| 高 | 知识产权归属不利 | P5 |
| 中 | 保密期限缺失 | P3 |

- 无序项一
- 无序项二

1. 有序项一
2. 有序项二

> 这是引用块
'''
    flowables = markdown_to_flowables(sample_md, styles, 500)
    kinds = [type(f).__name__ for f in flowables]
    assert 'Table' in kinds, '应解析出表格'
    assert kinds.count('ListFlowable') == 2, f'应解析出 2 个列表，实际 {kinds}'
    assert any(isinstance(f, Paragraph) for f in flowables), '应有段落/标题'
    passed += 1
    print(f'  ✅ Markdown 解析出 {len(flowables)} 个块（含表格与 2 个列表）')

    # ---------- 四、真实生成 PDF ----------
    work_dir = Path(tempfile.mkdtemp(prefix='md_pdf_test_'))
    pdf_path = work_dir / 'report.pdf'
    result = md_to_pdf(
        sample_md, str(pdf_path), title='合同审查报告',
        subtitle='合同编号：CG-2026-0001', font_name=font,
    )
    assert Path(result).is_file(), 'PDF 应已生成'
    raw = Path(result).read_bytes()
    assert raw.startswith(b'%PDF-'), '文件头必须是 %PDF-'
    assert len(raw) > 2000, f'PDF 体积过小（{len(raw)} 字节），可能内容没写进去'
    assert pdf_path.with_suffix('.pdf').samefile(Path(result))
    passed += 1
    print(f'  ✅ 生成有效 PDF（{len(raw)} 字节），含中文/表格/列表')

    # ---------- 五、空正文与特殊字符 ----------
    empty_pdf = work_dir / 'empty.pdf'
    md_to_pdf('', str(empty_pdf), title='空报告')
    assert Path(empty_pdf).read_bytes().startswith(b'%PDF-'), '空正文也要产出合法 PDF'

    tricky_md = '# 含特殊字符 <&>" 的报告\n\n- a < b & c > d\n- `code & more`'
    tricky_pdf = work_dir / 'tricky.pdf'
    md_to_pdf(tricky_md, str(tricky_pdf), title='特殊字符')
    assert Path(tricky_pdf).read_bytes().startswith(b'%PDF-'), \
        '含 & < > 的文本不应让导出失败'
    passed += 1
    print('  ✅ 空正文与含 & < > 的文本都能正常导出')

    # ---------- 六、目录自动创建 ----------
    nested = work_dir / 'a' / 'b' / 'c.pdf'
    md_to_pdf('# x', str(nested), title='嵌套目录')
    assert nested.is_file(), '输出目录不存在时应自动创建'
    passed += 1
    print('  ✅ 输出目录自动创建')

    print(f'md_pdf_utils 自测全部通过 ✅ 共 {passed} 个用例')
