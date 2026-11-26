"""The name of the directory one request lands in.

The datasets tree is `<datasets>/<vendor>/<dataset>/<name>/`, and this module
answers what `<name>` is. It used to be the request id -- `req_fc1b8371…` --
which is unique, stable and unreadable: a person browsing the share sees 32 hex
characters and cannot tell which days of which dataset they are looking at
without opening each directory.

So the name leads with the window the request asked for, and carries just
enough of the id to stay unique:

    2026-11-04_2026-11-06-1a2b3c

The dates are what a person navigates by. The six hex characters are what tells
two filings of the same window apart -- a retry, or a second request for the
same days, lands beside the first rather than on top of it. A request with no
window (a plan type that is not date-shaped) keeps its id, because there is
nothing better to say about it.

How finely the window is written is not this module's opinion: the plan type
declares how it splits the data (`split_duration`), the Job carries that
declaration, and `landing_name` reads it. A per-day plan names days; a per-hour
or unsplit one names instants, because a folder holding hours, or a single file
covering part of a day, is not described by a date. The structure of the tree
stays here -- there is one store and one browser -- and only the precision of
one segment comes from the plan.

Two spellings, one fact: `landing_name` is what a new run writes, and
`legacy_name` is what everything written before this change is called. Callers
try the new name and fall back to the old one, so history does not vanish from
the page because a layout changed.
"""

from __future__ import annotations

import re

# A window's date, and the hour it starts in when the name is written at that
# precision. The time is kept only to the minute: seconds and zone offsets are
# noise in a directory name, and the window's *start* is what tells two filings
# of one hour apart.
_STAMP = re.compile(r"^(\d{4}-\d{2}-\d{2})(?:[T ](\d{2}):(\d{2}))?")

# How much of the request id survives into a directory name. Six hex characters
# is short enough to read past and long enough that two ids do not collide
# (16**6 = 16.7 million filings of one dataset and window).
_TAIL = 6


def _stamp(value: object, *, fine: bool) -> str:
    """`2026-11-04T09:30:00Z` -> `2026-11-04`, or `2026-11-04T0930` when fine.

    `""` for anything that is not a timestamp, so the caller can fall back
    rather than write a name built from half a fact.
    """
    match = _STAMP.match(str(value).strip()) if value is not None else None
    if match is None:
        return ""
    day, hour, minute = match.group(1), match.group(2), match.group(3)
    if not fine or not hour or not minute:
        return day
    return f"{day}T{hour}{minute}"


def _coarse(split: object) -> bool:
    """Whether a name written at day precision is still true of this Job.

    The split is the plan's declaration of how it cuts the data up, and it is
    the same fact that decides how finely the folder can honestly be named: a
    `day` split holds one file per day, so the day is what a person browses by.
    Anything else -- `hour`, an unsplit window, a unit invented later -- either
    cuts inside a day or fits in one file, and a day-only name would hide where
    the cuts are (or name a zero-length span, since the window is half-open).
    """
    return str(split or "day").strip().lower() == "day"


def _tail(request_id: str) -> str:
    text = str(request_id)
    if text.startswith("req_"):
        text = text[len("req_") :]
    return "".join(ch for ch in text if ch.isalnum())[:_TAIL]


def vendor_slug(vendor: object) -> str:
    """The vendor's folder name: folded to lower case, one separator, no case.

    Case is not meaningful in a directory name and two spellings of one vendor
    would be two folders. Underscores fold to hyphens for the same reason.
    """
    return (
        " ".join(str(vendor or "").split()).lower().replace(" ", "-").replace("_", "-")
    )


def landing_name(
    request_id: str,
    start: object = None,
    end: object = None,
    *,
    split: object = "day",
) -> str:
    """The directory name for `request_id`'s bytes: the window, then a short id.

    `split` is the Job's own `split_duration` -- the plan's declaration of how
    it cuts the data up -- and it decides the precision of the window's ends.
    A per-day split names days (`2026-11-04_2026-11-06-fc1b83`); anything finer
    or unsplit names instants (`2026-11-04T0930_2026-11-04T1600-fc1b83`), which
    is the only name that stays true when one folder holds hours, or one file
    spans a part of a day.

    Falls back to the request id -- the whole, unreadable thing -- when the
    request carries no usable window, because a name that says nothing is still
    better than one that says something false.
    """
    fine = not _coarse(split)
    first, last = _stamp(start, fine=fine), _stamp(end, fine=fine)
    tail = _tail(request_id)
    if not first or not last or not tail:
        return str(request_id)
    return f"{first}_{last}-{tail}"


def legacy_name(request_id: str) -> str:
    """What a directory written before the dates-first layout is called.

    One function rather than a literal in each caller, so the fallback reads as
    a fact about history and not as a second layout somebody might extend.
    """
    return str(request_id)
