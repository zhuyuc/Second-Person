"""前端静态构建产物引用一致性测试。

保护契约：FastAPI 挂载的 app/static/index.html 中引用的 JS/CSS 资源必须存在，
避免只提交 index.html 或只提交 assets 导致生产页面 404。
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
STATIC_DIR = ROOT / "app" / "static"
INDEX_HTML = STATIC_DIR / "index.html"
_ASSET_REF_RE = re.compile(r'''(?:src|href)\s*=\s*["']([^"']+)["']''', re.I)


def _asset_refs() -> list[str]:
    assert INDEX_HTML.exists(), "app/static/index.html 不存在，请先执行前端构建"
    html = INDEX_HTML.read_text(encoding="utf-8")
    assert html.strip(), "app/static/index.html 为空"
    return [ref for ref in _ASSET_REF_RE.findall(html)
            if ref.startswith("/assets/")]


def test_static_index_references_existing_assets():
    missing = []
    empty = []
    for ref in _asset_refs():
        target = STATIC_DIR / ref.lstrip("/")
        if not target.exists():
            missing.append(ref)
        elif target.stat().st_size <= 0:
            empty.append(ref)

    assert not missing, "index.html 引用了不存在的静态资源：" + ", ".join(missing)
    assert not empty, "index.html 引用了空静态资源：" + ", ".join(empty)


def test_static_index_references_entry_js_and_css():
    refs = _asset_refs()
    js_refs = [ref for ref in refs if re.search(r"/index-[^/]+\.js$", ref)]
    css_refs = [ref for ref in refs if re.search(r"/index-[^/]+\.css$", ref)]
    assert js_refs, "index.html 未引用 Vite 入口 JS"
    assert css_refs, "index.html 未引用 Vite 入口 CSS"


def test_static_build_contains_entry_js_and_css():
    assets_dir = STATIC_DIR / "assets"
    assert assets_dir.is_dir(), "app/static/assets 目录不存在"
    assert list(assets_dir.glob("index-*.js")), "缺少 Vite 入口 JS 构建产物"
    assert list(assets_dir.glob("index-*.css")), "缺少 Vite 入口 CSS 构建产物"
