"""
技能系统（产品文档 §技能系统 / 开发文档 §6.19 skills/_index.md）。

- 每个技能一个目录：skills/{name}/SKILL.md + templates/ + references/
- 三级渐进加载：Level 0 _index.md 目录 / Level 1 SKILL.md / Level 2 templates+references
- status：active / draft / archived（frontmatter，与 skill_usage 表同步）
- draft：Lint 第七项提炼生成，下次对话确认后启用
- 90 天未使用自动归档（Lint 顺带执行）
- apply_skill_write() 由 FileWriter 的 skill 处理器调用（改 status + 重建 _index.md）
"""
from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

import yaml

from memory.md_file import dump_frontmatter_doc, split_frontmatter
from infrastructure.timeutil import now_cst


def _skills_dir(data_dir) -> Path:
    return Path(data_dir) / "skills"


# ---------------------------------------------------------------------------
# FileWriter 的 skill 处理器入口
# ---------------------------------------------------------------------------
def apply_skill_write(data_dir, db, payload: dict) -> None:
    op = payload.get("op")
    name = payload.get("skill_name", "")
    sdir = _skills_dir(data_dir) / name
    now = now_cst().isoformat(timespec="seconds")

    if op == "create_draft":
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "templates").mkdir(exist_ok=True)
        (sdir / "references").mkdir(exist_ok=True)
        (sdir / "SKILL.md").write_text(payload.get("skill_md", ""), encoding="utf-8")
        skill_id = f"skill_{name}"
        db.execute(
            "INSERT OR REPLACE INTO skill_usage(skill_id,skill_name,status,use_count,last_used)"
            " VALUES(?,?,?,COALESCE((SELECT use_count FROM skill_usage WHERE skill_id=?),0),?)",
            (skill_id, name, "draft", skill_id, now))
    elif op == "create_active":
        # 用户手动创建：直接 active
        sdir.mkdir(parents=True, exist_ok=True)
        (sdir / "templates").mkdir(exist_ok=True)
        (sdir / "references").mkdir(exist_ok=True)
        (sdir / "SKILL.md").write_text(payload.get("skill_md", ""), encoding="utf-8")
        skill_id = f"skill_{name}"
        db.execute(
            "INSERT OR REPLACE INTO skill_usage(skill_id,skill_name,status,use_count,last_used)"
            " VALUES(?,?,'active',COALESCE((SELECT use_count FROM skill_usage WHERE skill_id=?),0),?)",
            (skill_id, name, skill_id, now))
    elif op in ("activate", "archive"):
        status = "active" if op == "activate" else "archived"
        _update_skill_status(sdir / "SKILL.md", status)
        db.execute(
            "UPDATE skill_usage SET status=? WHERE skill_name=?", (status, name))
    elif op == "delete":
        if (sdir / "SKILL.md").exists():
            import shutil
            shutil.rmtree(sdir, ignore_errors=True)
        db.execute("DELETE FROM skill_usage WHERE skill_name=?", (name,))

    rebuild_skills_index(data_dir, db)


def _update_skill_status(skill_md: Path, status: str) -> None:
    if not skill_md.exists():
        return
    fm, body = split_frontmatter(skill_md.read_text(encoding="utf-8"))
    fm["status"] = status
    skill_md.write_text(dump_frontmatter_doc(fm, body), encoding="utf-8")


def rebuild_skills_index(data_dir, db) -> None:
    sdir = _skills_dir(data_dir)
    rows = db.query_all(
        "SELECT skill_name,status,use_count,last_used FROM skill_usage")
    active = [r for r in rows if r["status"] == "active"]
    draft = [r for r in rows if r["status"] == "draft"]
    archived = [r for r in rows if r["status"] == "archived"]
    pending_confirm = [r["skill_name"] for r in draft]
    fm = {"total": len(rows), "active": len(active), "draft": len(draft),
          "archived": len(archived), "pending_confirm": pending_confirm,
          "md_schema_version": 1}
    lines = ["## active"]
    for r in active:
        lines.append(
            f"- [[{r['skill_name']}]] | {r['use_count']} 次 | 最后使用 {r['last_used'] or '-'}")
    lines.append("## draft")
    for r in draft:
        lines.append(f"- [[{r['skill_name']}]] | 待确认 [draft]")
    (sdir / "_index.md").write_text(dump_frontmatter_doc(fm, "\n".join(lines)),
                                    encoding="utf-8")


class SkillManager:
    def __init__(self, data_dir, db, file_writer=None):
        self.data_dir = Path(data_dir)
        self.db = db
        self.fw = file_writer

    def list_drafts(self) -> list[dict]:
        idx = _skills_dir(self.data_dir) / "_index.md"
        if not idx.exists():
            return []
        fm, _ = split_frontmatter(idx.read_text(encoding="utf-8"))
        names = fm.get("pending_confirm", []) or []
        return [{"skill_id": f"skill_{n}", "skill_name": n} for n in names]

    def draft_count(self) -> int:
        idx = _skills_dir(self.data_dir) / "_index.md"
        if not idx.exists():
            return 0
        fm, _ = split_frontmatter(idx.read_text(encoding="utf-8"))
        return int(fm.get("draft", 0))

    def load_index(self) -> str:
        """Level 0：技能目录（约 500 token）。

        输出格式化目录（技能名 + 一句话用途），剥离 _index.md 的
        frontmatter 管理元数据（total/pending_confirm/时间戳等对 LLM 是噪声）。
        """
        lines = []
        for name in self.active_names():
            brief = self._skill_brief(name)
            lines.append(f"- {name}：{brief}" if brief else f"- {name}")
        return "\n".join(lines)

    def _skill_brief(self, name: str) -> str:
        """从 SKILL.md 提取一句话用途（标题后的首个正文行，截 80 字）。"""
        text = self.load_skill(name)
        if not text:
            return ""
        _fm, body = split_frontmatter(text)
        for line in body.splitlines():
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            return s[:80]
        return ""

    def _skill_aliases(self, name: str) -> list[str]:
        """读取 SKILL.md frontmatter 的 aliases 触发别名列表。"""
        text = self.load_skill(name)
        if not text:
            return []
        fm, _body = split_frontmatter(text)
        aliases = fm.get("aliases") or []
        if isinstance(aliases, str):
            aliases = [aliases]
        return [str(a).strip() for a in aliases if str(a).strip()]

    def load_skill(self, name: str) -> str:
        """Level 1：SKILL.md 主文件。"""
        f = _skills_dir(self.data_dir) / name / "SKILL.md"
        return f.read_text(encoding="utf-8") if f.exists() else ""

    def load_references(self, name: str, max_chars: int = 4000) -> str:
        """Level 2：templates/ + references/ 文本拼接（按需加载）。"""
        base = _skills_dir(self.data_dir) / name
        parts: list[str] = []
        for sub in ("templates", "references"):
            d = base / sub
            if not d.exists():
                continue
            for f in sorted(d.rglob("*")):
                if f.is_file() and f.suffix.lower() in (".md", ".txt"):
                    parts.append(f"# {sub}/{f.name}\n" +
                                 f.read_text(encoding="utf-8", errors="ignore"))
        return ("\n\n".join(parts))[:max_chars]

    def active_names(self) -> list[str]:
        rows = self.db.query_all(
            "SELECT skill_name FROM skill_usage WHERE status='active'")
        return [r["skill_name"] for r in rows]

    def match_skills(self, text: str, limit: int = 2) -> list[str]:
        """请求级按需匹配：技能名或 frontmatter aliases 别名出现在文本中即命中。

        别名制解决"字面念出技能名才触发"的覆盖率问题：如用户问
        "今天上证指数多少"可经别名"指数/股价"命中行情查询技能。
        """
        if not text:
            return []
        low = text.lower()
        hits = []
        for name in self.active_names():
            keys = [name.lower(), name.replace("_", " ").lower()]
            keys += [a.lower() for a in self._skill_aliases(name)]
            if any(k and k in low for k in keys):
                hits.append(name)
                if len(hits) >= limit:
                    break
        return hits

    async def create_draft(self, name: str, skill_md: str) -> None:
        if self.fw:
            await self.fw.submit("skill", {"op": "create_draft", "skill_name": name,
                                           "skill_md": skill_md})

    async def create_skill(self, name: str, skill_md: str) -> None:
        """用户手动创建技能（直接 active）。"""
        if self.fw:
            await self.fw.submit("skill", {"op": "create_active", "skill_name": name,
                                           "skill_md": skill_md})

    async def activate(self, name: str) -> None:
        if self.fw:
            await self.fw.submit("skill", {"op": "activate", "skill_name": name})

    async def delete(self, name: str) -> None:
        if self.fw:
            await self.fw.submit("skill", {"op": "delete", "skill_name": name})

    def record_use(self, name: str) -> None:
        self.db.execute(
            "UPDATE skill_usage SET use_count=use_count+1, last_used=? WHERE skill_name=?",
            (now_cst().isoformat(timespec="seconds"), name))

    def archive_unused(self, days: int = 90) -> list[str]:
        """90 天未使用的 active 技能归档（Lint 第七项调用）。返回归档技能名。"""
        cutoff = datetime.now().timestamp() - days * 86400
        archived = []
        for r in self.db.query_all(
                "SELECT skill_name,last_used FROM skill_usage WHERE status='active'"):
            lu = r["last_used"]
            try:
                ts = datetime.fromisoformat(lu).timestamp() if lu else 0
            except ValueError:
                ts = 0
            if ts < cutoff:
                archived.append(r["skill_name"])
        return archived
