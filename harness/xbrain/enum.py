"""Enumerator — Phase 1. Walks UserRepostsTimeline cursor pages into the store.

Stop conditions:
  - no bottom cursor (end of feed), or
  - two consecutive pages with zero NEW ids (double-empty rule), or
  - --max-pages reached.
Resume: cursor checkpointed in kv after every page; --resume continues it.
"""
from __future__ import annotations

import time

from .lane import AuthRotted, GraphQLLane, QueryIdRotted, RateLimited
from .store import Store

DOUBLE_EMPTY_STOP = 2


class Enumerator:
    def __init__(self, lane: GraphQLLane, store: Store, user_id: str, count: int = 20):
        self.lane = lane
        self.store = store
        self.user_id = user_id
        self.count = count

    def run(self, resume: bool = False, max_pages: int = 0, log=print) -> dict:
        cursor = self.store.kv_get("enum_cursor") if resume else None
        if not resume:
            self.store.kv_set("enum_cursor", "")
            self.store.kv_set("enum_empty", "0")

        pages = 0
        empty_streak = int(self.store.kv_get("enum_empty") or 0)
        t0 = time.time()
        try:
            while True:
                items, bottom = self.lane.fetch_page(self.user_id, cursor or None, self.count)
                new = self.store.insert_discovered(items)
                pages += 1

                if new == 0:
                    empty_streak += 1
                else:
                    empty_streak = 0
                self.store.kv_set("enum_empty", str(empty_streak))

                log(f"page {pages}: {len(items)} items, {new} new, "
                    f"cursor={'yes' if bottom else 'no'} | {self.lane.status()}")

                self.store.kv_set("enum_cursor", bottom or "")
                if not bottom:
                    log("end of feed (no bottom cursor)")
                    self.store.kv_set("enum_done", "1")
                    break
                if empty_streak >= DOUBLE_EMPTY_STOP:
                    log(f"double-empty stop ({DOUBLE_EMPTY_STOP} pages, no new ids)")
                    self.store.kv_set("enum_done", "1")
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
