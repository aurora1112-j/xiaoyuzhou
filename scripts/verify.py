#!/usr/bin/env python3
"""Live smoke test against the real API — needs a valid token.

This is the online counterpart to `pytest tests/`, which is fully offline.
Run it after logging in:

    xyz send-code <phone> && xyz login <phone> <code>
    python scripts/verify.py
"""

import json
import sys

from xiaoyuzhou.client import XiaoyuzhouClient, XiaoyuzhouError


def main() -> int:
    with XiaoyuzhouClient() as c:
        if not c.creds.access_token:
            print("not logged in. run `xyz send-code <phone>` then `xyz login <phone> <code>`.",
                  file=sys.stderr)
            return 1
        try:
            subs = c.list_subscriptions()
            print(f"--- subscriptions ({len(subs)}) ---")
            for s in subs[:5]:
                print(f"  {s['pid']:>24} | {s.get('title')!r} by {s.get('author')!r}")
                print(f"    latest={s.get('latest_episode_pub_date')} unread={s.get('has_unread')}")
            if not subs:
                print("  (empty — check sortBy params or session)")
                return 2

            pid = subs[0]["pid"]
            eps = c.list_episodes(pid, limit=5)
            print(f"\n--- episodes of {subs[0]['title']!r} ({len(eps)}) ---")
            for e in eps[:3]:
                print(f"  {e['eid']:>24} | {e.get('title')!r}  ({e.get('duration_seconds')}s)")
                print(f"    audio: {(e.get('audio_url') or '')[:80]}")

            if eps:
                ep = eps[0]
                t = c.get_transcript_url(ep["eid"], ep["media_id"])
                print(f"\n--- transcript for {ep['title']!r} ---")
                print(json.dumps(t, indent=2, ensure_ascii=False))

            print("\n--- play history ---")
            hist = c.list_play_history(limit=10)
            print(f"  count: {len(hist)}")
            for h in hist[:5]:
                flag = "✓" if h.get("is_finished") else ("•" if h.get("is_played") else "·")
                print(f"  {flag} {h.get('podcast_title')!r} > {h.get('title')!r}")
            return 0
        except XiaoyuzhouError as e:
            print(f"ERROR [{e.kind}] {e.message}", file=sys.stderr)
            if e.hint:
                print(f"hint: {e.hint}", file=sys.stderr)
            return 1


if __name__ == "__main__":
    sys.exit(main())
