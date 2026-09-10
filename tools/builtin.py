"""
内置工具 Path A（开发文档 §6.2 内置工具入参 schema）。

memory_save / memory_search / memory_get / file_read / file_write /
shell_exec / web_fetch / calculator / datetime_now / generate_document /
format_template_save
- memory_search 只走第 1 层 Hybrid 预筛（不 LLM 精筛、不加载 detail）
- file_write / shell_exec 直接执行（无确认环节，错了通过重新生成纠正）
- generate_document 生成 Word/MD 文件供下载（落地 temp/exports，夜间链清理）
- format_template_save 提取附件文档格式骨架并存为高优先级记忆（场景级格式绑定）
- 所有工具通过 register_builtins() 注入依赖后注册到 ToolRegistry
"""
from __future__ import annotations

import ast
import asyncio
import logging
import operator
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

logger = logging.getLogger("second_person.tools.builtin")

from .base import ToolRegistry, ToolSpec
from .sandbox import Sandbox
from .web_fetch import web_fetch as _web_fetch
from .web_search import web_search as _web_search
from infrastructure.prompt_loader import PROMPTS
from infrastructure.timeutil import now_cst

# ---- 安全计算器（只允许算术表达式） --------------------------------------
_OPS = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.Mod: operator.mod,
    ast.USub: operator.neg, ast.FloorDiv: operator.floordiv,
}


def _safe_eval(node):
    if isinstance(node, ast.Constant):
        if isinstance(node.value, (int, float)):
            return node.value
        raise ValueError("仅支持数字")
    if isinstance(node, ast.BinOp):
        return _OPS[type(node.op)](_safe_eval(node.left), _safe_eval(node.right))
    if isinstance(node, ast.UnaryOp):
        return _OPS[type(node.op)](_safe_eval(node.operand))
    raise ValueError("非法表达式")


def calculator(expression: str) -> float:
    tree = ast.parse(expression, mode="eval")
    return float(_safe_eval(tree.body))


def datetime_now(tz: str = "Asia/Shanghai") -> str:
    try:
        return datetime.now(ZoneInfo(tz)).isoformat(timespec="seconds")
    except Exception:  # noqa: BLE001
        return now_cst().isoformat(timespec="seconds")


def register_builtins(registry: ToolRegistry, *, palace, retriever, file_writer,
                      sandbox: Sandbox, data_dir, config,
                      llm=None, providers=None, memory_gate=None) -> None:
    data_dir = Path(data_dir)

    # ---- memory_save（主动记忆，created_by=user_explicit，跳过归属判定） ----
    async def memory_save(title: str, summary: str, detail: str, domain: str,
                          confidence: str = "strong", links: list | None = None,
                          entities: list | None = None) -> str:
        # P1-B：显式路径最小闸口 —— 即便用户主动调用也要挡以下四类
        title = (title or "").strip()
        summary = (summary or "").strip()
        detail = (detail or "").strip()
        if not title or not (summary or detail):
            raise ValueError("memory_save 拒绝：标题与摘要/详情不能为空")
        if len(detail) < 5 and len(summary) < 5:
            raise ValueError("memory_save 拒绝：内容过短，无法作为长期记忆")
        item = {"title": title, "summary": summary, "detail": detail,
                "domain": domain, "attribution": "verified",
                "entities": entities or []}
        if memory_gate is not None:
            decision = memory_gate.evaluate(item, "memory", explicit=True)
            if not decision.allowed:
                raise ValueError(decision.reason)
            # 与近 7 天被拒绝候选的语义近邻 → 拒绝（避免用户在气头上强推
            # 之前主动否决过的内容）
            try:
                from memory.write_gate import content_bucket as _cb
                from infrastructure.timeutil import now_cst as _now
                from datetime import timedelta as _td
                bucket = _cb(item)
                if bucket:
                    cutoff = (_now() - _td(days=7)).isoformat(timespec="seconds")
                    recent_rej = memory_gate.db.query_one(
                        "SELECT decision_reason FROM memory_write_candidates "
                        "WHERE content_bucket=? AND status='rejected' "
                        "AND updated_at>=? LIMIT 1", (bucket, cutoff)) if memory_gate.db else None
                    if recent_rej:
                        raise ValueError(
                            f"memory_save 拒绝：近 7 天有语义相同的候选已被拒绝"
                            f"（原因：{recent_rej['decision_reason']}）。"
                            f"如需强制保存，请显式撤销拒绝记录。")
            except ValueError:
                raise
            except Exception:  # noqa: BLE001 - 查询失败降级为跳过检查
                pass
            # 与现有 disputed 记忆冲突提示（不阻断，走 conflict 建索引即可）
            try:
                if palace and memory_gate.db:
                    disputed = memory_gate.db.query_one(
                        "SELECT id FROM memories WHERE confidence='disputed' "
                        "AND (title LIKE ? OR summary LIKE ?) LIMIT 1",
                        (f"%{title[:15]}%", f"%{summary[:20]}%"))
                    if disputed:
                        logger.info("memory_save：新记忆与 disputed 记忆 %s "
                                    "可能相关，保存但不自动裁决", disputed["id"])
            except Exception:  # noqa: BLE001
                pass
        seq = palace.next_memory_seq()
        from memory.naming import memory_id as mk
        mid = mk(seq)
        now = now_cst()
        fm = {"id": mid, "title": title[:30], "domain": domain,
              "confidence": confidence, "lifecycle": "active", "source_type": "memory",
              "access_count": 0, "created_at": now.strftime("%Y-%m-%d"),
              "updated_at": now.strftime("%Y-%m-%d"),
              "links": links or [], "entities": entities or [],
              "created_by": "user_explicit", "verification_state": "direct",
              "freshness_state": "current", "usefulness_score": 0,
              "write_channel": "explicit", "write_score": 100,
              "evidence_count": 1,
              "last_verified_at": now.isoformat(timespec="seconds"),
              "sensitivity_level": "none",
              "valid_from": now.strftime("%Y-%m-%d"),
              "evidence_refs": [{"source_type": "user_explicit",
                                  "excerpt": detail[:500],
                                  "captured_at": now.isoformat(timespec="seconds")}]}
        await file_writer.submit("memory", {
            "op": "create", "frontmatter": fm, "summary": summary[:30],
            "detail": detail, "change_log": f"[{now:%Y-%m-%d}] 用户主动记忆",
            "links": links or [], "entities": entities or [], "source": "user",
            "evidence_refs": fm["evidence_refs"]})
        return mid

    async def memory_search(query: str, top_k: int = 10, domain: str = None,
                            lifecycle: str = "active,stable,stale") -> list:
        query_vec = None
        if retriever.embed_fn:
            try:
                query_vec = (await retriever.embed_fn([query]))[0]
            except Exception:  # noqa: BLE001
                pass
        cands = await retriever.hybrid_presearch(query, query_vec)
        allow = {s.strip() for s in (lifecycle or "").split(",") if s.strip()}
        out = []
        for c in cands.candidates[:top_k]:
            if domain or allow:
                row = palace.get(c.memory_id)
                if domain and (not row or row["domain"] != domain):
                    continue
                if allow and (not row or row["lifecycle"] not in allow):
                    continue
            out.append({"memory_id": c.memory_id,
                       "title": c.title, "summary": c.summary})
        return out

    def memory_get(memory_id: str) -> dict:
        from memory.md_file import parse_memory_md
        row = palace.get(memory_id)
        if not row:
            return {"error": "记忆不存在"}
        f = data_dir / row["md_path"]
        if not f.exists():
            return {"id": memory_id, "title": row["title"], "summary": row["summary"],
                    "detail": row["summary"], "degraded": True}
        doc = parse_memory_md(f.read_text(encoding="utf-8"))
        return {"id": memory_id, "title": doc.title, "summary": doc.summary,
                "detail": doc.detail, "links": doc.links, "entities": doc.entities}

    def file_read(path: str, max_bytes: int = 1048576) -> str:
        real = sandbox.resolve_path(path)
        if not real.exists():
            return f"[文件不存在：{path}]"
        return real.read_bytes()[:max_bytes].decode("utf-8", errors="ignore")

    def file_write(path: str, content: str, mode: str = "w") -> bool:
        real = sandbox.resolve_path(path)
        real.parent.mkdir(parents=True, exist_ok=True)
        with open(real, "a" if mode == "a" else "w", encoding="utf-8") as fp:
            fp.write(content)
        return True

    async def shell_exec(cmd: str, timeout: int = 30,
                          _ws_ctx=None) -> dict:
        """执行 shell 命令。

        `_ws_ctx` 由 ToolExecutor 注入（needs_workspace=True）：
        - shell_enabled=False（read-only / workspace-write 档位）→ 直接拒
        - shell_enabled=True （danger-full-access 档位）→ 用 ctx.shell_cwd 作 cwd
        兼容缺 ctx 的老调用路径：退回全局 sandbox（历史行为）
        """
        if _ws_ctx is not None:
            if not getattr(_ws_ctx, "shell_enabled", False):
                mode = getattr(_ws_ctx, "sandbox_mode", "unknown")
                return {"returncode": -1, "stdout": "",
                        "stderr": f"SANDBOX_DENIED: 当前档位 {mode} 不允许 shell 执行"}
            cwd = getattr(_ws_ctx, "shell_cwd", None) or sandbox.workspace
        else:
            cwd = sandbox.workspace
        sandbox.check_command(cmd)
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            env=sandbox.clean_env())
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return {"returncode": -1, "stdout": "", "stderr": "命令超时"}
        return {"returncode": proc.returncode,
                "stdout": out.decode("utf-8", errors="ignore"),
                "stderr": err.decode("utf-8", errors="ignore")}

    async def web_fetch_tool(url: str, timeout: int = 15) -> str:
        # LLM 传入的 timeout 生效，上限不超过全局配置防滥用
        from memory import _constants as _mem_const
        cap = _mem_const.WEB_FETCH_TIMEOUT_SECONDS
        large_cap = _mem_const.WEB_FETCH_TIMEOUT_LARGE_SECONDS
        wf = config.get_raw("web_fetch", {}) or {}
        prefer_pdf = True if not isinstance(wf, dict) else bool(
            wf.get("prefer_pdf", True))
        max_bytes = None
        max_chars = None
        if isinstance(wf, dict):
            if wf.get("max_response_bytes") is not None:
                max_bytes = int(wf["max_response_bytes"])
            if wf.get("max_body_chars") is not None:
                max_chars = int(wf["max_body_chars"])
            if wf.get("timeout_seconds") is not None:
                cap = int(wf["timeout_seconds"])
            if wf.get("timeout_large_seconds") is not None:
                large_cap = int(wf["timeout_large_seconds"])
        # 显式 timeout 不超过普通帽；大文档超时由 web_fetch 内部按改写/PDF 选用
        return await _web_fetch(
            url, timeout=min(timeout or cap, cap),
            allow_private=config.get_raw("allow_private_network_fetch", False),
            prefer_pdf=prefer_pdf,
            max_response_bytes=max_bytes,
            max_body_chars=max_chars,
            timeout_large=large_cap)

    async def web_search_tool(query: str, max_results: int = 5) -> list:
        from memory import _constants as _mem_const
        return await _web_search(query, max_results=max_results,
                                 timeout=_mem_const.WEB_FETCH_TIMEOUT_SECONDS)

    async def generate_document(title: str, format: str = "docx",
                                content: str = "") -> dict:
        """生成 Word/Markdown/PPT/Excel 文档文件，落地 temp/exports，返回下载链接。"""
        from urllib.parse import quote
        from .doc_export import (md_to_docx_bytes, md_to_pptx_bytes,
                                 md_to_xlsx_bytes, sanitize_filename)
        fmt = (format or "docx").lower().lstrip(".")
        if fmt == "markdown":
            fmt = "md"
        elif fmt in ("ppt", "powerpoint"):
            fmt = "pptx"
        elif fmt in ("excel", "xls"):
            fmt = "xlsx"
        if fmt not in ("docx", "md", "pptx", "xlsx"):
            raise ValueError(f"不支持的格式：{format}（仅 docx/md/pptx/xlsx）")
        if not (content or "").strip():
            raise ValueError("文档内容为空")
        exports = data_dir / "temp" / "exports"
        safe = sanitize_filename(title)
        stored = f"{uuid.uuid4().hex[:12]}_{safe}.{fmt}"

        def _build() -> int:
            # 文档构建为 CPU 密集，连同写盘一起丢工作线程（零阻塞铁律）
            exports.mkdir(parents=True, exist_ok=True)
            p = exports / stored
            if fmt == "docx":
                p.write_bytes(md_to_docx_bytes(content, safe))
            elif fmt == "pptx":
                p.write_bytes(md_to_pptx_bytes(content, safe))
            elif fmt == "xlsx":
                p.write_bytes(md_to_xlsx_bytes(content, safe))
            else:
                p.write_text(content, encoding="utf-8")
            return p.stat().st_size

        size = await asyncio.to_thread(_build)
        fname = f"{safe}.{fmt}"
        url = f"/api/files/{quote(stored)}"
        return {"filename": fname, "download_url": url, "size_bytes": size,
                "note": f"文件已生成。必须在回复中原样保留此 Markdown 下载链接："
                f"[{fname}]({url})"}

    # ---- format_template_save（格式绑定：附件骨架提取 + 场景级记忆存储） ----
    async def format_template_save(scenario: str,
                                   attachment_text: str = "") -> dict:
        """从附件正文提取格式骨架，存为高优先级记忆（is_important）。
        后续同场景写作时由语义检索自动召回并注入。"""
        from infrastructure.json_repair import repair_json
        if not (attachment_text or "").strip():
            return {"ok": False,
                    "error": "未找到附件正文，请重新上传文档后再试"}
        if not (scenario or "").strip():
            return {"ok": False, "error": "缺少适用场景描述"}
        scenario = scenario.strip()[:30]
        # 截断过长附件（骨架提取只需主干结构，前 30000 字符足够）
        text = attachment_text[:30000]
        if len(attachment_text) > 30000:
            text += "\n\n…（文档过长，已截断）"
        # LLM 提取格式骨架
        snap = providers.snapshot_for("agent") if providers else None
        if snap is None or llm is None:
            return {"ok": False, "error": "LLM 不可用，无法提取格式骨架"}
        prompt = PROMPTS.load_raw("app/prompts/format_skeleton")
        resp = await llm.chat(
            snap, [{"role": "system", "content": prompt},
                   {"role": "user", "content": text}],
            source="system_agent")
        data = repair_json(resp["content"])
        skeleton = (data.get("skeleton") or "").strip()
        if not skeleton:
            return {"ok": False, "error": "格式骨架提取失败，请检查附件内容"}
        # 存为高优先级记忆：domain=output_format，is_important 防生命周期降级；
        # 一次性 create 直接带 is_important（避免先建后改的异步读回竞态），
        # wait=True 确保落库后才向用户确认（失败由工具执行层捕获报错）
        from memory.naming import memory_id as mk_mid
        seq = palace.next_memory_seq()
        mid = mk_mid(seq)
        now = now_cst()
        fm = {"id": mid, "title": f"{scenario}输出格式模板"[:30],
              "domain": "output_format",
              "confidence": "strong", "lifecycle": "active",
              "source_type": "memory",
              "access_count": 0, "created_at": now.strftime("%Y-%m-%d"),
              "updated_at": now.strftime("%Y-%m-%d"),
              "links": [], "entities": [scenario, "输出格式"],
              "is_important": True, "created_by": "user_explicit",
              "verification_state": "direct", "freshness_state": "current",
              "usefulness_score": 0, "valid_from": now.strftime("%Y-%m-%d")}
        detail = (f"适用场景：{scenario}\n\n"
                  f"以下为从用户提供的范例文档中提取的格式骨架，"
                  f"生成{scenario}时必须遵循此结构与风格：\n\n{skeleton}")
        await file_writer.submit("memory", {
            "op": "create", "frontmatter": fm,
            "summary": f"用户指定的{scenario}输出格式约束，写同类文档时必须遵循"[:30],
            "detail": detail,
            "change_log": f"[{now:%Y-%m-%d}] 用户指定输出格式模板",
            "entities": [scenario, "输出格式"],
            "entity_types": {scenario: "concept", "输出格式": "concept"},
            "source": "user", "reason": "格式绑定",
            "evidence_refs": [{"source_type": "user_explicit", "excerpt": scenario,
                                "captured_at": now.isoformat(timespec="seconds")}]}, wait=True)
        return {"ok": True, "memory_id": mid, "scenario": scenario,
                "note": f"已记住「{scenario}」的输出格式模板，"
                f"后续写{scenario}时将自动遵循此格式"}

    # ---- 注册 -------------------------------------------------------------
    registry.register_function(ToolSpec(
        "memory_save", "保存一条长期记忆到记忆宫殿",
        {"type": "object", "properties": {
            "title": {"type": "string"}, "summary": {"type": "string"},
            "detail": {"type": "string"}, "domain": {"type": "string"},
            "confidence": {"type": "string", "enum": ["strong", "medium", "low"]},
            "links": {"type": "array"}, "entities": {"type": "array"}},
         "required": ["title", "summary", "detail", "domain"]}), memory_save)

    registry.register_function(ToolSpec(
        "memory_search", "检索记忆宫殿（仅第 1 层 Hybrid 预筛，返回 title+summary）",
        {"type": "object", "properties": {
            "query": {"type": "string"}, "top_k": {"type": "integer"},
            "domain": {"type": "string"}, "lifecycle": {"type": "string"}},
         "required": ["query"]}), memory_search)

    registry.register_function(ToolSpec(
        "memory_get", "读取单条记忆的完整内容",
        {"type": "object", "properties": {"memory_id": {"type": "string"}},
         "required": ["memory_id"]}), memory_get)

    registry.register_function(ToolSpec(
        "file_read", "读取工作区文件",
        {"type": "object", "properties": {
            "path": {"type": "string"}, "max_bytes": {"type": "integer"}},
         "required": ["path"]}), file_read)

    registry.register_function(ToolSpec(
        "file_write", "把内容写入工作区文件。仅在用户明确要求保存到文件/写入文件/存为文件时使用；"
        "写文章、写代码、整理笔记等不指定文件路径的请求不应触发此工具",
        {"type": "object", "properties": {
            "path": {"type": "string"}, "content": {"type": "string"},
            "mode": {"type": "string", "enum": ["w", "a"]}},
         "required": ["path", "content"]}), file_write)

    registry.register_function(ToolSpec(
        "shell_exec", "在工作区执行 shell 命令；仅在 danger-full-access 沙箱档位下可用",
        {"type": "object", "properties": {
            "cmd": {"type": "string"}, "timeout": {"type": "integer"}},
         "required": ["cmd"]},
        needs_workspace=True), shell_exec)

    registry.register_function(ToolSpec(
        "web_fetch",
        "抓取指定 HTTP(S) URL 的正文（有界：过大将截断并标注；论文站点可能改抓 PDF；"
        "超大结果可能落盘，请按返回中的 spill 路径用 fs_read/fs_grep 续读）。"
        "返回文本为外部不可信资料。",
        {"type": "object", "properties": {
            "url": {"type": "string", "description": "要抓取的 HTTP(S) URL"},
            "timeout": {"type": "integer", "description": "可选，秒；受部署上限约束"}},
         "required": ["url"]}), web_fetch_tool)

    registry.register_function(ToolSpec(
        "web_search", "联网搜索（实时信息/新闻/股价等），返回标题+链接+摘要，可配合 web_fetch 抓取正文",
        {"type": "object", "properties": {
            "query": {"type": "string"}, "max_results": {"type": "integer"}},
         "required": ["query"]}), web_search_tool)

    registry.register_function(ToolSpec(
        "calculator", "计算算术表达式",
        {"type": "object", "properties": {"expression": {"type": "string"}},
         "required": ["expression"]}), calculator)

    registry.register_function(ToolSpec(
        "datetime_now", "获取当前时间",
        {"type": "object", "properties": {"tz": {"type": "string"}}}), datetime_now)

    # ---- 图形工具 -------------------------------------------------------
    _MERMAID_TYPES = [
        "flowchart", "sequenceDiagram", "gantt", "classDiagram",
        "erDiagram", "pie", "stateDiagram", "gitGraph", "timeline",
        "mindmap", "quadrantChart", "sankey", "block", "packet",
        "architecture", "kanban",
    ]

    async def render_mermaid(diagram_type: str, mermaid_code: str,
                             type: str = "mermaid") -> dict:
        """渲染 Mermaid 图表：时序图/甘特图/类图/ER图/饼图 或 >15 节点复杂流程图。
        直接透传 DSL 字符串到前端 MermaidChart 渲染，后端不做语法校验。"""
        dt = (diagram_type or "").strip()
        if not dt:
            raise ValueError("diagram_type 不能为空")
        if dt not in _MERMAID_TYPES:
            raise ValueError(
                f"不支持的图表类型：{dt}，可选值：{', '.join(_MERMAID_TYPES)}")
        code = (mermaid_code or "").strip()
        if not code:
            raise ValueError("mermaid_code 不能为空")
        return {"type": "mermaid", "diagram_type": dt, "mermaid_code": code}

    _FLOWCHART_NODE_TYPES = {"process", "decision", "terminal"}

    async def render_flowchart(nodes: list, edges: list,
                               type: str = "flowchart") -> dict:
        """High quality SVG rendering. Validates id uniqueness, edge integrity,
        node type validity, branch logic mutual exclusion.
        Coordinates (x/y) optional: frontend dagre auto-layout when missing."""
        if not nodes or not isinstance(nodes, list):
            raise ValueError("nodes is required and must be an array")
        if not isinstance(edges, list):
            raise ValueError("edges must be an array")
        seen_ids = set()
        decision_nodes = set()
        for i, n in enumerate(nodes):
            if not isinstance(n, dict):
                raise ValueError(f"nodes[{i}] must be an object")
            nid = n.get("id")
            if not nid or not isinstance(nid, str):
                raise ValueError(f"nodes[{i}].id missing or not string")
            if nid in seen_ids:
                raise ValueError(f"duplicate node id: {nid}")
            seen_ids.add(nid)
            nt = n.get("type", "")
            if nt not in _FLOWCHART_NODE_TYPES:
                raise ValueError(
                    f"nodes[{i}].type='{nt}' invalid, "
                    f"allowed: {', '.join(sorted(_FLOWCHART_NODE_TYPES))}")
            if nt == "decision":
                decision_nodes.add(nid)
            label = n.get("label", "")
            if not isinstance(label, str) or not label.strip():
                raise ValueError(f"nodes[{i}].label missing or empty")
            if len(label) > 10:
                pass
            for axis in ("x", "y"):
                v = n.get(axis)
                if v is not None and (not isinstance(v, (int, float)) or v < 0):
                    raise ValueError(
                        f"nodes[{i}].{axis}={v} invalid, must be non-negative or omitted")
        out_degree = {}
        from_labels = {}
        for j, e in enumerate(edges):
            if not isinstance(e, dict):
                raise ValueError(f"edges[{j}] must be an object")
            for endpoint in ("from", "to"):
                ep = e.get(endpoint)
                if not ep or ep not in seen_ids:
                    raise ValueError(
                        f"edges[{j}].{endpoint}='{ep}' references unknown node")
            src = e["from"]
            out_degree[src] = out_degree.get(src, 0) + 1
            lbl = (e.get("label") or "").strip()
            if lbl:
                s = from_labels.setdefault(src, set())
                if lbl in s:
                    raise ValueError(
                        f"node '{src}' has duplicate edge label: '{lbl}', "
                        "labels from same source must be unique")
                s.add(lbl)
        for dn in decision_nodes:
            if out_degree.get(dn, 0) < 2:
                raise ValueError(
                    f"decision node '{dn}' needs at least 2 outgoing edges "
                    f"(currently {out_degree.get(dn, 0)})")
        return {"type": "flowchart", "nodes": nodes, "edges": edges}

    registry.register_function(ToolSpec(
        "render_mermaid",
        "生成 Mermaid 图表：时序图/甘特图/类图/ER图/饼图/状态图/思维导图等，"
        "或节点数 >15 的复杂流程图（render_flowchart 只适合 ≤15 节点）。"
        "直接透传 Mermaid DSL，前端 MermaidChart 渲染。"
        "流程图请用 flowchart 类型（TB/LR 方向），同时输出 diagram_type 与 mermaid_code",
        {"type": "object", "properties": {
            "diagram_type": {"type": "string",
                             "enum": list(_MERMAID_TYPES),
                             "description": "Mermaid 图表类型：flowchart/sequenceDiagram/classDiagram/stateDiagram/erDiagram/pie/gantt/timeline/mindmap 等"},
            "mermaid_code": {"type": "string", "description": "Mermaid DSL 源码（不含 ```mermaid 围栏）"},
            "type": {"type": "string", "const": "mermaid"}},
         "required": ["diagram_type", "mermaid_code"]}), render_mermaid)

    registry.register_function(ToolSpec(
        "render_flowchart",
        "生成高质量 SVG 流程图（≤15 节点，含判断/循环/并行分支）。输出 nodes + edges 结构化"
        " JSON，前端 FlowChartSVG 按品牌色系渲染。坐标规则：画布宽 800、起始 y=60、"
        " 节点间距 100px、并行分支横向每列 200px、坐标一律整数。"
        " 不适合渲染 >15 节点流程图/时序图/类图/ER图（请用 render_mermaid）",
        {"type": "object", "properties": {
            "nodes": {"type": "array", "items": {"type": "object", "properties": {
                "id": {"type": "string", "description": "唯一英文标识"},
                "type": {"type": "string",
                         "enum": list(_FLOWCHART_NODE_TYPES),
                         "description": "process=矩形处理 / decision=菱形判断 / terminal=胶囊起止"},
                "label": {"type": "string", "description": "节点标签，不超过 10 个汉字"},
                "x": {"type": "integer", "description": "节点中心 x 坐标"},
                "y": {"type": "integer", "description": "节点顶部 y 坐标"}},
                "required": ["id", "type", "label"]}},
            "edges": {"type": "array", "items": {"type": "object", "properties": {
                "from": {"type": "string", "description": "起始节点 id"},
                "to": {"type": "string", "description": "目标节点 id"},
                "label": {"type": "string", "description": "分支标签，如 是/否（仅判断分支需要）"}},
                "required": ["from", "to"]}},
            "type": {"type": "string", "const": "flowchart"}},
         "required": ["nodes", "edges"]}), render_flowchart)

    # ---- generate_image（本地 ComfyUI + SDXL；一次一张） ----
    async def generate_image(
        prompt: str,
        negative_prompt: str = "",
        size: str = "1024x1024",
        n: int = 1,
        style_hint: str = "",
        **kwargs,
    ) -> dict:
        from infrastructure.image_gen import (
            ALLOWED_SIZES, ComfyUIAdapter, ImageGenRequest)
        from infrastructure.json_repair import repair_json
        from langfuse.integration import get_tracer, mark_preview

        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("generate_image 需要非空 prompt")

        emit = kwargs.get("_emit")
        session_id = kwargs.get("_session_id") or ""

        clamped = False
        try:
            n_req = int(n or 1)
        except (TypeError, ValueError):
            n_req = 1
        if n_req != 1:
            clamped = True

        size_raw = (size or "1024x1024").lower().replace("*", "x")
        if size_raw not in ALLOWED_SIZES:
            size_raw = "1024x1024"

        if providers is None:
            raise RuntimeError("未配置文生图模型：ProviderRegistry 不可用")
        snap = providers.snapshot_for("image_gen")
        if snap is None:
            raise RuntimeError(
                "未配置文生图模型：请在设置页为「文生图模型（本地 ComfyUI）」"
                "指定 Provider（base_url 如 http://127.0.0.1:8188，"
                "model_id 为 SDXL checkpoint 文件名）")
        if (getattr(snap, "provider_type", "") or "").strip().lower() != "comfyui":
            raise RuntimeError(
                "文生图槽位须配置类型为 ComfyUI 的 Provider（V1 仅支持本地 ComfyUI）")

        # P0.5：中文/短句润色（失败则回退原 prompt）
        refined_neg = (negative_prompt or "").strip()
        final_prompt = prompt
        refine_on = bool(config.get("image_gen_refine_enabled", True))
        if refine_on and llm is not None:
            refine_snap = providers.snapshot_for("retriever_refine") \
                or providers.snapshot_for("agent") or snap
            try:
                sys_p = PROMPTS.load_raw("agent/prompts/image_prompt_refine")
                user_p = prompt if not refined_neg else (
                    f"描述：{prompt}\n负面词：{refined_neg}")
                resp = await llm.chat(
                    refine_snap,
                    [{"role": "system", "content": sys_p},
                     {"role": "user", "content": user_p}],
                    source="image_prompt_refine")
                data = repair_json(resp.get("content") or "")
                if (data.get("prompt") or "").strip():
                    final_prompt = data["prompt"].strip()
                if (data.get("negative_prompt") or "").strip():
                    refined_neg = data["negative_prompt"].strip()
            except Exception:  # noqa: BLE001
                logger.debug("image_prompt_refine 失败，回退原 prompt", exc_info=True)

        wf = Path(config.get_raw(
            "image_gen_comfyui_workflow",
            "./workflows/sdxl_txt2img.json") or "./workflows/sdxl_txt2img.json")
        if not wf.is_absolute():
            wf = (data_dir.parent / wf).resolve()
        adapter = ComfyUIAdapter(
            base_url=(snap.base_url or "http://127.0.0.1:8188").rstrip("/"),
            workflow_path=wf,
            data_dir=data_dir,
            timeout_sec=float(config.get("image_gen_timeout_sec", 180) or 180),
            default_steps=int(config.get("image_gen_steps", 24) or 24),
            default_size="1024x1024",
            prompt_max_chars=int(
                config.get("image_gen_prompt_max_chars", 1500) or 1500),
        )
        req = ImageGenRequest(
            prompt=final_prompt,
            negative_prompt=refined_neg,
            size=size_raw,
            steps=int(config.get("image_gen_steps", 24) or 24),
            n=1,
            style_hint=(style_hint or "").strip(),
            model_id=snap.model_id or "",
        )

        async def on_progress(stage: str, label: str):
            if emit is None:
                return
            try:
                await emit("step_progress", {
                    "turn_id": "", "step": 0,
                    "phase": "image_gen",
                    "stage": stage,
                    "label": label,
                })
            except Exception:  # noqa: BLE001
                pass

        tracer = get_tracer()
        gen = tracer.generation_start(
            "llm.image_gen",
            model=snap.model_id or "sdxl-local",
            input={"prompt": final_prompt[:500], "n": 1, "size": "1024x1024",
                   "backend": "comfyui",
                   "steps": int(config.get("image_gen_steps", 24) or 24)},
            metadata={"source": "image_gen", "provider_id": snap.provider_id},
        )
        try:
            result = await adapter.generate(
                req, provider_id=snap.provider_id, model_id=snap.model_id or "",
                session_id=session_id, on_progress=on_progress)
            payload = result.to_dict()
            notes = []
            if clamped:
                notes.append("当前本地模式一次只生成 1 张")
            if final_prompt != prompt:
                notes.append("已自动润色英文 prompt")
            if notes:
                payload["summary"] = (
                    (payload.get("summary") or "") + "（" + "；".join(notes) + "）"
                ).strip()
            if gen is not None:
                gen.end(output={
                    "n": payload.get("n"),
                    "filenames": payload.get("filenames"),
                    "latency_ms": payload.get("latency_ms"),
                    "preview": mark_preview(
                        payload.get("summary") or "", content_type="tool_result"),
                })
            return payload
        except Exception as exc:  # noqa: BLE001
            if gen is not None:
                try:
                    gen.end(level="ERROR", status_message=str(exc)[:300])
                except Exception:  # noqa: BLE001
                    pass
            msg = str(exc)
            low = msg.lower()
            if "未启动" in msg or "ConnectError" in type(exc).__name__ or "连接" in msg:
                raise RuntimeError(
                    "本地生图服务未启动：请先运行 image_gen/comfyui 下的 "
                    "run_nvidia_gpu.bat，并确认 http://127.0.0.1:8188 可访问"
                ) from exc
            if "out of memory" in low or ("cuda" in low and "memory" in low) \
                    or "oom" in low:
                raise RuntimeError(
                    "显存不足：请关闭向日葵/ToDesk 等远控及其他占显存程序后重试"
                ) from exc
            raise

    registry.register_function(ToolSpec(
        "generate_image",
        "用本地 ComfyUI + SDXL 生成一张图片。用户明确要求画/生成/绘/出图时调用。"
        "一次只生成 1 张；同轮不要重复调用。prompt 可用中文（系统会润色成英文）。"
        "成功后气泡会展示图片，回复只需简短中文说明；不要伪造外链或输出 base64。"
        "用户若还要另一张，说明「当前一次一张，需要的话我可以再画一张」。",
        {"type": "object", "properties": {
            "prompt": {"type": "string",
                       "description": "出图描述（中英文均可；含主体/场景/光线/风格）"},
            "negative_prompt": {"type": "string",
                                "description": "负面提示词（可选）"},
            "size": {"type": "string",
                     "description": "尺寸；V1 仅 1024x1024，其他值会映射回默认"},
            "n": {"type": "integer",
                  "description": "张数；强制为 1"},
            "style_hint": {"type": "string",
                           "enum": ["photo", "illustration", "sketch", "anime"],
                           "description": "可选风格标签，并入 prompt 前缀"}},
         "required": ["prompt"]},
        parallel_safe=False), generate_image)

    # ---- generate_video（本地 ComfyUI + Wan；一次一条短视频） ----
    async def generate_video(
        prompt: str,
        negative_prompt: str = "",
        size: str = "480x832",
        duration_sec: float = 3,
        n: int = 1,
        motion_hint: str = "",
        style_hint: str = "",
        **kwargs,
    ) -> dict:
        from infrastructure.video_gen import (
            ALLOWED_SIZES, ComfyUIVideoAdapter, VideoGenRequest,
            clamp_duration_sec, normalize_size)
        from infrastructure.json_repair import repair_json
        from langfuse.integration import get_tracer, mark_preview

        prompt = (prompt or "").strip()
        if not prompt:
            raise ValueError("generate_video 需要非空 prompt")

        emit = kwargs.get("_emit")
        session_id = kwargs.get("_session_id") or ""

        clamped_notes: list[str] = []
        try:
            n_req = int(n or 1)
        except (TypeError, ValueError):
            n_req = 1
        if n_req != 1:
            clamped_notes.append("当前本地模式一次只生成 1 条")

        size_raw = normalize_size(size, "480x832")
        if (size or "").lower().replace("*", "x") not in ALLOWED_SIZES:
            clamped_notes.append("尺寸已映射为 480p 档")

        default_dur = int(config.get("video_gen_default_duration_sec", 3) or 3)
        duration_i, dur_clamped = clamp_duration_sec(duration_sec, default_dur)
        max_dur = int(config.get("video_gen_max_duration_sec", 4) or 4)
        if duration_i > max_dur:
            duration_i = max_dur
            dur_clamped = True
        if dur_clamped:
            clamped_notes.append(f"时长已夹紧为 {duration_i} 秒")

        if providers is None:
            raise RuntimeError("未配置文生视频模型：ProviderRegistry 不可用")
        snap = providers.snapshot_for("video_gen")
        if snap is None:
            raise RuntimeError(
                "未配置文生视频模型：请在设置页为「文生视频模型（本地 ComfyUI）」"
                "指定 Provider（base_url 如 http://127.0.0.1:8188，"
                "model_id 为 Wan 2.1 T2V 权重文件名）")
        if (getattr(snap, "provider_type", "") or "").strip().lower() != "comfyui":
            raise RuntimeError(
                "文生视频槽位须配置类型为 ComfyUI 的 Provider（V1 仅支持本地 ComfyUI）")

        refined_neg = (negative_prompt or "").strip()
        final_prompt = prompt
        refine_on = bool(config.get("video_gen_refine_enabled", True))
        if refine_on and llm is not None:
            refine_snap = providers.snapshot_for("retriever_refine") \
                or providers.snapshot_for("agent") or snap
            try:
                sys_p = PROMPTS.load_raw("agent/prompts/video_prompt_refine")
                user_p = prompt if not refined_neg else (
                    f"描述：{prompt}\n负面词：{refined_neg}")
                resp = await llm.chat(
                    refine_snap,
                    [{"role": "system", "content": sys_p},
                     {"role": "user", "content": user_p}],
                    source="video_prompt_refine")
                data = repair_json(resp.get("content") or "")
                if (data.get("prompt") or "").strip():
                    final_prompt = data["prompt"].strip()
                if (data.get("negative_prompt") or "").strip():
                    refined_neg = data["negative_prompt"].strip()
            except Exception:  # noqa: BLE001
                logger.debug("video_prompt_refine 失败，回退原 prompt", exc_info=True)

        wf = Path(config.get_raw(
            "video_gen_comfyui_workflow",
            "./workflows/wan21_t2v_1_3b.json") or "./workflows/wan21_t2v_1_3b.json")
        if not wf.is_absolute():
            wf = (data_dir.parent / wf).resolve()
        fps = int(config.get("video_gen_fps", 16) or 16)
        adapter = ComfyUIVideoAdapter(
            base_url=(snap.base_url or "http://127.0.0.1:8188").rstrip("/"),
            workflow_path=wf,
            data_dir=data_dir,
            timeout_sec=float(config.get("video_gen_timeout_sec", 600) or 600),
            default_steps=int(config.get("video_gen_steps", 20) or 20),
            default_size="480x832",
            default_duration_sec=default_dur,
            fps=fps,
            prompt_max_chars=int(
                config.get("video_gen_prompt_max_chars", 1500) or 1500),
        )
        req = VideoGenRequest(
            prompt=final_prompt,
            negative_prompt=refined_neg,
            size=size_raw,
            duration_sec=duration_i,
            fps=fps,
            steps=int(config.get("video_gen_steps", 20) or 20),
            n=1,
            motion_hint=(motion_hint or "").strip(),
            style_hint=(style_hint or "").strip(),
            model_id=snap.model_id or "",
        )

        async def on_progress(stage: str, label: str):
            if emit is None:
                return
            try:
                await emit("step_progress", {
                    "turn_id": "", "step": 0,
                    "phase": "video_gen",
                    "stage": stage,
                    "label": label,
                })
            except Exception:  # noqa: BLE001
                pass

        tracer = get_tracer()
        gen = tracer.generation_start(
            "llm.video_gen",
            model=snap.model_id or "wan21-t2v-1.3b",
            input={"prompt": final_prompt[:500], "n": 1, "size": size_raw,
                   "duration_sec": duration_i, "fps": fps, "backend": "comfyui",
                   "steps": int(config.get("video_gen_steps", 20) or 20)},
            metadata={"source": "video_gen", "provider_id": snap.provider_id},
        )
        try:
            result = await adapter.generate(
                req, provider_id=snap.provider_id, model_id=snap.model_id or "",
                session_id=session_id, on_progress=on_progress)
            payload = result.to_dict()
            if final_prompt != prompt:
                clamped_notes.append("已自动润色英文 prompt")
            if clamped_notes:
                payload["summary"] = (
                    (payload.get("summary") or "") + "（" + "；".join(clamped_notes) + "）"
                ).strip()
            if gen is not None:
                gen.end(output={
                    "n": payload.get("n"),
                    "filenames": payload.get("filenames"),
                    "latency_ms": payload.get("latency_ms"),
                    "duration_sec": payload.get("duration_sec"),
                    "preview": mark_preview(
                        payload.get("summary") or "", content_type="tool_result"),
                })
            return payload
        except Exception as exc:  # noqa: BLE001
            if gen is not None:
                try:
                    gen.end(level="ERROR", status_message=str(exc)[:300])
                except Exception:  # noqa: BLE001
                    pass
            msg = str(exc)
            low = msg.lower()
            if "未启动" in msg or "ConnectError" in type(exc).__name__ or "连接" in msg:
                raise RuntimeError(
                    "本地生视频服务未启动：请先运行 image_gen/comfyui 下的 "
                    "run_nvidia_gpu.bat，并确认 http://127.0.0.1:8188 可访问"
                ) from exc
            if "out of memory" in low or ("cuda" in low and "memory" in low) \
                    or "oom" in low:
                raise RuntimeError(
                    "显存不足：请关闭向日葵/ToDesk 等远控及其他占显存程序后重试；"
                    "也可将时长改为更短（2–3 秒）"
                ) from exc
            raise

    registry.register_function(ToolSpec(
        "generate_video",
        "用本地 ComfyUI + Wan 2.1 生成一条短视频。用户明确要求生成视频/短片/动画时调用。"
        "一次只生成 1 条，默认约 3 秒、480p；同轮不要重复调用，也不要与 generate_image 同轮混用。"
        "prompt 可用中文（系统会润色成英文镜头描述）。成功后气泡会播放视频，回复只需简短中文说明；"
        "不要伪造外链或输出 base64。本地生成可能需要几分钟。",
        {"type": "object", "properties": {
            "prompt": {"type": "string",
                       "description": "镜头描述（中英文均可；含主体/动作/场景/运镜/风格）"},
            "negative_prompt": {"type": "string",
                                "description": "负面提示词（可选）"},
            "size": {"type": "string",
                     "description": "尺寸；V1 仅 480x832 或 832x480"},
            "duration_sec": {"type": "number",
                             "description": "时长秒数；V1 允许 2–4，默认 3"},
            "n": {"type": "integer",
                  "description": "条数；强制为 1"},
            "motion_hint": {"type": "string",
                            "enum": ["slow", "medium", "dynamic"],
                            "description": "可选运动强度，并入 prompt 前缀"},
            "style_hint": {"type": "string",
                           "enum": ["cinematic", "anime", "realistic"],
                           "description": "可选风格标签，并入 prompt 前缀"}},
         "required": ["prompt"]},
        parallel_safe=False), generate_video)

    registry.register_function(ToolSpec(
        "generate_document",
        "生成文档文件（Word / Markdown / PPT / Excel）供用户下载。当用户要求把内容"
        " 生成/导出为 word、docx、md、markdown、ppt、pptx、xlsx、excel 文档、报告、"
        " 演示文稿或表格文件时调用；只需提供 title（文档标题）与 format"
        " （docx/md/pptx/xlsx，默认 docx），content 留空即可，将由本轮回复正文自动填充；"
        " 返回的下载链接必须原样出现在回复中",
        {"type": "object", "properties": {
            "title": {"type": "string", "description": "文档标题，用作文件名"},
            "format": {"type": "string", "enum": ["docx", "md", "pptx", "xlsx"],
                       "description": "文件格式：docx=Word 文档（默认）；md=Markdown；pptx=PPT 演示文稿；xlsx=Excel 表格"},
            "content": {"type": "string", "description": "文档正文（可选，留空则由回复正文自动填充）"}},
         # content 不强制必填：长文档正文由主回复延迟填充（与 file_write 一致），
         # 避免 tool_infer 阶段因填不出长正文而被 validate_params 硬拒
         "required": ["title"]}), generate_document)

    registry.register_function(ToolSpec(
        "format_template_save",
        "提取附件文档的格式骨架并存为格式模板记忆。当用户上传范例文档并要求"
        "'以后写 XX 按这个格式/记住这个格式'时调用；scenario 为适用场景"
        "（如'产品文档'），attachment_text 由系统自动注入无需手动填写",
        {"type": "object", "properties": {
            "scenario": {"type": "string",
                         "description": "格式适用的场景名称，如'产品文档''技术方案''周报'"},
            "attachment_text": {"type": "string",
                                "description": "附件正文（系统自动注入，无需手动填写）"}},
         "required": ["scenario"]}), format_template_save)
