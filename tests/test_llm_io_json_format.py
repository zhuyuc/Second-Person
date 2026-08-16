"""LLM I/O JSON 格式统一改造 — 回归测试。

覆盖三档改造的关键契约：
1. title_gen prompt 要求 JSON 输出 + 解析逻辑
2. format_scenario prompt 要求 JSON 输出 + 解析逻辑
3. 确定性 citation 检测（detect_citations）
4. 确定性 memory_confirm 检测（detect_memory_confirm）
5. extract_citations 旧格式兼容
6. extract_memory_confirm 旧格式兼容
7. json_mode 透传（LLMClient → OpenAI body）
8. json_mode 对 Anthropic/Google 静默降级
9. repair_json 基线不受影响

运行：python tests/test_llm_io_json_format.py（退出码 0 = 全部通过）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

failures: list[str] = []


def check(cond: bool, msg: str) -> None:
    if not cond:
        failures.append(msg)
        print(f"  FAIL: {msg}")
    else:
        print(f"  OK:   {msg}")


# ---- 1. title_gen prompt 格式 ----
def test_title_gen_prompt():
    from infrastructure.prompt_loader import PROMPTS
    content = PROMPTS.load_raw("app/prompts/title_gen")
    check('{"title"' in content, "title_gen prompt 应包含 JSON 输出格式说明")
    check("citations" not in content.lower(),
          "title_gen prompt 不应包含 citations 指令")


# ---- 2. format_scenario prompt 格式 ----
def test_format_scenario_prompt():
    from infrastructure.prompt_loader import PROMPTS
    content = PROMPTS.load_raw("agent/prompts/format_scenario")
    check('{"scenario"' in content,
          "format_scenario prompt 应包含 JSON 输出格式说明")


# ---- 3. 确定性 citation 检测 ----
def test_detect_citations_basic():
    from agent.response_synthesizer import detect_citations
    memories = [
        {"id": "mem_1", "title": "Python编程偏好", "summary": "用户偏好用 Python 做数据分析",
         "detail": "用户喜欢用 pandas 和 numpy"},
        {"id": "mem_2", "title": "投资策略", "summary": "用户关注指数基金",
         "detail": "用户持有沪深300ETF"},
        {"id": "mem_3", "title": "宠物信息", "summary": "用户养了一只金毛犬",
         "detail": ""},
    ]
    # 回复引用了 Python + pandas → 应检测到 mem_1
    resp = "你之前提到喜欢用 Python 做数据分析，推荐试试 pandas 的新版本。"
    result = detect_citations(resp, memories)
    check("mem_1" in result, "应检测到引用了 Python编程偏好 记忆")

    # 回复引用了指数基金 → 应检测到 mem_2
    resp2 = "关于你关注的指数基金，沪深300ETF 最近表现不错。"
    result2 = detect_citations(resp2, memories)
    check("mem_2" in result2, "应检测到引用了投资策略记忆")

    # 无关回复 → 不应检测到任何引用
    resp3 = "今天天气很好，适合出门散步。"
    result3 = detect_citations(resp3, memories)
    check(len(result3) == 0, "无关回复不应检测到引用")


def test_detect_citations_title_match():
    from agent.response_synthesizer import detect_citations
    memories = [
        {"id": "mem_x", "title": "每日晨跑计划", "summary": "用户每天 6 点跑步",
         "detail": ""},
    ]
    resp = "你的每日晨跑计划执行得如何？"
    result = detect_citations(resp, memories)
    check("mem_x" in result, "标题完整匹配应触发引用检测")


def test_detect_citations_empty():
    from agent.response_synthesizer import detect_citations
    check(detect_citations("", []) == [], "空输入应返回空列表")
    check(detect_citations("hello", []) == [], "无记忆应返回空列表")
    check(detect_citations("", [{"id": "m1", "title": "t", "summary": "s"}]) == [],
          "空回复应返回空列表")


# ---- 4. 确定性 memory_confirm 检测 ----
def test_detect_memory_confirm_positive():
    from agent.response_synthesizer import detect_memory_confirm
    cand = {"id": "mem_123", "title": "用户住在北京"}
    # 用户确认
    result = detect_memory_confirm("是的，没错", "好的，已确认。", cand)
    check(result is not None, "确认词应触发检测")
    check(result["confirmed"] is True, "确认词应返回 confirmed=True")
    check(result["id"] == "mem_123", "应返回正确的 memory id")


def test_detect_memory_confirm_negative():
    from agent.response_synthesizer import detect_memory_confirm
    cand = {"id": "mem_456", "title": "用户喜欢咖啡"}
    result = detect_memory_confirm("不是，我已经不喝咖啡了", "好的，了解。", cand)
    check(result is not None, "否认词应触发检测")
    check(result["confirmed"] is False, "否认词应返回 confirmed=False")


def test_detect_memory_confirm_ambiguous():
    from agent.response_synthesizer import detect_memory_confirm
    cand = {"id": "mem_789", "title": "用户学过钢琴"}
    # 既有确认又有否认词 → 模糊 → 不做判定
    result = detect_memory_confirm("是的，但其实不太对", "嗯我理解。", cand)
    check(result is None, "同时含确认和否认词时应返回 None（不做判定）")


def test_detect_memory_confirm_no_candidate():
    from agent.response_synthesizer import detect_memory_confirm
    result = detect_memory_confirm("是的没错", "好的", None)
    check(result is None, "无候选时应返回 None")


# ---- 5. extract_citations 旧格式兼容 ----
def test_extract_citations_legacy():
    from agent.response_synthesizer import extract_citations
    text = '这是回复正文。\n{"citations":["mem_1","mem_2"]}'
    body, cites = extract_citations(text, {"mem_1", "mem_2", "mem_3"})
    check("mem_1" in cites, "旧格式 citations 应被正确提取")
    check("mem_2" in cites, "旧格式多个 citation 应被正确提取")
    check('{"citations"' not in body, "提取后正文不应包含 JSON 声明")


def test_extract_citations_no_declaration():
    from agent.response_synthesizer import extract_citations
    text = "这是一段普通的回复，没有任何声明。"
    body, cites = extract_citations(text, {"mem_1"})
    check(len(cites) == 0, "无声明时应返回空列表")
    check(body == text, "无声明时正文应保持不变")


# ---- 6. extract_memory_confirm 旧格式兼容 ----
def test_extract_memory_confirm_legacy():
    from agent.response_synthesizer import extract_memory_confirm
    text = '好的，已确认。\n{"memory_confirm":{"id":"mem_abc","confirmed":true}}'
    body, confirm = extract_memory_confirm(text)
    check(confirm is not None, "旧格式 memory_confirm 应被提取")
    check(confirm["id"] == "mem_abc", "应提取正确的 id")
    check(confirm["confirmed"] is True, "应提取正确的 confirmed 值")
    check('{"memory_confirm"' not in body, "提取后正文不应包含 JSON 声明")


def test_extract_memory_confirm_none():
    from agent.response_synthesizer import extract_memory_confirm
    text = "普通回复"
    body, confirm = extract_memory_confirm(text)
    check(confirm is None, "无声明时应返回 None")
    check(body == text, "无声明时正文保持不变")


# ---- 7. json_mode 透传到 OpenAI body ----
def test_json_mode_openai_body():
    """验证 json_mode=True 时 _openai_chat 会设置 response_format。"""
    import asyncio
    from unittest.mock import AsyncMock, patch, MagicMock
    from infrastructure.llm_provider import LLMClient, ProviderSnapshot

    client = LLMClient()
    snap = ProviderSnapshot(
        provider_id="test", provider_type="openai_compatible",
        base_url="http://localhost:8000/v1", api_key="sk-test",
        model_id="test-model")

    captured_body = {}

    async def mock_post(url, json=None, headers=None):
        captured_body.update(json or {})
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "choices": [{"message": {"content": '{"test": 1}'}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        return resp

    async def run():
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock(post=mock_post))
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_ctx

            with patch("langfuse.integration.get_tracer") as mock_tracer:
                mock_gen = MagicMock()
                mock_gen.end = MagicMock()
                mock_tracer.return_value.generation_start.return_value = mock_gen

                await client.chat(
                    snap,
                    [{"role": "user", "content": "test"}],
                    source="test",
                    json_mode=True,
                )

    asyncio.run(run())
    check(captured_body.get("response_format") == {"type": "json_object"},
          "json_mode=True 应设置 response_format")


def test_json_mode_false_no_response_format():
    """验证 json_mode=False 时不设置 response_format。"""
    import asyncio
    from unittest.mock import AsyncMock, patch, MagicMock
    from infrastructure.llm_provider import LLMClient, ProviderSnapshot

    client = LLMClient()
    snap = ProviderSnapshot(
        provider_id="test", provider_type="openai_compatible",
        base_url="http://localhost:8000/v1", api_key="sk-test",
        model_id="test-model")

    captured_body = {}

    async def mock_post(url, json=None, headers=None):
        captured_body.update(json or {})
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "choices": [{"message": {"content": "hello"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        return resp

    async def run():
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock(post=mock_post))
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_ctx

            with patch("langfuse.integration.get_tracer") as mock_tracer:
                mock_gen = MagicMock()
                mock_gen.end = MagicMock()
                mock_tracer.return_value.generation_start.return_value = mock_gen

                await client.chat(
                    snap,
                    [{"role": "user", "content": "test"}],
                    source="test",
                )

    asyncio.run(run())
    check("response_format" not in captured_body,
          "默认不应设置 response_format")


# ---- 8. Anthropic json_mode 降级 ----
def test_json_mode_anthropic_ignored():
    """验证 Anthropic provider 静默忽略 json_mode。"""
    import asyncio
    from unittest.mock import AsyncMock, patch, MagicMock
    from infrastructure.llm_provider import LLMClient, ProviderSnapshot

    client = LLMClient()
    snap = ProviderSnapshot(
        provider_id="test", provider_type="anthropic",
        base_url="https://api.anthropic.com", api_key="sk-test",
        model_id="claude-sonnet-4-20250514")

    captured_body = {}

    async def mock_post(url, json=None, headers=None):
        captured_body.update(json or {})
        resp = MagicMock()
        resp.raise_for_status = MagicMock()
        resp.json.return_value = {
            "content": [{"type": "text", "text": '{"result": "ok"}'}],
            "usage": {"input_tokens": 10, "output_tokens": 5}}
        return resp

    async def run():
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_ctx = AsyncMock()
            mock_ctx.__aenter__ = AsyncMock(return_value=MagicMock(post=mock_post))
            mock_ctx.__aexit__ = AsyncMock(return_value=False)
            mock_client_cls.return_value = mock_ctx

            with patch("langfuse.integration.get_tracer") as mock_tracer:
                mock_gen = MagicMock()
                mock_gen.end = MagicMock()
                mock_tracer.return_value.generation_start.return_value = mock_gen

                await client.chat(
                    snap,
                    [{"role": "system", "content": "sys"},
                     {"role": "user", "content": "test"}],
                    source="test",
                    json_mode=True,
                )

    asyncio.run(run())
    check("response_format" not in captured_body,
          "Anthropic 不应设置 response_format（静默降级）")


# ---- 9. repair_json 基线不变 ----
def test_repair_json_baseline():
    from infrastructure.json_repair import repair_json
    # 正常 JSON
    check(repair_json('{"a": 1}') == {"a": 1}, "正常 JSON 应直接解析")
    # markdown 围栏
    check(repair_json('```json\n{"b": 2}\n```') == {"b": 2},
          "markdown 围栏应被剥离")
    # 尾逗号
    check(repair_json('{"c": 3,}') == {"c": 3}, "尾逗号应被修复")
    # 嵌套子串提取
    check(repair_json('output: {"d": 4}') == {"d": 4},
          "应从文本中提取 JSON 子串")
    # 无效输入应抛异常
    try:
        repair_json("not json at all")
        check(False, "无效输入应抛 ValueError")
    except ValueError:
        check(True, "无效输入正确抛出 ValueError")


# ---- 10. response_synth prompt 不再要求 citations 输出 ----
def test_response_synth_no_citation_output():
    from infrastructure.prompt_loader import PROMPTS
    content = PROMPTS.load_raw("agent/prompts/response_synth")
    check('在回复末尾声明 citations' not in content,
          "response_synth 不应要求在末尾输出 citations JSON")
    check("无需在末尾输出任何 JSON 声明" not in content or
          "自动检测引用" in content,
          "response_synth 应说明系统自动检测引用")


def test_build_response_prompt_no_citation_mandate():
    from agent.response_synthesizer import build_response_prompt
    memories = [{"id": "mem_1", "title": "测试", "summary": "测试摘要", "detail": ""}]
    prompt = build_response_prompt("你好", [], memories)
    system_content = prompt[0]["content"]
    check("必须在回复末尾声明 citations" not in system_content,
          "合成 prompt 不应强制要求 citations 声明")
    check("可直接引用" in system_content or "自动检测" in system_content,
          "合成 prompt 应说明可自然引用记忆")


# ---- 11. _tokenize_for_match ----
def test_tokenize_for_match():
    from agent.response_synthesizer import _tokenize_for_match
    tokens = _tokenize_for_match("Python编程和pandas数据分析")
    check("python" in tokens, "应包含英文词 python")
    check("pandas" in tokens, "应包含英文词 pandas")
    check("编程" in tokens, "应包含中文二字组合")
    check("数据" in tokens, "应包含中文二字组合 数据")
    check("分析" in tokens, "应包含中文二字组合 分析")


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
