"""Central time authority for the sim."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Datex:
    """Day-stepping clock. Day 0 is the start of the run."""

    day: int = 0

    def add_days(self, n: int) -> None:
        self.day += n

    def next_day(self) -> None:
        self.day += 1

    def is_decision_day(self, cadence_days: int) -> bool:
        return self.day % cadence_days == 0
