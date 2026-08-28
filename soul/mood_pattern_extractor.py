"""
情绪模式提取器 —— 每周分析 mood_history，把稳定的情绪模式沉淀为 memory。

- 定位：独立调度任务，对主链路零影响
- 输入：mood_history 表（user scope，近 N 天，intensity > 0.3）
- 输出：通过 distiller.write_item 写入 domain=mood_pattern 的记忆
- 时间：统一使用 now_cst()（项目时间规范）
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import timedelta

from infrastructure.timeutil import now_cst
from soul import _mood_constants as _mood
from soul.mood_manager import _mood_cn

logger = logging.getLogger("second_person.mood_pattern")


class MoodPatternExtractor:
    def __init__(self, db, distiller, config):
        self.db = db
        self.distiller = distiller
        self.config = config

    async def extract(self) -> int:
        """分析 mood_history，为达标情绪写入记忆。返回新增记忆条数。"""
        window_days = _mood.MOOD_PATTERN_WINDOW_DAYS
        min_occurrences = _mood.PATTERN_MIN_OCCURRENCES
        cutoff = (now_cst() - timedelta(days=window_days)
                  ).isoformat(timespec="seconds")

        rows = self.db.query_all(
            "SELECT mood, intensity, note, create_time FROM mood_history "
            "WHERE scope='user' AND create_time > ? AND intensity > 0.3",
            (cutoff,))
        if len(rows) < min_occurrences:
            return 0

        mood_counter = Counter(r["mood"] for r in rows)
        added = 0
        for mood, count in mood_counter.items():
            if count < min_occurrences or mood == "neutral":
                continue
            title = f"用户情绪模式：{_mood_cn(mood)}"[:30]
            summary = (f"过去 {window_days} 天内出现 {count} 次 "
                       f"{_mood_cn(mood)} 情绪，作为长期沟通模式参考。")[:30]
            # 示例取当前情绪自己的带备注样本（sqlite3.Row 无 .get，用方括号索引）
            sample = next((r["note"] for r in rows
                           if r["mood"] == mood and r["note"]), "") or ""
            detail = (f"情绪标签：{mood}\n"
                      f"窗口：{window_days} 天\n"
                      f"出现次数：{count}\n"
                      f"近期示例：{sample[:100]}")
            await self.distiller.write_item({
                "title": title,
                "summary": summary,
                "detail": detail,
                "domain": "mood_pattern",
                "attribution": "inferred",
                "entities": [],
            }, source_type="mood_pattern")
            added += 1
        return added
