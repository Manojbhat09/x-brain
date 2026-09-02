"""Enumerator — Phase 1. Walks UserRepostsTimeline or UserTweets cursor pages into the store.

Stop conditions:
  - no bottom cursor (end of feed), or
  - two consecutive pages with zero NEW ids (double-empty rule), or
  - --max-pages reached.
Resume: cursor checkpointed per-kind (enum_cursor[:posts]) after every page; --resume continues it.
Design note: comments/replies are NOT handled here — they need a separate
SearchTimeline/UserReplies enumerator with ancestor BFS (see design comment below).
"""
from __future__ import annotations

import time

from .lane import AuthRotted, GraphQLLane, QueryIdRotted, RateLimited
from .store import Store

DOUBLE_EMPTY_STOP = 2


class Enumerator:
    def __init__(self, lane: GraphQLLane, store: Store, user_id: str, count: int = 20, kind: str = "reposts"):
        self.lane = lane
        self.store = store
        self.user_id = user_id
        self.count = count
        self.kind = kind if kind in ("reposts", "posts") else "reposts"
        # per-kind cursor keys so reposts ↔ posts don't clobber each other
        self._cursor_key = "enum_cursor" if self.kind == "reposts" else "enum_cursor_posts"
        self._empty_key = "enum_empty" if self.kind == "reposts" else "enum_empty_posts"
        self._done_key = "enum_done" if self.kind == "reposts" else "enum_done_posts"

    def run(self, resume: bool = False, max_pages: int = 0, log=print) -> dict:
        cursor = self.store.kv_get(self._cursor_key) if resume else None
        if not resume:
            self.store.kv_set(self._cursor_key, "")
            self.store.kv_set(self._empty_key, "0")

        pages = 0
        empty_streak = int(self.store.kv_get(self._empty_key) or 0)
        t0 = time.time()
        try:
            while True:
                items, bottom = self.lane.fetch_page(self.user_id, cursor or None, self.count, kind=self.kind)
                raw_n = len(items)
                if self.kind == "posts":
                    # UserTweets returns both originals + reposts; --posts-only keeps originals
                    items = [it for it in items if not it.get("is_retweet")]
                new = self.store.insert_discovered(items)
                if self.kind == "posts" and raw_n != len(items):
                    log(f"  posts filter: {raw_n} fetched → {len(items)} originals ({raw_n - len(items)} reposts dropped)")
                pages += 1

                if new == 0:
                    empty_streak += 1
                else:
                    empty_streak = 0
                self.store.kv_set(self._empty_key, str(empty_streak))

                log(f"page {pages} [{self.kind}]: {len(items)} items, {new} new, "
                    f"cursor={'yes' if bottom else 'no'} | {self.lane.status(self.kind)}")

                self.store.kv_set(self._cursor_key, bottom or "")
                if not bottom:
                    log("end of feed (no bottom cursor)")
                    self.store.kv_set(self._done_key, "1")
                    break
                if empty_streak >= DOUBLE_EMPTY_STOP:
                    log(f"double-empty stop ({DOUBLE_EMPTY_STOP} pages, no new ids)")
                    self.store.kv_set(self._done_key, "1")
                    break
                if max_pages and pages >= max_pages:
                    log(f"max-pages {max_pages} reached — resumable")
                    break
                cursor = bottom
        except (RateLimited, AuthRotted, QueryIdRotted) as e:
            log(f"STOPPED: {e} — state checkpointed, rerun with --resume")
            return {"pages": pages, "error": str(e), "elapsed_s": round(time.time() - t0)}

        return {
            "pages": pages,
            "stats": self.store.stats(),
            "elapsed_s": round(time.time() - t0),
        }
