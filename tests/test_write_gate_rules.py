"""write_gate 规则化再评分 / 意向词强制降级 / knowledge 门禁 契约测试。"""
from memory.write_gate import (
    MemoryWriteGate, derive_rule_signals, has_tentative_marker, score_item,
)


class _Cfg(dict):
    def get(self, key, default=None):
        return super().get(key, default)


def _item(text, attribution="verified", entities=None, **overrides):
    base = {
        "title": text[:30], "summary": text, "detail": text,
        "domain": "work", "attribution": attribution,
        "entities": entities or [],
        # 模拟 LLM 灌水的自评分：全给到接近满分
        "stability": 0.95, "reuse": 0.95, "user_specificity": 0.95,
        "explicitness": 0.9,
    }
    base.update(overrides)
    return base


def test_llm_self_score_is_capped_by_rules():
    """LLM 灌水 stability=0.95 时，规则派生上限应生效。"""
    tentative = _item("我大概会考虑用 Rust 重写这个模块")
    _, parts = score_item(tentative)
    assert parts["stability"] <= 0.35, parts
    assert parts["reuse"] <= 0.35, parts

    verified_with_first_person = _item("我一直用 VS Code 做前端开发")
    _, parts = score_item(verified_with_first_person)
    # 稳定的第一人称长期偏好可以拿到 verified 的上限
    assert parts["stability"] == 0.7
    assert parts["user_specificity"] >= 0.6


def test_explicitness_capped_without_anchor():
    """LLM 说 explicitness=0.9，但没有"记住/以后都"锚点，只能到 0.5。"""
    item = _item("我经常用 pandas 做数据分析")
    _, parts = score_item(item)
    assert parts["explicitness"] <= 0.5


def test_explicit_anchor_allows_full_explicitness():
    item = _item("请记住我以后都用 pytest 而不是 unittest")
    _, parts = score_item(item)
    assert parts["explicitness"] == 1.0


def test_temporary_pattern_kills_score():
    """临时词直接把 stability/reuse 压顶，且触发 temporariness 减 25 分。"""
    for text in (
        "我今天想试试新的键盘布局",
        "for now let's use draft mode",
        "这次帮我用方案二",
    ):
        s, parts = score_item(_item(text))
        assert parts["temporariness"] == 1.0, text
        assert s < 45, (text, s)


def test_gate_forces_inferred_on_tentative():
    """命中'我大概/probably'类意向词 → attribution 强制降为 inferred + low。"""
    gate = MemoryWriteGate(None, _Cfg(memory_candidate_min_score=45))
    item = _item("我大概会转向 Kubernetes", attribution="verified")
    gate.evaluate(item, "memory")
    assert item["attribution"] == "inferred"
    assert item["confidence"] == "low"


def test_knowledge_channel_has_min_score():
    """imported 走 knowledge 通道时也过 memory_knowledge_min_score。"""
    gate = MemoryWriteGate(None, _Cfg(memory_knowledge_min_score=55))
    # 描述第三人的低 specificity 文档条目（含实体过 is_well_formed）
    low = _item("张三是这个团队的核心", attribution="imported",
                entities=["张三", "团队"],
                stability=0.3, reuse=0.3, user_specificity=0.2,
                explicitness=0.1)
    decision = gate.evaluate(low, "knowledge")
    assert decision.status == "rejected"
    assert "知识条目复用价值不足" in decision.reason


def test_first_person_signals_boost_specificity():
    caps = derive_rule_signals(_item("我一直用 rust"))
    assert caps["user_specificity_cap"] >= 0.6


def test_has_tentative_marker_covers_zh_and_en():
    assert has_tentative_marker("我想试试 Vim")
    assert has_tentative_marker("maybe we should refactor")
    assert has_tentative_marker("感觉 Redis 会更合适")
    assert not has_tentative_marker("我常用 VS Code")
