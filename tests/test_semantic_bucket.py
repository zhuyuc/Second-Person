"""语义指纹 (content_bucket) 契约。"""
from memory.write_gate import content_bucket, fingerprint


def _item(text):
    return {"title": text[:30], "summary": text, "detail": text,
            "domain": "work"}


def test_fingerprint_still_strict():
    # 精确指纹保留原语义：同文完全一致
    a = _item("我偏好直接的项目沟通")
    b = _item("我偏好直接的项目沟通")
    assert fingerprint(a) == fingerprint(b)


def test_content_bucket_matches_identical_content():
    # 完全相同内容 → 相同 bucket（幂等契约，不承诺跨措辞聚合）
    a = _item("我偏好直接的项目沟通")
    b = _item("  我偏好直接的项目沟通  ")  # 只是空白差异
    assert content_bucket(a) == content_bucket(b)


def test_content_bucket_differs_for_unrelated():
    a = _item("我偏好直接的项目沟通")
    b = _item("我住在杭州西湖区")
    assert content_bucket(a) != content_bucket(b)


def test_content_bucket_empty_for_no_content():
    assert content_bucket({"title": "", "summary": "", "detail": ""}) == ""
