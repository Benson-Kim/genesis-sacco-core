"""Unit tests for the shared keyset cursor helpers - pure, no I/O (P10)."""

import uuid
from datetime import UTC, datetime

import pytest

from genesis.application.pagination import build_created_id_cursor, parse_created_id_cursor
from genesis.errors import InvalidInputError


def test_cursor_round_trip() -> None:
    ts = datetime(2026, 7, 29, 12, 0, tzinfo=UTC)
    row_id = uuid.uuid4()
    cursor = build_created_id_cursor(ts, row_id)
    parsed_ts, parsed_id = parse_created_id_cursor(cursor, entity="loan")
    assert parsed_ts == ts
    assert parsed_id == str(row_id)


def test_garbage_cursor_rejected() -> None:
    with pytest.raises(InvalidInputError, match="invalid loan cursor"):
        parse_created_id_cursor("not-a-cursor", entity="loan")


def test_missing_uuid_rejected() -> None:
    with pytest.raises(InvalidInputError, match="invalid application cursor"):
        parse_created_id_cursor("2026-07-29T12:00:00+00:00|nope", entity="application")


def test_naive_timestamp_rejected() -> None:
    """A forged cursor without a UTC offset must not be accepted."""
    cursor = f"2026-07-29T12:00:00|{uuid.uuid4()}"
    with pytest.raises(InvalidInputError, match="invalid application cursor"):
        parse_created_id_cursor(cursor, entity="application")
