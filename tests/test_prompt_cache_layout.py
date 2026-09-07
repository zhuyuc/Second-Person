"""Prompt 缓存布局：可变状态走 messages 尾，MCP 按会话门控。"""
from __future__ import annotations

from pathlib import Path

from agent.turn_events import TurnEventStore
from agent.turn_runtime import TurnRuntime
from infrastructure.db import Database

ROOT = Path(__file__).resolve().parent.parent


def _db(tmp_path: Path) -> Database:
    db = Database(tmp_path / "cache_layout.db")
    db.run_migrations(ROOT / "migrations")
    return db


class _Cfg:
    def __init__(self, **values):
        self.values = values

    def get(self, key, default=None):
        return self.values.get(key, default)


def test_model_messages_projects_volatile_context_tail(tmp_path: Path):
    db = _db(tmp_path)
    store = TurnEventStore(db)
    turn = store.start_turn("sess_cache", reasoning_effort="high", max_steps=4)
    turn_id = turn["id"]
    store.append(turn_id, "user.message", actor="user", model_visible=True,
                 payload={"content": "你好"})
    for event_type, text in (
        ("context.mood", "[当前情绪状态] 测试情绪"),
        ("context.location", "[当前位置] 测试位置"),
        ("context.constraints", "本轮约束：测试"),
        ("context.project", "[项目] demo"),
        ("context.project_instructions", "[项目说明书] baseline"),
        ("context.project_instructions_changes", "[项目说明书变更] delta"),
    ):
        store.append(turn_id, event_type, actor="host", model_visible=True,
                     payload={"content": text})

    messages, _ = store.model_messages(turn_id)
    contents = [m["content"] for m in messages if m["role"] == "user"]
    assert contents[0] == "你好"
    assert "[当前情绪状态] 测试情绪" in contents
    assert "[当前位置] 测试位置" in contents
    assert "本轮约束：测试" in contents
    assert "[项目] demo" in contents
    assert "[项目说明书] baseline" in contents
    assert "[项目说明书变更] delta" in contents


def test_system_prompt_keeps_mood_rules_only(tmp_path: Path):
    """system 只含情绪规则；状态/位置/约束由 tail contexts 产出。"""
    from agent.core import AgentCore

    class _Mood:
        def build_rules(self):
            return "【情绪表达规则】稳定规则正文"

        def build_state_context(self):
            return "[当前情绪状态] 本轮可变状态"

    class _Soul:
        def read_core(self):
            return "core"

        def full_style_text(self):
            return "style"

    class _Profile:
        def identity_snippet(self):
            return ""

    class _Skills:
        def load_index(self):
            return ""

        def list_drafts(self):
            return []

    class _Ctx:
        def read_consciousness_hint(self):
            return "请用中文回答"

    class _FakeAssembler:
        def assemble(self, blocks):
            parts = []
            for b in blocks:
                if getattr(b, "content", None):
                    parts.append(f"[{b.key}]\n{b.content}")
            return "\n\n".join(parts)

    class _ToolPrompts:
        def build_rules(self):
            return "工具规则"

    core = AgentCore.__new__(AgentCore)
    core.config = _Cfg(mood_enabled=True, mood_influence_strength=0.5)
    core.mood = _Mood()
    core.soul = _Soul()
    core.profile = _Profile()
    core.skills = _Skills()
    core.ctx_entry = _Ctx()
    core.prompt_assembler = _FakeAssembler()
    core.tool_prompts = _ToolPrompts()
    core.mood_action_dispatcher = None
    core.db = _db(tmp_path)
    core._should_ask_low_confirm = lambda *_a, **_k: False  # noqa: E731
    core.lifecycle = type("L", (), {"next_low_confirm_candidate": lambda self: None})()

    system = core._build_system_prompt(
        onboarding=False, location="上海", sid="sess_x",
        user_message="你好")
    assert "【情绪表达规则】稳定规则正文" in system
    assert "[当前情绪状态]" not in system
    assert "上海" not in system
    assert "请用中文回答" not in system

    tail = core._build_turn_tail_contexts(
        sid="sess_x", onboarding=False, location="上海", user_message="你好")
    assert "[当前情绪状态]" in (tail["mood_context"] or "")
    assert "上海" in (tail["location_context"] or "")
    assert "请用中文回答" in (tail["constraints_context"] or "")


def _make_runtime(db, inject_mode: str) -> TurnRuntime:
    return TurnRuntime(
        db=db,
        config=_Cfg(mcp_tools_inject_mode=inject_mode),
        sessions=None, registry=None, executor=type("E", (), {})(),
        llm=None, providers=None,
        system_prompt=lambda *_a: "s",
        context_loader=lambda **_k: {},
    )


def test_session_ctx_gates_connectors_by_project(tmp_path: Path):
    """mcp_tools_inject_mode=project_only：无 project_id 时不注入连接器。"""
    db = _db(tmp_path)
    db.execute(
        "INSERT INTO sessions(session_id, title) VALUES(?,?)",
        ("sess_no_project", "t"))
    ctx = _make_runtime(db, "project_only")._session_ctx("sess_no_project")
    assert ctx.include_connector_tools is False

    db.execute(
        "INSERT INTO sessions(session_id, title, project_id) VALUES(?,?,?)",
        ("sess_with_project", "t", "proj_1"))
    ctx2 = _make_runtime(db, "project_only")._session_ctx("sess_with_project")
    assert ctx2.include_connector_tools is True

    always = _make_runtime(db, "always")._session_ctx("sess_no_project")
    assert always.include_connector_tools is True
    never = _make_runtime(db, "never")._session_ctx("sess_with_project")
    assert never.include_connector_tools is False
