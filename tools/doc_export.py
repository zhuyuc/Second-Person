"""
文档导出 —— Markdown → DOCX / MD 字节流生成（零新增依赖）。

- 管线：markdown 库（tables/fenced_code 扩展）→ HTML → BeautifulSoup 遍历
  → python-docx 渲染，全部为项目既有依赖。
- 覆盖语法：h1-h6 / 段落 / 加粗 / 斜体 / 删除线 / 行内代码 / 链接 / 图片占位
  / 有序无序嵌套列表 / 围栏代码块（灰底等宽）/ 引用块 / 表格 / 分隔线。
- CPU 密集（大文档解析+构建 XML），调用方必须经 asyncio.to_thread 执行，
  禁止在事件循环上直接调用（对话零阻塞架构铁律）。
"""
from __future__ import annotations

import io
import logging
import re

logger = logging.getLogger("second_person.doc_export")

_CODE_FONT = "Consolas"
_CODE_BG = "F2F3F5"      # 代码块底色
_QUOTE_COLOR = "666666"  # 引用文字灰
_LINK_COLOR = "2563EB"   # 链接蓝


def sanitize_filename(name: str, default: str = "导出文档") -> str:
    """去除文件名非法字符，兜底默认名。"""
    name = re.sub(r'[\\/:*?"<>|\r\n]+', "_", (name or "").strip())
    return (name[:80] or default)


def md_to_docx_bytes(md_text: str, title: str | None = None) -> bytes:
    """Markdown 文本 → docx 文件字节。CPU 密集，须在工作线程调用。"""
    import markdown
    from bs4 import BeautifulSoup
    from docx import Document

    html = markdown.markdown(
        md_text or "", extensions=["tables", "fenced_code", "sane_lists"])
    soup = BeautifulSoup(html, "html.parser")

    doc = Document()
    _setup_base_style(doc)
    if title:
        doc.add_heading(title, level=0)
    for el in soup.children:
        _render_block(doc, el)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---- 文档级样式 -----------------------------------------------------------
def _setup_base_style(doc) -> None:
    from docx.oxml.ns import qn
    from docx.shared import Pt
    style = doc.styles["Normal"]
    style.font.name = "Calibri"
    style.font.size = Pt(11)
    # 中文字体：eastAsia 需走 rPr 底层属性
    style.element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")


# ---- 块级渲染 -------------------------------------------------------------
def _render_block(doc, el, quote_depth: int = 0) -> None:
    name = getattr(el, "name", None)
    if name is None:  # 顶层游离文本（通常是空白）
        text = str(el).strip()
        if text:
            _fill_inline(doc.add_paragraph(), [el])
        return
    if name in ("h1", "h2", "h3", "h4", "h5", "h6"):
        doc.add_heading(el.get_text(strip=True), level=int(name[1]))
    elif name == "p":
        p = doc.add_paragraph()
        if quote_depth:
            _style_quote(p, quote_depth)
        _fill_inline(p, el.children, quote=bool(quote_depth))
    elif name in ("ul", "ol"):
        _render_list(doc, el, ordered=(name == "ol"), level=0)
    elif name == "pre":
        code = el.find("code")
        _render_code_block(doc, (code or el).get_text())
    elif name == "blockquote":
        for child in el.children:
            if getattr(child, "name", None):
                _render_block(doc, child, quote_depth=quote_depth + 1)
    elif name == "table":
        _render_table(doc, el)
    elif name == "hr":
        _render_hr(doc)
    else:  # 未覆盖标签（div 等）：降级为纯文本段落
        text = el.get_text(strip=True)
        if text:
            doc.add_paragraph(text)


def _render_list(doc, el, ordered: bool, level: int) -> None:
    # python-docx 内置样式仅到 3 级，更深层夹紧到 3
    suffix = "" if level == 0 else f" {min(level + 1, 3)}"
    style = ("List Number" if ordered else "List Bullet") + suffix
    for li in el.find_all("li", recursive=False):
        p = doc.add_paragraph(style=style)
        nested = []
        inline = []
        for child in li.children:
            if getattr(child, "name", None) in ("ul", "ol"):
                nested.append(child)
            elif getattr(child, "name", None) == "p":
                # 宽松列表：li 内含 p，取其行内内容
                inline.extend(list(child.children))
            else:
                inline.append(child)
        _fill_inline(p, inline)
        for sub in nested:
            _render_list(doc, sub, ordered=(sub.name == "ol"), level=level + 1)


def _render_code_block(doc, code_text: str) -> None:
    from docx.shared import Pt
    p = doc.add_paragraph()
    _shade_paragraph(p, _CODE_BG)
    lines = code_text.rstrip("\n").split("\n")
    for i, line in enumerate(lines):
        run = p.add_run(line)
        run.font.name = _CODE_FONT
        run.font.size = Pt(9)
        if i < len(lines) - 1:
            run.add_break()


def _render_table(doc, el) -> None:
    rows = el.find_all("tr")
    if not rows:
        return
    ncols = max(len(r.find_all(["th", "td"])) for r in rows)
    if not ncols:
        return
    table = doc.add_table(rows=len(rows), cols=ncols)
    table.style = "Table Grid"
    for ri, tr in enumerate(rows):
        for ci, cell in enumerate(tr.find_all(["th", "td"])[:ncols]):
            para = table.cell(ri, ci).paragraphs[0]
            _fill_inline(para, cell.children, bold_all=(cell.name == "th"))


def _render_hr(doc) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    p = doc.add_paragraph()
    pPr = p._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:color"), "CCCCCC")
    borders.append(bottom)
    pPr.append(borders)


# ---- 行内渲染 -------------------------------------------------------------
def _fill_inline(p, nodes, bold=False, italic=False, strike=False,
                 code=False, quote=False, bold_all=False) -> None:
    from docx.shared import Pt, RGBColor
    for node in nodes:
        name = getattr(node, "name", None)
        if name is None:
            text = str(node)
            if not text:
                continue
            run = p.add_run(text)
            run.bold = bold or bold_all
            run.italic = italic
            run.font.strike = strike
            if code:
                run.font.name = _CODE_FONT
                run.font.size = Pt(9)
                _shade_run(run, _CODE_BG)
            if quote:
                run.font.color.rgb = RGBColor.from_string(_QUOTE_COLOR)
        elif name in ("strong", "b"):
            _fill_inline(p, node.children, True, italic, strike, code,
                         quote, bold_all)
        elif name in ("em", "i"):
            _fill_inline(p, node.children, bold, True, strike, code,
                         quote, bold_all)
        elif name in ("del", "s", "strike"):
            _fill_inline(p, node.children, bold, italic, True, code,
                         quote, bold_all)
        elif name == "code":
            _fill_inline(p, node.children, bold, italic, strike, True,
                         quote, bold_all)
        elif name == "a":
            _add_hyperlink(p, node.get("href", ""),
                           node.get_text() or node.get("href", ""))
        elif name == "img":
            alt = node.get("alt") or node.get("src", "")
            p.add_run(f"[图片: {alt}]").italic = True
        elif name == "br":
            if p.runs:
                p.runs[-1].add_break()
        else:  # span 等其余行内容器
            _fill_inline(p, node.children, bold, italic, strike, code,
                         quote, bold_all)


def _add_hyperlink(p, url: str, text: str) -> None:
    """python-docx 无内置超链接 API，走 relationship + w:hyperlink 底层构建。"""
    from docx.opc.constants import RELATIONSHIP_TYPE as RT
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    try:
        r_id = p.part.relate_to(url, RT.HYPERLINK, is_external=True)
    except Exception:  # noqa: BLE001 - 非法 URL 降级为普通文本
        p.add_run(text)
        return
    link = OxmlElement("w:hyperlink")
    link.set(qn("r:id"), r_id)
    r = OxmlElement("w:r")
    rPr = OxmlElement("w:rPr")
    color = OxmlElement("w:color")
    color.set(qn("w:val"), _LINK_COLOR)
    u = OxmlElement("w:u")
    u.set(qn("w:val"), "single")
    rPr.append(color)
    rPr.append(u)
    r.append(rPr)
    t = OxmlElement("w:t")
    t.text = text
    t.set(qn("xml:space"), "preserve")
    r.append(t)
    link.append(r)
    p._p.append(link)


# ---- 底层着色 -------------------------------------------------------------
def _shade_paragraph(p, hex_color: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), hex_color)
    p._p.get_or_add_pPr().append(shd)


def _shade_run(run, hex_color: str) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), hex_color)
    run._r.get_or_add_rPr().append(shd)


def _style_quote(p, depth: int) -> None:
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt
    p.paragraph_format.left_indent = Pt(14 * depth)
    # 左侧竖线：引用视觉标记
    pPr = p._p.get_or_add_pPr()
    borders = OxmlElement("w:pBdr")
    left = OxmlElement("w:left")
    left.set(qn("w:val"), "single")
    left.set(qn("w:sz"), "12")
    left.set(qn("w:color"), "D1D5DB")
    borders.append(left)
    pPr.append(borders)
