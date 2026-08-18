"""LLM 空返回重试与 quick_intent 降级文案 — 回归测试。

背景：DeepSeek 偶发 HTTP 200 但 content 为空（输出全被 reasoning_content
占用），曾导致 quick_intent 的 JSON 解析失败被误报为"LLM 调用失败"，且前端
思考面板【执行路径】行因 thinking_delta 与 mode_decision 双发而重复渲染。

覆盖契约：
1. 空返回（EmptyCompletionError）走退避重试，中途恢复则正常返回
2. 重试耗尽抛 LLMError 且透出空返回真实原因
3. quick_intent 输出解析失败（LLM 调用已成功）文案不误报为调用失败
4. quick_intent 真实调用失败保留原降级文案
5. quick_intent 正常 JSON 路径不受影响
6. 【执行路径】展示行单一渲染点：后端不再发同文案 thinking_delta，
   前端 ChatView 与飞书适配器统一由 mode_decision 事件渲染
7. thinking_enabled 通用开关经 _normalize_extra_body 翻译成厂商原生参数
   （DeepSeek 为 thinking.type；thinking_enabled 原样透传会被 DeepSeek
   忽略导致思考关不掉，是空返回的根因）

运行：python tests/test_llm_empty_completion.py（退出码 0 = 全部通过）
"""
import asyncio
import sys
from pathlib import Path

_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(_ROOT))

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)
        print(f"  FAIL: {msg}")
    else:
        print(f"  OK:   {msg}")


def _snapshot():
    from infrastructure.llm_provider import ProviderSnapshot
    return ProviderSnapshot(provider_id="t", provider_type="openai_compatible",
                            base_url="http://x", api_key="k", model_id="m")


# ---- 1. 空返回可重试：前两次失败第三次成功 ----
def test_empty_completion_retry_recovers():
    import infrastructure.llm_provider as lp
    from infrastructure.llm_provider import (
        EmptyCompletionError, LLMClient)
    orig = lp.RETRY_DELAYS
    lp.RETRY_DELAYS = [0.0, 0.0, 0.0]
    try:
        client = LLMClient()
        calls = {"n": 0}

        async def fn():
            calls["n"] += 1
            if calls["n"] < 3:
                raise EmptyCompletionError(
                    "模型 m 返回空内容（completion_tokens=60）")
            return {"content": '{"ok": true}',
                    "usage": {"input_tokens": 1, "output_tokens": 1}}

        res = asyncio.run(
            client._call_with_retry(_snapshot(), "quick_intent", None, fn))
        check(calls["n"] == 3, "空返回应走退避重试并在第三次成功")
        check(res["content"] == '{"ok": true}', "重试恢复后正常返回内容")
    finally:
        lp.RETRY_DELAYS = orig


# ---- 2. 重试耗尽：LLMError 透出空返回原因 ----
def test_empty_completion_retry_exhausted():
    import infrastructure.llm_provider as lp
    from infrastructure.llm_provider import (
        EmptyCompletionError, LLMClient, LLMError)
    orig = lp.RETRY_DELAYS
    lp.RETRY_DELAYS = [0.0, 0.0, 0.0]
    try:
        client = LLMClient()
        calls = {"n": 0}

        async def fn():
            calls["n"] += 1
            raise EmptyCompletionError(
                "模型 m 返回空内容（completion_tokens=60）")

        try:
            asyncio.run(
                client._call_with_retry(_snapshot(), "quick_intent", None, fn))
            check(False, "重试耗尽应抛 LLMError")
        except LLMError as e:
            check(calls["n"] == 4, "应为 1 次首调 + 3 次重试共 4 次尝试")
            check("返回空内容" in str(e), "异常信息应透出空返回真实原因")
    finally:
        lp.RETRY_DELAYS = orig


# ---- 3. 输出解析失败文案不误报为调用失败 ----
def test_quick_intent_parse_fail_wording():
    from agent.intent_parser import IntentParser

    class FakeLLM:
        async def chat(self, snap, messages, **kw):
            return {"content": "这不是JSON", "usage": {}}

    p = IntentParser(FakeLLM(), lambda: _snapshot())
    r = asyncio.run(p.quick_intent("你好"))
    check("解析失败" in r.complexity_reason
          and "LLM 调用失败" not in r.complexity_reason,
          "解析失败文案应如实标注，不误报为 LLM 调用失败")
    check(r.needs_convergence and r.complexity_hint == 5,
          "解析失败仍保守进入深度收敛路径")


# ---- 4. 真实调用失败保留原降级文案 ----
def test_quick_intent_call_fail_wording():
    from agent.intent_parser import IntentParser
    from infrastructure.llm_provider import LLMError

    class FakeLLM:
        async def chat(self, snap, messages, **kw):
            raise LLMError("LLM 调用失败（已重试）：x")

    p = IntentParser(FakeLLM(), lambda: _snapshot())
    r = asyncio.run(p.quick_intent("你好"))
    check("快速预判 LLM 调用失败" in r.complexity_reason,
          "真实调用失败保留原降级文案")


# ---- 5. 正常 JSON 路径不受影响 ----
def test_quick_intent_normal_json():
    from agent.intent_parser import IntentParser

    class FakeLLM:
        async def chat(self, snap, messages, **kw):
            return {"content": ('{"intent_hypothesis":"打招呼",'
                                '"needs_convergence":false,'
                                '"complexity_reason":"简单问候",'
                                '"complexity_hint":1}'),
                    "usage": {}}

    p = IntentParser(FakeLLM(), lambda: _snapshot())
    r = asyncio.run(p.quick_intent("你好"))
    check(not r.needs_convergence and r.complexity_reason == "简单问候",
          "正常 JSON 路径不受护栏影响")


# ---- 6. 【执行路径】单一渲染点（防重复渲染回潮） ----
def test_exec_path_single_render_point():
    core_src = (_ROOT / "agent" / "core.py").read_text(encoding="utf-8")
    check("【执行路径】" not in core_src,
          "后端 core.py 不应再内联发送【执行路径】thinking_delta 文案")
    vue_src = (_ROOT / "frontend" / "src" / "views" /
               "ChatView.vue").read_text(encoding="utf-8")
    check("ev === 'mode_decision'" in vue_src and "【执行路径】" in vue_src,
          "前端【执行路径】展示行由 mode_decision 事件单一渲染")
    feishu_src = (_ROOT / "gateway" / "platforms" /
                  "feishu.py").read_text(encoding="utf-8")
    check("mode_decision" in feishu_src and "【执行路径】" in feishu_src,
          "飞书端【执行路径】展示行同样由 mode_decision 事件渲染")


# ---- 7. thinking_enabled 厂商参数翻译（空返回根因防护） ----
def test_thinking_param_vendor_translation():
    from infrastructure.llm_provider import (
        ProviderSnapshot, _normalize_extra_body)

    def snap(ptype="openai_compatible", url="http://x"):
        return ProviderSnapshot(provider_id="t", provider_type=ptype,
                                base_url=url, api_key="k", model_id="m")

    ds = snap(url="https://api.deepseek.com")
    eb = _normalize_extra_body(ds, {"thinking_enabled": False})
    check(eb == {"thinking": {"type": "disabled"}}
          and "thinking_enabled" not in eb,
          "DeepSeek 应翻译为 thinking.type=disabled，不残留无效字段")
    eb = _normalize_extra_body(ds, {"thinking_enabled": True})
    check(eb == {"thinking": {"type": "enabled"}},
          "DeepSeek thinking_enabled=True 应翻译为 thinking.type=enabled")
    eb = _normalize_extra_body(ds, None)
    check(eb == {}, "无 extra_body 时归一化返回空 dict")
    eb = _normalize_extra_body(snap(ptype="anthropic"),
                               {"thinking_enabled": False})
    check("thinking_enabled" not in eb,
          "Anthropic 应剔除无效开关（思考默认关闭）")
    eb = _normalize_extra_body(
        snap(url="https://token-plan-cn.xiaomimimo.com/v1"),
        {"thinking_enabled": False})
    check(eb.get("thinking_enabled") is False,
          "其他 OpenAI 兼容厂商保持透传语义不变")


# ---- Run all ----
if __name__ == "__main__":
    import traceback

    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    for t in tests:
        print(f"\n> {t.__name__}")
        try:
            t()
        except Exception:
            failures.append(f"{t.__name__} raised: {traceback.format_exc()}")
            print(f"  EXCEPTION: {traceback.format_exc()}")

    print(f"\n{'=' * 60}")
    if failures:
        print(f"FAILED ({len(failures)} failures):")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    else:
        print(f"ALL {len(tests)} tests PASSED")
        sys.exit(0)
