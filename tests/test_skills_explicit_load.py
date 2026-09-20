"""Storyboard auto + director-style @-only skills."""
from __future__ import annotations

from pathlib import Path

from agent.turn_events import TurnEventStore
from infrastructure.db import Database
from soul.builtin_skills import DEPRECATED_STORYBOARD_SKILLS, STORYBOARD_CORE_NAME
from soul.skill_manager import SkillManager

ROOT = Path(__file__).resolve().parent.parent


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "skills.db")
    db.run_migrations(ROOT / "migrations")
    return db


def test_builtin_install_picker_and_auto_storyboard(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    db = _db(tmp_path)
    sm = SkillManager(data, db)
    sm.ensure_builtin_skills()

    assert STORYBOARD_CORE_NAME in sm.active_names()
    assert sm.is_picker_visible(STORYBOARD_CORE_NAME) is False
    picker = sm.list_for_picker()
    assert all(i["category"] == "director-style" for i in picker)
    assert len(picker) >= 20
    assert sm.resolve_explicit_refs([STORYBOARD_CORE_NAME]) == []
    assert "style-wong-kar-wai" in sm.resolve_explicit_refs(["style-wong-kar-wai"])
    wong = next(i for i in picker if i["name"] == "style-wong-kar-wai")
    assert "冲突押在距离" in wong["brief"]
    assert "风格透镜" not in wong["brief"]
    wong_md = (data / "skills" / "style-wong-kar-wai" / "SKILL.md").read_text(encoding="utf-8")
    assert "## 取舍" in wong_md
    assert "## 冲突" in wong_md
    assert "和谁不一样" not in wong_md

    # Workshop always auto storyboard
    assert sm.needs_auto_storyboard(
        channel="workshop", message="随便", has_director_style=False)
    # Chat needs intent
    assert sm.needs_auto_storyboard(
        channel="web", message="帮我写分镜", has_director_style=False)
    assert not sm.needs_auto_storyboard(
        channel="web", message="今天天气怎么样", has_director_style=False)

    ctx = sm.assemble_turn_skills_context(
        auto_names=[STORYBOARD_CORE_NAME],
        explicit_names=["style-wong-kar-wai"],
    )
    assert ctx and "[内置分镜能力]" in ctx and "[本轮大师风格]" in ctx
    assert "王家卫" in ctx or "wong" in ctx.lower()
    assert "一镜要撑起什么" in ctx or "人物、背景、声光" in ctx
    assert "## 取舍" in ctx
    assert "## 冲突" in ctx


def test_deprecated_storyboard_archived(tmp_path: Path):
    data = tmp_path / "data"
    data.mkdir()
    db = _db(tmp_path)
    sm = SkillManager(data, db)
    # Plant an old @ storyboard skill as active
    old = DEPRECATED_STORYBOARD_SKILLS[0]
    sdir = data / "skills" / old
    sdir.mkdir(parents=True)
    (sdir / "SKILL.md").write_text(
        f"---\nname: {old}\ncategory: storyboard\nstatus: active\n---\n# old\n",
        encoding="utf-8",
    )
    db.execute(
        "INSERT INTO skill_usage(skill_id,skill_name,status,use_count,last_used)"
        " VALUES(?,?, 'active', 0, ?)",
        (f"skill_{old}", old, "2020-01-01T00:00:00"))
    sm.ensure_builtin_skills()
    row = db.query_one("SELECT status FROM skill_usage WHERE skill_name=?", (old,))
    assert row["status"] == "archived"
    assert old not in [i["name"] for i in sm.list_for_picker()]


def test_context_skills_event_still_tails(tmp_path: Path):
    db = _db(tmp_path)
    store = TurnEventStore(db)
    turn = store.start_turn("sess_skill", reasoning_effort="high", max_steps=4)
    turn_id = turn["id"]
    store.append(turn_id, "user.message", actor="user", model_visible=True,
                 payload={"content": "写分镜"})
    store.append(turn_id, "context.skills", actor="host", model_visible=True,
                 payload={"content": "[内置分镜能力] demo"})
    messages, _ = store.model_messages(turn_id)
    contents = [m["content"] for m in messages if m["role"] == "user"]
    assert any("[内置分镜能力]" in c for c in contents)


def test_chat_send_contract_skill_refs():
    from app.contracts import parse_chat_send

    req = parse_chat_send({
        "message": "hello",
        "skill_refs": ["style-wong-kar-wai", "style-nolan", "extra"],
    })
    assert req.skill_refs == ["style-wong-kar-wai", "style-nolan"]
