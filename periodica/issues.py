"""What an issue is: one day, one month, or a numbered issue of a year.

Everything that names or orders an issue goes through ``IssueId`` so the three kinds stay
consistent: the database key, the folder name, the OPF date and the sort order.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date

DAY = "day"
MONTH = "month"
NUMBER = "number"
PERIODS = (DAY, MONTH, NUMBER)


@dataclass(frozen=True)
class IssueId:
    period: str
    year: int
    month: int = 0   # day and month issues
    day: int = 0     # day issues
    number: int = 0  # numbered issues

    @classmethod
    def for_day(cls, d: date) -> IssueId:
        return cls(DAY, d.year, d.month, d.day)

    @classmethod
    def for_month(cls, year: int, month: int) -> IssueId:
        if not 1 <= month <= 12:
            raise ValueError(f"not a month: {month}")
        return cls(MONTH, year, month)

    @classmethod
    def for_number(cls, year: int, number: int) -> IssueId:
        if not 1 <= number <= 999:
            raise ValueError(f"not an issue number: {number}")
        return cls(NUMBER, year, number=number)

    @classmethod
    def from_stored(cls, period: str, issue_date: str, number: int | None) -> IssueId:
        """Rebuild from a database row (``issue_date`` is always a full ISO date)."""
        d = date.fromisoformat(issue_date)
        if period == MONTH:
            return cls.for_month(d.year, d.month)
        if period == NUMBER:
            return cls.for_number(d.year, int(number or 0))
        return cls.for_day(d)

    @property
    def nominal_date(self) -> date:
        """A real date for storage and fallbacks: the day, the 1st of the month, or 1 January."""
        if self.period == DAY:
            return date(self.year, self.month, self.day)
        if self.period == MONTH:
            return date(self.year, self.month, 1)
        return date(self.year, 1, 1)

    @property
    def key(self) -> str:
        """Unique per newspaper and sorts correctly within one kind: 2026-09-15, 2026-09, 2026#038."""
        if self.period == DAY:
            return self.nominal_date.isoformat()
        if self.period == MONTH:
            return f"{self.year:04d}-{self.month:02d}"
        return f"{self.year:04d}#{self.number:03d}"

    @property
    def label(self) -> str:
        """Folder and file name part, and what the UI shows: 2026-09-15, 2026-09, 2026 #38."""
        if self.period == NUMBER:
            return f"{self.year:04d} #{self.number:02d}"
        return self.key

    @property
    def series_index(self) -> str:
        """Jellyfin sort order within a newspaper (kept below 2**31 for Bookshelf)."""
        if self.period == DAY:
            return self.nominal_date.strftime("%Y%m%d")
        if self.period == MONTH:
            return f"{self.year:04d}{self.month:02d}00"
        return str(self.year * 1000 + self.number)

    @property
    def opf_date(self) -> str:
        """``dc:date``: a numbered issue has no real date, only its year."""
        if self.period == NUMBER:
            return f"{self.year:04d}"
        return self.nominal_date.isoformat()

    @property
    def is_daily(self) -> bool:
        return self.period == DAY
