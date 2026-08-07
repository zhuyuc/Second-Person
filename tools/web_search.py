"""
web_search 联网搜索工具（无需 API Key）。

面向国内网络环境：优先使用 Bing（cn.bing.com，国内可达），失败再退回 DuckDuckGo。
返回前若干条结果（标题 / 链接 / 摘要），供 Agent 结合 web_fetch 进一步抓取正文。
属只读、非破坏性工具。依赖 BeautifulSoup（项目已用于 web_fetch 正文提取）。
"""
from __future__ import annotations

import asyncio
import logging
from urllib.parse import parse_qs, quote, unquote, urlparse

import httpx

from infrastructure.http_client import timeout_for

logger = logging.getLogger("second_person.web_search")

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")


def _soup(html: str):
    from bs4 import BeautifulSoup
    return BeautifulSoup(html, "html.parser")


def _unwrap_ddg(href: str) -> str:
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    try:
        if "duckduckgo.com/l/" in href:
            q = parse_qs(urlparse(href).query)
            if "uddg" in q:
                return unquote(q["uddg"][0])
    except Exception:  # noqa: BLE001
        pass
    return href


async def _search_bing(query: str, max_results: int, timeout: int) -> list[dict]:
    url = "https://cn.bing.com/search?q=" + quote(query) + "&ensearch=0"
    _web = timeout_for("web")
    tcfg = httpx.Timeout(connect=_web.connect, read=timeout,
                         write=timeout, pool=_web.pool)
    async with httpx.AsyncClient(timeout=tcfg, follow_redirects=True, max_redirects=5,
                                 headers={"User-Agent": _UA,
                                          "Accept-Language": "zh-CN,zh;q=0.9"}) as c:
        r = await c.get(url)
        r.raise_for_status()
        html = r.text
    # 结果页 BS4 解析为同步 CPU 操作，丢工作线程（聊天热路径）
    return await asyncio.to_thread(_parse_bing, html, max_results)


def _parse_bing(html: str, max_results: int) -> list[dict]:
    soup = _soup(html)
    out: list[dict] = []
    for li in soup.select("li.b_algo")[: max_results * 2]:
        a = li.select_one("h2 a")
        if not a or not a.get("href"):
            continue
        p = li.select_one(".b_caption p") or li.select_one("p")
        out.append({"title": a.get_text(strip=True), "url": a["href"],
                    "snippet": p.get_text(" ", strip=True) if p else ""})
        if len(out) >= max_results:
            break
    return out


async def _search_ddg(query: str, max_results: int, timeout: int) -> list[dict]:
    _web = timeout_for("web")
    tcfg = httpx.Timeout(connect=_web.connect, read=timeout,
                         write=timeout, pool=_web.pool)
    async with httpx.AsyncClient(timeout=tcfg, follow_redirects=True, max_redirects=5,
                                 headers={"User-Agent": _UA}) as c:
        r = await c.post("https://html.duckduckgo.com/html/",
                         data={"q": query, "kl": "cn-zh"})
        r.raise_for_status()
        html = r.text
    return await asyncio.to_thread(_parse_ddg, html, max_results)


def _parse_ddg(html: str, max_results: int) -> list[dict]:
    soup = _soup(html)
    out: list[dict] = []
    for res in soup.select(".result, .web-result")[: max_results * 2]:
        a = res.select_one(".result__a")
        if not a:
            continue
        snip = res.select_one(".result__snippet")
        u = _unwrap_ddg(a.get("href", ""))
        if not u:
            continue
        out.append({"title": a.get_text(strip=True), "url": u,
                    "snippet": snip.get_text(" ", strip=True) if snip else ""})
        if len(out) >= max_results:
            break
    return out


async def web_search(query: str, max_results: int = 5, timeout: int = 15) -> list[dict]:
    if not query or not query.strip():
        return []
    try:
        import bs4  # noqa: F401
    except ImportError:  # pragma: no cover
        return [{"title": "", "url": "", "snippet": "[未安装 bs4，无法解析搜索结果]"}]

    errors = []
    for name, fn in (("bing", _search_bing), ("duckduckgo", _search_ddg)):
        try:
            res = await fn(query, max_results, timeout)
            if res:
                return res
        except Exception as e:  # noqa: BLE001
            errors.append(f"{name}: {e}")
            logger.warning("web_search %s 失败：%s", name, e)
    if errors:
        return [{"title": "", "url": "", "snippet": "[联网搜索暂不可用：" + "；".join(errors)[:200] + "]"}]
    return []
