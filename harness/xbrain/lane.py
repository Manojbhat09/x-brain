"""GraphQL lane: executes UserRepostsTimeline requests with pacing, TID, and
rate-window persistence. Raises typed errors the FSM understands."""
from __future__ import annotations

import time
from pathlib import Path

import requests

from . import protocol
from .ratelimit import RateLimiter
from .store import parse_timeline
class Deleted(Exception):
    pass


class AuthRotted(Exception):
    pass


class QueryIdRotted(Exception):
    pass


class RateLimited(Exception):
    def __init__(self, reset_s: float):
        self.reset_s = reset_s
        super().__init__(f"429, reset in {reset_s:.0f}s")


class GraphQLLane:
    def __init__(self, creds: dict, limits_path: Path, cache_dir: Path):
        self.creds = creds
        self.http = requests.Session()
        self.http.headers["Accept-Encoding"] = "gzip, deflate"
        self.limiter = RateLimiter(limits_path)
        self.cache_dir = cache_dir

    def fetch_page(self, user_id: str, cursor: str | None, count: int = 20, kind: str = "reposts") -> tuple[list[dict], str | None]:
        """One timeline page → (items, bottom_cursor). kind: reposts|posts."""
        url = protocol.build_url(user_id, cursor, count, kind=kind)
        op = protocol.OP_POSTS if kind == "posts" else protocol.OP_REPOSTS
        pairs = protocol._load_tid_pairs(self.cache_dir / "tid_pairs.json", self.http)
        headers = protocol.build_headers(self.creds, url, pairs)

        self.limiter.wait(op)
        resp = self.http.post(url, headers=headers, timeout=30)
        self.limiter.observe(op, resp.headers)

        if resp.status_code == 429:
            reset = int(resp.headers.get("x-rate-limit-reset", time.time() + 900))
            raise RateLimited(max(0, reset - time.time()))
        if resp.status_code in (401, 403):
            raise AuthRotted(f"HTTP {resp.status_code} — refresh auth_token/ct0")
        if resp.status_code == 404:
            raise QueryIdRotted("404 — queryId likely rotated; re-sniff from live tab")
        if resp.status_code == 420:  # X's 'enhance your calm'
            raise RateLimited(900)
        resp.raise_for_status()

        items, bottom = parse_timeline(resp.json())
        return items, bottom

    def status(self, kind: str = "reposts") -> str:
        op = protocol.OP_POSTS if kind == "posts" else protocol.OP_REPOSTS
        return self.limiter.status(op)

    # --- L2: per-ID detail (thread context) -----------------------------------
    def fetch_tweet_detail(self, tweet_id: str) -> dict:
        from .store import parse_tweet_detail
        url = protocol.build_tweet_url(tweet_id)
        pairs = protocol._load_tid_pairs(self.cache_dir / "tid_pairs.json", self.http)
        headers = protocol.build_headers(self.creds, url, pairs)
        self.limiter.wait(protocol.OP_TWEET)
        resp = self.http.get(url, headers=headers, timeout=30)
        self.limiter.observe(protocol.OP_TWEET, resp.headers)
        if resp.status_code == 429:
            reset = int(resp.headers.get("x-rate-limit-reset", time.time() + 900))
            raise RateLimited(max(0, reset - time.time()))
        if resp.status_code in (401, 403):
            raise AuthRotted(f"HTTP {resp.status_code}")
        if resp.status_code == 404:
            raise QueryIdRotted("404 on TweetResultByRestId — qid rotated or tweet deleted")
        resp.raise_for_status()
        return parse_tweet_detail(resp.json(), tweet_id)


class FxLane:
    """Fallback lane D1-C: fxtwitter public API, no cookies, no auth."""

    def __init__(self):
        self.http = requests.Session()

    def fetch_tweet(self, tweet_id: str) -> dict:
        from .store import parse_fx_tweet
        r = self.http.get(f"https://api.fxtwitter.com/status/{tweet_id}", timeout=20)
        if r.status_code in (404, 400):
            raise Deleted(tweet_id)
        r.raise_for_status()
        return parse_fx_tweet(r.json(), tweet_id)
