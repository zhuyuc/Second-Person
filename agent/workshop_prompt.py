"""把分镜镜头卡收成提交给视频模型的画面句。

左侧脚本仍是完整分镜。出片只提交这里的结果，预览按钮展示的也是这一份。
"""
from __future__ import annotations

import re

_SHOT_HEAD = re.compile(r"(?m)^[ \t]*(?:\*\*)?[ \t]*镜[ \t]*(\d+)")
_FIELD = re.compile(
    r"^(人物|背景|声光|音乐|动作|冲突|承接|对白)\s*[：:]\s*(.*)$"
)
_SHARED_KEYS = ("人物识别", "背景锁定", "声光锁定", "兵器与手段", "说话")
_SHOT_KEYS = ("人物", "对白", "动作", "声光", "背景")


def compile_model_prompt(script: str) -> tuple[str, str]:
    """返回 (提交文本, 模式)。模式 shots=已按镜抽出；raw=原文提交。"""
    text = (script or "").strip()
    if not text:
        return "", "empty"
    preamble, shots = _split_shots(text)
    if not shots:
        return text, "raw"
    lines: list[str] = []
    shared = _shared_bits(preamble)
    if shared:
        lines.append("人物与场景：" + shared)
    for num, body in shots:
        bits = []
        fields = _field_map(body)
        for key in _SHOT_KEYS:
            val = (fields.get(key) or "").strip()
            if val:
                bits.append(val)
        if bits:
            lines.append(f"镜{num}：" + " ".join(bits))
    compiled = "\n".join(lines).strip()
    if not compiled:
        return text, "raw"
    return compiled, "shots"


def _split_shots(text: str) -> tuple[str, list[tuple[str, str]]]:
    matches = list(_SHOT_HEAD.finditer(text))
    if not matches:
        return text, []
    preamble = text[: matches[0].start()]
    shots: list[tuple[str, str]] = []
    for i, match in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        line_end = text.find("\n", match.start())
        body_start = line_end + 1 if line_end != -1 and line_end < end else match.end()
        shots.append((match.group(1), text[body_start:end]))
    return preamble, shots


def _field_map(block: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    current: str | None = None
    buf: list[str] = []
    for raw in block.splitlines():
        line = raw.strip().strip("*").strip()
        if not line:
            continue
        matched = _FIELD.match(line)
        if matched:
            if current:
                fields[current] = " ".join(buf).strip()
            current = matched.group(1)
            buf = [matched.group(2).strip()] if matched.group(2).strip() else []
        elif current:
            buf.append(line)
    if current:
        fields[current] = " ".join(buf).strip()
    return fields


def _shared_bits(preamble: str) -> str:
    found: list[str] = []
    for raw in preamble.splitlines():
        line = raw.strip().lstrip("-").strip().strip("*").strip()
        if not line:
            continue
        for key in _SHARED_KEYS:
            if line.startswith(key):
                rest = re.split(r"[：:]", line, maxsplit=1)
                if len(rest) == 2 and rest[1].strip():
                    found.append(rest[1].strip())
                break
    return " ".join(found)
