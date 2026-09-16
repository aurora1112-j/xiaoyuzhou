# xiaoyuzhou-cli

> Read-only CLI for **Xiaoyuzhou FM (小宇宙 FM / 小宇宙播客)** — fetch an episode's audio, shownotes and comments from the public web pages. No account, no token.

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.11%2B-green.svg)](https://www.python.org/)

## What this is

`xyz` reads what xiaoyuzhoufm.com publishes on its **public pages** and prints it as JSON. It composes with `jq`, uses exit codes for control flow, and needs no login.

- 单集音频 / an episode's audio — direct download, resumable naming, progress
- Shownotes — plain text (links preserved) or raw HTML
- 热门评论 / the comments the page carries, with replies and podcaster flags
- 播客信息 / a show's metadata and the episodes its page lists

## Scope, and why it is drawn here

This tool deliberately talks **only** to `https://www.xiaoyuzhoufm.com/`, which `robots.txt` permits (it disallows only `/www`, `/www/login` and `/www/recharge`). It sends an ordinary desktop browser User-Agent and nothing else — no forged device fingerprint, no auth headers, no reverse-engineered app endpoints.

**Not implemented, by design:**

| Not available | Why |
|---|---|
| 我的订阅 / subscriptions | Requires a logged-in account |
| 播放历史 / play history | Requires a logged-in account |
| 官方字幕 / official transcripts | The page exposes only `transcript.mediaId`, no URL. Fetching one needs the app's private token-authenticated endpoint, whose CDN enforces a User-Agent allowlist. |
| 全量评论 / all comments | The page carries only the first batch (~20). The web surface has no pagination endpoint. |

Using an unauthorised third-party tool against the app's private API violates §3.9 and §3.10 of [小宇宙软件许可及服务协议](https://post.xiaoyuzhoufm.com/podcast-agreement/) and can get an account suspended under §8.2. That is why the login-based surface was removed rather than merely throttled.

**For transcripts**, `xyz ep` reports `audio_url` and `has_official_transcript`, so you can download the audio and transcribe it locally with whatever tool you prefer.

## Commands

```
xyz ep <eid|url>                    # metadata + shownotes + top comments
xyz ep <eid|url> --text             # just the shownotes, as plain text
xyz ep <eid|url> --html             # also include raw shownotes HTML
xyz ep <eid|url> --no-comments      # skip the comments section

xyz audio <eid|url> -o <path>       # download the audio (file or directory)
xyz audio <eid|url> --url-only      # print the URL, download nothing

xyz comments <eid|url>              # the comments the page carries
xyz comments <eid|url> --identity   # include commenter uid/avatar/bio

xyz podcast <pid|url>               # a show + the episodes its page lists
```

Every command accepts a bare 24-hex id **or** the full page URL. `--fields a,b,c` projects the output to just those keys; `--jsonl` prints one object per line.

### Exit codes

| Code | Meaning |
|---|---|
| `0` | success |
| `2` | a described error — JSON with `error`/`message`/`hint` on stderr |
| `1` | an unexpected error |
| `3` | `--text` was asked for text this result has none of |
| `130` | interrupted |

## Quick start

```bash
cd xiaoyuzhou
pip install -e .          # installs the `xyz` command
# …or run without installing:  ./run ep <eid>
```

```bash
# What is this episode about?
xyz ep 6952ae2814db1df9ef6556f8 --text

# Grab the audio, named "<date> <title>.m4a"
xyz audio 6952ae2814db1df9ef6556f8 -o ~/Podcasts/

# What did listeners say? (top comments, newest show first)
xyz comments 6952ae2814db1df9ef6556f8 | jq -r '.comments[] | "\(.author): \(.text)"'

# Latest episodes of a show, title and date only
xyz podcast 60bc9bc72e2eec1ef1ca23ac --fields episodes \
  | jq -r '.episodes[] | "\(.pub_date_local)  \(.title)"'
```

## Notes

- **Truncation is always declared.** When the page carries fewer comments or episodes than exist, the output sets `truncated: true` and a `note` stating the real total — 20 of 275 is never presented as all of them.
- **Commenter identity is dropped by default.** `uid`, `avatar`, `bio` and `gender` are omitted unless you pass `--identity`; nicknames and IP regions (which the site shows publicly) are kept.
- **Audio extensions follow the MIME type.** Xiaoyuzhou serves both mp3 and m4a; naming an AAC file `.mp3` breaks players and transcribers.
- **Downloads are atomic.** Audio is written to `.part` and renamed on success, so an interrupted run never leaves a file that looks complete.
- **Requests are spaced ~1.5s apart** per client. This is courtesy toward the origin, not evasion.
- **Dates are Beijing-local.** `pubDate` is UTC upstream, but the app and its listeners mean Beijing days, so `pub_date_local` converts before formatting.
- **Paid or private episodes** publish no public media URL; `xyz audio` reports `no_audio_url` rather than writing an empty file.

## Development

```bash
pip install -e ".[dev]"
pytest          # 140 tests, fully offline — no network, no account
ruff check .
```

Tests run against real `__NEXT_DATA__` payloads captured from the site (in `tests/fixtures/`), so they validate the shape the site actually serves. CI covers Python 3.11–3.13; runners are UTC on purpose, since the Beijing-time conversions look fine on a CST machine either way.

## Acknowledgments

Endpoint and page-shape exploration built on the earlier work in [r266-tech/xiaoyuzhou](https://github.com/r266-tech/xiaoyuzhou) (MIT).

## License

[MIT](LICENSE) — see the file for full text.
