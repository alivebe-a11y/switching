from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import datetime
from typing import ClassVar, Iterable

from switching.signal import Signal


class Detector(ABC):
    """Contract for a trend detector.

    A detector pulls candidate events from a data source, filters them for a
    specific thesis, and yields Signal objects. Price correlation is the
    caller's job — detectors should not touch yfinance directly so the
    scanning and backtesting paths can reuse the same source output.
    """

    name: ClassVar[str]
    description: ClassVar[str]

    @abstractmethod
    def scan(self, since: datetime) -> Iterable[Signal]:
        """Yield signals for events at or after ``since``."""
