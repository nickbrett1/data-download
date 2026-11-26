"""The directory name one request lands in.

A request id is unique, stable and unreadable; a window is readable and not
unique. The name is both: the dates first, a short id second. These tests pin
the shape, the fallbacks and the fact that the id still decides uniqueness.
"""

from __future__ import annotations

from data_download.layout import landing_name, legacy_name, vendor_slug


def test_the_window_leads_and_a_short_id_follows():
    name = landing_name("req_fc1b83716aa44c18ab3a13ea70e3bb8d", "2026-11-04", "2026-11-06")
    assert name == "2026-11-04_2026-11-06-fc1b83"


def test_a_day_split_names_the_day_an_intraday_request_covers():
    """An intraday request carries a time; a per-day job's folder is the day."""
    name = landing_name(
        "req_abc123",
        "2026-11-04T09:30:00Z",
        "2026-11-04T16:00:00Z",
        split="day",
    )
    assert name == "2026-11-04_2026-11-04-abc123"


def test_a_plan_that_declares_no_split_does_not_get_a_day_name():
    """Silence is not a declaration: an unnamed cut must not be guessed coarsely.

    A date-only window degrades to the same name either way, so the only case
    this changes is the one where a day-only name would read as an empty span.
    """
    name = landing_name(
        "req_abc123",
        "2026-11-04T09:30:00Z",
        "2026-11-04T16:00:00Z",
    )
    assert name == "2026-11-04T0930_2026-11-04T1600-abc123"


def test_the_plans_split_decides_how_finely_the_window_is_written():
    """The plan declares the cut; the folder is named at that precision.

    A day split names days (the file-per-day convention the store holds). An
    hourly cut, or an unsplit window in one file, cannot be described by a date
    alone: the hours are where the data is cut, and a day-only name would show
    `2026-11-04_2026-11-04` -- a half-open window that reads as empty.
    """
    hourly = landing_name(
        "req_abc123",
        "2026-11-04T09:30:00Z",
        "2026-11-04T16:00:00Z",
        split="hour",
    )
    assert hourly == "2026-11-04T0930_2026-11-04T1600-abc123"

    unsplit = landing_name(
        "req_abc123",
        "2026-11-04T09:30:00Z",
        "2026-11-04T16:00:00Z",
        split="none",
    )
    assert unsplit == hourly


def test_a_split_finer_than_a_day_names_a_day_spanning_window_in_days():
    """An unsplit week is one file, and one file is described by its days."""
    name = landing_name("req_abc123", "2026-11-04", "2026-11-07", split="none")
    assert name == "2026-11-04_2026-11-07-abc123"


def test_an_unknown_split_names_the_window_in_instants():
    """A unit invented later must not silently fall back to a day-only name."""
    name = landing_name(
        "req_abc123",
        "2026-11-04T09:30:00Z",
        "2026-11-04T16:00:00Z",
        split="week",
    )
    assert name == "2026-11-04T0930_2026-11-04T1600-abc123"


def test_a_fine_name_with_no_window_still_keeps_the_request_id():
    assert landing_name("req_abc", None, None, split="hour") == "req_abc"


def test_two_filings_of_one_window_do_not_collide():
    first = landing_name("req_aaaaaaaaaaaaaaaa", "2026-11-04", "2026-11-06")
    second = landing_name("req_bbbbbbbbbbbbbbbb", "2026-11-04", "2026-11-06")
    assert first == "2026-11-04_2026-11-06-aaaaaa"
    assert second == "2026-11-04_2026-11-06-bbbbbb"
    assert first != second


def test_no_usable_window_keeps_the_request_id():
    """A name that says nothing beats one that says something false."""
    for start, end in [(None, None), ("2026-11-04", None), ("soon", "later"), ("", "")]:
        assert landing_name("req_abc", start, end) == "req_abc"


def test_the_id_is_sanitised_before_it_reaches_a_path():
    """The tail is six alphanumerics, whatever the id claims to be."""
    assert landing_name("req_../../etc", "2026-11-04", "2026-11-06").endswith("-etc")
    assert landing_name("req_...", "2026-11-04", "2026-11-06") == "req_..."


def test_the_legacy_name_is_the_whole_id():
    assert legacy_name("req_fc1b83") == "req_fc1b83"


def test_a_vendor_folder_folds_case_and_underscores():
    assert vendor_slug("Databento") == "databento"
    assert vendor_slug("  Data_Bento Inc ") == "data-bento-inc"
    assert vendor_slug(None) == ""
