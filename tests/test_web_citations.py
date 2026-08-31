"""web_search / web_fetch 引用链接提取。"""
from agent.turn_runtime import _extract_web_citations


def test_extract_web_search_citations():
    result = [
        {"title": "Report", "url": "https://a.com", "snippet": "x"},
        {"title": "", "url": "https://b.com", "snippet": ""},
        {"title": "No URL", "snippet": "y"},
    ]
    cites = _extract_web_citations("web_search", result)
    assert len(cites) == 2
    assert cites[0] == {"title": "Report", "url": "https://a.com"}
    assert cites[1]["url"] == "https://b.com"
    assert cites[1]["title"] == "https://b.com"


def test_extract_web_fetch_citation():
    cites = _extract_web_citations(
        "web_fetch", None, {"url": "https://example.com/doc"})
    assert cites == [{"title": "https://example.com/doc",
                       "url": "https://example.com/doc"}]


def test_extract_ignores_other_tools():
    assert _extract_web_citations("lookup", {"summary": "x"}) == []
