"""CLI tests: exit codes, JSON contract, flag behaviour. No network.

The CLI's promise is JSON on stdout, exit codes for control flow and never
prose — these hold it to that.
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from xiaoyuzhou.cli import main
from xiaoyuzhou.public_client import PublicClient, XiaoyuzhouError

from .test_public_client import EID, PID, load_fixture, page_html

AUDIO_BODY = b"AUDIO" * 200


@pytest.fixture
def cli(monkeypatch):
    """Installs a PublicClient backed by a local mock transport."""

    def _install(*, episode=True, podcast=False, audio_status=200, raise_error=None):
        ep_payload = load_fixture("episode.json")
        pod_payload = load_fixture("podcast.json")

        def handler(request: httpx.Request) -> httpx.Response:
            url = str(request.url)
            if raise_error:
                raise raise_error
            if "media" in url or url.endswith((".m4a", ".mp3")):
                return httpx.Response(audio_status, content=AUDIO_BODY)
            if "/podcast/" in url:
                return httpx.Response(200, text=page_html(pod_payload))
            return httpx.Response(200, text=page_html(ep_payload))

        def factory(*args, **kwargs):
            c = PublicClient(min_interval=0)
            c._http = httpx.Client(
                transport=httpx.MockTransport(handler), follow_redirects=True
            )
            return c

        import xiaoyuzhou.cli as cli_mod

        monkeypatch.setattr(cli_mod, "PublicClient", factory)

    return _install


class TestEpCommand:
    def test_outputs_json_with_core_fields(self, cli, capsys) -> None:
        cli()
        assert main(["ep", EID]) == 0
        out = json.loads(capsys.readouterr().out)
        for key in ("eid", "title", "audio_url", "shownotes_text", "duration"):
            assert key in out

    def test_includes_comments_by_default(self, cli, capsys) -> None:
        cli()
        main(["ep", EID])
        out = json.loads(capsys.readouterr().out)
        assert out["comments_returned"] > 0
        assert out["comments"][0]["text"]

    def test_declares_comment_truncation(self, cli, capsys) -> None:
        cli()
        main(["ep", EID])
        out = json.loads(capsys.readouterr().out)
        assert out["comments_truncated"] is True
        assert "comments_note" in out

    def test_no_comments_flag(self, cli, capsys) -> None:
        cli()
        main(["ep", EID, "--no-comments"])
        out = json.loads(capsys.readouterr().out)
        assert "comments" not in out

    def test_html_excluded_unless_requested(self, cli, capsys) -> None:
        cli()
        main(["ep", EID])
        assert "shownotes_html" not in json.loads(capsys.readouterr().out)
        main(["ep", EID, "--html"])
        assert "shownotes_html" in json.loads(capsys.readouterr().out)

    def test_text_prints_bare_shownotes(self, cli, capsys) -> None:
        cli()
        assert main(["ep", EID, "--text"]) == 0
        out = capsys.readouterr().out
        assert out.strip()
        assert not out.lstrip().startswith("{")
        assert "<p>" not in out

    def test_text_with_empty_shownotes_exits_3(self, cli, capsys, monkeypatch) -> None:
        payload = load_fixture("episode.json")
        payload["props"]["pageProps"]["episode"]["shownotes"] = ""

        def handler(request):
            return httpx.Response(200, text=page_html(payload))

        def factory(*a, **k):
            c = PublicClient(min_interval=0)
            c._http = httpx.Client(transport=httpx.MockTransport(handler))
            return c

        import xiaoyuzhou.cli as cli_mod

        monkeypatch.setattr(cli_mod, "PublicClient", factory)
        assert main(["ep", EID, "--text"]) == 3
        assert json.loads(capsys.readouterr().err)["error"] == "no_text"

    def test_fields_projection(self, cli, capsys) -> None:
        cli()
        main(["ep", EID, "--fields", "eid,title"])
        assert set(json.loads(capsys.readouterr().out)) == {"eid", "title"}

    def test_accepts_url(self, cli, capsys) -> None:
        cli()
        assert main(["ep", f"https://www.xiaoyuzhoufm.com/episode/{EID}"]) == 0
        assert json.loads(capsys.readouterr().out)["eid"] == EID

    def test_chinese_not_escaped(self, cli, capsys) -> None:
        cli()
        main(["ep", EID])
        assert "\\u" not in capsys.readouterr().out


class TestCommentsCommand:
    def test_outputs_comments(self, cli, capsys) -> None:
        cli()
        assert main(["comments", EID]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["returned"] > 0
        assert out["truncated"] is True

    def test_identity_hidden_by_default(self, cli, capsys) -> None:
        cli()
        main(["comments", EID])
        assert "author_detail" not in json.loads(capsys.readouterr().out)["comments"][0]

    def test_identity_flag(self, cli, capsys) -> None:
        cli()
        main(["comments", EID, "--identity"])
        assert "author_detail" in json.loads(capsys.readouterr().out)["comments"][0]


class TestPodcastCommand:
    def test_lists_episodes(self, cli, capsys) -> None:
        cli()
        assert main(["podcast", PID]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["title"]
        assert out["episodes_returned"] > 0


class TestAudioCommand:
    def test_url_only_does_not_download(self, cli, capsys, tmp_path) -> None:
        cli()
        assert main(["audio", EID, "--url-only"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert out["audio_url"].startswith("https://")
        assert "suggested_filename" in out
        assert list(tmp_path.iterdir()) == []

    def test_downloads_to_path(self, cli, capsys, tmp_path) -> None:
        cli()
        dest = tmp_path / "a.m4a"
        assert main(["audio", EID, "-o", str(dest), "--quiet"]) == 0
        out = json.loads(capsys.readouterr().out)
        assert Path(out["saved_to"]) == dest.resolve()
        assert dest.read_bytes() == AUDIO_BODY

    def test_downloads_into_directory(self, cli, capsys, tmp_path) -> None:
        cli()
        assert main(["audio", EID, "-o", str(tmp_path), "--quiet"]) == 0
        saved = Path(json.loads(capsys.readouterr().out)["saved_to"])
        assert saved.parent == tmp_path.resolve()

    def test_cdn_failure_exits_2(self, cli, capsys, tmp_path) -> None:
        cli(audio_status=403)
        assert main(["audio", EID, "-o", str(tmp_path), "--quiet"]) == 2
        assert json.loads(capsys.readouterr().err)["error"] == "audio_http_error"


class TestExitCodesAndErrorContract:
    def test_bad_id_exits_2_with_hint(self, cli, capsys) -> None:
        cli()
        assert main(["ep", "not-an-id"]) == 2
        err = json.loads(capsys.readouterr().err)
        assert err["error"] == "bad_eid"
        assert "hint" in err

    def test_network_error_exits_2(self, cli, capsys) -> None:
        cli(raise_error=httpx.ConnectError("down"))
        assert main(["ep", EID]) == 2
        assert json.loads(capsys.readouterr().err)["error"] == "network_error"

    def test_unexpected_error_exits_1(self, monkeypatch, capsys) -> None:
        import xiaoyuzhou.cli as cli_mod

        def boom(*a, **k):
            raise RuntimeError("kaboom")

        monkeypatch.setattr(cli_mod, "PublicClient", boom)
        assert main(["ep", EID]) == 1
        assert json.loads(capsys.readouterr().err)["error"] == "unexpected"

    def test_errors_go_to_stderr_not_stdout(self, cli, capsys) -> None:
        cli()
        main(["ep", "bad"])
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err

    @pytest.mark.parametrize(
        "exc",
        [
            XiaoyuzhouError("not_found", "gone"),
            RuntimeError("raw"),
            ValueError("parse"),
        ],
    )
    def test_stderr_is_always_valid_json(self, monkeypatch, capsys, exc) -> None:
        import xiaoyuzhou.cli as cli_mod

        def boom(*a, **k):
            raise exc

        monkeypatch.setattr(cli_mod, "PublicClient", boom)
        main(["ep", EID])
        payload = json.loads(capsys.readouterr().err)
        assert "error" in payload and "message" in payload


class TestParser:
    def test_no_subcommand_is_an_error(self) -> None:
        with pytest.raises(SystemExit) as excinfo:
            main([])
        assert excinfo.value.code != 0

    def test_login_commands_are_gone(self) -> None:
        """Nothing in the CLI should offer to log in any more."""
        for cmd in ("login", "send-code", "subs", "history", "transcript"):
            with pytest.raises(SystemExit):
                main([cmd, "x"])

    def test_help_lists_available_commands(self, capsys) -> None:
        with pytest.raises(SystemExit):
            main(["--help"])
        out = capsys.readouterr().out
        for cmd in ("ep", "audio", "comments", "podcast"):
            assert cmd in out
