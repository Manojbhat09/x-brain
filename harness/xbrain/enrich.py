"""Enrichment engine — L2 author-thread level (Phase 3).

For each tweet in stage 'discovered':
  fetch full tweet + conversation → extract root, in_reply_to, thread ancestors
  → upsert L2 fields → stage 'llm_queued' (for L4/L5 cards).

Lane policy (D1): GraphQL TweetResultByRestId primary → FxTwitter fallback.
Crash safety: lease column with 10-min expiry; kill -9 anywhere is resumable.
"""
from __future__ import annotations

import time

from .lane import AuthRotted, GraphQLLane, QueryIdRotted, RateLimited
from .store import Store

LEASE_S = 600


class Enricher:
    def __init__(self, lane: GraphQLLane, fx, store: Store, batch: int = 50):
        self.lane = lane
        self.fx = fx          # FxLane or None
        self.store = store
        self.batch = batch

    # --- FSM helpers ---------------------------------------------------------
    def _claim(self, tid: str) -> bool:
        now = int(time.time())
        cur = self.store.db.execute(
            "SELECT stage, attempts, lease_until FROM tweets WHERE tweet_id=?", (tid,)).fetchone()
        if not cur:
            return False
        stage, attempts, lease = cur
        if stage != "discovered":
            return False
        if lease and lease > now:
            return False  # another worker holds it
        self.store.db.execute(
            "UPDATE tweets SET stage='enriching', lease_until=? WHERE tweet_id=?",
            (now + LEASE_S, tid))
        self.store.db.commit()
        return True

    def _release(self, tid: str, stage: str, bump_attempt: bool = False):
        self.store.db.execute(
            "UPDATE tweets SET stage=?, lease_until=NULL, attempts=attempts+? WHERE tweet_id=?",
            (stage, 1 if bump_attempt else 0, tid))
        self.store.db.commit()

    # --- main loop -----------------------------------------------------------
    def run(self, max_items: int = 0, log=print) -> dict:
        rows = self.store.pending_discovered(limit=self.batch if not max_items else min(self.batch, max_items))
        done = errs = 0
        t0 = time.time()
        for tid in rows:
            if not self._claim(tid):
                continue
            try:
                detail = self._fetch_detail(tid)
                if detail.get("deleted"):
                    self._release(tid, "tombstone")
                    log(f"tombstoned {tid} (deleted/unavailable)")
                    continue
                self.store.upsert_l2(tid, detail)
                self._release(tid, "llm_queued")
                done += 1
                log(f"enriched {tid}: conv={detail.get('conversation_id')} "
                    f"likes={detail.get('likes')} replies={detail.get('replies_seen')}")
            except RateLimited as e:
                self._release(tid, "discovered", bump_attempt=False)
                log(f"rate window hit ({e}) — stopping, resumable")
                break
            except Exception as e:
                attempts = self.store.bump_attempts(tid)
                if attempts >= 5:
                    self._release(tid, "quarantined")
                    log(f"quarantined {tid}: {e}")
                else:
                    self._release(tid, "discovered", bump_attempt=True)
                    log(f"retry later {tid}: {e}")
                errs += 1
        return {"done": done, "errors": errs, "elapsed_s": round(time.time() - t0)}

    # --- lane policy ---------------------------------------------------------
    def _fetch_detail(self, tid: str) -> dict:
        try:
            return self.lane.fetch_tweet_detail(tid)
        except QueryIdRotted:
            if self.fx:
                return self.fx.fetch_tweet(tid)
            raise
        except AuthRotted:
            if self.fx:
                return self.fx.fetch_tweet(tid)
            raise
        except RateLimited:
            # GraphQL window spent -> shed load to the cookie-free lane
            if self.fx:
                return self.fx.fetch_tweet(tid)
            raise
