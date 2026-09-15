"""Pure-function tests for the normalizers — no network, no credentials."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from xiaoyuzhou.client import (
    CN_TZ,
    _app_headers,
    _format_ts,
    _local_date,
    _normalize_episode,
    _normalize_history_item,
    _normalize_podcast,
)


class TestNullSafety:
    """Upstream sends `null` for absent objects, not a missing key.

    `p.get("k", {})` only defaults when the KEY is absent; an explicit null
    still returns None. Every nested access must use `or {}`.
    """

    @pytest.mark.parametrize(
        "nested_key",
        ["readTrackInfo", "image"],
    )
    def test_podcast_survives_explicit_null(self, nested_key: str) -> None:
        out = _normalize_podcast({"pid": "p", nested_key: None})
        assert out["pid"] == "p"

    @pytest.mark.parametrize(
        "nested_key",
        ["enclosure", "media", "podcast", "image", "transcript"],
    )
    def test_episode_survives_explicit_null(self, nested_key: str) -> None:
        out = _normalize_episode({"eid": "e", nested_key: None})
        assert out["eid"] == "e"

    def test_episode_survives_null_media_source(self) -> None:
        out = _normalize_episode({"eid": "e", "media": {"source": None}})
        assert out["audio_url"] is None

    def test_history_survives_null_episode(self) -> None:
        assert _normalize_history_item({"episode": None})["eid"] is None

    def test_all_normalizers_accept_empty_dict(self) -> None:
        for fn in (_normalize_podcast, _normalize_episode, _normalize_history_item):
            assert isinstance(fn({}), dict)


class TestHasUnread:
    """has_unread compares two timestamps; string `>` is not a valid comparison
    when the two sides carry different precision (`.000Z` vs bare `Z`)."""

    def test_unseen_newer_episode_is_unread(self) -> None:
        out = _normalize_podcast(
            {
                "latestEpisodePubDate": "2026-01-03T10:00:00.000Z",
                "readTrackInfo": {"lastSeenAt": "2026-01-02T10:00:00.000Z"},
            }
        )
        assert out["has_unread"] is True

    def test_already_seen_is_not_unread(self) -> None:
        out = _normalize_podcast(
            {
                "latestEpisodePubDate": "2026-01-01T10:00:00.000Z",
                "readTrackInfo": {"lastSeenAt": "2026-01-02T10:00:00.000Z"},
            }
        )
        assert out["has_unread"] is False

    def test_mixed_precision_does_not_fool_the_comparison(self) -> None:
        """'2026-01-02T10:00:00.000Z' > '2026-01-02T10:00:01Z' is True as raw
        strings ('.' = 0x2E > '1'), but the episode is OLDER. Must compare as
        instants."""
        out = _normalize_podcast(
            {
                "latestEpisodePubDate": "2026-01-02T10:00:00.000Z",
                "readTrackInfo": {"lastSeenAt": "2026-01-02T10:00:01Z"},
            }
        )
        assert out["has_unread"] is False

    def test_offset_and_utc_forms_compare_as_instants(self) -> None:
        """Beijing 18:00+08:00 == 10:00Z — same instant, so nothing is unread."""
        out = _normalize_podcast(
            {
                "latestEpisodePubDate": "2026-01-02T18:00:00+08:00",
                "readTrackInfo": {"lastSeenAt": "2026-01-02T10:00:00Z"},
            }
        )
        assert out["has_unread"] is False

    def test_missing_last_seen_is_unknown_not_true(self) -> None:
        """Never having opened the show is not evidence of an unread episode."""
        out = _normalize_podcast(
            {"latestEpisodePubDate": "2026-01-03T10:00:00.000Z", "readTrackInfo": {}}
        )
        assert out["has_unread"] is None

    def test_missing_pub_date_is_unknown(self) -> None:
        out = _normalize_podcast({"readTrackInfo": {"lastSeenAt": "2026-01-02T10:00:00Z"}})
        assert out["has_unread"] is None

    def test_unparseable_timestamp_is_unknown_not_a_crash(self) -> None:
        out = _normalize_podcast(
            {
                "latestEpisodePubDate": "not-a-date",
                "readTrackInfo": {"lastSeenAt": "2026-01-02T10:00:00Z"},
            }
        )
        assert out["has_unread"] is None


class TestLocalDate:
    def test_utc_evening_becomes_next_beijing_day(self) -> None:
        assert _local_date("2026-05-28T16:30:00.000Z") == "2026-05-29"

    def test_utc_morning_stays_same_beijing_day(self) -> None:
        assert _local_date("2026-05-28T02:30:00.000Z") == "2026-05-28"

    def test_explicit_offset_is_respected(self) -> None:
        assert _local_date("2026-05-29T00:30:00+08:00") == "2026-05-29"

    def test_none_and_empty(self) -> None:
        assert _local_date(None) == ""
        assert _local_date("") == ""

    def test_garbage_falls_back_to_leading_chars(self) -> None:
        assert _local_date("garbage") == "garbage"


class TestMediaIdPreference:
    def test_prefers_native_transcript_media_id(self) -> None:
        """RSS-bridged shows: media.id is an external URL, transcript lives
        under the native transcriptMediaId."""
        out = _normalize_episode(
            {
                "eid": "e",
                "transcriptMediaId": "native-123",
                "media": {"id": "https://ximalaya.example/a.m4a"},
            }
        )
        assert out["media_id"] == "native-123"

    def test_falls_back_to_media_id(self) -> None:
        out = _normalize_episode({"eid": "e", "media": {"id": "m-1"}})
        assert out["media_id"] == "m-1"

    def test_falls_back_to_transcript_object(self) -> None:
        out = _normalize_episode({"eid": "e", "transcript": {"mediaId": "t-9"}})
        assert out["media_id"] == "t-9"


class TestAppHeaders:
    def test_local_time_is_beijing_not_naive_local(self) -> None:
        """The header hardcodes a +0800 suffix, so the clock part must be
        Beijing time. On a UTC host a naive datetime.now() would be 8h off."""
        stamp = _app_headers()["local-time"]
        assert stamp.endswith("+0800")
        parsed = datetime.strptime(stamp[:-5], "%Y-%m-%dT%H:%M:%S.%f").replace(tzinfo=CN_TZ)
        assert abs((parsed - datetime.now(CN_TZ)).total_seconds()) < 120

    def test_local_time_matches_beijing_even_when_host_is_utc(self, monkeypatch) -> None:
        """Simulate a UTC server: the emitted clock must still read Beijing."""
        import xiaoyuzhou.client as client_mod

        fixed_utc = datetime(2026, 5, 28, 20, 0, 0, tzinfo=UTC)

        class FrozenDatetime(datetime):
            @classmethod
            def now(cls, tz=None):
                # Mimic stdlib: naive local time when tz is None. Host = UTC.
                return fixed_utc.astimezone(tz) if tz else fixed_utc.replace(tzinfo=None)

        monkeypatch.setattr(client_mod, "datetime", FrozenDatetime)
        stamp = client_mod._app_headers()["local-time"]
        # 20:00 UTC == 04:00 next-day Beijing
        assert stamp.startswith("2026-05-29T04:00:00"), stamp

    def test_token_headers_are_conditional(self) -> None:
        assert "x-jike-access-token" not in _app_headers()
        assert _app_headers("tok")["x-jike-access-token"] == "tok"


class TestFormatTs:
    @pytest.mark.parametrize(
        "ms,expected",
        [
            (0, "00:00:00"),
            (1000, "00:00:01"),
            (61_000, "00:01:01"),
            (3_661_000, "01:01:01"),
            (-5, "00:00:00"),
        ],
    )
    def test_format(self, ms: int, expected: str) -> None:
        assert _format_ts(ms) == expected


def test_cn_tz_is_utc_plus_8() -> None:
    assert CN_TZ.utcoffset(None) == timedelta(hours=8)
