"""Provider 模态 × 协议校验、槽位误绑、能力表夹紧。"""
from __future__ import annotations

from pathlib import Path

import pytest

from infrastructure.db import Database
from infrastructure.provider_modality import (
    infer_modality, protocols_for, validate_combo,
)
from infrastructure.video_gen.profiles import (
    CLOUD_PROFILE, capability_hint, clamp_duration, normalize_video_size,
    video_profile_for,
)

ROOT = Path(__file__).resolve().parent.parent


class _Config:
    def __init__(self, **v):
        self.values = v

    def get(self, k, default=None):
        return self.values.get(k, default)

    def get_raw(self, k, default=None):
        return self.values.get(k, default)


def test_custom_endpoint_uses_the_url_as_written():
    from infrastructure.provider_modality import endpoint_url
    # 非标准完整路径：保持原样（不智能改写）
    raw = "https://example.com/api/v1/services/aigc/text2image/image-synthesis"
    assert endpoint_url(raw, "custom", "/images/generations") == raw
    assert endpoint_url(raw, "custom", "/chat/completions") == raw
    assert endpoint_url(
        "https://api.example.com/v1", "openai_compatible", "/chat/completions",
    ) == "https://api.example.com/v1/chat/completions"
    assert "openai_compatible" in protocols_for("text")
    assert "anthropic" in protocols_for("text")


def test_custom_endpoint_smart_derives_openai_style_paths():
    from infrastructure.provider_modality import endpoint_url
    root = "https://gateway.example.com/v1"
    assert endpoint_url(root, "custom", "/chat/completions") == (
        root + "/chat/completions")
    assert endpoint_url(root, "custom", "/embeddings") == root + "/embeddings"
    assert endpoint_url(root, "custom", "/images/generations") == (
        root + "/images/generations")
    # 填了完整 chat 叶子 → embedding / images 自动剥叶子再拼
    chat = root + "/chat/completions"
    assert endpoint_url(chat, "custom", "/chat/completions") == chat
    assert endpoint_url(chat, "custom", "/embeddings") == root + "/embeddings"
    assert endpoint_url(chat, "custom", "/images/generations") == (
        root + "/images/generations")


def test_anthropic_messages_url_and_headers():
    from infrastructure.provider_modality import (
        anthropic_headers, anthropic_messages_url,
    )
    assert anthropic_messages_url(
        "https://ark.cn-beijing.volces.com/api/plan"
    ) == "https://ark.cn-beijing.volces.com/api/plan/v1/messages"
    assert anthropic_messages_url(
        "https://ark.cn-beijing.volces.com/api/coding/"
    ) == "https://ark.cn-beijing.volces.com/api/coding/v1/messages"
    assert anthropic_messages_url(
        "https://api.anthropic.com/v1"
    ) == "https://api.anthropic.com/v1/messages"
    headers = anthropic_headers("sk-test")
    assert headers["Authorization"] == "Bearer sk-test"
    assert headers["x-api-key"] == "sk-test"
    assert headers["anthropic-version"] == "2023-06-01"
    assert "custom" in protocols_for("text")
    assert "google" not in protocols_for("text")
    assert protocols_for("image") == frozenset({
        "openai_compatible", "custom", "comfyui"})
    assert protocols_for("video") == frozenset({
        "openai_compatible", "custom", "comfyui",
        "kling", "dashscope"})
    assert "comfyui" not in protocols_for("text")
    assert "anthropic" not in protocols_for("image")
    assert "anthropic" not in protocols_for("video")
    assert "custom" in protocols_for("video")


def test_validate_combo_follows_text_protocol_split():
    validate_combo("text", "openai_compatible")
    validate_combo("text", "anthropic")
    validate_combo("text", "custom")
    validate_combo("image", "openai_compatible")
    validate_combo("image", "custom")
    validate_combo("image", "comfyui")
    validate_combo("video", "openai_compatible")
    validate_combo("video", "custom")
    validate_combo("video", "kling")
    validate_combo("video", "dashscope")
    validate_combo("video", "comfyui")
    with pytest.raises(ValueError, match="不支持协议"):
        validate_combo("text", "comfyui")
    with pytest.raises(ValueError, match="不支持协议"):
        validate_combo("text", "kling")
    with pytest.raises(ValueError, match="不支持协议"):
        validate_combo("text", "google")
    with pytest.raises(ValueError, match="不支持协议"):
        validate_combo("image", "google")
    with pytest.raises(ValueError, match="不支持协议"):
        validate_combo("video", "google")
    with pytest.raises(ValueError, match="不支持协议"):
        validate_combo("image", "anthropic")
    with pytest.raises(ValueError, match="不支持协议"):
        validate_combo("video", "anthropic")


def test_validate_slot_provider_blocks_anthropic_non_chat():
    from infrastructure.provider_modality import validate_slot_provider
    validate_slot_provider("chat", "anthropic")
    validate_slot_provider("agent", "anthropic")
    with pytest.raises(ValueError, match="Anthropic"):
        validate_slot_provider("embedding", "anthropic")
    with pytest.raises(ValueError, match="Anthropic"):
        validate_slot_provider("image_gen", "anthropic")
    with pytest.raises(ValueError, match="Anthropic"):
        validate_slot_provider("video_gen", "anthropic")


def test_validate_provider_accepts_kling_protocol():
    from app.services.settings_service import SettingsService
    body = {
        "display_name": "kling-3.0-turbo",
        "modality": "video",
        "provider_type": "kling",
        "base_url": "https://api-beijing.klingai.com",
        "api_key": "ak:sk",
        "model_id": "kling-3.0-turbo",
        "output_price": 0.8,
    }
    SettingsService(None).validate_provider_required(body)
    assert body["modality"] == "video"


def test_infer_modality_comfyui_wan():
    assert infer_modality("comfyui", "wan2.1_t2v_1.3B_fp16.safetensors") == "video"
    assert infer_modality("comfyui", "sd_xl_base_1.0.safetensors") == "image"
    assert infer_modality("kling", "kling-3.0-turbo") == "video"
    assert infer_modality("dashscope", "wan2.6-t2v") == "video"
    assert infer_modality("openai_compatible", "kling-v3-turbo") == "text"
    assert infer_modality("custom", "wan2.6-t2v") == "text"


def test_probe_snapshot_video_custom_skips_llm_chat(monkeypatch):
    """视频+自定义必须走视频探测，禁止 chat/embedding ping。"""
    from app.services.settings_service import SettingsService
    from infrastructure.llm_provider import ProviderSnapshot

    calls = {"llm": 0, "video": 0}

    class _LLM:
        async def probe(self, snap):
            calls["llm"] += 1
            return {"ok": True, "protocol": "chat"}

    svc = SettingsService(type("C", (), {"llm": _LLM()})())

    async def fake_probe(self, timeout=12.0):
        calls["video"] += 1
        assert "klingai.com" in (self.base_url or "")
        return {"ok": True, "protocol": "video"}

    monkeypatch.setattr(
        "infrastructure.video_gen.cloud_adapter.KlingVideoAdapter.probe",
        fake_probe)
    snap = ProviderSnapshot(
        "test", "kling", "https://api-beijing.klingai.com",
        "ak:sk", "kling-3.0-turbo", modality="video")

    async def scenario():
        out = await svc.probe_snapshot(snap)
        assert out["ok"] is True
        assert out["protocol"] == "video"
        assert calls["video"] == 1
        assert calls["llm"] == 0

    import asyncio
    asyncio.run(scenario())


def test_cloud_profile_ignores_local_yaml_duration_cap():
    snap = type("S", (), {"provider_type": "openai_compatible"})()
    profile = video_profile_for(snap, _Config(
        video_gen_max_duration_sec=4, video_gen_default_duration_sec=3))
    assert profile.max_duration == 60
    assert profile.default_duration == 15
    assert profile.refine == "off"
    assert profile.local_gpu is False
    # 云端等待上限必须跟主对话工具预算同源，不能再写死 180
    assert profile.timeout_sec == 600.0
    d, clamped = clamp_duration(profile, 90)
    assert d == 60 and clamped is True
    d2, c2 = clamp_duration(CLOUD_PROFILE, 15)
    assert d2 == 15 and c2 is False
    assert normalize_video_size(CLOUD_PROFILE, "9:16") == "9:16"
    assert normalize_video_size(CLOUD_PROFILE, "16:9") == "16:9"
    assert "云端" in capability_hint(CLOUD_PROFILE)


def test_cloud_profile_reads_video_gen_timeout_sec():
    snap = type("S", (), {"provider_type": "openai_compatible"})()
    profile = video_profile_for(snap, _Config(video_gen_timeout_sec=900))
    assert profile.timeout_sec == 900.0
    assert profile.engine == "cloud"
    # 常量默认值也与主对话 600s 对齐
    assert CLOUD_PROFILE.timeout_sec == 600.0


def test_local_profile_still_clamped_by_yaml():
    snap = type("S", (), {"provider_type": "comfyui"})()
    profile = video_profile_for(snap, _Config(
        video_gen_max_duration_sec=4, video_gen_default_duration_sec=3,
        video_gen_refine_enabled=True))
    assert profile.max_duration == 4
    assert profile.refine == "wan_en"
    d, clamped = clamp_duration(profile, 10)
    assert d == 4 and clamped is True


def test_set_assignment_rejects_wrong_modality(tmp_path: Path):
    from connectors.credential_store import CredentialStore
    from infrastructure.provider_registry import ProviderRegistry

    db = Database(tmp_path / "mod.db")
    db.run_migrations(ROOT / "migrations")
    try:
        creds = CredentialStore(db, tmp_path)
        reg = ProviderRegistry(db, creds)
        chat_id = reg.add_provider(
            pid="prov_chat", display_name="Chat",
            provider_type="openai_compatible",
            base_url="https://api.example.com",
            model_id="gpt-test", api_key="sk-test",
            input_price=1.0, output_price=2.0, context_window=128000,
            modality="text")
        vid_id = reg.add_provider(
            pid="prov_vid", display_name="Kling",
            provider_type="kling",
            base_url="https://api.kling.com",
            model_id="kling-v3-turbo", api_key="ak:sk",
            input_price=None, output_price=0.4, context_window=0,
            modality="video")
        with pytest.raises(ValueError, match="模态"):
            reg.set_assignment("video_gen", chat_id)
        with pytest.raises(ValueError, match="模态"):
            reg.set_assignment("chat", vid_id)
        reg.set_assignment("video_gen", vid_id)
        assert reg.assignment("video_gen") == vid_id
    finally:
        db.close()


def test_add_provider_rejects_invalid_combo(tmp_path: Path):
    from connectors.credential_store import CredentialStore
    from infrastructure.provider_registry import ProviderRegistry

    db = Database(tmp_path / "combo.db")
    db.run_migrations(ROOT / "migrations")
    try:
        creds = CredentialStore(db, tmp_path)
        reg = ProviderRegistry(db, creds)
        with pytest.raises(ValueError, match="不支持协议"):
            reg.add_provider(
                pid="prov_bad", display_name="Bad",
                provider_type="comfyui",
                base_url="http://127.0.0.1:8188",
                model_id="x", api_key="local",
                input_price=None, output_price=None, context_window=0,
                modality="text")
    finally:
        db.close()


def test_ensure_does_not_overwrite_cloud_video(tmp_path: Path):
    from connectors.credential_store import CredentialStore
    from infrastructure.provider_registry import (
        ProviderRegistry, ensure_slot_assignments)

    db = Database(tmp_path / "keep.db")
    db.run_migrations(ROOT / "migrations")
    try:
        creds = CredentialStore(db, tmp_path)
        reg = ProviderRegistry(db, creds)
        pid = reg.add_provider(
            pid="prov_kling", display_name="Kling Turbo",
            provider_type="kling",
            base_url="https://api.kling.com",
            model_id="kling-v3-turbo", api_key="ak:sk",
            input_price=None, output_price=0.4, context_window=0,
            modality="video")
        reg.set_assignment("video_gen", pid)
        filled = ensure_slot_assignments(reg)
        assert "video_gen" not in filled
        assert reg.assignment("video_gen") == pid
        snap = reg.snapshot_for("video_gen")
        assert snap.model_id == "kling-v3-turbo"
        assert snap.provider_type == "kling"
    finally:
        db.close()


def test_turn_tail_includes_media_capability():
    from agent.core import AgentCore

    core = AgentCore.__new__(AgentCore)
    core.config = _Config()
    core.mood = None
    core.ctx_entry = type("C", (), {"read_consciousness_hint": lambda self: ""})()
    core.skills = type("S", (), {"list_drafts": lambda self: []})()
    core.lifecycle = type("L", (), {})()
    core._should_ask_low_confirm = lambda *_a, **_k: False  # noqa: E731

    class _Providers:
        def snapshot_for(self, slot):
            if slot == "video_gen":
                return type("S", (), {
                    "provider_type": "openai_compatible",
                    "model_id": "kling-v3-turbo",
                })()
            if slot == "image_gen":
                return type("S", (), {"provider_type": "openai_compatible"})()
            return None

    core.providers = _Providers()
    tail = core._build_turn_tail_contexts(
        sid="s", onboarding=False, location=None, user_message="hi")
    text = tail["constraints_context"] or ""
    assert "[当前生成能力]" in text
    assert "云端" in text
    assert "文生视频" in text
    assert "文生图" in text
