"""State: SQLite FSM per tweet + append-only ids.jsonl. Single writer."""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS tweets(
  tweet_id      TEXT PRIMARY KEY,
  stage         TEXT NOT NULL DEFAULT 'discovered',
  attempts      INTEGER NOT NULL DEFAULT 0,
  first_seen    TEXT,
  author_handle TEXT,
  author_name   TEXT,
  created_at    TEXT,
  repost_index  INTEGER,
  is_quote      INTEGER,
  quoted_id     TEXT,
  media_json    TEXT,
  text_len      INTEGER,
  text          TEXT,
  flags         TEXT
);
CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT);
"""


class Store:
    def __init__(self, data_dir: Path):
        self.dir = data_dir
        self.dir.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._lock = threading.Lock()
        self._main_db = sqlite3.connect(self.dir / "state.sqlite", timeout=30, check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=15000")
        self.db.executescript(SCHEMA)
        self._migrate()
        self.jsonl = (self.dir / "raw" / "ids.jsonl")
        self.jsonl.parent.mkdir(parents=True, exist_ok=True)

    def _migrate(self):
        """Add columns introduced after v1 without breaking existing DBs."""
        have = {r[1] for r in self.db.execute("PRAGMA table_info(tweets)")}
        for col, typ in [
            ("text", "TEXT"),
            ("original_id", "TEXT"), ("is_retweet", "INTEGER"),
            ("views", "INTEGER"),
            ("lease_until", "INTEGER"),
            ("root_id", "TEXT"), ("in_reply_to", "TEXT"),
            ("conversation_id", "TEXT"), ("ancestors_json", "TEXT"),
            ("likes", "INTEGER"), ("retweets", "INTEGER"),
            ("topic", "TEXT"), ("entities_json", "TEXT"),
            ("link_url", "TEXT"), ("link_domain", "TEXT"), ("link_title", "TEXT"),
            ("link_desc", "TEXT"), ("link_content", "TEXT"), ("link_error", "TEXT"),
            ("deep_reason", "TEXT"), ("reference_value", "TEXT"),
            ("study_topics_json", "TEXT"), ("go_deeper", "TEXT"),
            ("summary", "TEXT"), ("reason", "TEXT"), ("confidence", "TEXT"),
            ("route_tier", "TEXT"), ("route_json", "TEXT"),
        ]:
            if col not in have:
                self.db.execute(f"ALTER TABLE tweets ADD COLUMN {col} {typ}")
        self.db.commit()

    # --- kv (cursor checkpoints) ---
    def kv_get(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return row[0] if row else None

    def kv_set(self, key: str, value: str) -> None:
        self.db.execute(
            "INSERT INTO kv(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))
        self.db.commit()

    # --- tweets ---
    def known_ids(self) -> set[str]:
        return {r[0] for r in self.db.execute("SELECT tweet_id FROM tweets")}

    def insert_discovered(self, items: list[dict]) -> int:
        """items: L1 dicts from the lane. Returns count of NEW rows."""
        new = 0
        base_idx = self.db.execute("SELECT COALESCE(MAX(repost_index),-1)+1 FROM tweets").fetchone()[0]
        fmt = "%a %b %d %H:%M:%S %z %Y"
        for i, it in enumerate(items):
            if self.db.execute("SELECT 1 FROM tweets WHERE tweet_id=?", (it["tweet_id"],)).fetchone():
                continue
            try:
                iso = datetime.strptime(it.get("created_at") or "", fmt)\
                      .astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            except Exception:
                iso = None
            if self.db.execute("SELECT 1 FROM tweets WHERE tweet_id=?", (it["tweet_id"],)).fetchone():
                continue
            self.db.execute(
                "INSERT INTO tweets(tweet_id, stage, first_seen, author_handle, author_name,"
                " created_at, created_iso, repost_index, is_quote, quoted_id, media_json, text_len, text, flags,"
                " original_id, is_retweet)"
                " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (it["tweet_id"], "discovered", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 it.get("author_handle"), it.get("author_name"), it.get("created_at"), iso,
                 base_idx + i, int(bool(it.get("is_quote"))), it.get("quoted_id"),
                 json.dumps(it.get("media", [])), it.get("text_len", 0),
                 it.get("text", ""), json.dumps(it.get("flags", {})),
                 it.get("original_id"), int(bool(it.get("is_retweet")))))
            with self.jsonl.open("a") as f:
                f.write(json.dumps(it, ensure_ascii=False) + "\n")
                f.flush()
                import os
                os.fsync(f.fileno())
            new += 1
        self.db.commit()
        return new

    def stats(self) -> dict:
        out = {}
        for stage, n in self.db.execute("SELECT stage, COUNT(*) FROM tweets GROUP BY stage"):
            out[stage] = n
        out["total"] = sum(out.values())
        return out

    def pending_llm(self, limit: int = 100) -> list[dict]:
        return [{"tweet_id": r[0], "text": r[1] or "",
                 "link_title": r[2], "link_desc": r[3], "link_content": r[4],
                 "link_error": r[5], "quoted_id": r[6], "link_domain": r[7],
                 "is_quote": r[8], "media_json": r[9]}
                for r in self.db.execute(
            "SELECT tweet_id, text, link_title, link_desc, link_content, link_error, "
            "quoted_id, link_domain, is_quote, media_json "
            "FROM tweets WHERE stage='llm_queued' LIMIT ?", (limit,))]

    def save_route(self, tid: str, profile: dict) -> None:
        self.db.execute("UPDATE tweets SET route_tier=?, route_json=? WHERE tweet_id=?",
                        (profile.get("tier"),
                         json.dumps(profile, ensure_ascii=False), tid))
        self.db.commit()

    def claim_llm(self, tid: str, lease_s: int = 600) -> bool:
        now = int(time.time())
        cur = self._get_db().execute(
            "SELECT stage, lease_until FROM tweets WHERE tweet_id=?", (tid,)).fetchone()
        if not cur or cur[0] != "llm_queued":
            return False
        if cur[1] and cur[1] > now:
            return False
        self._get_db().execute("UPDATE tweets SET stage='llm_running', lease_until=? WHERE tweet_id=?",
                        (now + lease_s, tid))
        self._locked_commit()
        return True

    def release_llm(self, tid: str, ok: bool):
        stage = "done" if ok else "llm_queued"
        self._get_db().execute("UPDATE tweets SET stage=?, lease_until=NULL WHERE tweet_id=?",
                        (stage, tid))
        self._locked_commit()

    def expire_llm_leases(self):
        now = int(time.time())
        self._get_db().execute("UPDATE tweets SET stage='llm_queued', lease_until=NULL "
                        "WHERE stage='llm_running' AND lease_until < ?", (now,))
        self.db.commit()

    def mark_llm_done(self, tid: str) -> None:
        self.db.execute("UPDATE tweets SET stage='done' WHERE tweet_id=?", (tid,))
        self.db.commit()

    # --- link resolution + deep curation ----------------------------------------
    def pending_links(self, limit: int = 500) -> list[tuple]:
        return self.db.execute(
            "SELECT tweet_id, text FROM tweets "
            "WHERE text LIKE '%https://t.co/%' AND link_url IS NULL "
            "AND stage != 'tombstone' LIMIT ?", (limit,)).fetchall()

    def save_link_info(self, tid: str, info: dict) -> None:
        self.db.execute(
            "UPDATE tweets SET link_url=?, link_domain=?, link_title=?, "
            "link_desc=?, link_content=?, link_error=? WHERE tweet_id=?",
            (info.get("link_url"), info.get("link_domain"), info.get("link_title"),
             info.get("link_desc"), info.get("link_content"), info.get("link_error"), tid))
        self.db.commit()

    def pending_deep(self, limit: int = 500) -> list[tuple]:
        return self.db.execute(
            "SELECT tweet_id, text, link_title, link_desc, link_content FROM tweets "
            "WHERE stage IN ('done','llm_queued') AND deep_reason IS NULL LIMIT ?",
            (limit,)).fetchall()

    def save_deep(self, tid: str, d: dict) -> None:
        self.db.execute(
            "UPDATE tweets SET deep_reason=?, reference_value=?, "
            "study_topics_json=?, go_deeper=? WHERE tweet_id=?",
            (d.get("deep_reason"), d.get("reference_value"),
             json.dumps(d.get("study_topics", []), ensure_ascii=False),
             d.get("go_deeper"), tid))
        self.db.commit()

    def pending_fuse(self, limit: int = 500) -> list[dict]:
        rows = self.db.execute(
            "SELECT tweet_id, text, media_understanding, link_title, link_desc, link_content "
            "FROM tweets WHERE fused IS NULL AND stage != 'tombstone' AND "
            "(media_understanding IS NOT NULL OR "
            " (link_content IS NOT NULL AND length(link_content) > 50)) LIMIT ?",
            (limit,)).fetchall()
        return [{"tweet_id": r[0], "text": r[1],
                 "media_understanding": json.loads(r[2]) if r[2] else None,
                 "link_title": r[3], "link_desc": r[4], "link_content": r[5]}
                for r in rows]

    def save_fusion(self, tid: str, d: dict) -> None:
        self.db.execute(
            "UPDATE tweets SET unified_summary=?, fused_topics_json=?, "
            "key_entities_json=?, content_type=?, fused=1 WHERE tweet_id=?",
            (d.get("unified_summary"),
             json.dumps(d.get("fused_topics", []), ensure_ascii=False),
             json.dumps(d.get("key_entities", []), ensure_ascii=False),
             d.get("content_type"), tid))
        self.db.commit()

    def save_thinking(self, tid: str, thinking: str) -> None:
        self.db.execute("UPDATE tweets SET thinking=? WHERE tweet_id=?",
                        (thinking[:8000], tid))
        self.db.commit()

    # --- vision (media understanding) ------------------------------------------
    def pending_vision(self, limit: int = 100000) -> list[tuple]:
        return self.db.execute(
            "SELECT tweet_id, media_json, "
            "CASE WHEN media_json LIKE '%video_thumb%' OR media_json LIKE '%ext_tw_video%' "
            "OR media_json LIKE '%amplify_video%' THEN 'video' ELSE 'image' END, text "
            "FROM tweets WHERE media_json IS NOT NULL AND media_json != '[]' "
            "AND media_understanding IS NULL AND stage != 'tombstone' LIMIT ?",
            (limit,)).fetchall()

    @property
    def db(self):
        return self._get_db()

    def _get_db(self):
        if hasattr(self._local, "db") and self._local.db:
            return self._local.db
        # main thread uses self.db, workers get their own
        import threading as _t
        if _t.current_thread() is _t.main_thread():
            return self._main_db
        self._local.db = __import__("sqlite3").connect(self.dir / "state.sqlite", timeout=30, check_same_thread=False)
        self._local.db.execute("PRAGMA journal_mode=WAL")
        self._local.db.execute("PRAGMA busy_timeout=15000")
        return self._local.db
    def _locked_execute(self, sql, params=()):
        with self._lock:
            return self._get_db().execute(sql, params)
    def _locked_commit(self):
        with self._lock:
            self._get_db().commit()

    def save_media_understanding(self, tid: str, result: dict) -> None:
        self._locked_execute("UPDATE tweets SET media_understanding=? WHERE tweet_id=?",
                        (json.dumps(result, ensure_ascii=False), tid))
        self._locked_commit()



    # --- L2/L5 upserts + FSM queries ------------------------------------------
    def pending_discovered(self, limit: int = 50) -> list[str]:
        now = int(time.time())
        return [r[0] for r in self.db.execute(
            "SELECT tweet_id FROM tweets WHERE stage='discovered' AND "
            "(lease_until IS NULL OR lease_until < ?) ORDER BY repost_index LIMIT ?",
            (now, limit))]

    def bump_attempts(self, tid: str) -> int:
        self.db.execute("UPDATE tweets SET attempts=attempts+1 WHERE tweet_id=?", (tid,))
        self.db.commit()
        return self.db.execute("SELECT attempts FROM tweets WHERE tweet_id=?", (tid,)).fetchone()[0]

    def upsert_l2(self, tid: str, d: dict) -> None:
        self.db.execute(
            "UPDATE tweets SET root_id=?, in_reply_to=?, conversation_id=?, "
            "ancestors_json=?, likes=COALESCE(?, likes), retweets=COALESCE(?, retweets), "
            "author_handle=COALESCE(?, author_handle) WHERE tweet_id=?",
            (d.get("root_id"), d.get("in_reply_to"), d.get("conversation_id"),
             json.dumps(d.get("ancestors", [])), d.get("likes"), d.get("retweets"),
             d.get("author_handle"), tid))
        self.db.commit()

    def upsert_l5(self, tid: str, fields: dict) -> None:
        cols, vals = [], []
        for k in ("topic", "summary", "reason", "confidence"):
            if k in fields:
                cols.append(f"{k}=?")
                vals.append(fields[k])
        if "entities" in fields:
            cols.append("entities_json=?")
            vals.append(json.dumps(fields["entities"], ensure_ascii=False))
        if not cols:
            return
        vals.append(tid)
        self.db.execute(f"UPDATE tweets SET {', '.join(cols)} WHERE tweet_id=?", vals)
        self.db.commit()


# --- response parsing (UserRepostsTimeline) ----------------------------------

def parse_timeline(payload: dict) -> tuple[list[dict], str | None]:
    """Returns (items, bottom_cursor). Tolerant to shape drift."""
    items: list[dict] = []
    cursor: str | None = None
    try:
        instructions = (payload["data"]["user"]["result"]["timeline"]["timeline"]["instructions"])
    except (KeyError, TypeError):
        return items, cursor

    for ins in instructions:
        if ins.get("type") not in ("TimelineAddEntries", "TimelineAddToModule"):
            continue
        for entry in ins.get("entries", []):
            eid = entry.get("entryId", "")
            content = entry.get("content", {})
            ctype = content.get("entryType") or content.get("type") or ""
            if ctype in ("TimelineTimelineItem", "timeline-tweet") or eid.startswith("tweet-"):
                item = content.get("itemContent") or {}
                tweet = _extract_tweet(item)
                if tweet:
                    items.append(tweet)
            elif ctype in ("TimelineTimelineModule", "timeline-module") or eid.startswith(("profile-conversation", "trend")):
                for sub in content.get("items", []) or []:
                    tweet = _extract_tweet((sub.get("item") or {}).get("itemContent") or {})
                    if tweet:
                        tweet["flags"]["in_module"] = True
                        items.append(tweet)
            elif "cursor-bottom" in eid or "CursorBottom" in ctype:
                cursor = content.get("value") or (content.get("content") or {}).get("value")
    return items, cursor


def _descend_rt(result: dict, legacy: dict) -> tuple[dict, dict, str | None]:
    """For RT wrappers (full_text 'RT @...', own counts zero), return the
    ORIGINAL tweet's result+legacy and its id. Else pass through."""
    rt = (legacy.get("retweeted_status_result") or {}).get("result") or {}
    if rt.get("__typename") == "TweetWithVisibilityResults":
        rt = rt.get("tweet") or {}
    if rt.get("__typename") == "Tweet" and rt.get("rest_id"):
        return rt, (rt.get("legacy") or {}), rt.get("rest_id")
    return result, legacy, None


def _extract_tweet(item: dict) -> dict | None:
    if not item or item.get("itemType") == "TimelinePromotedTweet" or item.get("promotedMetadata"):
        return None  # ads are not user reposts
    tr = item.get("tweet_results") or {}
    result = tr.get("result") or {}
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet") or {}
    if result.get("__typename") not in ("Tweet",) or not result:
        return None
    legacy = result.get("legacy") or {}
    tid = result.get("rest_id") or legacy.get("id_str")
    if not tid:
        return None
    core = (result.get("core") or {}).get("user_results", {}).get("result", {})
    if core.get("__typename") == "UserResultsByRestId":
        core = core.get("user") or {}
    ulegacy = core.get("legacy") or {}
    ucore = core.get("core") or {}  # new-style user shape (2026): core.screen_name
    screen = ulegacy.get("screen_name") or ucore.get("screen_name")
    uname = ulegacy.get("name") or ucore.get("name")
    media = [
        m.get("media_url_https")
        for m in (legacy.get("entities", {}).get("media") or [])
        if m.get("media_url_https")
    ]
    quoted = result.get("quoted_status_result", {}).get("result", {})
    quoted_id = quoted.get("rest_id") or (quoted.get("legacy") or {}).get("id_str")
    # RT wrapper: attribute content to the ORIGINAL author, keep wrapper id
    orig_result, orig_legacy, orig_id = _descend_rt(result, legacy)
    is_rt = orig_id is not None
    if is_rt:
        ocore = (orig_result.get("core") or {}).get("user_results", {}).get("result", {})
        if ocore.get("__typename") == "UserResultsByRestId":
            ocore = ocore.get("user") or {}
        olegacy = ocore.get("legacy") or {}
        ocore2 = ocore.get("core") or {}
        screen = olegacy.get("screen_name") or ocore2.get("screen_name") or screen
        uname = olegacy.get("name") or ocore2.get("name") or uname
        legacy = orig_legacy
        omedia = [
            m.get("media_url_https")
            for m in (legacy.get("entities", {}).get("media") or [])
            if m.get("media_url_https")
        ]
        if omedia:
            media = omedia
    return {
        "tweet_id": tid,
        "original_id": orig_id,
        "is_retweet": is_rt,
        "author_handle": screen,
        "author_name": uname,
        "created_at": legacy.get("created_at"),
        "text": legacy.get("full_text", ""),
        "text_len": len(legacy.get("full_text", "")),
        "is_quote": bool(quoted_id),
        "quoted_id": quoted_id,
        "media": media,
        "flags": {},
    }


# --- L2 detail parsers --------------------------------------------------------

def parse_tweet_detail(payload: dict, want_id: str) -> dict:
    """TweetResultByRestId → L2 fields: root, in_reply_to, ancestors, replies.
    NOTE: response nests under data.tweetResult (verified live 2026-08-24)."""
    out = {"root_id": want_id, "in_reply_to": None, "ancestors": [],
           "replies_seen": 0, "conversation_id": None, "deleted": False}
    tr = payload.get("tweetResult") or (payload.get("data") or {}).get("tweetResult")
    if not tr:
        out["deleted"] = True
        return out
    result = tr.get("result") or {}
    if result.get("__typename") == "TweetWithVisibilityResults":
        result = result.get("tweet") or {}
    if result.get("__typename") != "Tweet":
        out["deleted"] = True
        return out
    legacy = result.get("legacy") or {}
    # RT wrapper: engagement belongs to the ORIGINAL tweet
    orig_result, legacy, _orig_id = _descend_rt(result, legacy)
    out["conversation_id"] = legacy.get("conversation_id_str")
    out["in_reply_to"] = legacy.get("in_reply_to_status_id_str")
    out["likes"] = legacy.get("favorite_count")
    out["retweets"] = legacy.get("retweet_count")
    out["replies_seen"] = legacy.get("reply_count") or 0
    out["views"] = ((orig_result.get("views") or {}).get("count"))
    return out


def parse_fx_tweet(payload: dict, want_id: str) -> dict:
    """fxtwitter /status/:id → L2 fields."""
    out = {"root_id": want_id, "in_reply_to": None, "ancestors": [],
           "replies_seen": 0, "conversation_id": None, "deleted": False}
    t = (payload.get("tweet") or {})
    if not t:
        out["deleted"] = True
        return out
    out["in_reply_to"] = t.get("replying_to_status") or None
    out["author_handle"] = (t.get("author") or {}).get("screen_name")
    out["text"] = t.get("text", "")
    out["created_at"] = t.get("created_at")
    out["likes"] = (t.get("likes") or {}).get("count")
    out["retweets"] = (t.get("retweets") or {}).get("count")
    out["replies_seen"] = (t.get("replies") or {}).get("count") or 0
    return out
