"""The two small value types the game reads from a chat message, plus clocks."""

import json
import time
from dataclasses import dataclass


@dataclass(frozen=True)
class Scope:
    """One conversation: a group or a private chat, seen by one bot account."""

    bot_id: str
    kind: str  # "group" | "private"
    peer_id: str

    @property
    def key(self) -> str:
        return json.dumps([self.bot_id, self.kind, self.peer_id], separators=(",", ":"))


@dataclass(frozen=True)
class Event:
    """One incoming message. ``mentions`` lists the user ids @-mentioned in it."""

    scope: Scope
    sender: str
    text: str
    sender_name: str = ""  # display text only, never identity
    mentions: tuple[str, ...] = ()


class Clock:
    def now(self) -> float:
        return time.time()


@dataclass
class FakeClock(Clock):
    timestamp: float = 1791158400.0

    def now(self) -> float:
        return self.timestamp

    def advance(self, seconds: float) -> None:
        self.timestamp += seconds
