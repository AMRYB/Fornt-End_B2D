"""Tests for the CLI discovery option-selection helper."""

from __future__ import annotations

from agentic_core.cli import parse_user_answer

OPTIONS = ["Web app only", "Mobile app only", "Both web and mobile"]


def test_free_text_used_verbatim_when_no_options():
    assert parse_user_answer("Something custom", []) == "Something custom"


def test_free_text_used_when_not_a_number():
    assert parse_user_answer("Mobile first", OPTIONS) == "Mobile first"


def test_picks_option_by_number():
    assert parse_user_answer("2", OPTIONS) == "Mobile app only"


def test_picks_multiple_options():
    assert parse_user_answer("1,3", OPTIONS) == "Web app only; Both web and mobile"


def test_picks_multiple_options_with_spaces():
    assert parse_user_answer("1 2", OPTIONS) == "Web app only; Mobile app only"


def test_out_of_range_numbers_ignored():
    assert parse_user_answer("9", OPTIONS) == "9"


def test_mixed_numbers_and_text_uses_text():
    assert parse_user_answer("2 and a note", OPTIONS) == "2 and a note"


def test_blank_returns_empty():
    assert parse_user_answer("   ", OPTIONS) == ""