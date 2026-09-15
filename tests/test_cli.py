"""CLI surface tests: field projection, exit codes, error contract.

The CLI's promise is "JSON on stdout, exit codes for control flow, never prose"
— these tests hold it to that. No network: the client is monkeypatched.
"""

from __future__ import annotations

import json

import pytest

from xiaoyuzhou.cli import _clean, main
from xiaoyuzhou.client import XiaoyuzhouError

EPISODE = {
    "eid": "e1",
    "title": "T",
    "shownotes_html": "<p>hi</p>",
    "pub_date": "2026-05-28T01:00:00.000Z",
}


class TestFieldProjection:
    def test_shownotes_dropped_by_default(self) -> None:
        assert "shownotes_html" not in _clean(EPISODE, keep_full=False, fields=None)

    def test_full_keeps_shownotes(self) -> None:
        assert _clean(EPISODE, keep_full=True, fields=None)["shownotes_html"] == "<p>hi</p>"

    def test_explicit_field_request_wins_over_slimming(self) -> None:
        """Regression: --fields title,shownotes_html used to return a null for
        shownotes because slimming ran before projection. An agent would read
        that as 'this episode has no shownotes'."""
        out = _clean(EPISODE, keep_full=False, fields=["title", "shownotes_html"])
        assert out == {"title": "T", "shownotes_html": "<p>hi</p>"}

    def test_projection_preserves_requested_order(self) -> None:
        out = _clean(EPISODE, keep_full=False, fields=["pub_date", "title"])
        assert list(out) == ["pub_date", "title"]

    def test_unknown_field_is_null_not_an_error(self) -> None:
        assert _clean(EPISODE, keep_full=False, fields=["nope"]) == {"nope": None}

    def test_non_dict_passes_through(self) -> None:
        assert _clean("raw", keep_full=False, fields=["a"]) == "raw"


class FakeClient:
    """Stands in for XiaoyuzhouClient inside cli.run()."""

    def __init__(self, **behaviour) -> None:
        self.behaviour = behaviour

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def _result(self, name):
        val = self.behaviour[name]
        if isinstance(val, Exception):
            raise val
        return val

    def list_subscriptions(self):
        return self._result("subs")

    def list_episodes(self, pid, limit=20, since=None, until=None):
        return self._result("episodes")

    def get_episode(self, eid):
        return self._result("episode")

    def fetch_transcript(self, eid, media_id, fmt="plain", include_segments=False):
        return self._result("transcript")


@pytest.fixture
def patch_client(monkeypatch):
    def _install(**behaviour):
        import xiaoyuzhou.cli as cli_mod

        monkeypatch.setattr(cli_mod, "XiaoyuzhouClient", lambda *a, **k: FakeClient(**behaviour))

    return _install


class TestExitCodes:
    def test_success_is_zero(self, patch_client, capsys) -> None:
        patch_client(subs=[{"pid": "p", "title": "S"}])
        assert main(["subs"]) == 0
        assert json.loads(capsys.readouterr().out)[0]["pid"] == "p"

    def test_known_error_is_two_and_goes_to_stderr(self, patch_client, capsys) -> None:
        patch_client(subs=XiaoyuzhouError("not_logged_in", "no token", hint="run login"))
        assert main(["subs"]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert json.loads(captured.err) == {
            "error": "not_logged_in",
            "message": "no token",
            "hint": "run login",
        }

    def test_unexpected_error_is_one(self, patch_client, capsys) -> None:
        patch_client(subs=RuntimeError("boom"))
        assert main(["subs"]) == 1
        assert json.loads(capsys.readouterr().err)["error"] == "unexpected"

    def test_text_without_rendered_text_is_three(self, patch_client, capsys) -> None:
        """--text on a segments/no_subtitle result has nothing to print."""
        patch_client(transcript={"status": "no_subtitle"})
        code = main(["transcript", "e1", "--media-id", "m", "--text"])
        assert code == 3
        assert json.loads(capsys.readouterr().err)["error"] == "no_text"

    def test_text_prints_bare_text_on_success(self, patch_client, capsys) -> None:
        patch_client(transcript={"status": "available", "text": "hello world"})
        assert main(["transcript", "e1", "--media-id", "m", "--text"]) == 0
        assert capsys.readouterr().out.strip() == "hello world"


class TestErrorContractIsAlwaysJson:
    @pytest.mark.parametrize(
        "exc",
        [
            XiaoyuzhouError("api_error", "500"),
            RuntimeError("boom"),
            ValueError("Expecting value: line 1 column 1 (char 0)"),
        ],
    )
    def test_stderr_is_parseable_json(self, patch_client, capsys, exc) -> None:
        patch_client(subs=exc)
        main(["subs"])
        payload = json.loads(capsys.readouterr().err)
        assert "error" in payload and "message" in payload


class TestOutputShape:
    def test_jsonl_is_one_object_per_line(self, patch_client, capsys) -> None:
        patch_client(subs=[{"pid": "a"}, {"pid": "b"}])
        main(["subs", "--jsonl"])
        lines = capsys.readouterr().out.strip().splitlines()
        assert [json.loads(line)["pid"] for line in lines] == ["a", "b"]

    def test_default_is_pretty_array(self, patch_client, capsys) -> None:
        patch_client(subs=[{"pid": "a"}])
        main(["subs"])
        out = capsys.readouterr().out
        assert out.startswith("[")
        assert "\n  " in out

    def test_chinese_is_not_escaped(self, patch_client, capsys) -> None:
        patch_client(subs=[{"title": "小宇宙"}])
        main(["subs"])
        assert "小宇宙" in capsys.readouterr().out

    def test_fields_projection_end_to_end(self, patch_client, capsys) -> None:
        patch_client(episodes=[EPISODE])
        main(["episodes", "P", "--fields", "title,shownotes_html"])
        assert json.loads(capsys.readouterr().out) == [
            {"title": "T", "shownotes_html": "<p>hi</p>"}
        ]
