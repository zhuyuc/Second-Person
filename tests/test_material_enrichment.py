"""答案材料充实层（Material Enrichment Layer）回归测试。

保护契约（全部功能点原子化）：
1. 画像摘要 summary_text：全维度拼装 + 截断（缺口站注入用）
2. 材料块 material_block：通用设计——默认注入全部维度（无场景规则表），
   可显式过滤维度；确认状态 + 推断条目标注（合成注入用）
3. 材料闸门 _material_gate：开关关/无意图/非 chat 意图/brief 场景跳过，
   chat 任务触发（零 LLM）
4. gap_detect.md 契约：material_gap 类型 + 画像对照二规则 + material_slots
5. elicitation_decision.md 契约：已知信息禁止追问
6. synth_elicitation_answered.md 契约：已确认回填/推断注明/仅未知占位
   + 策略/骨架方向豁免
7. response_synth.md 契约：画像材料是个人事实合法来源 + 已备料禁追问
8. PARAM_SCHEMA：material_enrichment_enabled 含 label/desc 且类型 bool
9. GapDetector：画像摘要进入输入 + material_slots 解析透传
10. clarification_router：画像摘要进入 user_prompt（快速通道对称站）
11. 合成层：材料非空时注入产出优先硬约束（含无关维度忽略）+ 策略段降级
12. _has_collect_direction：策略/骨架“信息收集”方向检测（零 LLM）
13. 材料节点二次检索：画像实际内容 + slots 作为检索线索
运行：pytest tests/test_material_enrichment.py -v
"""
import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))

ROOT = Path(__file__).parent.parent

# ---------------------------------------------------------------------------
# 1-3. ProfileManager（零 LLM 方法）
# ---------------------------------------------------------------------------

PROFILE_MD = """---
last_rebuilt: 2026-08-13T03:05:34
source_memory_count: 3
---
## 基本身份 [已确认]
- 产品经理兼独立开发者
- 主要使用中文沟通

## 沟通偏好 [部分推断]
- 偏好直接简洁沟通
- 常在凌晨在线工作[推断]

## 专业领域 [部分推断]
- AI 对话产品设计与开发
- 投资分析，关注宁德时代估值[推断]
"""


@pytest.fixture
def profile_mgr(tmp_path):
    from user_profile.profile_manager import ProfileManager
    d = tmp_path / "profile"
    d.mkdir()
    (d / "user_profile.md").write_text(PROFILE_MD, encoding="utf-8")
    return ProfileManager(tmp_path)


def test_summary_text_joins_dimensions(profile_mgr):
    text = profile_mgr.summary_text()
    assert "基本身份[已确认]" in text
    assert "产品经理兼独立开发者" in text
    assert "沟通偏好[部分推断]" in text
    assert "专业领域[部分推断]" in text


def test_summary_text_truncates(profile_mgr):
    text = profile_mgr.summary_text(max_chars=40)
    assert len(text) <= 40


def test_material_block_all_dimensions_by_default(profile_mgr):
    """通用设计：不传维度时注入全部维度（无场景规则表）。"""
    block = profile_mgr.material_block()
    for name in ("基本身份", "沟通偏好", "专业领域"):
        assert name in block


def test_material_block_dimension_filter_still_works(profile_mgr):
    block = profile_mgr.material_block(["基本身份"])
    assert "基本身份" in block
    assert "沟通偏好" not in block


def test_dimension_names_lists_all(profile_mgr):
    names = profile_mgr.dimension_names()
    assert "基本身份" in names and "专业领域" in names


def test_material_block_marks_inferred(profile_mgr):
    block = profile_mgr.material_block()
    assert "偏好直接简洁沟通" in block
    assert "（推断）" in block  # 推断条目必须标注
    assert "沟通偏好[部分推断]" in block


def test_material_block_missing_dimension_silent(profile_mgr):
    block = profile_mgr.material_block(["不存在的维度"])
    assert block == ""


# ---------------------------------------------------------------------------
# 4. 材料闸门 _material_gate（零 LLM）
# ---------------------------------------------------------------------------

class _Cfg(dict):
    def get(self, k, d=None):
        return super().get(k, d)


class _QR:
    def __init__(self, needs_convergence):
        self.needs_convergence = needs_convergence


class _Intent:
    def __init__(self, itype, tools=None, summary="s"):
        self.intent_type = itype
        self.tools_needed = tools or []
        self.intent_summary = summary


def _fake_core(cfg):
    from agent.core import AgentCore
    return AgentCore.__new__(AgentCore), cfg


def test_gate_disabled_by_config():
    core, cfg = _fake_core(_Cfg({"material_enrichment_enabled": False}))
    core.config = cfg
    assert core._material_gate(
        "帮我写简历", _QR(True), [_Intent("chat")]) is False


def test_gate_no_intents():
    core, cfg = _fake_core(_Cfg({"material_enrichment_enabled": True}))
    core.config = cfg
    assert core._material_gate("帮我写简历", _QR(True), []) is False


def test_gate_non_chat_intent_skipped():
    core, cfg = _fake_core(_Cfg({"material_enrichment_enabled": True}))
    core.config = cfg
    assert core._material_gate(
        "查一下今天天气", _QR(False), [_Intent("query_external")]) is False
    assert core._material_gate(
        "帮我算 1+1", _QR(False), [_Intent("compute")]) is False


def test_gate_brief_skipped():
    core, cfg = _fake_core(_Cfg({"material_enrichment_enabled": True}))
    core.config = cfg
    assert core._material_gate("在吗", _QR(False), [_Intent("chat")]) is False


def test_gate_chat_task_triggered():
    core, cfg = _fake_core(_Cfg({"material_enrichment_enabled": True}))
    core.config = cfg
    assert core._material_gate(
        "帮我写一个简历", _QR(True), [_Intent("chat")]) is True
    # 长消息的 chat（即使快速通道）也触发
    assert core._material_gate(
        "帮我看看这个功能怎么做比较好，总觉得现在的实现有点复杂",
        _QR(False), [_Intent("chat")]) is True


# ---------------------------------------------------------------------------
# 5-7. Prompt 契约（文本级）
# ---------------------------------------------------------------------------

def test_gap_detect_prompt_contract():
    text = (ROOT / "agent" / "prompts" / "gap_detect.md").read_text(
        encoding="utf-8")
    assert "material_gap" in text
    assert "material_slots" in text
    assert "画像" in text  # 画像对照规则
    assert "不触发追问" in text or "不生成 retarget_tasks" in text


def test_elicitation_decision_prompt_contract():
    text = (ROOT / "agent" / "prompts" / "elicitation_decision.md").read_text(
        encoding="utf-8")
    assert "系统已知用户信息（画像）" in text
    assert "禁止进入 questions" in text


def test_synth_elicitation_answered_prompt_contract():
    text = (ROOT / "agent" / "prompts" / "synth_elicitation_answered.md"
            ).read_text(encoding="utf-8")
    assert "已确认事实" in text
    assert "推断事实" in text
    assert "完全未知" in text
    assert "禁止编造" in text
    assert "策略/骨架方向豁免" in text


def test_final_prompt_injects_produce_first_constraint():
    from agent.core import AgentCore
    core = AgentCore.__new__(AgentCore)
    prompt = core._build_final_prompt(
        system_prompt="S", history=[], message="帮我写简历",
        tool_results=[], memories=[], onboarding=False,
        profile_material="- 基本身份[已确认]：产品经理")
    sys_content = prompt[0]["content"]
    assert "产出优先硬约束" in sys_content
    assert "用户画像材料" in sys_content
    assert "与当前任务无关的维度忽略" in sys_content


def test_final_prompt_strategy_downgrade_with_material():
    from agent.core import AgentCore
    core = AgentCore.__new__(AgentCore)
    from types import SimpleNamespace
    strategy = SimpleNamespace(
        angle="先收集信息", depth=1, form="对话型", tone="平和",
        insight_hooks=[])
    prompt = core._build_final_prompt(
        system_prompt="S", history=[], message="帮我写简历",
        tool_results=[], memories=[], onboarding=False,
        strategy=strategy, profile_material="- 基本身份[已确认]：产品经理")
    sys_content = prompt[0]["content"]
    # 策略段声明了与产出优先硬约束的服从关系
    assert "服从产出优先硬约束" in sys_content


def test_final_prompt_no_material_no_constraint():
    from agent.core import AgentCore
    core = AgentCore.__new__(AgentCore)
    prompt = core._build_final_prompt(
        system_prompt="S", history=[], message="帮我写简历",
        tool_results=[], memories=[], onboarding=False)
    assert "产出优先硬约束" not in prompt[0]["content"]


def test_has_collect_direction():
    from agent.core import _has_collect_direction
    from types import SimpleNamespace
    assert _has_collect_direction(
        SimpleNamespace(angle="先收集信息再定制"), None)
    assert _has_collect_direction(
        None, SimpleNamespace(reframe={"real_question": "如何引导用户提供背景信息"}))
    assert not _has_collect_direction(
        SimpleNamespace(angle="全面评估"), None)
    assert not _has_collect_direction(None, None)


def test_response_synth_prompt_contract():
    text = (ROOT / "agent" / "prompts" / "response_synth.md").read_text(
        encoding="utf-8")
    # 画像材料是个人事实的合法来源
    assert "用户画像材料" in text
    # 备料后禁止追问
    assert "视为已备料" in text and "禁止以信息不足为由拒产出" in text


# ---------------------------------------------------------------------------
# 材料节点二次检索：画像材料块内容作为检索线索（知识库/经历记忆可命中）
# ---------------------------------------------------------------------------

class _FakeRetriever:
    def __init__(self):
        self.queries = []

    async def retrieve(self, query, llm_available=True, session_id=None,
                       context_text=None):
        self.queries.append(query)
        from types import SimpleNamespace
        return SimpleNamespace(hits=[], related=[])


def _fake_enrich_core(profile_mgr, retriever):
    from types import SimpleNamespace
    fake = SimpleNamespace(
        profile=profile_mgr,
        retriever=retriever,
        _material_slots=[],
    )
    return fake


def test_material_retrieval_query_contains_profile_content(profile_mgr):
    from agent.core import AgentCore
    retriever = _FakeRetriever()
    core = _fake_enrich_core(profile_mgr, retriever)
    emitted = []

    async def emit(event, data):
        emitted.append((event, data))

    block = profile_mgr.material_block()
    extras = asyncio.run(AgentCore._run_material_enrichment(
        core, "sess-x", "帮我写一个简历", [_Intent("chat", summary="撰写个人简历")],
        block, profile_mgr.dimension_names(), emit))
    # 检索 query 含画像实际内容（产品经理等），而非仅原始提问
    assert retriever.queries, "定向二次检索未被调用"
    q = retriever.queries[0]
    assert "产品经理兼独立开发者" in q
    # 思考外露
    assert any(e == "thinking_delta" and "材料充实" in d["text"]
               for e, d in emitted)


def test_material_retrieval_query_with_slots(profile_mgr):
    from agent.core import AgentCore
    retriever = _FakeRetriever()
    core = _fake_enrich_core(profile_mgr, retriever)
    core._material_slots = ["用户的职业经历与项目经验（需定向检索）"]

    async def emit(event, data):
        pass

    asyncio.run(AgentCore._run_material_enrichment(
        core, "sess-x", "帮我写一个简历", [_Intent("chat", summary="撰写个人简历")],
        profile_mgr.material_block(), profile_mgr.dimension_names(), emit))
    q = retriever.queries[0]
    assert "用户的职业经历与项目经验" in q


# ---------------------------------------------------------------------------
# 8. 参数 schema
# ---------------------------------------------------------------------------

def test_param_schema_has_material_enrichment():
    from infrastructure.config_manager import PARAM_SCHEMA
    spec = {p["key"]: p for p in PARAM_SCHEMA}.get(
        "material_enrichment_enabled")
    assert spec is not None, "material_enrichment_enabled 未登记 PARAM_SCHEMA"
    assert spec["type"] == "bool"
    assert spec.get("label"), "缺少人话 label"
    assert spec.get("desc"), "缺少业务说明 desc"


# ---------------------------------------------------------------------------
# 9-10. 缺口站与追问站的画像摘要注入（fake LLM 捕获输入）
# ---------------------------------------------------------------------------

class _FakeLLM:
    def __init__(self, payload):
        self._payload = payload
        self.calls = []

    async def chat(self, snap, messages, source=None, session_id=None, **kw):
        self.calls.append({"snap": snap, "messages": messages, "source": source})
        return {"content": self._payload}


class _Snap:
    model_id = "fake-model"


def _understanding():
    from agent.intent_parser import (
        Understanding, Intent, EmotionState, FocusResult)
    return Understanding(
        rich_intent=Intent("i1", "撰写个人简历", "chat", confidence=0.8),
        emotion_state=EmotionState(valence="平静", intensity=0.2),
        focus=FocusResult(
            demand_points=[{"point": "写简历", "weight": 1.0}],
            primary_focus="写简历"))


GAP_PAYLOAD = ('{"gaps":[{"type":"material_gap","description":"缺少用户经历材料"}],'
               '"has_gaps":false,"retarget_tasks":[],"unresolvable":false,'
               '"material_slots":["用户的职业经历与项目经验（需定向检索）"]}')


def test_gap_detector_profile_summary_injected_and_slots_parsed():
    from agent.intent_parser import GapDetector
    llm = _FakeLLM(GAP_PAYLOAD)
    det = GapDetector(llm, lambda: _Snap())
    gap = asyncio.run(det.detect(
        _understanding(), "帮我写简历",
        recent_history="用户：帮我写简历",
        profile_summary="基本身份[已确认]：产品经理兼独立开发者"))
    # 画像摘要进入 user content
    user_content = llm.calls[0]["messages"][1]["content"]
    assert "系统已知用户信息（画像）" in user_content
    assert "产品经理兼独立开发者" in user_content
    # material_slots 解析透传
    assert gap.material_slots == ["用户的职业经历与项目经验（需定向检索）"]
    assert not gap.has_gaps


def test_gap_detector_no_profile_omits_section():
    from agent.intent_parser import GapDetector
    llm = _FakeLLM(GAP_PAYLOAD)
    det = GapDetector(llm, lambda: _Snap())
    asyncio.run(det.detect(_understanding(), "帮我写简历"))
    user_content = llm.calls[0]["messages"][1]["content"]
    assert "系统已知用户信息" not in user_content


def test_clarification_router_profile_summary_injected():
    from agent.strategy_engine import StrategyEngine
    llm = _FakeLLM('{"enumerable": true, "questions": [], "reason": "x"}')
    eng = StrategyEngine(llm, lambda: _Snap(), _Cfg(), ROOT / "data")
    asyncio.run(eng.clarification_router(
        "sid-1", "帮我写简历", "缺少岗位方向",
        {"elicitation_max_questions": 3},
        profile_summary="基本身份[已确认]：产品经理兼独立开发者"))
    user_content = llm.calls[0]["messages"][1]["content"]
    assert "系统已知用户信息（画像）" in user_content
    assert "产品经理兼独立开发者" in user_content


def test_clarification_router_no_profile_omits_section():
    from agent.strategy_engine import StrategyEngine
    llm = _FakeLLM('{"enumerable": false, "questions": [], "reason": "x"}')
    eng = StrategyEngine(llm, lambda: _Snap(), _Cfg(), ROOT / "data")
    asyncio.run(eng.clarification_router(
        "sid-1", "帮我写简历", "缺少岗位方向",
        {"elicitation_max_questions": 3}))
    user_content = llm.calls[0]["messages"][1]["content"]
    assert "系统已知用户信息" not in user_content
