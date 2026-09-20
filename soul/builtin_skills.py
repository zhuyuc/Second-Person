"""Built-in storyboard (auto) + director-style (@ only) skill packs.

Full skill text lives in soul/skill_texts/*.md and is copied into data/skills on ensure.
"""

from __future__ import annotations

from pathlib import Path

_TEXT_DIR = Path(__file__).resolve().parent / "skill_texts"

_STORYBOARD_TPL = """# {{title}} · {{type}}
画幅与总时长取自【本次约束】。各镜秒数相加等于该总时长。
故事很长时：本片只写能装进此时长的阶段性成果，不降质量硬完结。

共享设定
- 人物识别（穿着按这场的地方与时候；用户写过就用他的）：
- 轴线与站位（谁在左、谁在右；主视线朝哪）：
- 背景锁定：
- 声光锁定（主光从哪一侧来）：
- 关键道具（在谁哪只手里）：
- 兵器与手段（每人哪一件；若有法术：名称/视觉签名、从哪起、规则、代价）：
- 本片主冲突（一句话）：
- 本段范围（第几段；本段只解决什么；结束停在哪一拍，可未完）：
- 行为路径（只写本段；打斗则试探→对手真本事→斗智→绝境→险胜）：
- 说话（每人怎么开口：短、哑、骂、念）：

镜 1 · 秒 · 景别 · 机位
人物：（朝向与视线；身体有无伤或增益；护体/法力状态）
背景：
声光：（出招：蓄/放/落点；兵器相交或法术对撞的声光）
音乐：
对白：（谁对谁，「原句」；或写这一镜不说话，还响着什么）
动作：（距离与步法；这一板谁出招、谁拆；结尾停在哪一下）
冲突：

镜 2 起
承接：（接着上一镜的哪一下；人为什么在这里、为什么这一下会发生）
人物：（穿着仍是进这场时那一身，除非写了更换；伤、护体裂、法力代价还在不在）
背景：（上一击留下的痕迹、阵眼、地势破坏还在不在）
声光：（出招链：蓄→放→行进→命中或被拆→余痕）
音乐：
对白：（接着上一句答、顶回去，或话断在半截；这一镜不说话要写明）
动作：（先写接着；步法与距离；兵器/法术仍是这个人自己的，除非上一镜已经写了换手或破招）
冲突：（从上一镜哪一下来；这一镜谁占上风，是否逼近绝境或翻盘）
"""

_DIRECTOR_FILES = (
    "style-wong-kar-wai",
    "style-stephen-chow",
    "style-zhang-yimou",
    "style-ang-lee",
    "style-johnnie-to",
    "style-tsui-hark",
    "style-kurosawa",
    "style-miyazaki",
    "style-nolan",
    "style-villeneuve",
    "style-wes-anderson",
    "style-spielberg",
    "style-hitchcock",
    "style-kubrick",
    "style-scorsese",
    "style-fincher",
    "style-tarantino",
    "style-ridley-scott",
    "style-cameron",
    "style-bong-joon-ho",
)


def _load_skill_md(name: str) -> str:
    return (_TEXT_DIR / f"{name}.md").read_text(encoding="utf-8")


STORYBOARD_CORE_NAME = "storyboard-core"

DEPRECATED_STORYBOARD_SKILLS = (
    "storyboard-single-shot",
    "storyboard-short-drama",
    "storyboard-ad-hook",
    "storyboard-atmosphere",
)

BUILTIN_SKILLS: list[dict] = [
    {
        "name": STORYBOARD_CORE_NAME,
        "skill_md": _load_skill_md(STORYBOARD_CORE_NAME),
        "templates": {"shot_list.md": _STORYBOARD_TPL},
    },
    *[
        {"name": name, "skill_md": _load_skill_md(name), "templates": {}}
        for name in _DIRECTOR_FILES
    ],
]
