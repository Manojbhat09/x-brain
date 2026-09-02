"""Wire protocol for x.com internal GraphQL — constants, TID generation, request building.

Sources: live XHR capture (2026-08-24) + protocol study of tamnd/x-cli (see
x-brain-harness-lld.md appendix 12).
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import secrets
import struct
import time
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

# --- constants (browser-faithful) -------------------------------------------

PUBLIC_WEB_BEARER = (
    "AAAAAAAAAAAAAAAAAAAAANRILgAAAAAAnNwIzUejRCOuH5E6I8xnZz4puTs%3D"
    "1Zv7ttfk8LF81IUq16cHjhLTvJu4FA33AGWWjCpTnA"
).replace("%3D", "=")

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36"
)

SEC_CH_UA = '"Chromium";v="142", "Not_A Brand";v="24", "Google Chrome";v="142"'

GRAPHQL_BASE = "https://x.com/i/api/graphql"

OP_REPOSTS = "UserRepostsTimeline"
QID_REPOSTS = "bV_DHAIvQ945LAA1-eIIow"  # captured live 2026-08-24; rotates on X deploys

OP_TWEET = "TweetResultByRestId"  # per-ID fetch (L2 enrichment)
QID_TWEET = "8CEYnZhCp0dx9DFyyEBlbQ"  # from x-cli table; overridable via config

FIELD_TOGGLES = {"withArticlePlainText": False}

FEATURES = {
    "rweb_video_screen_enabled": False,
    "rweb_cashtags_enabled": True,
    "profile_label_improvements_pcf_label_in_post_enabled": True,
    "responsive_web_profile_redirect_enabled": True,
    "rweb_tipjar_consumption_enabled": False,
    "verified_phone_label_enabled": True,
    "creator_subscriptions_tweet_preview_api_enabled": True,
    "responsive_web_graphql_timeline_navigation_enabled": True,
    "premium_content_api_read_enabled": False,
    "communities_web_enable_tweet_community_results_fetch": True,
    "c9s_tweet_anatomy_moderator_badge_enabled": True,
    "responsive_web_grok_analyze_button_fetch_trends_enabled": False,
    "responsive_web_grok_analyze_post_followups_enabled": True,
    "rweb_cashtags_composer_attachment_enabled": True,
    "responsive_web_jetfuel_frame": True,
    "responsive_web_grok_share_attachment_enabled": True,
    "responsive_web_grok_annotations_enabled": True,
    "articles_preview_enabled": True,
    "responsive_web_edit_tweet_api_enabled": True,
    "rweb_conversational_replies_downvote_enabled": False,
    "graphql_is_translatable_rweb_tweet_is_translatable_enabled": True,
    "view_counts_everywhere_api_enabled": True,
    "longform_notetweets_consumption_enabled": True,
    "responsive_web_twitter_article_tweet_consumption_enabled": True,
    "content_disclosure_indicator_enabled": True,
    "content_disclosure_ai_generated_indicator_enabled": True,
    "responsive_web_grok_show_grok_translated_post": True,
    "responsive_web_grok_analysis_button_from_backend": True,
    "post_ctas_fetch_enabled": False,
    "freedom_of_speech_not_reach_fetch_enabled": True,
    "standardized_nudges_misinfo": True,
    "tweet_with_visibility_results_prefer_gql_limited_actions_policy_enabled": True,
    "longform_notetweets_rich_text_read_enabled": True,
    "longform_notetweets_inline_media_enabled": False,
    "responsive_web_grok_image_annotation_enabled": True,
    "responsive_web_grok_imagine_annotation_enabled": True,
    "responsive_web_grok_community_note_auto_translation_is_enabled": True,
    "responsive_web_enhance_cards_enabled": False,
}

# --- x-client-transaction-id (port of x-cli tid.go) --------------------------

TID_KEYWORD = "obfiowerehiring"
TID_PAIRS_URL = (
    "https://raw.githubusercontent.com/fa0311/x-client-transaction-id-pair-dict/"
    "refs/heads/main/pair.json"
)
TID_EPOCH_OFFSET = 1682924400  # X's epoch: 2023-05-01T07:00:00Z
TID_TTL = 3600


def _load_tid_pairs(cache: Path, http: requests.Session) -> list[dict]:
    """Fetch (animationKey, verification) pairs; cache 1h; fall back to stale."""
    if cache.exists():
        rec = json.loads(cache.read_text())
        if time.time() - rec["fetched"] < TID_TTL and rec["pairs"]:
            return rec["pairs"]
    try:
        r = http.get(TID_PAIRS_URL, timeout=15)
        r.raise_for_status()
        pairs = r.json()
        if not isinstance(pairs, list) or not pairs:
            raise ValueError("empty pair dict")
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"fetched": time.time(), "pairs": pairs}))
        return pairs
    except Exception:
        if cache.exists():  # stale is better than none
            return json.loads(cache.read_text())["pairs"]
        return []


def generate_tid(method: str, path: str, pair: dict) -> str:
    """Compute x-client-transaction-id for '<METHOD> <path>' with one pair."""
    ver = pair["verification"]
    key = base64.b64decode(ver + "=" * (-len(ver) % 4))
    t = int(time.time()) - TID_EPOCH_OFFSET
    data = f"{method}!{path}!{t}{TID_KEYWORD}{pair['animationKey']}"
    digest = hashlib.sha256(data.encode()).digest()[:16]
    buf = key + struct.pack("<I", t) + digest + b"\x03"
    r = secrets.randbelow(256)
    out = bytes([r]) + bytes(b ^ r for b in buf)
    return base64.b64encode(out).decode().rstrip("=")


# --- request building --------------------------------------------------------


def build_variables(user_id: str, cursor: str | None, count: int = 20) -> dict:
    v: dict = {
        "userId": user_id,
        "count": count,
        "includePromotedContent": True,
        "withVoice": True,
    }
    if cursor:
        v["cursor"] = cursor
    return v


def build_url(user_id: str, cursor: str | None, count: int = 20) -> str:
    """POST <base>/<qid>/UserRepostsTimeline?variables=&features=&fieldToggles= (empty body)."""
    qs = (
        f"variables={quote(json.dumps(build_variables(user_id, cursor, count), separators=(',', ':')))}"
        f"&features={quote(json.dumps(FEATURES, separators=(',', ':')))}"
        f"&fieldToggles={quote(json.dumps(FIELD_TOGGLES, separators=(',', ':')))}"
    )
    return f"{GRAPHQL_BASE}/{QID_REPOSTS}/{OP_REPOSTS}?{qs}"


def build_headers(creds: dict, url: str, tid_pairs: list[dict]) -> dict:
    """Browser-faithful header set. TID hashes url PATH only (never the query)."""
    h = {
        "Authorization": f"Bearer {PUBLIC_WEB_BEARER}",
        "Cookie": f"auth_token={creds['auth_token']}; ct0={creds['ct0']}",
        "x-csrf-token": creds["ct0"],
        "x-twitter-auth-type": "OAuth2Session",
        "x-twitter-active-user": "yes",
        "x-twitter-client-language": "en",
        "User-Agent": BROWSER_UA,
        "sec-ch-ua": SEC_CH_UA,
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://x.com/",
        "Origin": "https://x.com",
        "priority": "u=1, i",
    }
    if tid_pairs:
        method = "POST"  # browser sends POST with empty body for this op
        tid = generate_tid(method, urlparse(url).path, random.choice(tid_pairs))
        if tid:
            h["x-client-transaction-id"] = tid
    return h


def build_tweet_url(tweet_id: str) -> str:
    """TweetResultByRestId — per-ID fetch (L2)."""
    v = quote(json.dumps({"tweetId": tweet_id,
                          "withCommunity": False,
                          "includePromotedContent": False,
                          "withVoice": False}, separators=(",", ":")))
    f = quote(json.dumps(FEATURES, separators=(",", ":")))
    return f"{GRAPHQL_BASE}/{QID_TWEET}/{OP_TWEET}?variables={v}&features={f}"
