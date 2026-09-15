# Changelog

## Unreleased

Initial version: a read-only CLI for Xiaoyuzhou FM.

### Added

- `subs`, `episodes`, `episode`, `transcript`, `transcript-url`, `search`,
  `history` — all read-only; no write endpoints are exposed.
- SMS login (`send-code` / `login`) with atomic token storage: `fcntl.flock`
  plus a uuid'd temp file and `os.replace`, directory `0o700`, file `0o600`.
- Transparent auth: a 401 triggers one refresh and a retry. A 4xx during
  refresh clears the stored token; network errors leave it alone.
- Real pagination: `list_episodes` follows the upstream `loadMoreKey` cursor
  (ceiling 100 pages), dedupes on `eid` so a cursor that shifts mid-walk can't
  yield the same episode twice, and stops as soon as the feed drops below
  `--since`. `--limit` is always an upper bound.
- Beijing-local date windows. The API reports `pubDate` in UTC while the app
  and the user both mean Beijing days, so `--since` / `--until` convert before
  comparing — an episode posted 00:00–08:00 Beijing lands on the right day.
- Composable output: JSON to stdout, exit codes for control flow, `--jsonl`
  for piping, `--fields` to project, `--full` to keep `shownotes_html`.
- Transcript support for RSS-bridged shows, which serve the transcript under a
  native `transcriptMediaId` rather than `media.id`.
- 84 offline tests covering the normalizers, pagination, CLI exit codes and
  credential handling. No token and no network required.
- CI on Python 3.11–3.13 with `ruff`. Runners are UTC deliberately: the
  Beijing-time conversions are invisible on a CST machine.

### Notes

- `has_unread` is tri-state (`true` / `false` / `null`). It is derived from
  `latestEpisodePubDate` versus `readTrackInfo.lastSeenAt`, not reported
  directly by upstream, and is `null` when the show has never been opened or a
  timestamp won't parse. Treat `null` as unknown, not as "unread".
