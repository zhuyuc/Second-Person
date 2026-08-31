"""Dependency wiring callbacks for AppContainer."""
from __future__ import annotations

import base64
import hashlib
import logging
import re
from pathlib import Path
from typing import Callable

from infrastructure.json_repair import repair_json
from infrastructure.prompt_loader import PROMPTS
from infrastructure.timeutil import now_cst

logger = logging.getLogger("second_person.container.wiring")

DISTILL_PROMPT = PROMPTS.load_raw("app/prompts/distill")
MEMORY_CANDIDATE_PROMPT = PROMPTS.load_raw("agent/prompts/memory_candidate_extract")
DISTILL_DOC_PROMPT = PROMPTS.load_raw("app/prompts/distill_document")
EXTRACT_IMAGE_PROMPT = PROMPTS.load_raw("app/prompts/extract_image")
EXTRACT_IMAGE_USER_PROMPT = PROMPTS.load_raw("app/prompts/extract_image_user")


def setup_domain_labeler(container) -> None:
    from memory.domain_labeler import DomainLabeler

    async def domain_translate_fn(domains: list[str]) -> dict:
        snap = container.providers.snapshot_for("agent")
        if snap is None:
            return {}
        resp = await container.llm.chat(
            snap, [{"role": "system",
                    "content": PROMPTS.load_raw("app/prompts/domain_label")},
                   {"role": "user", "content": "\n".join(domains)}],
            source="system_agent", json_mode=True)
        return repair_json(resp["content"]).get("labels", {})

    container.domain_labeler = DomainLabeler(container.db, domain_translate_fn)


def setup_notifier(container) -> Callable[[str, str], None]:
    def notifier(ntype: str, msg: str) -> None:
        container.notifications.push(ntype, msg)

    container.notifier = notifier
    container.db.alert_hook = notifier
    return notifier


def setup_file_writer_hooks(container) -> None:
    def _refresh_consciousness_hint() -> None:
        try:
            container.ctx_entry.set_consciousness_hint(
                container.index_builder.important_keywords())
        except Exception:  # noqa: BLE001
            logger.warning("意识提示刷新失败", exc_info=True)

    container.refresh_consciousness_hint = _refresh_consciousness_hint

    def _index_rebuild() -> None:
        container.index_builder.rebuild()
        _refresh_consciousness_hint()

    container.fw.index_rebuild_fn = _index_rebuild
    container.fw.context_entry_apply_fn = lambda patch: container.ctx_entry.apply_patch(
        patch)

    def _mark_dirty(domain: str) -> None:
        container.index_builder.mark_dirty(domain)
        container.domain_labeler.schedule(domain)

    container.fw.mark_dirty_fn = _mark_dirty
    container.fw.resolve_failed_fn = lambda: container.notifications.resolve(
        "filewriter_failed", "✅ 此前的写入失败已自动重试成功，数据已恢复，无需处理")


def setup_file_watcher(container, d: Path) -> None:
    from memory.file_watcher import FileWatcher
    from memory.recovery import reindex_changed

    notifier = container.notifier

    def _on_memory_change(paths) -> None:
        try:
            r = reindex_changed(container.db, d, paths, vector_store=container.vs)
            loop = getattr(container, "_loop", None)
            if loop is not None and not loop.is_closed():
                from infrastructure.background_tasks import track_task
                loop.call_soon_threadsafe(
                    lambda: track_task(
                        container.fw.submit("index", {}),
                        name="watcher:index"))
            else:
                container.index_builder.rebuild(force=True)
                container.refresh_consciousness_hint()
            if r.get("invalid_files"):
                notifier("md_format_error",
                         "以下记忆文件格式异常，未更新索引："
                         + "、".join(r["invalid_files"][:5]))
            if r.get("missing"):
                notifier("md_missing",
                         f"检测到 {r['missing']} 个记忆文件被外部删除，可从备份恢复")
        except Exception:  # noqa: BLE001
            logger.exception("记忆文件变更处理失败")

    def _on_soul_change(path) -> None:
        notifier("soul_reloaded", "人格文件已被外部修改，已重新加载")

    container.file_watcher = FileWatcher(
        d, on_memory_change=_on_memory_change,
        on_soul_change=_on_soul_change, on_profile_change=None)
    container.fw.mark_internal_fn = container.file_watcher.mark_internal
    container.sessions.fw = container.fw
    container.projects._mark_internal_fn = container.file_watcher.mark_internal


def setup_embed_fn(container) -> None:
    async def embed_fn(texts: list[str]) -> list[list[float]]:
        snap = container.providers.snapshot_for("embedding")
        if snap is None:
            raise RuntimeError("Embedding 未配置")
        return await container.llm.embed(snap, texts)

    container.embed_fn = embed_fn


def make_llm_refine_fn(container):
    async def llm_refine(query: str, candidates: list[dict],
                         session_id: str | None = None,
                         context_text: str | None = None) -> list[str]:
        snap = container.providers.snapshot_for("retriever_refine")
        if snap is None:
            raise RuntimeError("retriever_refine / agent 槽位均未配置，第 2 层精筛不可用")
        listing = "\n".join(f"{c['id']}: {c['title']} - {c['summary']}"
                            for c in candidates)
        ctx_part = f"最近对话：\n{context_text}\n\n" if context_text else ""
        prompt = [{"role": "system", "content":
                   PROMPTS.load_raw("app/prompts/memory_refine")},
                  {"role": "user", "content":
                   f"{ctx_part}当前问题：{query}\n候选：\n{listing}"}]
        resp = await container.llm.chat(snap, prompt, source="agent",
                                       session_id=session_id, json_mode=True,
                                       extra_body={"thinking_enabled": False})
        data = repair_json(resp["content"])
        return data.get("ids", [])

    return llm_refine


def make_extract_fn(container):
    async def extract_fn(text: str, source_type: str = "memory") -> dict:
        snap = container.providers.snapshot_for("agent")
        if snap is None:
            return {"items": []}
        prompt = (DISTILL_DOC_PROMPT if source_type == "knowledge"
                  else MEMORY_CANDIDATE_PROMPT if source_type == "memory"
                  else DISTILL_PROMPT)
        resp = await container.llm.chat(
            snap, [{"role": "system", "content": prompt},
                   {"role": "user", "content": text}], source="system_agent",
            json_mode=True)
        try:
            data = repair_json(resp["content"])
        except ValueError as exc:
            from infrastructure.json_repair import REPAIR_STATS
            logger.warning(
                "extract_fn JSON 修复失败：source_type=%s consecutive=%d err=%s",
                source_type, REPAIR_STATS.consecutive_failures, exc)
            from memory import _constants as _mem_const
            threshold = _mem_const.JSON_REPAIR_ALERT_THRESHOLD
            if (REPAIR_STATS.consecutive_failures >= threshold
                    and hasattr(container, "notifications")):
                try:
                    container.notifications.push(
                        "json_repair_failed",
                        f"提取器 JSON 连续 {REPAIR_STATS.consecutive_failures} "
                        "次修复失败，请检查模型输出质量")
                except Exception:  # noqa: BLE001
                    pass
            return {"items": []}
        items = data.get("items") if isinstance(data, dict) else None
        if not items:
            logger.debug("extract_fn 无候选：source_type=%s len=%d",
                         source_type, len(text))
        return data

    return extract_fn


def make_image_extract_fn(container):
    async def image_extract_fn(path) -> str:
        engine = container.config.get("image_parse_engine", "vlm")
        if engine == "off":
            return ""
        if engine == "ocr":
            from scheduler.ingest import ocr_extract_text
            return await ocr_extract_text(path)
        snap = container.providers.snapshot_for("vision")
        if snap is None:
            return ""
        try:
            from scheduler.ingest import image_to_data_url
            url = image_to_data_url(path)
            resp = await container.llm.chat(
                snap, [{"role": "system", "content": EXTRACT_IMAGE_PROMPT},
                       {"role": "user", "content": EXTRACT_IMAGE_USER_PROMPT}],
                images=[url], source="vision")
            return (resp.get("content") or "").strip()
        except Exception:  # noqa: BLE001
            logger.warning("图片 VLM 解析失败：%s", path)
            return ""

    return image_extract_fn


def make_skill_draft_fn(container):
    async def skill_draft_fn(item: dict) -> None:
        title = item.get("title", "skill")
        key = title.strip().lower()[:40]
        if not key:
            return
        now = now_cst().isoformat(timespec="seconds")
        row = container.db.query_one(
            "SELECT occurrences, drafted FROM skill_patterns WHERE pattern_key=?",
            (key,))
        if row:
            occ = (row["occurrences"] or 0) + 1
            container.db.execute(
                "UPDATE skill_patterns SET occurrences=?, last_seen=?, "
                "title=?, detail=? WHERE pattern_key=?",
                (occ, now, title, item.get("detail", ""), key))
            drafted = row["drafted"]
        else:
            occ, drafted = 1, 0
            container.db.execute(
                "INSERT INTO skill_patterns(pattern_key,title,detail,occurrences,"
                "drafted,first_seen,last_seen) VALUES(?,?,?,1,0,?,?)",
                (key, title, item.get("detail", ""), now, now))
        threshold = container.config.get("skill_draft_threshold", 3)
        if occ >= threshold and not drafted:
            name = title[:20].replace(" ", "_")
            md = f"---\nstatus: draft\n---\n# {title}\n{item.get('detail', '')}"
            await container.skills.create_draft(name, md)
            container.db.execute(
                "UPDATE skill_patterns SET drafted=1 WHERE pattern_key=?", (key,))

    return skill_draft_fn


def make_soul_feedback_fn(container):
    async def soul_feedback_fn(item: dict) -> None:
        feedback_kind = item.get("feedback_kind", "persona")
        proposed = item.get("detail", "")
        canonical_dim = item.get("canonical_dim", "")

        if feedback_kind == "style" and canonical_dim and canonical_dim != "other":
            style_key = f"style:{canonical_dim}:{proposed[:50].strip()}"
            style_change_key = hashlib.md5(
                style_key.encode()).hexdigest()[:16]
            now_str = now_cst().isoformat(timespec="seconds")
            if container.conflict_scanner.check_rejection_protection(style_change_key, now_str):
                return
            occ, newly_enqueued = container.conflict_scanner.accumulate_feedback(
                style_change_key, proposed, item.get(
                    "summary", ""), "style"
            )
            if newly_enqueued:
                current_dialog = container.soul.read_style().get("对话风格", "")
                container.conflict_scanner.enqueue_persona_review(
                    style_change_key, proposed, item.get("summary", ""),
                    occ, current_dialog
                )
            return

        ptype = "behavior" if feedback_kind == "behavior" else "persona"
        raw_key = f"persona:{proposed[:50].strip()}"
        change_key = hashlib.md5(raw_key.encode()).hexdigest()[:16]
        now_str = now_cst().isoformat(timespec="seconds")

        if container.conflict_scanner.check_rejection_protection(change_key, now_str):
            return

        occ, newly_enqueued = container.conflict_scanner.accumulate_feedback(
            change_key, proposed, item.get("summary", ""), ptype
        )
        if newly_enqueued:
            current_dialog = container.soul.read_style().get("对话风格", "")
            container.conflict_scanner.enqueue_persona_review(
                change_key, proposed, item.get(
                    "summary", ""), occ, current_dialog
                )

    return soul_feedback_fn


def make_merge_judge_fn(container):
    async def merge_judge_fn(new_item: dict, existing: dict) -> dict:
        snap = container.providers.snapshot_for("agent")
        if snap is None:
            return {"relation": "same"}
        existing_detail = (existing.get("detail") or "")[:300]
        content = (f"新信息：{new_item.get('title', '')} — "
                   f"{new_item.get('summary', '')}\n{new_item.get('detail', '')}\n\n"
                   f"已有记忆：{existing.get('title', '')} — "
                   f"{existing.get('summary', '')}"
                   + (f"\n{existing_detail}" if existing_detail else ""))
        resp = await container.llm.chat(
            snap, [{"role": "system",
                    "content": PROMPTS.load_raw("app/prompts/merge_judge")},
                   {"role": "user", "content": content}], source="system_agent",
            json_mode=True)
        return repair_json(resp["content"])

    return merge_judge_fn


def setup_image_kb_fn(container) -> None:
    async def image_kb_fn(images) -> None:
        for idx, url in enumerate(images or []):
            m = re.match(
                r"data:(image/[\w.+-]+);base64,(.+)", url or "", re.S)
            if not m:
                continue
            ext = m.group(1).split("/")[1].split("+")[0]
            try:
                content = base64.b64decode(m.group(2))
            except Exception:  # noqa: BLE001
                continue
            fn = f"chat-image-{now_cst():%Y%m%d%H%M%S}-{idx+1}.{ext}"
            await container.ingest.ingest_file(fn, content, source="chat_image")

    container.core.image_kb_fn = image_kb_fn


def build_container_layers(container, d: Path) -> None:
    """Wire file-watcher / embed callbacks after FileWriter exists."""
    setup_file_writer_hooks(container)
    setup_file_watcher(container, d)
    setup_embed_fn(container)
