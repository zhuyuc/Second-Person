"""Response-shape and feedback signal persistence."""
from __future__ import annotations


class SignalCollector:
    def __init__(self, db):
        self.db = db

    def set_explicit_reaction(self, message_id: int, reaction: int) -> None:
        self.db.execute(
            "UPDATE response_signals SET explicit_reaction=? WHERE message_id=?",
            (reaction, message_id))
