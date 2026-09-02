"""LinkResolver — t.co expansion + page scraping + content extraction.

Every link a repost carries is followed: final URL, domain, title,
og:description, and extracted main text (capped). Special-cased:
  - x.com|twitter.com status links -> skipped (quoted tweets handled elsewhere)
  - YouTube -> oEmbed title/author
  - arxiv -> og:title/description (abstract)
Generic fallback: og: meta tags + <p> text via BeautifulSoup.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse

import requests

try:
    from bs4 import BeautifulSoup
except ImportError:
    BeautifulSoup = None

UA = ("Mozilla/5.0 (Windows NT 10.0;Win64;x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/142.0.0.0 Safari/537.36")
T_CO = re.compile(r"https://t\.co/\w+")
SKIP_DOMAINS = {"x.com", "twitter.com", "pbs.twimg.com", "pic.twitter.com"}
CONTENT_CAP = 2500


class LinkResolver:
    def __init__(self, timeout: int = 15, min_interval: float = 0.8):
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
        self.timeout = timeout
        self.min_interval = min_interval
        self._last = 0.0

    def _pace(self):
        import time
        since = time.time() - self._last
        if since < self.min_interval:
            time.sleep(self.min_interval - since)
        self._last = time.time()

    def resolve_tweet(self, text: str) -> dict | None:
        """Resolve the first non-X link in a tweet's text. Returns info dict or None."""
        urls = T_CO.findall(text or "")
        for u in urls:
            info = self.resolve(u)
            if info:
                return info
        return None

    def resolve(self, tco_url: str) -> dict | None:
        self._pace()
        try:
            r = self.http.get(tco_url, timeout=self.timeout, allow_redirects=True, stream=True)
            final = r.url
            domain = urlparse(final).netloc.lower().removeprefix("www.")
            if r.status_code >= 400:
                return {"link_url": tco_url, "link_domain": None, "link_title": None,
                        "link_desc": None, "link_content": None,
                        "link_error": f"dead link (HTTP {r.status_code})"}
            if domain == "t.co":
                # t.co serves a JS/meta redirect page (HTTP 200, no 30x)
                target = self._extract_redirect(r.text)
                if not target:
                    return {"link_url": tco_url, "link_domain": None, "link_title": None,
                            "link_desc": None, "link_content": None,
                            "link_error": "no redirect target on t.co page"}
                if self._in_network(target):
                    return None  # quoted tweets / media cover these
                return self._fetch_page(target)
            if self._in_network(final):
                return None
            r.close()
            return self._fetch_page(final)
        except requests.RequestException as e:
            return {"link_url": tco_url, "link_domain": None, "link_title": None,
                    "link_desc": None, "link_content": None, "link_error": str(e)[:120]}

    @staticmethod
    def _in_network(url: str) -> bool:
        d = urlparse(url).netloc.lower().removeprefix("www.")
        return any(d == x or d.endswith("." + x) for x in SKIP_DOMAINS) or d == "t.co"

    @staticmethod
    def _extract_redirect(html: str) -> str | None:
        m = (re.search(r'location\.replace\("([^"]+)"\)', html)
             or re.search(r'location\.href\s*=\s*"([^"]+)"', html)
             or re.search(r'url=([^">]+)', html))
        return m.group(1).replace("\\/", "/").strip() if m else None

    def _fetch_page(self, url: str) -> dict:
        r = self.http.get(url, timeout=self.timeout, allow_redirects=True, stream=True)
        final = r.url
        domain = urlparse(final).netloc.lower().removeprefix("www.")
        if r.status_code >= 400:
            return {"link_url": final, "link_domain": domain, "link_title": None,
                    "link_desc": None, "link_content": None,
                    "link_error": f"target dead (HTTP {r.status_code})"}
        if self._in_network(final):
            return None
        try:
            cl = int(r.headers.get("content-length") or 0)
            if cl > 1_500_000:
                return {"link_url": final, "link_domain": domain, "link_title": None,
                        "link_desc": None, "link_content": None, "link_error": "too large"}
            html = r.raw.read(1_500_000, decode_content=True).decode("utf-8", "replace") \
                if not cl else r.content.decode("utf-8", "replace")
        finally:
            r.close()
        return self._parse_html(final, domain, html)

    def _parse_html(self, url: str, domain: str, html: str) -> dict:
        out = {"link_url": url, "link_domain": domain, "link_title": None,
               "link_desc": None, "link_content": None, "link_error": None}
        if domain.endswith("youtube.com") or domain.endswith("youtu.be"):
            out.update(self._oembed(url))
            return out
        if not BeautifulSoup:
            m = re.search(r"<title[^>]*>(.*?)</title>", html, re.S | re.I)
            out["link_title"] = m.group(1).strip()[:300] if m else None
            return out
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript"]):
            tag.decompose()
        og = lambda p: (soup.find("meta", {"property": p}) or
                        soup.find("meta", {"name": p}) or {})
        out["link_title"] = ((og("og:title").get("content") or
                              (soup.title.string if soup.title else "") or "").strip()[:300] or None)
        out["link_desc"] = (og("og:description").get("content") or "").strip()[:600] or None
        paras = [p.get_text(" ", strip=True) for p in soup.find_all("p")]
        body = " ".join(p for p in paras if len(p) > 40)[:CONTENT_CAP]
        out["link_content"] = body or None
        return out

    def _oembed(self, url: str) -> dict:
        try:
            r = self.http.get(f"https://www.youtube.com/oembed?url={url}&format=json",
                              timeout=self.timeout)
            d = r.json()
            return {"link_title": d.get("title"), "link_desc":
                    f"YouTube video by {d.get('author_name')}", "link_content": None}
        except Exception:
            return {"link_title": None, "link_desc": None, "link_content": None}
