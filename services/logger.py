from __future__ import annotations

from datetime import datetime
from typing import List


class InMemoryLog:
    def __init__(self) -> None:
        self.messages: List[str] = []

    def add(self, message: str) -> None:
        timestamp = datetime.now().strftime("%H:%M:%S")
        self.messages.append(f"[{timestamp}] {message}")

    def clear(self) -> None:
        self.messages.clear()

    def text(self) -> str:
        return "\n".join(self.messages)

    def list(self) -> list[str]:
        return list(self.messages)
