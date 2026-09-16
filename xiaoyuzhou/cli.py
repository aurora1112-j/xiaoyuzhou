"""Xiaoyuzhou FM CLI — public data only, no account required.

Every command prints JSON to stdout and uses exit codes for control flow, so a
caller never has to parse prose:

  0  success
  2  a known, described error (JSON on stderr)
  1  an unexpected error (JSON on stderr)
  3  --text asked for text that this result has none of

  xyz ep <eid|url>                    # metadata + shownotes + top comments
  xyz ep <eid|url> --text             # just the shownotes as plain text
  xyz audio <eid|url> -o <path>       # download the audio
  xyz comments <eid|url>              # the comments the public page carries
  xyz podcast <pid|url>              # a show and the episodes its page lists

Not available by design: subscriptions, play history and official transcripts
all require a logged-in account and the app's private API. `xyz ep` reports
`audio_url` and `has_official_transcript` so you can transcribe locally.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path
from typing import Any

from .public_client import (
    PublicClient,
    XiaoyuzhouError,
    default_audio_name,
)


def _emit(obj: Any, *, jsonl: bool = False, fields: list[str] | None = None) -> None:
    if isinstance(obj, list):
        items = [_project(x, fields) for x in obj]
        if jsonl:
            for x in items:
                print(json.dumps(x, ensure_ascii=False))
            return
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return
    print(json.dumps(_project(obj, fields), ensure_ascii=False, indent=2))


def _project(obj: Any, fields: list[str] | None) -> Any:
    if not fields or not isinstance(obj, dict):
        return obj
    return {k: obj.get(k) for k in fields}


def _fields(arg: str | None) -> list[str] | None:
    return [f.strip() for f in arg.split(",") if f.strip()] if arg else None


def _human_size(n: int | None) -> str:
    if not n:
        return "?"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f}{unit}" if unit == "B" else f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="xyz",
        description="Xiaoyuzhou FM CLI — reads public pages, no account needed.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def add_out(sp, *, jsonl: bool = True) -> None:
        if jsonl:
            sp.add_argument(
                "--jsonl", action="store_true", help="one JSON object per line"
            )
        sp.add_argument(
            "--fields", default=None, help="comma-separated keys to keep"
        )

    sp = sub.add_parser("ep", help="one episode: metadata, shownotes, top comments")
    sp.add_argument("episode", help="episode id or its xiaoyuzhoufm.com URL")
    sp.add_argument(
        "--html", action="store_true", help="also include the raw shownotes HTML"
    )
    sp.add_argument(
        "--no-comments", action="store_true", help="skip the comments section"
    )
    sp.add_argument(
        "--text",
        action="store_true",
        help="print only the shownotes as plain text (no JSON)",
    )
    add_out(sp, jsonl=False)

    sp = sub.add_parser("audio", help="download an episode's audio")
    sp.add_argument("episode", help="episode id or its xiaoyuzhoufm.com URL")
    sp.add_argument(
        "-o",
        "--output",
        default=".",
        help="output file, or a directory to name the file automatically",
    )
    sp.add_argument(
        "--url-only",
        action="store_true",
        help="print the audio URL without downloading",
    )
    sp.add_argument("--quiet", action="store_true", help="no progress output")

    sp = sub.add_parser("comments", help="the comments the public page carries")
    sp.add_argument("episode", help="episode id or its xiaoyuzhoufm.com URL")
    sp.add_argument(
        "--identity",
        action="store_true",
        help="include commenter identity fields (uid, avatar, bio) — off by default",
    )
    add_out(sp)

    sp = sub.add_parser("podcast", help="a show and the episodes its page lists")
    sp.add_argument("podcast", help="podcast id or its xiaoyuzhoufm.com URL")
    add_out(sp)

    return p


def _cmd_ep(args: argparse.Namespace, client: PublicClient) -> int | None:
    episode, raw_comments = client.get_episode(args.episode)
    if args.text:
        if not episode.shownotes_text:
            print(
                json.dumps(
                    {
                        "error": "no_text",
                        "message": f"episode {episode.eid} has empty shownotes",
                        "hint": "drop --text to see the rest of the metadata",
                    },
                    ensure_ascii=False,
                ),
                file=sys.stderr,
            )
            return 3
        print(episode.shownotes_text)
        return None

    out = episode.as_dict(include_html=args.html)
    if not args.no_comments:
        from .public_client import _normalize_comment

        items = [_normalize_comment(c) for c in raw_comments]
        out["comments_returned"] = len(items)
        out["comments"] = items
        total = episode.comment_count
        if isinstance(total, int) and total > len(items):
            out["comments_truncated"] = True
            out["comments_note"] = (
                f"showing {len(items)} of {total}; the public page carries only "
                "the first batch and the web surface has no pagination endpoint"
            )
    _emit(out, fields=_fields(args.fields))
    return None


def _cmd_audio(args: argparse.Namespace, client: PublicClient) -> int | None:
    episode, _ = client.get_episode(args.episode)
    if not episode.audio_url:
        raise XiaoyuzhouError(
            "no_audio_url",
            f"episode {episode.eid} exposes no audio URL",
            hint="paid or private episodes do not publish a public media URL",
        )
    if args.url_only:
        _emit(
            {
                "eid": episode.eid,
                "title": episode.title,
                "audio_url": episode.audio_url,
                "audio_mime": episode.audio_mime,
                "audio_size_bytes": episode.audio_size_bytes,
                "suggested_filename": default_audio_name(episode),
            }
        )
        return None

    quiet = args.quiet or not sys.stderr.isatty()
    state = {"last": -1}

    def on_progress(done: int, total: int | None) -> None:
        if quiet:
            return
        if total:
            pct = int(done * 100 / total)
            if pct == state["last"]:
                return
            state["last"] = pct
            print(
                f"\r  {pct:3d}%  {_human_size(done)} / {_human_size(total)}",
                end="",
                file=sys.stderr,
                flush=True,
            )
        else:
            print(f"\r  {_human_size(done)}", end="", file=sys.stderr, flush=True)

    if not quiet:
        print(f"→ {episode.title}", file=sys.stderr)
    path = client.download_audio(episode, Path(args.output), on_progress=on_progress)
    if not quiet:
        print("", file=sys.stderr)
    _emit(
        {
            "eid": episode.eid,
            "title": episode.title,
            "saved_to": str(path.resolve()),
            "size_bytes": path.stat().st_size,
            "duration": episode.duration,
            "mime": episode.audio_mime,
        }
    )
    return None


def run(args: argparse.Namespace) -> int | None:
    """Dispatch one subcommand. Returns an exit code, or None on success."""
    with PublicClient() as client:
        if args.cmd == "ep":
            return _cmd_ep(args, client)
        if args.cmd == "audio":
            return _cmd_audio(args, client)
        if args.cmd == "comments":
            _emit(
                client.get_comments(args.episode, include_identity=args.identity),
                fields=_fields(args.fields),
            )
            return None
        if args.cmd == "podcast":
            _emit(client.get_podcast(args.podcast), fields=_fields(args.fields))
            return None
    return None


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return run(args) or 0
    except XiaoyuzhouError as e:
        print(json.dumps(e.as_dict(), ensure_ascii=False), file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(
            json.dumps({"error": "interrupted", "message": "cancelled by user"}),
            file=sys.stderr,
        )
        return 130
    except BrokenPipeError:
        # A downstream pipe (e.g. `| head`) closed. Point stdout at devnull so
        # the interpreter's shutdown flush cannot re-raise and print a warning.
        with contextlib.suppress(Exception):
            os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0
    except Exception as e:  # noqa: BLE001 — surface anything else as clean JSON
        print(
            json.dumps(
                {"error": "unexpected", "message": str(e)}, ensure_ascii=False
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
