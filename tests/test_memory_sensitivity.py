"""memory.sensitivity 三档识别与脱敏契约测试。

覆盖：
- high：API key / password / verification code / private key / id_card
- medium：手机 / 邮箱 / URL-token / 中国地址
- none：纯业务描述不误伤
- redact_evidence：excerpt 按级别处理
- redact_payload_for_trace：递归 dict/list，深度上限
"""
from memory.sensitivity import (
    detect_level, hash_secret, redact, redact_evidence,
    redact_payload_for_trace, scan,
)


def test_high_level_masks_secrets():
    for text in (
        "请记住我的 api_key: sk-abcdef1234567890",
        "密码是 hunter2xxx",
        "sk-abcdefghijklmnopqrst",
        "AKIAABCDEFGHIJKLMNOP",
        "验证码：123456",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEpQIBAA\n-----END RSA PRIVATE KEY-----",
        "身份证 11010119800101001X",
    ):
        assert detect_level(text) == "high", text
        assert "[REDACTED:" in redact(text), text


def test_medium_level_marks_pii_without_blocking():
    for text, kind in (
        ("联系我 13812345678", "cn_mobile"),
        ("邮箱是 alice@example.com", "email"),
        ("http://x.com/api?token=abc123def", "url_token"),
        ("我住浙江省杭州市西湖区文一路 100 号", "cn_address"),
    ):
        result = scan(text)
        assert result.level == "medium", (text, result.level)
        assert f"[REDACTED:{kind}]" in result.redacted, text


def test_none_level_does_not_touch_normal_text():
    for text in (
        "用户偏好项目沟通直截了当",
        "我在负责搜索团队的架构演进",
        "关注 AI Infra 领域的性能优化",
    ):
        assert detect_level(text) == "none", text
        assert redact(text) == text


def test_redact_evidence_high_keeps_only_hash():
    ev = {"source_ref": "s1:1",
          "excerpt": "api_key: sk-abcdef1234567890"}
    out = redact_evidence(ev)
    assert out["excerpt"].startswith("[REDACTED")
    assert out["excerpt_hash"] == hash_secret(ev["excerpt"])
    assert out["sensitivity_level"] == "high"


def test_redact_evidence_medium_keeps_structure():
    ev = {"excerpt": "我的邮箱 alice@example.com，欢迎联系"}
    out = redact_evidence(ev)
    assert out["sensitivity_level"] == "medium"
    assert "[REDACTED:email]" in out["excerpt"]
    assert "欢迎联系" in out["excerpt"]


def test_redact_payload_for_trace_walks_nested():
    payload = {
        "messages": [
            {"role": "user", "content": "我的 password: hunter2xxx，还有 alice@x.com"},
        ],
        "meta": {"session": "s1"},
    }
    out = redact_payload_for_trace(payload)
    dumped = str(out)
    assert "hunter2xxx" not in dumped
    assert "alice@x.com" not in dumped
    assert "[REDACTED:password]" in dumped
    assert "[REDACTED:email]" in dumped
    # 结构保留
    assert out["messages"][0]["role"] == "user"
    assert out["meta"]["session"] == "s1"


def test_redact_payload_for_trace_depth_cap():
    # 构造深嵌套：超过 6 层时替换为 [TRUNCATED:depth]
    payload = "leaf"
    for _ in range(10):
        payload = {"nested": payload}
    out = redact_payload_for_trace(payload)
    # 找到最深路径，确认被截断
    ptr = out
    while isinstance(ptr, dict) and "nested" in ptr:
        ptr = ptr["nested"]
    assert ptr == "[TRUNCATED:depth]"
