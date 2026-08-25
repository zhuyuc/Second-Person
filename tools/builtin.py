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
import operator
import uuid
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

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
        if memory_gate is not None:
            decision = memory_gate.evaluate(
                {"title": title, "summary": summary, "detail": detail,
                 "domain": domain, "attribution": "verified"},
                "memory", explicit=True)
            if not decision.allowed:
                raise ValueError(decision.reason)
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

    async def shell_exec(cmd: str, timeout: int = 30) -> dict:
        sandbox.check_command(cmd)
        proc = await asyncio.create_subprocess_shell(
            cmd, cwd=str(sandbox.workspace),
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
        cap = config.get("web_fetch_timeout_seconds", 15)
        return await _web_fetch(url, timeout=min(timeout or cap, cap),
                                allow_private=config.get_raw("allow_private_network_fetch", False))

    async def web_search_tool(query: str, max_results: int = 5) -> list:
        return await _web_search(query, max_results=max_results,
                                 timeout=config.get("web_fetch_timeout_seconds", 15))

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
         "required": ["title", "summary", "detail", "domain"]},
        destructive=True), memory_save)

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
         "required": ["path", "content"]}, destructive=True), file_write)

    registry.register_function(ToolSpec(
        "shell_exec", "在工作区执行 shell 命令",
        {"type": "object", "properties": {
            "cmd": {"type": "string"}, "timeout": {"type": "integer"}},
         "required": ["cmd"]}, destructive=True), shell_exec)

    registry.register_function(ToolSpec(
        "web_fetch", "抓取网页正文",
        {"type": "object", "properties": {
            "url": {"type": "string"}, "timeout": {"type": "integer"}},
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
         "required": ["diagram_type", "mermaid_code"]},
        destructive=False), render_mermaid)

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
         "required": ["nodes", "edges"]},
        destructive=False), render_flowchart)

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
         "required": ["scenario"]}, destructive=True), format_template_save)
