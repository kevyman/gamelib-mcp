"""Tests for the cross-platform last-played coercion helper."""

import unittest

from gamelib_mcp.data.last_played import (
    EPIC_LAST_PLAYED_KEYS,
    XBOX_LAST_PLAYED_KEYS,
    coerce_last_played_date,
    extract_last_played,
)


class CoerceLastPlayedDateTests(unittest.TestCase):
    def test_unix_epoch_int(self) -> None:
        # Steam's rtime_last_played shape.
        self.assertEqual(coerce_last_played_date(1709424000), "2024-03-03")

    def test_unix_epoch_as_digit_string(self) -> None:
        self.assertEqual(coerce_last_played_date("1709424000"), "2024-03-03")

    def test_zero_epoch_is_never_played_not_1970(self) -> None:
        # GetOwnedGames uses 0 for "never played"; 1970-01-01 would be a lie
        # that the history gate would then treat as a real (very old) date.
        self.assertIsNone(coerce_last_played_date(0))
        self.assertIsNone(coerce_last_played_date("0"))

    def test_negative_epoch_rejected(self) -> None:
        self.assertIsNone(coerce_last_played_date(-1))

    def test_iso_with_z_suffix(self) -> None:
        self.assertEqual(coerce_last_played_date("2024-03-02T18:30:00Z"), "2024-03-02")

    def test_xbox_seven_digit_fractional_seconds(self) -> None:
        # fromisoformat accepts at most 6 fractional digits; Xbox emits 7.
        self.assertEqual(
            coerce_last_played_date("2021-05-30T12:00:00.0000000Z"), "2021-05-30"
        )

    def test_iso_with_offset(self) -> None:
        self.assertEqual(
            coerce_last_played_date("2024-03-02T18:30:00+02:00"), "2024-03-02"
        )

    def test_bare_date(self) -> None:
        self.assertEqual(coerce_last_played_date("2024-03-02"), "2024-03-02")

    def test_pre_1980_is_a_sentinel_not_a_date(self) -> None:
        self.assertIsNone(coerce_last_played_date("1970-01-01T00:00:00Z"))

    def test_unparseable_and_empty_values(self) -> None:
        for value in (None, "", "   ", "not a date", [], {}, object()):
            self.assertIsNone(coerce_last_played_date(value))

    def test_bool_is_not_an_epoch(self) -> None:
        # bool is an int subclass — True would otherwise coerce to 1970-01-01.
        self.assertIsNone(coerce_last_played_date(True))
        self.assertIsNone(coerce_last_played_date(False))


class ExtractLastPlayedTests(unittest.TestCase):
    def test_first_parseable_key_wins(self) -> None:
        payload = {"lastPlayedDateTime": "2024-03-02T00:00:00Z", "lastModified": "2020-01-01"}
        self.assertEqual(
            extract_last_played(payload, EPIC_LAST_PLAYED_KEYS), "2024-03-02"
        )

    def test_falls_through_an_unparseable_key(self) -> None:
        payload = {"lastPlayedDateTime": None, "lastPlayed": "2023-06-06"}
        self.assertEqual(
            extract_last_played(payload, EPIC_LAST_PLAYED_KEYS), "2023-06-06"
        )

    def test_missing_keys_yield_none(self) -> None:
        self.assertIsNone(extract_last_played({"totalTime": 90}, EPIC_LAST_PLAYED_KEYS))

    def test_non_dict_payload(self) -> None:
        self.assertIsNone(extract_last_played(None, XBOX_LAST_PLAYED_KEYS))
        self.assertIsNone(extract_last_played(["x"], XBOX_LAST_PLAYED_KEYS))


class XboxExtractionTests(unittest.TestCase):
    def test_nested_title_history_shape(self) -> None:
        from gamelib_mcp.data.xbox import _extract_last_played

        entry = {"titleId": "1", "titleHistory": {"lastTimePlayed": "2024-01-05T09:00:00.1234567Z"}}
        self.assertEqual(_extract_last_played(entry), "2024-01-05")

    def test_flat_shape_accepted(self) -> None:
        from gamelib_mcp.data.xbox import _extract_last_played

        self.assertEqual(
            _extract_last_played({"titleId": "1", "lastTimePlayed": "2024-01-05"}),
            "2024-01-05",
        )

    def test_absent_last_played_never_blocks_ownership(self) -> None:
        from gamelib_mcp.data.xbox import _extract_last_played

        self.assertIsNone(_extract_last_played({"titleId": "1", "name": "Halo"}))
        self.assertIsNone(_extract_last_played({"titleHistory": "garbage"}))
        self.assertIsNone(_extract_last_played(None))


if __name__ == "__main__":
    unittest.main()
