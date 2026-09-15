"""Credential load/save tests — corruption handling, atomicity, permissions."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from xiaoyuzhou.client import Credentials, XiaoyuzhouError


@pytest.fixture
def token_path(tmp_path: Path) -> Path:
    return tmp_path / "state" / "xiaoyuzhou" / "token.json"


class TestLoad:
    def test_missing_file_yields_empty_creds(self, token_path: Path) -> None:
        assert Credentials.load(token_path).access_token is None

    def test_roundtrip(self, token_path: Path) -> None:
        Credentials(access_token="a", refresh_token="r", uid="u").save(token_path)
        again = Credentials.load(token_path)
        assert (again.access_token, again.refresh_token, again.uid) == ("a", "r", "u")

    def test_unknown_keys_are_ignored(self, token_path: Path) -> None:
        token_path.parent.mkdir(parents=True)
        token_path.write_text(json.dumps({"access_token": "a", "from_the_future": 1}))
        assert Credentials.load(token_path).access_token == "a"

    @pytest.mark.parametrize("junk", ["not json{", "", "   ", "\x00\x01"])
    def test_corrupt_file_raises_actionable_error(self, token_path: Path, junk: str) -> None:
        """Regression: a truncated token.json used to surface a raw stdlib
        message ('Expecting value: line 1 column 1'), which tells an agent
        nothing about how to recover."""
        token_path.parent.mkdir(parents=True)
        token_path.write_text(junk)
        with pytest.raises(XiaoyuzhouError) as excinfo:
            Credentials.load(token_path)
        err = excinfo.value
        assert err.kind == "corrupt_token"
        assert err.hint and "login" in err.hint.lower()
        assert str(token_path) in err.message

    def test_non_object_json_is_also_corrupt(self, token_path: Path) -> None:
        token_path.parent.mkdir(parents=True)
        token_path.write_text("[1, 2, 3]")
        with pytest.raises(XiaoyuzhouError) as excinfo:
            Credentials.load(token_path)
        assert excinfo.value.kind == "corrupt_token"


class TestSave:
    def test_creates_parents_and_sets_permissions(self, token_path: Path) -> None:
        Credentials(access_token="a").save(token_path)
        assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
        assert stat.S_IMODE(token_path.parent.stat().st_mode) == 0o700

    def test_leaves_no_temp_files_behind(self, token_path: Path) -> None:
        Credentials(access_token="a").save(token_path)
        leftovers = [p.name for p in token_path.parent.iterdir() if ".tmp" in p.name]
        assert leftovers == []

    def test_temp_files_are_gitignored_by_pattern(self) -> None:
        """Temp files sit next to the token and briefly hold a live credential;
        the ignore rules must cover them even outside a `state/` dir."""
        root = Path(__file__).resolve().parent.parent
        patterns = (root / ".gitignore").read_text().split()
        assert "*.tmp" in patterns

    def test_overwrite_is_atomic_in_content(self, token_path: Path) -> None:
        Credentials(access_token="first").save(token_path)
        Credentials(access_token="second").save(token_path)
        assert Credentials.load(token_path).access_token == "second"
        assert json.loads(token_path.read_text())["access_token"] == "second"

    def test_saved_at_is_refreshed(self, token_path: Path) -> None:
        c = Credentials(access_token="a", saved_at=0.0)
        c.save(token_path)
        assert c.saved_at > 0


class TestClear:
    def test_clear_removes_file_and_secrets(self, token_path: Path) -> None:
        creds = Credentials(access_token="a", refresh_token="r", device_id="d")
        creds.save(token_path)
        creds.clear(token_path)
        assert not token_path.exists()
        assert creds.access_token is None and creds.refresh_token is None

    def test_clear_keeps_device_id(self, token_path: Path) -> None:
        """device_id is not a secret and should survive a re-login."""
        creds = Credentials(access_token="a", device_id="stable-device")
        creds.save(token_path)
        creds.clear(token_path)
        assert creds.device_id == "stable-device"

    def test_clear_on_missing_file_is_a_noop(self, token_path: Path) -> None:
        Credentials().clear(token_path)  # must not raise
