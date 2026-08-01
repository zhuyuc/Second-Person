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
