"""
Soul 管理器（产品文档 §Agent 人格系统 / 开发文档 §2.8）。

SOUL_CORE（稳定层，手工编辑）+ SOUL_STYLE（演化层，三段）。
SOUL_STYLE 三段：对话风格 / 行为原则 / 输出样式
两条独立版本序列，各保留 3 版：
  dialog 序列 → 快照前两段（对话风格 + 行为原则），由"对话确认"路径管理
  auto 序列   → 快照第三段（输出样式），由输出画像 Agent 静默更新
回滚语义：按 dialog vN 回滚只还原前两段；按 auto vN 回滚只还原第三段，互不干扰
加载防护：读取时做注入扫描，命中则沿版本历史逐版回退，全脏回退到内置常量
apply_soul_style_write() 由 FileWriter 的 soul_style 处理器调用。
"""
from __future__ import annotations

import re
from pathlib import Path

from memory.md_file import split_frontmatter  # noqa: F401 (预留)

from .constants import (DEFAULT_SOUL_CORE, DEFAULT_SOUL_STYLE_DIALOG,
                        DEFAULT_SOUL_STYLE_OUTPUT, OUTPUT_STYLE_META_RULE)
from .injection_scan import scan_injection

MAX_VERSIONS = 3
DIALOG_SECTIONS = ["对话风格", "行为原则"]
AUTO_SECTIONS = ["输出样式"]


def _soul_dir(data_dir) -> Path:
    return Path(data_dir) / "soul"


def _history_dir(data_dir) -> Path:
    return _soul_dir(data_dir) / "SOUL_STYLE_HISTORY"


# ---------------------------------------------------------------------------
# 分段解析：SOUL_STYLE.md 由 ## 对话风格 / ## 行为原则 / ## 输出样式 组成
# ---------------------------------------------------------------------------
def parse_style_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    cur = None
    buf: list[str] = []
    for ln in text.splitlines():
        m = re.match(r"^## (.+)$", ln.strip())
        if m:
            if cur:
                sections[cur] = "\n".join(buf).strip()
            cur = m.group(1).strip()
            buf = []
        elif cur:
            buf.append(ln)
    if cur:
        sections[cur] = "\n".join(buf).strip()
    return sections


def render_style_sections(sections: dict[str, str]) -> str:
    order = ["对话风格", "行为原则", "输出样式"]
    parts = []
    for s in order:
        parts.append(f"## {s}")
        parts.append(sections.get(s, "").strip())
        parts.append("")
    return "\n".join(parts).strip() + "\n"


# ---------------------------------------------------------------------------
# FileWriter 的 soul_style 处理器入口
# ---------------------------------------------------------------------------
def apply_soul_style_write(data_dir, payload: dict) -> None:
    """
    payload: {section: 'dialog'|'auto', content: '<该段落新内容>',
              create_version: bool, diff_summary: str}
    dialog → 更新前两段；auto → 更新第三段。可选生成新版本文件。
    """
    section = payload.get("section", "dialog")
    content = payload.get("content", "")
    create_version = payload.get("create_version", True)

    soul_dir = _soul_dir(data_dir)
    soul_dir.mkdir(parents=True, exist_ok=True)
    style_path = soul_dir / "SOUL_STYLE.md"
    if style_path.exists():
        sections = parse_style_sections(style_path.read_text(encoding="utf-8"))
    else:
        sections = parse_style_sections(
            DEFAULT_SOUL_STYLE_DIALOG + "\n" + DEFAULT_SOUL_STYLE_OUTPUT)

    if section == "dialog":
        # content 含"对话风格"+"行为原则"两段的新文本
        new_sections = parse_style_sections(content)
        for s in DIALOG_SECTIONS:
            if s in new_sections:
                sections[s] = new_sections[s]
    else:  # auto
        # content 为输出样式段的纯文本
        sections["输出样式"] = content

    style_path.write_text(render_style_sections(sections), encoding="utf-8")

    if create_version:
        _write_version(data_dir, section, sections,
                       payload.get("diff_summary", ""))


def _write_version(data_dir, sequence: str, sections: dict, diff_summary: str) -> None:
    hist = _history_dir(data_dir)
    hist.mkdir(parents=True, exist_ok=True)
    version = _next_version(data_dir, sequence)
    if sequence == "dialog":
        body = "\n".join(
            f"## {s}\n{sections.get(s, '')}" for s in DIALOG_SECTIONS)
    else:
        body = f"## 输出样式\n{sections.get('输出样式', '')}"
    content = f"<!-- diff: {diff_summary} -->\n{body}\n"
    (hist / f"{sequence}_v{version:03d}.md").write_text(content, encoding="utf-8")
    _prune_versions(data_dir, sequence)


def _next_version(data_dir, sequence: str) -> int:
    hist = _history_dir(data_dir)
    if not hist.exists():
        return 1
    versions = [_ver_num(f.name, sequence)
                for f in hist.glob(f"{sequence}_v*.md")]
    versions = [v for v in versions if v is not None]
    return (max(versions) + 1) if versions else 1


def _ver_num(fname: str, sequence: str) -> int | None:
    m = re.match(rf"{sequence}_v(\d+)\.md", fname)
    return int(m.group(1)) if m else None


def _prune_versions(data_dir, sequence: str) -> None:
    hist = _history_dir(data_dir)
    files = sorted(hist.glob(f"{sequence}_v*.md"),
                   key=lambda f: _ver_num(f.name, sequence) or 0)
    while len(files) > MAX_VERSIONS:
        files.pop(0).unlink()


# ---------------------------------------------------------------------------
# 读取（带注入防护）
# ---------------------------------------------------------------------------
class SoulManager:
    def __init__(self, data_dir, file_writer=None, operation_logger=None, notifier=None):
        self.data_dir = Path(data_dir)
        self.fw = file_writer
        self.oplog = operation_logger
        self.notify = notifier or (lambda t, m: None)

    # ---- SOUL_CORE --------------------------------------------------------
    def read_core(self) -> str:
        p = _soul_dir(self.data_dir) / "SOUL_CORE.md"
        if not p.exists():
            return DEFAULT_SOUL_CORE
        text = p.read_text(encoding="utf-8")
        if scan_injection(text):
            self.notify("soul_reset", "SOUL_CORE 注入检测未通过，已回退默认人格")
            return DEFAULT_SOUL_CORE
        return text

    def write_core(self, content: str) -> None:
        _soul_dir(self.data_dir).mkdir(parents=True, exist_ok=True)
        p = _soul_dir(self.data_dir) / "SOUL_CORE.md"
        # 程序内部写入：标记 internal，避免 watcher 误判为外部修改而推通知
        if self.fw:
            try:
                self.fw.mark_internal(p)
            except Exception:  # noqa: BLE001
                pass
        p.write_text(content, encoding="utf-8")
        if self.oplog:
            self.oplog.log("soul_core_edit", "用户编辑核心人格")

    # ---- SOUL_STYLE 读取（注入防护 + 回退兜底链） -------------------------
    def read_style(self) -> dict[str, str]:
        p = _soul_dir(self.data_dir) / "SOUL_STYLE.md"
        if not p.exists():
            return parse_style_sections(
                DEFAULT_SOUL_STYLE_DIALOG + "\n" + DEFAULT_SOUL_STYLE_OUTPUT)
        text = p.read_text(encoding="utf-8")
        if scan_injection(text):
            return self._fallback_style()
        return parse_style_sections(text)

    def _fallback_style(self) -> dict[str, str]:
        """沿版本历史逐版回退，取最近一个扫描通过的版本；全脏回退内置常量。"""
        merged = parse_style_sections(
            DEFAULT_SOUL_STYLE_DIALOG + "\n" + DEFAULT_SOUL_STYLE_OUTPUT)
        for seq, secs in (("dialog", DIALOG_SECTIONS), ("auto", AUTO_SECTIONS)):
            ok = self._latest_clean_version(seq)
            if ok:
                for s in secs:
                    if s in ok:
                        merged[s] = ok[s]
            else:
                self.notify(
                    "soul_reset", f"SOUL_STYLE {seq} 序列全部版本注入检测未通过，已重置")
        return merged

    def _latest_clean_version(self, sequence: str) -> dict[str, str] | None:
        hist = _history_dir(self.data_dir)
        if not hist.exists():
            return None
        files = sorted(hist.glob(f"{sequence}_v*.md"),
                       key=lambda f: _ver_num(f.name, sequence) or 0, reverse=True)
        for f in files:
            text = f.read_text(encoding="utf-8")
            if not scan_injection(text):
                return parse_style_sections(text)
        return None

    def full_style_text(self, with_meta_rule: bool = True) -> str:
        sections = self.read_style()
        text = render_style_sections(sections)
        if with_meta_rule and sections.get("输出样式", "").strip():
            text += OUTPUT_STYLE_META_RULE
        return text

    # ---- 版本历史 / diff / 回滚 ------------------------------------------
    def history(self, source: str) -> list[dict]:
        hist = _history_dir(self.data_dir)
        if not hist.exists():
            return []
        out = []
        for f in sorted(hist.glob(f"{source}_v*.md"),
                        key=lambda f: _ver_num(f.name, source) or 0, reverse=True):
            text = f.read_text(encoding="utf-8")
            m = re.search(r"<!-- diff: (.*?) -->", text)
            out.append({"version": _ver_num(f.name, source),
                        "diff_summary": m.group(1) if m else "",
                        "current": False})
        if out:
            out[0]["current"] = True
        return out

    def diff(self, source: str, from_v: int, to_v: int) -> dict:
        return {"from": self._read_version(source, from_v),
                "to": self._read_version(source, to_v)}

    def _read_version(self, source: str, version: int) -> str:
        f = _history_dir(self.data_dir) / f"{source}_v{version:03d}.md"
        return f.read_text(encoding="utf-8") if f.exists() else ""

    async def rollback(self, source: str, version: int) -> None:
        """按序列回滚：只还原该序列负责的段落。"""
        text = self._read_version(source, version)
        if not text:
            raise KeyError(f"{source}_v{version}")
        if scan_injection(text):
            raise ValueError("目标版本注入检测未通过，拒绝回滚")
        secs = parse_style_sections(text)
        if source == "dialog":
            content = "\n".join(
                f"## {s}\n{secs.get(s, '')}" for s in DIALOG_SECTIONS)
        else:
            content = secs.get("输出样式", "")
        if self.fw:
            await self.fw.submit("soul_style", {
                "section": source, "content": content, "create_version": True,
                "diff_summary": f"回滚到 v{version}"})
        if self.oplog:
            self.oplog.log("soul_style_rollback", f"{source} 回滚到 v{version}")
