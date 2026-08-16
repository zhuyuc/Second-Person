"""从 @tabler/icons-webfont 中提取前端实际使用的图标，生成子集字体和 CSS。

用法：cd frontend && python scripts/subset-icons.py
输出：src/assets/fonts/tabler-icons-subset.woff2
      src/assets/fonts/tabler-icons-subset.woff
      src/assets/tabler-icons-subset.css
"""
import re
import struct
from pathlib import Path

from fontTools.ttLib import TTFont
from fontTools.subset import Subsetter, Options

ROOT = Path(__file__).resolve().parent.parent  # frontend/
SRC_DIR = ROOT / "src"
TABLER_CSS = ROOT / "node_modules/@tabler/icons-webfont/dist/tabler-icons.css"
TABLER_FONT_DIR = ROOT / "node_modules/@tabler/icons-webfont/dist/fonts"
OUT_FONT_DIR = SRC_DIR / "assets" / "fonts"
OUT_CSS = SRC_DIR / "assets" / "tabler-icons-subset.css"


def scan_used_icons() -> set[str]:
    """扫描 src/ 下所有 .vue/.js/.ts 文件，提取 ti-<name> 图标名。

    两种模式：
    1. HTML class 中直接写 "ti ti-xxx"
    2. JS/模板中以字符串形式引用 'ti-xxx' 或 "ti-xxx"（如动态绑定 icon: 'ti-settings'）
    """
    icons: set[str] = set()
    for ext in ("*.vue", "*.js", "*.ts"):
        for f in SRC_DIR.rglob(ext):
            text = f.read_text(encoding="utf-8", errors="ignore")
            icons.update(re.findall(r"ti[ -](ti-[a-z0-9-]+)", text))
            icons.update(re.findall(r"['\"]( ?ti-[a-z0-9-]+)['\"]", text))
    cleaned: set[str] = set()
    for icon in icons:
        name = icon.strip()
        if name.startswith("ti-"):
            cleaned.add(name)
    return cleaned


def parse_codepoints(css_text: str, needed: set[str]) -> dict[str, int]:
    """从 tabler-icons.css 中提取 .ti-xxx:before { content: "\\eXXX" } 映射。"""
    pattern = re.compile(r"\.(ti-[a-z0-9-]+):before\s*\{\s*content:\s*\"\\([0-9a-fA-F]+)\"")
    mapping: dict[str, int] = {}
    for m in pattern.finditer(css_text):
        name, cp_hex = m.group(1), m.group(2)
        if name in needed:
            mapping[name] = int(cp_hex, 16)
    return mapping


def subset_font(src_path: Path, codepoints: set[int]) -> None:
    """从 TTF 源子集化，输出 woff2 和 woff。"""
    import subprocess
    OUT_FONT_DIR.mkdir(parents=True, exist_ok=True)
    unicodes = ",".join(f"U+{cp:04X}" for cp in sorted(codepoints))

    for flavor in ("woff2", "woff"):
        out = OUT_FONT_DIR / f"tabler-icons-subset.{flavor}"
        cmd = [
            "pyftsubset", str(src_path),
            f"--unicodes={unicodes}",
            f"--flavor={flavor}",
            f"--output-file={out}",
            "--drop-tables=GSUB,GPOS,GDEF",
            "--no-hinting",
            "--desubroutinize",
        ]
        subprocess.run(cmd, check=True)
        print(f"  {flavor}: {out.stat().st_size / 1024:.1f} KB")


def generate_css(icon_map: dict[str, int]) -> str:
    """生成子集 CSS：@font-face（含 font-display: swap）+ 图标类。"""
    lines = [
        '@font-face {',
        '  font-family: "tabler-icons";',
        '  font-style: normal;',
        '  font-weight: 400;',
        '  font-display: swap;',
        '  src: url("./fonts/tabler-icons-subset.woff2") format("woff2"),',
        '       url("./fonts/tabler-icons-subset.woff") format("woff");',
        '}',
        '.ti {',
        '  font-family: "tabler-icons" !important;',
        '  speak: none;',
        '  font-style: normal;',
        '  font-weight: normal;',
        '  font-variant: normal;',
        '  text-transform: none;',
        '  line-height: 1;',
        '  -webkit-font-smoothing: antialiased;',
        '  -moz-osx-font-smoothing: grayscale;',
        '}',
    ]
    for name in sorted(icon_map):
        cp = icon_map[name]
        lines.append(f'.{name}:before {{ content: "\\{cp:04x}"; }}')
    return '\n'.join(lines) + '\n'


def main() -> None:
    print("Scanning source for icon usage...")
    used = scan_used_icons()
    print(f"  Found {len(used)} unique icons")

    print("Parsing codepoints from tabler CSS...")
    css_text = TABLER_CSS.read_text(encoding="utf-8")
    icon_map = parse_codepoints(css_text, used)

    missing = used - set(icon_map.keys())
    if missing:
        print(f"  WARNING: no codepoint found for: {missing}")
    print(f"  Mapped {len(icon_map)} icons to codepoints")

    codepoints = set(icon_map.values())

    ttf_src = TABLER_FONT_DIR / "tabler-icons.ttf"
    orig_size = (TABLER_FONT_DIR / "tabler-icons.woff2").stat().st_size

    print(f"Subsetting from TTF ({ttf_src.stat().st_size / 1024:.1f} KB)...")
    subset_font(ttf_src, codepoints)

    print("Generating subset CSS...")
    OUT_CSS.write_text(generate_css(icon_map), encoding="utf-8")
    print(f"  Written to {OUT_CSS.relative_to(ROOT)}")

    final_size = (OUT_FONT_DIR / "tabler-icons-subset.woff2").stat().st_size
    print(f"\nDone! {len(icon_map)} icons, woff2 {final_size/1024:.1f} KB (was {orig_size/1024:.1f} KB)")


if __name__ == "__main__":
    main()
