"""Decide whether the number in a ``Name.2026.09`` file name is a month or an issue number.

Decided per newspaper, first match wins:

1. The user's choice on the newspaper's page.
2. A number above 12 cannot be a month.
3. Once a newspaper has been detected as numbered, it stays numbered.
4. Compare the number with the *reference month*: the month of the daily papers in the same torrent,
   or failing that the download date. Monthly issues are often published a little early or
   downloaded a little late, so one month either side still counts as a match. Two or more months
   away (in the same year) means an issue number.
5. In January, 12, 1 and 2 are exactly where numbered magazines start their year too, so a newspaper
   with no decision yet is unclear and waits for the user - unless a second, different number turns
   up in the same January, which a monthly would not do.
6. Otherwise it is a monthly.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, datetime

from .issues import MONTH, NUMBER

JANUARY_AMBIGUOUS = {12, 1, 2}


@dataclass(frozen=True)
class Decision:
    period: str | None    # MONTH, NUMBER, or None when unclear
    detected: str | None  # what to remember as detected for the newspaper
    reason: str

    @property
    def unclear(self) -> bool:
        return self.period is None


def reference_month(daily_dates: Iterable[date], completion_on: float | None) -> tuple[int, int] | None:
    """The (year, month) a torrent belongs to: its daily papers first, then when it finished."""
    months = Counter((d.year, d.month) for d in daily_dates)
    if months:
        return months.most_common(1)[0][0]
    if completion_on:
        finished = datetime.fromtimestamp(completion_on)
        return finished.year, finished.month
    return None


def month_distance(year: int, month: int, ref: tuple[int, int]) -> int:
    return abs((year * 12 + month - 1) - (ref[0] * 12 + ref[1] - 1))


def decide(choice: str | None, detected: str | None, year: int, number: int,
           ref: tuple[int, int] | None, january_numbers: Iterable[int] = ()) -> Decision:
    """Apply the rules above to one ``year`` + ``number`` file name.

    ``january_numbers`` are the numbers already seen for this newspaper in the reference January.
    """
    if choice in (MONTH, NUMBER):
        return Decision(choice, detected, "set by you")
    if number > 12:
        return Decision(NUMBER, NUMBER, f"issue number {number} is above 12")
    if detected == NUMBER:
        return Decision(NUMBER, NUMBER, "numbered issues were detected earlier")
    if ref is not None:
        distance = month_distance(year, number, ref)
        where = f"{ref[1]}/{ref[0]}"
        if distance >= 2 and year == ref[0]:
            return Decision(NUMBER, NUMBER, f"{number}/{year} is {distance} months from the download's month {where}")
        if distance <= 1:
            if ref[1] == 1 and detected is None and number in JANUARY_AMBIGUOUS:
                if set(january_numbers) - {number}:
                    return Decision(NUMBER, NUMBER, "two different numbers arrived in the same January")
                return Decision(None, detected, "in January the number could be the month or the issue number")
            return Decision(MONTH, detected or MONTH, f"{number}/{year} matches the download's month {where}")
    if detected == MONTH:
        return Decision(MONTH, MONTH, "monthly issues were detected earlier")
    if ref is None:
        return Decision(None, detected, "no download date to compare the number with")
    return Decision(None, detected, f"the download ({ref[1]}/{ref[0]}) is from another year than {year}")
