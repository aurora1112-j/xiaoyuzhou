"""Pagination tests driven by a fake upstream — no network, no credentials.

`list_episodes` is the most intricate function in the package; these tests pin
down page-count, limit semantics and dedupe behaviour by counting the requests
a fake transport actually receives.
"""

from __future__ import annotations

import pytest

from xiaoyuzhou.client import EPISODE_LIST_MAX_PAGES, XiaoyuzhouClient

PAGE_SIZE = 25


def build_feed(days: int = 40, per_day: int = 3, start: str = "2026-05-28") -> list[dict]:
    """Newest-first feed, `per_day` episodes a day walking backwards from `start`."""
    from datetime import date, timedelta

    y, m, d = (int(x) for x in start.split("-"))
    day0 = date(y, m, d)
    feed: list[dict] = []
    for offset in range(days):
        day = day0 - timedelta(days=offset)
        # Newest-first *within* the day too, mirroring upstream's desc order.
        for k in range(per_day):
            hour = per_day - 1 - k
            feed.append(
                {
                    "eid": f"{day.isoformat()}-{k}",
                    "title": f"ep {day.isoformat()} #{k}",
                    # Keep hours in 00..08 UTC so the Beijing date (UTC+8)
                    # still lands on the same calendar day.
                    "pubDate": f"{day.isoformat()}T{hour:02d}:00:00.000Z",
                    "podcast": {"pid": "P", "title": "Show"},
                }
            )
    return feed


class FakeClient(XiaoyuzhouClient):
    """Bypasses __init__ (no creds, no httpx) and serves `feed` by cursor."""

    def __init__(self, feed: list[dict], page_size: int = PAGE_SIZE) -> None:
        self.feed = feed
        self.page_size = page_size
        self.calls: list[dict] = []

    def _api_post(self, path: str, payload: dict | None = None) -> dict:
        payload = payload or {}
        self.calls.append(payload)
        start = int(payload.get("loadMoreKey") or 0)
        page = self.feed[start : start + self.page_size]
        nxt = start + self.page_size
        return {"data": page, "loadMoreKey": nxt if nxt < len(self.feed) else None}

    @property
    def request_count(self) -> int:
        return len(self.calls)


@pytest.fixture
def feed() -> list[dict]:
    return build_feed()


class TestLimitIsAlwaysHonoured:
    """A caller that passes --limit N must never receive more than N."""

    def test_no_window(self, feed: list[dict]) -> None:
        c = FakeClient(feed)
        assert len(c.list_episodes("P", limit=5)) == 5
        assert c.request_count == 1

    def test_limit_caps_a_wide_since_window(self, feed: list[dict]) -> None:
        """Regression: --since used to ignore --limit and return the whole feed
        (120 episodes for 2 requested), blowing up an agent's context."""
        c = FakeClient(feed)
        got = c.list_episodes("P", limit=2, since="2026-01-01")
        assert len(got) == 2

    def test_limit_stops_paging_early(self, feed: list[dict]) -> None:
        """Honouring the limit must also mean not fetching the whole archive."""
        c = FakeClient(feed)
        c.list_episodes("P", limit=2, since="2026-01-01")
        assert c.request_count == 1

    def test_until_only_respects_limit(self, feed: list[dict]) -> None:
        c = FakeClient(feed)
        got = c.list_episodes("P", limit=3, until="2026-05-20")
        assert len(got) == 3
        assert all(e["pub_date"][:10] <= "2026-05-20" for e in got)


class TestWindowCorrectness:
    def test_full_day_window_is_complete(self, feed: list[dict]) -> None:
        """3/day across 2 days = 6; must not truncate at the 15/page cliff."""
        c = FakeClient(feed)
        got = c.list_episodes("P", since="2026-05-27", until="2026-05-28", limit=100)
        assert len(got) == 6

    def test_window_is_inclusive_on_both_ends(self, feed: list[dict]) -> None:
        c = FakeClient(feed)
        got = c.list_episodes("P", since="2026-05-28", until="2026-05-28", limit=100)
        assert {e["pub_date"][:10] for e in got} == {"2026-05-28"}
        assert len(got) == 3

    def test_window_spanning_many_pages(self) -> None:
        """A 10-day window at 3/day sits well past page 1; paging must cover it."""
        c = FakeClient(build_feed(days=60))
        got = c.list_episodes("P", since="2026-05-09", until="2026-05-18", limit=1000)
        assert len(got) == 30
        assert c.request_count > 1

    def test_empty_future_window_stops_fast(self, feed: list[dict]) -> None:
        """A window newer than the whole feed can be settled on page 1 — the
        feed is newest-first, so page 1 already proves nothing matches."""
        c = FakeClient(feed)
        assert c.list_episodes("P", since="2026-12-01", limit=100) == []
        assert c.request_count == 1

    def test_window_older_than_feed_is_empty(self, feed: list[dict]) -> None:
        c = FakeClient(feed)
        assert c.list_episodes("P", since="2020-01-01", until="2020-12-31", limit=100) == []

    def test_results_are_newest_first(self, feed: list[dict]) -> None:
        c = FakeClient(feed)
        dates = [e["pub_date"] for e in c.list_episodes("P", limit=10)]
        assert dates == sorted(dates, reverse=True)


class TestDedupe:
    def test_overlapping_pages_are_deduped(self) -> None:
        """If the show publishes mid-pagination the cursor shifts and a page
        repeats; callers must never see the same eid twice."""

        class ShiftingClient(FakeClient):
            def _api_post(self, path, payload=None):
                out = super()._api_post(path, payload)
                if len(self.calls) == 2:  # replay page 1 as page 2
                    out["data"] = self.feed[: self.page_size]
                return out

        c = ShiftingClient(build_feed())
        got = c.list_episodes("P", limit=40)
        eids = [e["eid"] for e in got]
        assert len(eids) == len(set(eids))

    def test_dedupe_does_not_drop_distinct_episodes(self, feed: list[dict]) -> None:
        c = FakeClient(feed)
        got = c.list_episodes("P", limit=30)
        assert len({e["eid"] for e in got}) == 30


class TestPagingSafety:
    def test_stops_at_max_pages(self) -> None:
        """An upstream that always returns a cursor must not loop forever."""

        class EndlessClient(FakeClient):
            def _api_post(self, path, payload=None):
                self.calls.append(payload or {})
                n = len(self.calls)
                return {
                    "data": [
                        {
                            "eid": f"e{n}-{i}",
                            "pubDate": "2026-05-28T01:00:00.000Z",
                            "podcast": {"pid": "P"},
                        }
                        for i in range(PAGE_SIZE)
                    ],
                    "loadMoreKey": n * PAGE_SIZE,
                }

        c = EndlessClient([])
        c.list_episodes("P", since="1999-01-01", limit=10_000)
        assert c.request_count <= EPISODE_LIST_MAX_PAGES

    def test_stops_on_empty_page(self) -> None:
        class EmptyClient(FakeClient):
            def _api_post(self, path, payload=None):
                self.calls.append(payload or {})
                return {"data": [], "loadMoreKey": 999}

        c = EmptyClient([])
        assert c.list_episodes("P", limit=10) == []
        assert c.request_count == 1

    def test_cursor_is_forwarded(self, feed: list[dict]) -> None:
        c = FakeClient(feed)
        c.list_episodes("P", since="2026-05-01", limit=1000)
        assert c.calls[0].get("loadMoreKey") is None
        assert c.calls[1]["loadMoreKey"] == PAGE_SIZE

    def test_pid_is_sent_on_every_page(self, feed: list[dict]) -> None:
        c = FakeClient(feed)
        c.list_episodes("P", since="2026-05-01", limit=1000)
        assert all(call["pid"] == "P" for call in c.calls)
