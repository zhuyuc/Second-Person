"""
用户画像管理（产品文档 §用户画像 / 开发文档 §6.19 user_profile.md）。

- 存 data/profile/user_profile.md，不进 memories 表
- 解析规则：二级标题=维度名，标题行方括号内为确认状态（已确认/部分推断/推断）；
  正文无序列表，行尾带 [推断] 的单条标记为推断项
- 画像 Agent 重建时整文件覆盖写入（走 FileWriter profile 处理器）
- 注入时机：第 2 步无条件注入 identity 维度；第 4 步意图相关维度追加注入
"""
from __future__ import annotations

import re
from pathlib import Path

from memory.md_file import split_frontmatter


class ProfileManager:
    def __init__(self, data_dir):
        self.path = Path(data_dir) / "profile" / "user_profile.md"

    def exists(self) -> bool:
        return self.path.exists()

    def read_raw(self) -> str:
        return self.path.read_text(encoding="utf-8") if self.path.exists() else ""

    def parse(self) -> dict:
        """返回 {rebuilt_at, source_memory_count, dimensions:[{name,status,items}]}。"""
        if not self.path.exists():
            return {"rebuilt_at": None, "source_memory_count": 0, "dimensions": []}
        fm, body = split_frontmatter(self.path.read_text(encoding="utf-8"))
        dims = []
        cur = None
        for ln in body.splitlines():
            hm = re.match(r"^#{2,3} (.+)$", ln.strip())
            if hm:
                title = hm.group(1).strip()
                status = "已确认"
                sm = re.search(r"\[(已确认|部分推断|推断)\]", title)
                if sm:
                    status = sm.group(1)
                    title = re.sub(r"\s*\[(已确认|部分推断|推断)\]", "", title).strip()
                cur = {"name": title, "status": status, "items": []}
                dims.append(cur)
            elif cur and re.match(r"^[-*•]\s+", ln.strip()):
                text = re.sub(r"^[-*•]\s+", "", ln.strip()).strip()
                # 状态标记可能在行尾：[已确认]/[部分推断]/[推断]
                inferred = ("[推断]" in text) or ("[部分推断]" in text)
                text = re.sub(r"\s*\[(已确认|部分推断|推断)\]", "", text)
                text = text.replace("**", "").strip()   # 去 Markdown 加粗标记
                if text:
                    cur["items"].append({"text": text, "inferred": inferred})
        return {"rebuilt_at": fm.get("last_rebuilt"),
                "source_memory_count": fm.get("source_memory_count", 0),
                "dimensions": dims}

    def identity_snippet(self, max_chars: int = 200) -> str:
        """第 2 步无条件注入的 identity 维度（约 50 token）。"""
        parsed = self.parse()
        for dim in parsed["dimensions"]:
            if "身份" in dim["name"] or dim["name"].lower() == "identity":
                items = "；".join(i["text"] for i in dim["items"])
                return f"用户身份：{items}"[:max_chars]
        return ""

    def dimension(self, name_keyword: str) -> dict | None:
        """第 4 步意图相关维度追加注入（如 decision_patterns）。"""
        for dim in self.parse()["dimensions"]:
            if name_keyword in dim["name"] or name_keyword.lower() in dim["name"].lower():
                return dim
        return None

    # ---- 答案材料充实层（零 LLM） ------------------------------------------

    def summary_text(self, max_chars: int = 1200) -> str:
        """画像全维度摘要：注入缺口判定站（gap_detect / clarification_router）。"""
        parsed = self.parse()
        lines = []
        for dim in parsed["dimensions"]:
            items = "；".join(i["text"] for i in dim["items"])
            if items:
                lines.append(f"{dim['name']}[{dim['status']}]：{items}")
        return "\n".join(lines)[:max_chars]

    def dimension_names(self) -> list[str]:
        """全部维度名列表（材料充实外露用，零 LLM）。"""
        return [d["name"] for d in self.parse()["dimensions"]]

    def material_block(self, dimensions: list[str] | None = None,
                       max_chars: int = 800) -> str:
        """把画像维度拼装为回答材料块（带确认状态与推断标注），注入响应合成。

        通用设计：dimensions 为 None 时注入画像全部维度，由合成模型按任务
        自选使用（材料段附带"无关维度忽略"指令），不维护场景规则表。
        """
        by_name = {d["name"]: d for d in self.parse()["dimensions"]}
        names = dimensions if dimensions is not None else list(by_name)
        out = []
        for name in names:
            dim = by_name.get(name)
            if not dim:
                continue
            items = []
            for it in dim["items"]:
                suffix = "（推断）" if it["inferred"] else ""
                items.append(f"{it['text']}{suffix}")
            if items:
                out.append(f"- {name}[{dim['status']}]：{'；'.join(items)}")
        return "\n".join(out)[:max_chars]
