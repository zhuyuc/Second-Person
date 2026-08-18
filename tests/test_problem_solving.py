"""自动路由、深度问题模型、质量门和长文可恢复交付的回归测试。"""
from __future__ import annotations

import asyncio
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.core import AgentCore, DeliveryJobManager
from agent.intent_parser import IntentParser
from agent.meta_cognitive import (
    DeliveryContract,
    ProblemModel,
    ProblemModelBuilder,
    RequirementItem,
)
from agent.response_synthesizer import QualityGate
from agent.session_context import SessionStore
from infrastructure.db import Database
from infrastructure.llm_provider import CircuitOpenError
from infrastructure.provider_registry import TASK_SLOTS

class _Snap:
    model_id = "fake"


class _LLM:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    async def chat(self, snap, messages, source=None, session_id=None, **kwargs):
        self.calls.append({"source": source, "messages": messages})
        value = self.responses.pop(0)
        if isinstance(value, BaseException):
            raise value
        return {"content": value}


def _model(form: str = "structured") -> ProblemModel:
    return ProblemModel(
        user_goal="完成完整产品方案",
        contract=DeliveryContract(
            deliverable_type="plan",
            delivery_form=form,
            explicit_requirements=[
                RequirementItem("R1", "给出自动路由方案", "提供实现机制", ["模型路由"], ["说明机制"]),
                RequirementItem("R2", "给出深度质量方案", "提供质量门", ["需求覆盖"], ["说明验收"]),
            ],
            acceptance_criteria=["两个明确需求都有解法"],
        ),
        assumptions=["模型路由结果需要校准"],
        analysis_actions=["需求覆盖", "质量校验"],
        outline=[
            {"id": "S1", "title": "自动路由", "requirement_ids": ["R1"]},
            {"id": "S2", "title": "深度质量", "requirement_ids": ["R2"]},
        ],
    )


def test_problem_model_preserves_explicit_list_items():
    builder = ProblemModelBuilder(_LLM([json.dumps({
        "user_goal": "只回答第一个", "requirements": [{
            "id": "R1", "raw_request": "实现自动模式", "expected_outcome": "模式", "solution_required": True,
        }], "contract": {"delivery_form": "structured"},
    }, ensure_ascii=False)]), lambda: _Snap())
    model = asyncio.run(builder.build("实现自动模式\n实现深度质量门", session_id="s"))
    assert len(model.contract.explicit_requirements) >= 2
    assert any("深度质量门" in r.raw_request for r in model.contract.explicit_requirements)


def test_problem_model_does_not_cap_explicit_requirement_count():
    request = "\n".join(f"- 需求 {i}：给出对应的完整解法" for i in range(1, 25))
    builder = ProblemModelBuilder(_LLM([]), lambda: None)
    model = asyncio.run(builder.build(request, session_id="s"))
    assert len(model.contract.explicit_requirements) == 24


def test_quality_gate_requires_each_explicit_requirement():
    report = QualityGate().validate("R1 自动路由方案：使用模型路由机制。", _model())
    assert not report.passed
    assert "R2" in report.missing_requirements
    complete = (
        "R1 自动路由方案：使用模型路由机制，依赖模型路由，按命中率验证。\n"
        "R2 深度质量方案：需求覆盖质量门逐项校验，依赖需求覆盖，按验收验证。\n"
        "假设：模型路由结果需要校准。风险、边界与验收：持续回归测试。"
    )
    assert QualityGate().validate(complete, _model()).passed


def test_quality_gate_rejects_requirement_list_without_a_solution():
    response = "R1 自动路由方案\nR2 深度质量方案\n风险与验收待后续排期。"
    report = QualityGate().validate(response, _model())
    assert not report.passed
    assert set(report.missing_requirements) == {"R1", "R2"}


def test_quick_intent_failure_is_conservative_deep():
    parser = IntentParser(_LLM([]), lambda: None)
    result = asyncio.run(parser.quick_intent("给我一个完整系统方案"))
    assert result.needs_convergence is True
    assert result.complexity_hint >= 5


def test_deep_analysis_slot_registered():
    slot = TASK_SLOTS["deep_analysis"]
    assert slot.label == "深度分析模型"
    assert slot.fallback == ("agent", "chat")


def test_deep_final_answer_prefers_the_deep_analysis_slot():
    source = (ROOT / "agent" / "core.py").read_text(encoding="utf-8")
    assert 'self.providers.snapshot_for("deep_analysis")' in source
    assert "self.llm.stream(response_snap, prompt" in source


def test_session_persists_safe_analysis_metadata(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.run_migrations(ROOT / "migrations")
    store = SessionStore(db, tmp_path / "data")
    sid = store.create_session()
    store.append_message(sid, "assistant", "答案", analysis_metadata={
        "requested_mode": "auto", "effective_mode": "deep", "route_reason": "多需求",
    })
    message = store.get_messages(sid)[0]
    assert message["analysis_metadata"]["effective_mode"] == "deep"
    db.close()


def test_deleting_session_removes_its_delivery_jobs(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.run_migrations(ROOT / "migrations")
    store = SessionStore(db, tmp_path / "data")
    sid = store.create_session()
    manager = DeliveryJobManager(db, _LLM([]), lambda: _Snap(), QualityGate())
    job = manager.create_or_resume(sid, "完整报告", _model("long_document"), "system")
    assert db.query_one("SELECT id FROM delivery_jobs WHERE id=?", (job["id"],))
    store.delete_session(sid)
    assert db.query_one("SELECT id FROM delivery_jobs WHERE id=?", (job["id"],)) is None
    assert db.query_one("SELECT id FROM delivery_sections WHERE job_id=?", (job["id"],)) is None
    db.close()


def test_delivery_job_resumes_completed_sections(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.run_migrations(ROOT / "migrations")
    llm = _LLM([
        "自动路由方案含机制、依赖、风险和验收。<!-- SECTION_COMPLETE -->",
        "深度质量方案含机制、依赖、风险和验收。假设需要校准。<!-- SECTION_COMPLETE -->",
    ])
    manager = DeliveryJobManager(db, llm, lambda: _Snap(), QualityGate())
    job = manager.create_or_resume("s1", "请写完整报告", _model("long_document"), "system")
    events = []

    async def emit(event, data):
        events.append((event, data))

    text, report = asyncio.run(manager.run(job["id"], session_id="s1", emit=emit))
    assert "自动路由" in text and "深度质量" in text
    assert any(event == "delivery_progress" and data["status"] == "completed" for event, data in events)
    # 再次运行不会重复调用模型，而是从 completed 章节直接拼装恢复。
    before = len(llm.calls)
    text2, _ = asyncio.run(manager.run(job["id"], session_id="s1", emit=emit))
    assert text2 == text
    assert len(llm.calls) == before
    assert report.coverage
    db.close()


def test_delivery_section_continues_until_explicit_completion_marker():
    llm = _LLM([
        "第一段保留完整方案细节。",
        "第二段补足依赖、风险和验收。<!-- SECTION_COMPLETE -->",
    ])
    manager = DeliveryJobManager(None, llm, lambda: _Snap(), QualityGate())
    model = _model("long_document")
    section = {
        "title": "自动路由",
        "requirement_ids_json": json.dumps(["R1"], ensure_ascii=False),
    }
    text = asyncio.run(manager._generate_section(
        _Snap(), {"base_system": "system"}, model, section, "s1"))
    assert "第一段" in text and "第二段" in text
    assert len(llm.calls) == 2


def test_gap_section_is_persisted_for_recovery(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.run_migrations(ROOT / "migrations")
    llm = _LLM([
        "补充内容：使用质量门校验需求覆盖、依赖、风险和验收。<!-- SECTION_COMPLETE -->",
    ])
    manager = DeliveryJobManager(db, llm, lambda: _Snap(), QualityGate())
    job = manager.create_or_resume("s1", "请写完整报告", _model("long_document"), "system")
    report = QualityGate().validate("R1 使用模型路由机制。R2 使用需求覆盖校验机制。", _model("long_document"))
    text = asyncio.run(manager._append_gap_section(
        _Snap(), job, _model("long_document"), "原始正文", report, "s1"))
    row = db.query_one(
        "SELECT status,content FROM delivery_sections WHERE job_id=? AND section_key='S_GAP'",
        (job["id"],))
    assert "补充内容" in text
    assert row["status"] == "completed" and "补充内容" in row["content"]
    db.close()


def test_delivery_job_keeps_progress_resumable_when_model_is_circuit_open(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.run_migrations(ROOT / "migrations")
    manager = DeliveryJobManager(
        db, _LLM([CircuitOpenError("open")]), lambda: _Snap(), QualityGate())
    job = manager.create_or_resume("s1", "请写完整报告", _model("long_document"), "system")

    async def emit(_event, _data):
        return None

    try:
        asyncio.run(manager.run(job["id"], session_id="s1", emit=emit))
    except CircuitOpenError:
        pass
    assert db.query_one("SELECT status FROM delivery_jobs WHERE id=?", (job["id"],))["status"] == "paused"
    assert manager.create_or_resume("s1", "请写完整报告", _model("long_document"), "system")["id"] == job["id"]
    db.close()


def test_delivery_job_reuses_completed_sections_after_a_retryable_failure(tmp_path: Path):
    db = Database(tmp_path / "test.db")
    db.run_migrations(ROOT / "migrations")
    manager = DeliveryJobManager(
        db, _LLM([RuntimeError("network")]), lambda: _Snap(), QualityGate())
    job = manager.create_or_resume("s1", "请写完整报告", _model("long_document"), "system")

    async def emit(_event, _data):
        return None

    try:
        asyncio.run(manager.run(job["id"], session_id="s1", emit=emit))
    except RuntimeError:
        pass
    assert db.query_one("SELECT status FROM delivery_jobs WHERE id=?", (job["id"],))["status"] == "failed"
    resumed = manager.create_or_resume("s1", "请写完整报告", _model("long_document"), "system")
    assert resumed["id"] == job["id"]
    db.close()


def test_closing_core_event_stream_cancels_its_worker():
    core = AgentCore.__new__(AgentCore)
    core.config = type("Config", (), {"get": lambda *_: 3})()
    core._session_queue = defaultdict(int)
    core._session_locks = defaultdict(asyncio.Lock)
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def fake_pipeline(_sid, _message, emit, *_args, **_kwargs):
        await emit("started", {})
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    core._pipeline = fake_pipeline

    async def exercise():
        stream = core.run("s1", "取消测试")
        first = await anext(stream)
        assert first["event"] == "started"
        assert started.is_set()
        await stream.aclose()
        await asyncio.wait_for(cancelled.wait(), timeout=1)

    asyncio.run(exercise())
