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

Two spellings, one fact: `landing_name` is what a new run writes, and
`legacy_name` is what everything written before this change is called. Callers
try the new name and fall back to the old one, so history does not vanish from
the page because a layout changed.
"""

from __future__ import annotations

import re

# A window's date, and only the date: an intraday request carries a timestamp,
# and the directory names the day it covers, not the hour it started.
_DATE = re.compile(r"^(\d{4}-\d{2}-\d{2})")

# How much of the request id survives into a directory name. Six hex characters
# is short enough to read past and long enough that two ids do not collide
# (16**6 = 16.7 million filings of one dataset and window).
_TAIL = 6


def _date(value: object) -> str:
    match = _DATE.match(str(value).strip()) if value is not None else None
    return match.group(1) if match else ""


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


def landing_name(request_id: str, start: object = None, end: object = None) -> str:
    """The directory name for `request_id`'s bytes: the window, then a short id.

    Falls back to the request id -- the whole, unreadable thing -- when the
    request carries no usable window, because a name that says nothing is still
    better than one that says something false.
    """
    first, last = _date(start), _date(end)
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
