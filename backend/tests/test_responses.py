from datetime import UTC, datetime
from uuid import uuid4

from app.schemas.responses import decode_cursor, encode_cursor, iso_utc


def test_iso_utc_marks_naive_values_as_utc():
    naive = datetime(2026, 1, 2, 3, 4, 5)
    assert iso_utc(naive) == "2026-01-02T03:04:05+00:00"
    assert iso_utc(naive.replace(tzinfo=UTC)) == "2026-01-02T03:04:05+00:00"
    assert iso_utc(None) is None


def test_cursor_round_trip_keeps_utc():
    row_id = uuid4()
    # SQLite returns naive UTC datetimes; the cursor must not be reinterpreted
    # in the host timezone.
    decoded = decode_cursor(encode_cursor(datetime(2026, 1, 2, 3, 4, 5), row_id))
    assert decoded == (datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC), row_id)
