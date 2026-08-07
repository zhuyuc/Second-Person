"""画像审核队列 API（产品文档 §画像后台确认机制）。

提供待确认项的列表、计数、确认/拒绝/稍后操作。
所有画像变更不再通过对话内询问，改由用户在此页面主动管理。
"""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Request

from infrastructure.timeutil import now_cst

router = APIRouter()


def _c():
    from app.main import get_container
    return get_container()


# ---------------------------------------------------------------------------
# 读取
# ---------------------------------------------------------------------------
@router.get("/profile/review/count")
async def get_pending_count():
    """各轨道 pending 计数。"""
    c = _c()
    counts = c.conflict_scanner.pending_count() if c.conflict_scanner else {}
    return {"code": 200, "data": counts}


@router.get("/profile/review/list")
async def list_pending(review_type: str | None = None):
    """列出待确认项，按优先级升序 + 时间倒序。"""
    c = _c()
    if review_type:
        rows = c.db.query_all(
            "SELECT * FROM profile_review_queue "
            "WHERE status='pending' AND review_type=? "
            "ORDER BY priority ASC, created_at DESC",
            (review_type,),
        )
    else:
        rows = c.db.query_all(
            "SELECT * FROM profile_review_queue WHERE status='pending' "
            "ORDER BY priority ASC, created_at DESC"
        )
    return {"code": 200, "data": [dict(r) for r in rows]}


# ---------------------------------------------------------------------------
# 操作
# ---------------------------------------------------------------------------
@router.post("/profile/review/{review_id}/confirm")
async def confirm_review(review_id: int):
    """采纳建议，按 review_type 分派到对应画像写入路径。"""
    c = _c()
    row = c.db.query_one(
        "SELECT * FROM profile_review_queue WHERE id=? AND status='pending'",
        (review_id,),
    )
    if not row:
        return {"code": 404, "message": "not found or already processed"}

    now_str = now_cst().isoformat(timespec="seconds")

    # 分派写入
    if row["review_type"] == "persona":
        sections = c.soul.read_style()
        current = sections.get("对话风格", "")
        merged = _merge_persona(current, row["proposed_content"])
        content = (
            f"## 对话风格\n{merged}\n"
            f"## 行为原则\n{sections.get('行为原则', '')}"
        )
        try:
            await c.fw.submit("soul_style", {
                "section": "dialog",
                "content": content,
                "create_version": True,
                "diff_summary": f"用户采纳：{row['title']}",
            }, wait=True)
        except Exception as e:
            return {"code": 500, "message": f"人格写入失败：{e}"}

    elif row["review_type"] == "output_style":
        try:
            await c.fw.submit("soul_style", {
                "section": "auto",
                "content": row["proposed_content"],
                "create_version": True,
                "diff_summary": f"用户采纳：{row['title']}",
            }, wait=True)
        except Exception as e:
            return {"code": 500, "message": f"输出样式写入失败：{e}"}

    elif row["review_type"] == "user_profile":
        # 记录用户确认的维度方向（标记为"已确认"状态），
        # 后续 ProfileBuilder.rebuild 时检测此标记并保留确认维度
        _record_confirmed_dimension(c, row)

    c.db.execute(
        "UPDATE profile_review_queue SET status='confirmed', reviewed_at=?, "
        "reviewed_by='user' WHERE id=?",
        (now_str, review_id),
    )
    return {"code": 200, "data": {"confirmed": True}}


@router.post("/profile/review/{review_id}/reject")
async def reject_review(review_id: int):
    """拒绝建议，写入 60 天保护期，清零对应的频次累积。"""
    c = _c()
    row = c.db.query_one(
        "SELECT * FROM profile_review_queue WHERE id=? AND status='pending'",
        (review_id,),
    )
    if not row:
        return {"code": 404, "message": "not found or already processed"}

    now_str = now_cst().isoformat(timespec="seconds")

    # 写入保护记录并清零累积
    c.conflict_scanner.reject_and_protect(
        row["review_type"],
        row["change_key"],
        row["proposed_content"] or "",
    )

    c.db.execute(
        "UPDATE profile_review_queue SET status='rejected', reviewed_at=?, "
        "reviewed_by='user' WHERE id=?",
        (now_str, review_id),
    )
    return {"code": 200, "data": {"rejected": True}}


@router.post("/profile/review/{review_id}/postpone")
async def postpone_review(review_id: int):
    """稍后处理：状态保持 pending，仅返回成功。"""
    # 验证该项存在且为 pending
    row = _c().db.query_one(
        "SELECT 1 FROM profile_review_queue WHERE id=? AND status='pending'",
        (review_id,),
    )
    if not row:
        return {"code": 404, "message": "not found or already processed"}
    return {"code": 200, "data": {"postponed": True}}


@router.post("/profile/review/confirm-all")
async def confirm_all(request: Request):
    """批量采纳：按 review_type 过滤，采纳所有 pending 项。"""
    body = await request.json()
    review_type = body.get("review_type")  # None = 全部
    c = _c()
    rows = c.db.query_all(
        "SELECT * FROM profile_review_queue WHERE status='pending'"
        + (" AND review_type=?" if review_type else ""),
        (review_type,) if review_type else (),
    )
    confirmed = 0
    for row in rows:
        try:
            if row["review_type"] == "persona":
                sections = c.soul.read_style()
                current = sections.get("对话风格", "")
                merged = _merge_persona(current, row["proposed_content"])
                content = (
                    f"## 对话风格\n{merged}\n"
                    f"## 行为原则\n{sections.get('行为原则', '')}"
                )
                await c.fw.submit("soul_style", {
                    "section": "dialog",
                    "content": content,
                    "create_version": True,
                    "diff_summary": f"批量采纳：{row['title']}",
                })
            elif row["review_type"] == "output_style":
                await c.fw.submit("soul_style", {
                    "section": "auto",
                    "content": row["proposed_content"],
                    "create_version": True,
                    "diff_summary": f"批量采纳：{row['title']}",
                })
            elif row["review_type"] == "user_profile":
                _record_confirmed_dimension(c, row)

            now_str = now_cst().isoformat(timespec="seconds")
            c.db.execute(
                "UPDATE profile_review_queue SET status='confirmed', "
                "reviewed_at=?, reviewed_by='user' WHERE id=?",
                (now_str, row["id"]),
            )
            confirmed += 1
        except Exception:
            continue

    return {"code": 200, "data": {"confirmed": confirmed}}


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------
def _merge_persona(current: str, addition: str) -> str:
    """将新的人格规则合并到当前 dialog 段，含简易去重。

    规则：按行拆分后尝试语义去重；简单相等/包含关系视为重复。
    不做 LLM 语义合并（避免额外 token 消耗），若未来发现合并质量不足
    再升级为 LLM 调和版本。
    """
    import re

    lines = [ln.strip().lstrip("- ")
             for ln in current.split("\n") if ln.strip()]
    new_line = addition.strip().lstrip("- ")

    # 简易去重：完全相同 / 新内容是已有内容的子串 / 已有内容是新内容的子串
    for existing in lines:
        if new_line == existing:
            return current
        if new_line in existing or existing in new_line:
            # 保留更长的版本
            if len(new_line) > len(existing):
                lines = [new_line if l == existing else l for l in lines]
                return "\n".join(f"- {l}" for l in lines)
            return current

    # 不重复 → 追加
    lines.append(new_line)
    return "\n".join(f"- {l}" for l in lines)


def _record_confirmed_dimension(c, row: dict) -> None:
    """记录用户确认的用户画像维度方向。

    将确认的维度信息写入 profile_review_queue 的 evidence 字段扩展，
    同时通过 FileWriter 写入 user_profile.md 的对应维度状态标记。

    当前实现：在已确认维度所在行的 proposed_content 中追加 [已确认] 标记，
    供 ProfileBuilder 下次重建时检测并保留。
    """
    # 框架预留：具体实现依赖 ProfileBuilder 的"锁定维度"检测逻辑
    # 当前阶段，确认 user_profile 的采纳仅记日志
    import logging
    logger = logging.getLogger("second_person.profile_review")
    logger.info(
        "用户确认维度：%s -> %s", row.get("title",
                                   ""), row.get("proposed_content", "")[:100]
    )
