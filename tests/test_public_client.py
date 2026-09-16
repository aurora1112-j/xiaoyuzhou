"""Tests for the public-data client. Fully offline: no network, no account.

The fixtures under ``tests/fixtures/`` are real ``__NEXT_DATA__`` payloads
captured from xiaoyuzhoufm.com (trimmed to a few comments and episodes), so
these tests validate against the shape the site actually serves rather than an
invented one.
"""

from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path

import httpx
import pytest

from xiaoyuzhou.public_client import (
    CN_TZ,
    USER_AGENT,
    Episode,
    PublicClient,
    XiaoyuzhouError,
    _normalize_comment,
    audio_extension,
    default_audio_name,
    extract_eid,
    extract_pid,
    format_duration,
    local_date,
    safe_filename,
    shownotes_to_text,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def page_html(payload: dict) -> str:
    """Wrap a payload the way the real page embeds it."""
    body = json.dumps(payload, ensure_ascii=False)
    return (
        "<!DOCTYPE html><html><head><title>t</title></head><body>"
        f'<script id="__NEXT_DATA__" type="application/json">{body}</script>'
        "</body></html>"
    )


def make_client(handler, **kwargs) -> PublicClient:
    """A PublicClient whose transport is a local mock — never touches network."""
    # min_interval=0: the courtesy delay is real behaviour, but making the suite
    # sleep for it would only slow the tests down.
    client = PublicClient(min_interval=0, **kwargs)
    client._http = httpx.Client(
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": USER_AGENT},
        follow_redirects=True,
    )
    return client


EID = "6952ae2814db1df9ef6556f8"
PID = "60bc9bc72e2eec1ef1ca23ac"


@pytest.fixture
def episode_client():
    payload = load_fixture("episode.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page_html(payload))

    with make_client(handler) as c:
        yield c


@pytest.fixture
def podcast_client():
    payload = load_fixture("podcast.json")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=page_html(payload))

    with make_client(handler) as c:
        yield c


class TestIdExtraction:
    @pytest.mark.parametrize(
        "value",
        [
            EID,
            f"https://www.xiaoyuzhoufm.com/episode/{EID}",
            f"https://www.xiaoyuzhoufm.com/episode/{EID}?s=eyJ1IjoiYWJjIn0",
            f"  https://www.xiaoyuzhoufm.com/episode/{EID}  ",
        ],
    )
    def test_accepts_id_and_urls(self, value: str) -> None:
        assert extract_eid(value) == EID

    def test_uppercase_hex_is_accepted(self) -> None:
        assert extract_eid(EID.upper()) == EID.upper()

    @pytest.mark.parametrize(
        "bad",
        ["", "   ", "not-an-id", "12345", "z" * 24, "https://www.xiaoyuzhoufm.com/"],
    )
    def test_rejects_garbage_with_a_hint(self, bad: str) -> None:
        with pytest.raises(XiaoyuzhouError) as excinfo:
            extract_eid(bad)
        assert excinfo.value.kind == "bad_eid"

    def test_podcast_url(self) -> None:
        assert extract_pid(f"https://www.xiaoyuzhoufm.com/podcast/{PID}") == PID

    def test_podcast_rejects_episode_url(self) -> None:
        """An episode URL passed to a podcast command must fail loudly."""
        with pytest.raises(XiaoyuzhouError):
            extract_pid(f"https://www.xiaoyuzhoufm.com/episode/{EID}")


class TestShownotesToText:
    def test_strips_tags_and_keeps_text(self) -> None:
        assert shownotes_to_text("<p>hello <span>world</span></p>") == "hello world"

    def test_keeps_link_targets(self) -> None:
        """Shownotes are mostly reference lists; bare anchor text loses the URL."""
        out = shownotes_to_text('<p>see <a href="https://e.com">this</a></p>')
        assert "https://e.com" in out

    def test_paragraphs_become_blank_lines(self) -> None:
        out = shownotes_to_text("<p>one</p><p>two</p>")
        assert out.splitlines()[0] == "one"
        assert "two" in out

    def test_entities_are_unescaped(self) -> None:
        assert "&" in shownotes_to_text("<p>a &amp; b</p>")

    def test_no_runaway_blank_lines(self) -> None:
        out = shownotes_to_text("<p>a</p><div></div><br/><br/><p>b</p>")
        assert "\n\n\n" not in out

    @pytest.mark.parametrize("value", [None, "", "   "])
    def test_empty_input(self, value) -> None:
        assert shownotes_to_text(value) == ""

    def test_malformed_html_still_yields_text(self) -> None:
        assert "keep" in shownotes_to_text("<p>keep<<<>unclosed")

    def test_real_shownotes_shrink_substantially(self) -> None:
        raw = load_fixture("episode.json")["props"]["pageProps"]["episode"]["shownotes"]
        text = shownotes_to_text(raw)
        assert text and len(text) < len(raw)
        assert "<p>" not in text


class TestFormatDuration:
    @pytest.mark.parametrize(
        "seconds,expected",
        [
            (0, "0:00"),
            (59, "0:59"),
            (60, "1:00"),
            (5491, "1:31:31"),
            (3600, "1:00:00"),
        ],
    )
    def test_format(self, seconds: int, expected: str) -> None:
        assert format_duration(seconds) == expected

    @pytest.mark.parametrize("bad", [None, "", "abc", -5, [1]])
    def test_missing_or_invalid_is_empty(self, bad) -> None:
        assert format_duration(bad) == ""


class TestLocalDate:
    def test_utc_evening_rolls_to_next_beijing_day(self) -> None:
        assert local_date("2026-05-28T16:30:00.000Z") == "2026-05-29"

    def test_utc_morning_stays(self) -> None:
        assert local_date("2026-05-28T02:30:00.000Z") == "2026-05-28"

    def test_explicit_offset_respected(self) -> None:
        assert local_date("2026-05-29T00:30:00+08:00") == "2026-05-29"

    @pytest.mark.parametrize("value", [None, ""])
    def test_empty(self, value) -> None:
        assert local_date(value) == ""

    def test_cn_tz(self) -> None:
        assert CN_TZ.utcoffset(None) == timedelta(hours=8)


class TestGetEpisode:
    def test_core_fields(self, episode_client: PublicClient) -> None:
        ep, _ = episode_client.get_episode(EID)
        assert ep.eid == EID
        assert ep.title
        assert ep.podcast_title
        assert ep.podcast_author
        assert ep.duration_seconds and ep.duration_seconds > 0
        assert ep.duration
        assert ep.web_url.endswith(EID)

    def test_audio_url_present(self, episode_client: PublicClient) -> None:
        ep, _ = episode_client.get_episode(EID)
        assert ep.audio_url and ep.audio_url.startswith("https://")
        assert ep.audio_mime

    def test_shownotes_both_forms(self, episode_client: PublicClient) -> None:
        ep, _ = episode_client.get_episode(EID)
        assert ep.shownotes_html and "<" in ep.shownotes_html
        assert ep.shownotes_text and "<p>" not in ep.shownotes_text

    def test_reports_official_transcript_exists(self, episode_client: PublicClient) -> None:
        """The page gives a transcript mediaId but no URL. Saying so is the
        point: a caller should know local transcription is the only route."""
        ep, _ = episode_client.get_episode(EID)
        assert ep.has_official_transcript is True

    def test_as_dict_omits_html_by_default(self, episode_client: PublicClient) -> None:
        ep, _ = episode_client.get_episode(EID)
        assert "shownotes_html" not in ep.as_dict()
        assert "shownotes_html" in ep.as_dict(include_html=True)

    def test_as_dict_is_json_serialisable(self, episode_client: PublicClient) -> None:
        ep, _ = episode_client.get_episode(EID)
        json.dumps(ep.as_dict(include_html=True), ensure_ascii=False)

    def test_accepts_a_url(self, episode_client: PublicClient) -> None:
        ep, _ = episode_client.get_episode(
            f"https://www.xiaoyuzhoufm.com/episode/{EID}"
        )
        assert ep.eid == EID


class TestComments:
    def test_returns_comments_with_fields(self, episode_client: PublicClient) -> None:
        result = episode_client.get_comments(EID)
        assert result["returned"] > 0
        c = result["comments"][0]
        assert c["author"]
        assert c["text"]
        assert isinstance(c["like_count"], int)

    def test_flags_the_podcaster(self, episode_client: PublicClient) -> None:
        """A host's own reply carries more weight than a listener's."""
        result = episode_client.get_comments(EID)
        assert any(c["is_podcaster"] for c in result["comments"])

    def test_includes_replies(self, episode_client: PublicClient) -> None:
        result = episode_client.get_comments(EID)
        with_replies = [c for c in result["comments"] if c.get("replies")]
        assert with_replies
        assert with_replies[0]["replies"][0]["text"]

    def test_truncation_is_declared(self, episode_client: PublicClient) -> None:
        """20-of-275 must never be presented as the complete set."""
        result = episode_client.get_comments(EID)
        assert result["comment_count"] > result["returned"]
        assert result["truncated"] is True
        assert "note" in result and str(result["comment_count"]) in result["note"]

    def test_identity_fields_dropped_by_default(self, episode_client: PublicClient) -> None:
        result = episode_client.get_comments(EID)
        assert "author_detail" not in result["comments"][0]
        blob = json.dumps(result, ensure_ascii=False)
        uid = load_fixture("episode.json")["props"]["pageProps"]["comments"][0][
            "author"
        ]["uid"]
        assert uid not in blob

    def test_identity_available_on_request(self, episode_client: PublicClient) -> None:
        result = episode_client.get_comments(EID, include_identity=True)
        assert "author_detail" in result["comments"][0]

    def test_not_truncated_when_all_present(self) -> None:
        payload = load_fixture("episode.json")
        payload["props"]["pageProps"]["episode"]["commentCount"] = 3

        def handler(request):
            return httpx.Response(200, text=page_html(payload))

        with make_client(handler) as c:
            result = c.get_comments(EID)
            assert result["truncated"] is False
            assert "note" not in result


class TestNormalizeComment:
    def test_null_author_does_not_crash(self) -> None:
        assert _normalize_comment({"id": "x", "author": None})["author"] is None

    def test_null_replies(self) -> None:
        assert "replies" not in _normalize_comment({"id": "x", "replies": None})

    def test_non_dict_replies_are_skipped(self) -> None:
        out = _normalize_comment({"id": "x", "replies": ["junk", {"id": "r"}]})
        assert len(out["replies"]) == 1

    def test_empty_dict(self) -> None:
        assert _normalize_comment({})["author"] is None


class TestGetPodcast:
    def test_metadata(self, podcast_client: PublicClient) -> None:
        pod = podcast_client.get_podcast(PID)
        assert pod["pid"] == PID
        assert pod["title"]
        assert pod["author"]
        assert pod["subscription_count"] > 0

    def test_lists_episodes_newest_first(self, podcast_client: PublicClient) -> None:
        pod = podcast_client.get_podcast(PID)
        assert pod["episodes_returned"] > 0
        dates = [e["pub_date"] for e in pod["episodes"]]
        assert dates == sorted(dates, reverse=True)

    def test_episode_entries_are_actionable(self, podcast_client: PublicClient) -> None:
        """Each listed episode should be directly fetchable."""
        pod = podcast_client.get_podcast(PID)
        e = pod["episodes"][0]
        assert extract_eid(e["web_url"]) == e["eid"]
        assert e["duration"]

    def test_truncation_declared(self, podcast_client: PublicClient) -> None:
        pod = podcast_client.get_podcast(PID)
        assert pod["episode_count"] > pod["episodes_returned"]
        assert pod["truncated"] is True

    def test_skips_malformed_entries(self) -> None:
        payload = load_fixture("podcast.json")
        payload["props"]["pageProps"]["podcast"]["episodes"] = [
            {"eid": "a" * 24, "title": "ok", "pubDate": "2026-01-01T00:00:00.000Z"},
            {"title": "no eid"},
            "junk",
            None,
        ]

        def handler(request):
            return httpx.Response(200, text=page_html(payload))

        with make_client(handler) as c:
            assert c.get_podcast(PID)["episodes_returned"] == 1


class TestErrorHandling:
    def test_404_is_not_found(self) -> None:
        def handler(request):
            return httpx.Response(404, text="nope")

        with make_client(handler) as c:
            with pytest.raises(XiaoyuzhouError) as excinfo:
                c.get_episode(EID)
            assert excinfo.value.kind == "not_found"
            assert excinfo.value.hint

    def test_500_is_http_error(self) -> None:
        def handler(request):
            return httpx.Response(500, text="boom")

        with make_client(handler) as c:
            with pytest.raises(XiaoyuzhouError) as excinfo:
                c.get_episode(EID)
            assert excinfo.value.kind == "http_error"

    def test_missing_next_data_is_flagged_as_layout_change(self) -> None:
        """If the site drops __NEXT_DATA__, say the tool needs updating rather
        than reporting a confusing parse failure."""

        def handler(request):
            return httpx.Response(200, text="<html><body>redesigned</body></html>")

        with make_client(handler) as c:
            with pytest.raises(XiaoyuzhouError) as excinfo:
                c.get_episode(EID)
            assert excinfo.value.kind == "page_shape_changed"

    def test_invalid_json_in_next_data(self) -> None:
        def handler(request):
            return httpx.Response(
                200,
                text='<script id="__NEXT_DATA__" type="application/json">{bad</script>',
            )

        with make_client(handler) as c:
            with pytest.raises(XiaoyuzhouError) as excinfo:
                c.get_episode(EID)
            assert excinfo.value.kind == "page_parse_failed"

    def test_page_without_episode_object(self) -> None:
        def handler(request):
            return httpx.Response(200, text=page_html({"props": {"pageProps": {}}}))

        with make_client(handler) as c:
            with pytest.raises(XiaoyuzhouError) as excinfo:
                c.get_episode(EID)
            assert excinfo.value.kind == "no_episode_data"

    def test_network_failure_is_described(self) -> None:
        def handler(request):
            raise httpx.ConnectError("no route")

        with make_client(handler) as c:
            with pytest.raises(XiaoyuzhouError) as excinfo:
                c.get_episode(EID)
            assert excinfo.value.kind == "network_error"

    def test_error_as_dict_shape(self) -> None:
        e = XiaoyuzhouError("k", "m", hint="h")
        assert e.as_dict() == {"error": "k", "message": "m", "hint": "h"}
        assert "hint" not in XiaoyuzhouError("k", "m").as_dict()


class TestNullSafety:
    """Upstream sends explicit nulls for absent objects, so `.get(k, {})` is
    not enough — the default only applies when the key is missing."""

    @pytest.mark.parametrize(
        "key", ["podcast", "enclosure", "media", "image", "transcript"]
    )
    def test_null_nested_objects(self, key: str) -> None:
        payload = load_fixture("episode.json")
        payload["props"]["pageProps"]["episode"][key] = None

        def handler(request):
            return httpx.Response(200, text=page_html(payload))

        with make_client(handler) as c:
            ep, _ = c.get_episode(EID)
            assert ep.eid == EID

    def test_null_media_source(self) -> None:
        payload = load_fixture("episode.json")
        payload["props"]["pageProps"]["episode"]["media"] = {"source": None}
        payload["props"]["pageProps"]["episode"]["enclosure"] = None

        def handler(request):
            return httpx.Response(200, text=page_html(payload))

        with make_client(handler) as c:
            ep, _ = c.get_episode(EID)
            assert ep.audio_url is None

    def test_null_comments(self) -> None:
        payload = load_fixture("episode.json")
        payload["props"]["pageProps"]["comments"] = None

        def handler(request):
            return httpx.Response(200, text=page_html(payload))

        with make_client(handler) as c:
            assert c.get_comments(EID)["returned"] == 0

    def test_falls_back_to_enclosure_when_media_absent(self) -> None:
        payload = load_fixture("episode.json")
        ep_raw = payload["props"]["pageProps"]["episode"]
        ep_raw["media"] = None
        ep_raw["enclosure"] = {"url": "https://cdn.example/a.mp3"}

        def handler(request):
            return httpx.Response(200, text=page_html(payload))

        with make_client(handler) as c:
            ep, _ = c.get_episode(EID)
            assert ep.audio_url == "https://cdn.example/a.mp3"


def _episode(**kwargs) -> Episode:
    base = dict(
        eid="e" * 24,
        title="T",
        podcast_title="P",
        podcast_author="A",
        pid="p" * 24,
        pub_date="2026-01-02T00:00:00.000Z",
        pub_date_local="2026-01-02",
        duration_seconds=60,
        duration="1:00",
        audio_url="https://cdn.example/a.m4a",
        audio_mime="audio/mp4",
        audio_size_bytes=1234,
        shownotes_text="notes",
        shownotes_html="<p>notes</p>",
        play_count=1,
        comment_count=0,
        clap_count=0,
        favorite_count=0,
        pay_type="FREE",
        cover_url=None,
        web_url="https://www.xiaoyuzhoufm.com/episode/" + "e" * 24,
        has_official_transcript=False,
    )
    base.update(kwargs)
    return Episode(**base)


class TestAudioNaming:
    @pytest.mark.parametrize(
        "mime,expected",
        [
            ("audio/mpeg", ".mp3"),
            ("audio/mp4", ".m4a"),
            ("audio/x-m4a", ".m4a"),
            ("audio/mp4; codecs=mp4a.40.2", ".m4a"),
            ("AUDIO/MPEG", ".mp3"),
        ],
    )
    def test_extension_from_mime(self, mime: str, expected: str) -> None:
        """Naming an AAC file .mp3 confuses players and transcribers alike."""
        assert audio_extension(_episode(audio_mime=mime)) == expected

    def test_falls_back_to_url_suffix(self) -> None:
        ep = _episode(audio_mime=None, audio_url="https://cdn.example/x.mp3?sign=1")
        assert audio_extension(ep) == ".mp3"

    def test_unknown_is_generic(self) -> None:
        assert audio_extension(_episode(audio_mime="x/y", audio_url="")) == ".audio"

    def test_default_name_has_date_title_and_ext(self) -> None:
        name = default_audio_name(_episode(title="第 1 期：开场"))
        assert name.startswith("2026-01-02 ")
        assert name.endswith(".m4a")

    @pytest.mark.parametrize("ch", ["/", "\\", ":", "*", "?", '"', "<", ">", "|"])
    def test_unsafe_characters_removed(self, ch: str) -> None:
        assert ch not in safe_filename(f"a{ch}b")

    def test_control_characters_removed(self) -> None:
        assert "\n" not in safe_filename("a\nb") and "\x00" not in safe_filename("a\x00b")

    def test_long_titles_truncated(self) -> None:
        assert len(safe_filename("长" * 300)) <= 80

    def test_never_empty(self) -> None:
        assert safe_filename("") and safe_filename("...") and safe_filename("///")

    def test_cjk_preserved(self) -> None:
        assert "播客" in safe_filename("播客 第一期")


class TestAudioDownload:
    def _audio_client(self, body: bytes, status: int = 200):
        payload = load_fixture("episode.json")

        def handler(request: httpx.Request) -> httpx.Response:
            if "media" in str(request.url) or str(request.url).endswith((".m4a", ".mp3")):
                return httpx.Response(status, content=body)
            return httpx.Response(200, text=page_html(payload))

        return make_client(handler)

    def test_downloads_to_named_file(self, tmp_path: Path) -> None:
        with self._audio_client(b"AUDIODATA" * 100) as c:
            ep, _ = c.get_episode(EID)
            dest = tmp_path / "out.m4a"
            path = c.download_audio(ep, dest)
            assert path == dest
            assert path.read_bytes() == b"AUDIODATA" * 100

    def test_directory_target_gets_generated_name(self, tmp_path: Path) -> None:
        with self._audio_client(b"X" * 10) as c:
            ep, _ = c.get_episode(EID)
            path = c.download_audio(ep, tmp_path)
            assert path.parent == tmp_path
            assert path.suffix in (".m4a", ".mp3")

    def test_creates_missing_parents(self, tmp_path: Path) -> None:
        with self._audio_client(b"X") as c:
            ep, _ = c.get_episode(EID)
            path = c.download_audio(ep, tmp_path / "a" / "b" / "c.m4a")
            assert path.exists()

    def test_no_part_file_left_on_success(self, tmp_path: Path) -> None:
        with self._audio_client(b"X" * 50) as c:
            ep, _ = c.get_episode(EID)
            c.download_audio(ep, tmp_path / "o.m4a")
            assert list(tmp_path.glob("*.part")) == []

    def test_partial_file_removed_on_failure(self, tmp_path: Path) -> None:
        """An interrupted download must not leave a file that looks complete."""
        payload = load_fixture("episode.json")

        def handler(request: httpx.Request) -> httpx.Response:
            if "media" in str(request.url):
                raise httpx.ReadError("connection dropped")
            return httpx.Response(200, text=page_html(payload))

        with make_client(handler) as c:
            ep, _ = c.get_episode(EID)
            dest = tmp_path / "o.m4a"
            with pytest.raises(XiaoyuzhouError) as excinfo:
                c.download_audio(ep, dest)
            assert excinfo.value.kind == "audio_fetch_failed"
            assert not dest.exists()
            assert list(tmp_path.glob("*.part")) == []

    def test_cdn_error_is_reported(self, tmp_path: Path) -> None:
        with self._audio_client(b"", status=403) as c:
            ep, _ = c.get_episode(EID)
            with pytest.raises(XiaoyuzhouError) as excinfo:
                c.download_audio(ep, tmp_path / "o.m4a")
            assert excinfo.value.kind == "audio_http_error"

    def test_missing_audio_url_is_refused(self, tmp_path: Path) -> None:
        with self._audio_client(b"X") as c:
            with pytest.raises(XiaoyuzhouError) as excinfo:
                c.download_audio(_episode(audio_url=None), tmp_path / "o.m4a")
            assert excinfo.value.kind == "no_audio_url"

    def test_progress_callback_receives_totals(self, tmp_path: Path) -> None:
        seen: list[tuple[int, int | None]] = []
        with self._audio_client(b"Z" * 5000) as c:
            ep, _ = c.get_episode(EID)
            c.download_audio(ep, tmp_path / "o.m4a", on_progress=lambda d, t: seen.append((d, t)))
        assert seen and seen[-1][0] == 5000


class TestRequestHygiene:
    def test_sends_a_plain_browser_user_agent(self) -> None:
        """No forged device fingerprint: an ordinary desktop UA and nothing more."""
        seen: dict[str, str] = {}
        payload = load_fixture("episode.json")

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, text=page_html(payload))

        with make_client(handler) as c:
            c.get_episode(EID)

        ua = seen["user-agent"]
        assert "Mozilla/5.0" in ua and "Chrome" in ua
        for forged in ("xiaomi", "mi 6", "android", "xiaoyuzhou/"):
            assert forged not in ua.lower()

    def test_sends_no_auth_headers(self) -> None:
        seen: dict[str, str] = {}
        payload = load_fixture("episode.json")

        def handler(request: httpx.Request) -> httpx.Response:
            seen.update(request.headers)
            return httpx.Response(200, text=page_html(payload))

        with make_client(handler) as c:
            c.get_episode(EID)

        for header in seen:
            assert not header.lower().startswith("x-jike")
        assert "authorization" not in seen

    def test_only_public_web_host_is_contacted(self) -> None:
        urls: list[str] = []
        payload = load_fixture("episode.json")

        def handler(request: httpx.Request) -> httpx.Response:
            urls.append(str(request.url))
            return httpx.Response(200, text=page_html(payload))

        with make_client(handler) as c:
            c.get_episode(EID)
            c.get_comments(EID)

        assert urls and all(u.startswith("https://www.xiaoyuzhoufm.com/") for u in urls)
        # The app's private API must never be touched.
        assert not any("api.xiaoyuzhoufm.com" in u for u in urls)
        assert not any("podcaster-api" in u for u in urls)

    def test_rate_limit_spaces_requests(self) -> None:
        """The courtesy delay is real behaviour and should be observable."""
        import time

        payload = load_fixture("episode.json")

        def handler(request):
            return httpx.Response(200, text=page_html(payload))

        client = PublicClient(min_interval=0.25)
        client._http = httpx.Client(transport=httpx.MockTransport(handler))
        with client:
            start = time.monotonic()
            client.get_episode(EID)
            client.get_episode(EID)
            assert time.monotonic() - start >= 0.25


class TestNoLoginSurfaceRemains:
    """The login-based client was removed rather than left to be misused."""

    def test_package_exports_only_public_client(self) -> None:
        import xiaoyuzhou

        assert not hasattr(xiaoyuzhou, "XiaoyuzhouClient")
        assert hasattr(xiaoyuzhou, "PublicClient")

    def test_no_token_or_login_code_in_the_package(self) -> None:
        pkg = Path(__file__).parent.parent / "xiaoyuzhou"
        joined = "\n".join(
            p.read_text(encoding="utf-8") for p in pkg.glob("*.py")
        ).lower()
        for banned in (
            "x-jike-access-token",
            "x-jike-refresh-token",
            "login_with_sms",
            "send_sms_code",
            "api.xiaoyuzhoufm.com",
            "podcaster-api",
        ):
            assert banned not in joined, f"{banned!r} still present"
