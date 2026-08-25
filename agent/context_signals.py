"""Cross-turn safety and proposal signals used by SessionStore."""
from __future__ import annotations

import re


_FAKE_GENERATION_PATTERNS = (
    r"已生成.*(文件|文档)", r"已导出.*(文件|文档)",
    r"已保存为.*(文件|文档)", r"文件已(生成|导出)",
    r"文档已(生成|导出|保存)",
    r"稍等.{0,12}(贴|给).{0,6}(结论|结果|答案)",
    r"我(现在|这就|马上)就?去(查|看|拆|搜|拉)(一?下)?",
    r"(先|这就)去.{2,20}(再|回头).{0,6}(告诉|回复|贴|给)",
    r"查完(直接)?(贴|给).{0,6}(结论|结果)",
)

_PROPOSAL_PATTERNS = (
    r"我(?:下一步|接下来)?可以(?:帮你|继续)?[^。！？!?\n]{4,60}"
    r"(?:，|,|。|.)?(?:需要|想|要)[^。！？!?\n]{0,20}(?:就说|告诉我|说一声|直接说)",
    r"(?:如果|若)(?:你)?(?:愿意|需要|想|要)[^。！？!?\n]{0,40}"
    r"(?:我|帮你)[^。！？!?\n]{4,60}",
)

_PROPOSAL_INSTRUCTION_EXCLUDE = (
    r"明确说(一声|一?句)?", r"直接说(一声|一?句)?[\"“']",
    r"你(可以|只需|直接)说[\"“']",
    r"说一?声[\"“'][^\"”']+[\"”'](就|才能|即可)?(触发|执行|启动)",
)


def detect_fake_claim(text: str) -> bool:
    tail = (text or "")[-200:]
    return any(re.search(pattern, tail) for pattern in _FAKE_GENERATION_PATTERNS)


def detect_proposal_sentence(text: str) -> str | None:
    tail = (text or "")[-200:]
    if any(re.search(pattern, tail) for pattern in _PROPOSAL_INSTRUCTION_EXCLUDE):
        return None
    for pattern in _PROPOSAL_PATTERNS:
        match = re.search(pattern, tail)
        if match:
            proposal = match.group().strip().rstrip("。！!.，,")
            if len(proposal) >= 10:
                return proposal[:80]
    return None
