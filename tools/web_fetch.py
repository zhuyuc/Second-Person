"""
web_fetch 外部访问策略（开发文档 §6.3 外部访问策略）。

- User-Agent: SecondPerson/{version} (personal assistant; +local)
- 超时：连接 5s / 总计 web_fetch_timeout_seconds（默认 15s）
- 响应大小上限 10 MB，超出截断
- 内容类型：text/html 走正文提取；text/plain 与 json 直接返回；
  application/pdf 复用文档 Ingest 的 PDF 解析器；其他只返回类型说明
- 不请求 robots.txt（用户本机主动抓取，等同浏览器）
- 重定向最多 5 次
- 私网防护：拒绝解析到 127/10/172.16/192.168/169.254（防 SSRF），除非用户显式允许
- HTML 用 readability 类算法提取正文
"""
from __future__ import annotations

import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

import httpx

MAX_BYTES = 10 * 1024 * 1024
UA = "SecondPerson/1.0.0 (personal assistant; +local)"
PRIVATE_NETS = [
    ipaddress.ip_network("127.0.0.0/8"), ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network(
        "172.16.0.0/12"), ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("169.254.0.0/16"),
]


class FetchError(RuntimeError):
    pass


def _is_private(host: str) -> bool:
    """同步 DNS 解析判私网（getaddrinfo 无超时控制，须在工作线程调用）。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror:
        return False
    for info in infos:
        ip = info[4][0]
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            continue
        if any(addr in net for net in PRIVATE_NETS):
            return True
    return False


async def _is_private_async(host: str) -> bool:
    """DNS 解析丢工作线程 + 5 秒超时，避免慢 DNS 同步挂死事件循环；
    超时按不可达处理（非私网，后续真实请求自有超时兑底）。"""
    try:
        return await asyncio.wait_for(asyncio.to_thread(_is_private, host), 5)
    except asyncio.TimeoutError:
        return False


async def web_fetch(url: str, timeout: int = 15,
                    allow_private: bool = False,
                    pdf_extract_fn=None) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise FetchError(f"仅支持 http/https：{url}")
    if not allow_private and await _is_private_async(parsed.hostname or ""):
        raise FetchError(f"拒绝访问私网地址（防 SSRF）：{url}")

    timeout_cfg = httpx.Timeout(
        connect=5.0, read=timeout, write=timeout, pool=timeout)
    async with httpx.AsyncClient(timeout=timeout_cfg, follow_redirects=True,
                                 max_redirects=5, headers={"User-Agent": UA}) as c:
        r = await c.get(url)
        r.raise_for_status()
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        content = r.content[:MAX_BYTES]

    if ctype == "application/pdf":
        if pdf_extract_fn:
            # PDF 解析为 CPU 密集，丢工作线程
            return await asyncio.to_thread(pdf_extract_fn, content)
        return "[PDF 内容，未配置解析器]"
    if ctype in ("text/plain", "application/json"):
        return content.decode(r.encoding or "utf-8", errors="ignore")
    if ctype == "text/html":
        # 最大 10MB HTML 的 readability/BS4 正文提取为 CPU 密集，丢工线程，
        # 避免聊天工具链调用时冻结所有会话的 SSE 流
        return await asyncio.to_thread(
            _extract_readable, content.decode(r.encoding or "utf-8", errors="ignore"))
    return f"[内容类型 {ctype}，不下载正文]"


def _extract_readable(html: str) -> str:
    """用 readability 提取正文；不可用则用 BeautifulSoup 去脚本后取文本。"""
    try:
        from readability import Document
        doc = Document(html)
        summary_html = doc.summary()
        try:
            from bs4 import BeautifulSoup
            return BeautifulSoup(summary_html, "html.parser").get_text("\n", strip=True)
        except Exception:  # noqa: BLE001
            return summary_html
    except Exception:  # noqa: BLE001
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, "html.parser")
            for tag in soup(["script", "style", "nav", "footer"]):
                tag.decompose()
            return soup.get_text("\n", strip=True)
        except Exception:  # noqa: BLE001
            return html
