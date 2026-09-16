# Changelog

## Unreleased

Rewritten to read only public data. The login-based surface was removed, not
throttled: using an unauthorised third-party tool against the app's private API
violates §3.9 and §3.10 of the [service agreement](https://post.xiaoyuzhoufm.com/podcast-agreement/)
regardless of how gently it is done.

### Added

- `xyz ep <eid|url>` — episode metadata, shownotes (plain text with links
  preserved, or raw HTML) and the comments the page carries.
- `xyz audio <eid|url>` — download an episode's audio. Names files
  `<date> <title>.<ext>` with the extension taken from the MIME type, writes
  through a `.part` file so an interrupted run leaves nothing that looks
  complete, and reports progress.
- `xyz comments <eid|url>` — comments with replies, podcaster and pinned flags,
  like counts and IP regions.
- `xyz podcast <pid|url>` — a show's metadata and the episodes its page lists.
- Every command accepts a bare 24-hex id or the full page URL.
- Truncation is always declared: when the page carries fewer items than exist,
  the output sets `truncated: true` plus a note with the real total.
- ~1.5s spacing between requests, as courtesy toward the origin.

### Removed

- `client.py` and the whole login surface — SMS login, token storage and
  refresh, subscriptions, play history, unread state.
- The forged device fingerprint (`Xiaomi / MI 6 / 1080x1920`) and the Android
  app User-Agent used to satisfy the transcript CDN's allowlist. Requests now
  send an ordinary desktop browser UA and nothing else.
- All calls to `api.xiaoyuzhoufm.com` and `podcaster-api.xiaoyuzhoufm.com`.
  The only host contacted is `www.xiaoyuzhoufm.com`, which `robots.txt`
  permits. A test asserts this stays true.

### Not available

- **Official transcripts.** The public page exposes only `transcript.mediaId`
  with no URL. `xyz ep` reports `audio_url` and `has_official_transcript` so
  you can download the audio and transcribe locally instead.
- **All comments.** The page carries roughly the first 20; the web surface has
  no pagination endpoint (probed: HTTP 404).
- **Subscriptions, play history, unread state.** Account-scoped.

### Notes

- Commenter identity fields (`uid`, `avatar`, `bio`, `gender`) are dropped
  unless `--identity` is passed. Nicknames and IP regions, which the site shows
  publicly, are kept.
- Dates are Beijing-local: `pubDate` is UTC upstream, but the app and its
  listeners mean Beijing days.
- 140 offline tests, run against real `__NEXT_DATA__` payloads captured from
  the site. CI covers Python 3.11–3.13 on UTC runners.
