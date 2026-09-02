"""Graph exporter — builds an Obsidian-style network graph from drained data.

Nodes: tweets, entities, topics, authors, link domains.
Edges: tweet->entity (extraction), tweet->topic, tweet->author, tweet->domain,
       entity<->entity co-occurrence, topic<->entity co-occurrence,
       author<->author shared-entity affinity, tweet->tweet quotes/retweets.

Outputs:
  obsidian vault: one markdown note per node with [[wikilinks]] as edges —
                  Obsidian's graph view renders the network natively.
  graph.json:     { nodes: [...], links: [...] } with weights — for
                  cytoscape.js / cosmograph / Gephi (via converter).
"""
from __future__ import annotations

import json
import re
import time
from collections import Counter, defaultdict
from pathlib import Path

SAFE = re.compile(r"[^\w\- ]")


def _slug(s: str) -> str:
    s = SAFE.sub("", (s or "").strip())[:80]
    return s or "unknown"


class GraphExporter:
    def __init__(self, store, out_dir: Path, min_cooccur: int = 2,
                 topic: str | None = None, limit: int = 0, min_mentions: int = 1):
        self.store = store
        self.out = Path(out_dir)
        self.min_cooccur = min_cooccur
        self.topic = topic
        self.limit = limit
        self.min_mentions = min_mentions

    # --- data gathering ----------------------------------------------------

    def _rows(self) -> list[dict]:
        q = ("SELECT tweet_id, author_handle, topic, entities_json, summary, reason, "
             "confidence, link_domain, quoted_id, is_retweet, original_id, text, "
             "created_iso, likes, retweets FROM tweets "
             "WHERE stage='done' AND topic IS NOT NULL")
        args: list = []
        if self.topic:
            q += " AND topic=?"
            args.append(self.topic)
        q += " ORDER BY created_iso"
        if self.limit:
            q += " LIMIT ?"
            args.append(self.limit)
        cur = self.store.db.execute(q, args)
        cur.row_factory = lambda c, r: dict(zip(
            [d[0] for d in c.description], r))
        out = []
        for d in cur.fetchall():
            try:
                d["entities"] = json.loads(d.pop("entities_json") or "[]")
            except ValueError:
                d["entities"] = []
            out.append(d)
        return out

    # --- graph construction --------------------------------------------------

    def build(self) -> dict:
        rows = self._rows()
        ent_count: Counter = Counter()
        ent_topics: dict[str, Counter] = defaultdict(Counter)
        ent_co: dict[str, Counter] = defaultdict(Counter)
        top_ents: dict[str, Counter] = defaultdict(Counter)
        auth_ents: dict[str, set] = defaultdict(set)
        tweet_ents: dict[str, list] = {}

        for r in rows:
            ents = r["entities"][:12]
            tweet_ents[r["tweet_id"]] = ents
            if r["topic"]:
                top_ents[r["topic"]][r["tweet_id"]] += 0  # ensure key
            for e in ents:
                ent_count[e] += 1
                if r["topic"]:
                    ent_topics[e][r["topic"]] += 1
                if r["author_handle"]:
                    auth_ents[r["author_handle"]].add(e)
                if r["topic"]:
                    top_ents[r["topic"]][e] += 1
            for i, a in enumerate(ents):
                for b in ents[i + 1:]:
                    ent_co[a][b] += 1
                    ent_co[b][a] += 1

        # author affinity via shared entities (jaccard-ish, capped)
        auth_aff: dict[str, Counter] = defaultdict(Counter)
        authors = [a for a in auth_ents if a]
        for i, a in enumerate(authors):
            for b in authors[i + 1:]:
                if not b:
                    continue
                shared = len(auth_ents[a] & auth_ents[b])
                if shared >= max(self.min_cooccur, 3):
                    auth_aff[a][b] = shared
                    auth_aff[b][a] = shared

        return {"rows": rows, "ent_count": ent_count, "ent_topics": ent_topics,
                "ent_co": ent_co, "top_ents": top_ents, "auth_ents": auth_ents,
                "auth_aff": auth_aff, "tweet_ents": tweet_ents}

    # --- obsidian vault ------------------------------------------------------

    def export_obsidian(self, g: dict) -> None:
        v = self.out
        for sub in ("entities", "topics", "authors", "tweets", "graphs"):
            (v / sub).mkdir(parents=True, exist_ok=True)
        rows, ent_count, ent_topics = (g["rows"], g["ent_count"], g["ent_topics"])
        ent_co, top_ents, auth_aff = (g["ent_co"], g["top_ents"], g["auth_aff"])

        tweets_by_ent: dict[str, list] = defaultdict(list)
        by_topic: dict[str, list] = defaultdict(list)
        by_author: dict[str, list] = defaultdict(list)
        for r in rows:
            by_topic[r["topic"]].append(r)
            if r["author_handle"]:
                by_author[r["author_handle"]].append(r)
            for e in r["entities"]:
                tweets_by_ent[e].append(r)

        # entity notes (suppress rare/singletons: extraction noise)
        for e, n in ent_count.most_common():
            if n < self.min_mentions:
                continue
            co = [(o, c) for o, c in ent_co[e].most_common(12) if c >= self.min_cooccur]
            tops = ent_topics[e].most_common(5)
            lines = [f"---", f"type: entity", f"mentions: {n}",
                     f"topics: {', '.join(t for t, _ in tops)}", f"---", "",
                     f"# {e}", "",
                     f"Mentioned in {n} posts across "
                     f"{len(tweets_by_ent[e])} notes.", ""]
            if tops:
                lines.append("Topics: " + ", ".join(
                    f"[[{t}]] ({c})" for t, c in tops))
            if co:
                lines.append("\n## Strongly connected")
                lines.extend(f"- [[{o}]] ({c}x co-occurrence)" for o, c in co)
            lines.append("\n## Recent mentions")
            for r in tweets_by_ent[e][:8]:
                lines.append(f"- [[{r['tweet_id']}]] "
                             f"{(r['text'] or '')[:90].replace(chr(10), ' ')}")
            (v / "entities" / f"{_slug(e)}.md").write_text("\n".join(lines))

        # topic notes
        for t, rs in by_topic.items():
            ents = top_ents[t].most_common(15)
            auths = Counter(r["author_handle"] for r in rs if r["author_handle"])
            lines = ["---", "type: topic", f"posts: {len(rs)}", "---", "",
                     f"# {t}", "",
                     f"{len(rs)} processed posts.",
                     f"Top authors: " + ", ".join(f"{a} ({c})" for a, c in auths.most_common(8)),
                     "", "## Core entities"]
            lines.extend(f"- [[{e}]] ({c})" for e, c in ents if c >= self.min_cooccur)
            lines.append("\n## Posts")
            for r in rs[:15]:
                lines.append(f"- [[{r['tweet_id']}]] "
                             f"{(r['summary'] or r['text'] or '')[:90]}")
            (v / "topics" / f"{_slug(t)}.md").write_text("\n".join(lines))

        # author notes
        for a, rs in by_author.items():
            aff = auth_aff[a].most_common(8)
            ents = Counter(e for r in rs for e in r["entities"]).most_common(12)
            lines = ["---", "type: author", f"posts: {len(rs)}", "---", "",
                     f"# @{a}", "",
                     f"Reposted {len(rs)} processed posts.",
                     f"Primary topics: " + ", ".join(
                         f"[[{t}]]" for t, _ in Counter(
                             r["topic"] for r in rs if r["topic"]).most_common(5)),
                     "", "## Entities they surface"]
            lines.extend(f"- [[{e}]] ({c})" for e, c in ents[:10])
            if aff:
                lines.append("\n## Kindred accounts (shared-entity affinity)")
                lines.extend(f"- [[{b}]] ({c} shared)" for b, c in aff)
            lines.append("\n## Posts")
            for r in rs[:10]:
                lines.append(f"- [[{r['tweet_id']}]] {(r['summary'] or '')[:80]}")
            (v / "authors" / f"{_slug(a)}.md").write_text("\n".join(lines))

        # tweet notes
        for r in rows:
            links = [f"[[{_slug(e)}|{e}]]" for e in r["entities"]]
            lines = ["---", "type: post", f"id: {r['tweet_id']}",
                     f"topic: {r['topic']}",
                     f"author: {r['author_handle'] or ''}",
                     f"date: {r['created_iso'] or ''}",
                     f"relevance: {r['reason']}/{r['confidence']}", "---", "",
                     (r["text"] or "").strip()[:600], "",
                     f"Topic: [[{_slug(r['topic'])}]] | "
                     f"Author: [[{_slug(r['author_handle'] or 'unknown')}]]",
                     f"Summary: {r['summary'] or ''}"]
            if r["quoted_id"]:
                lines.append(f"Quotes: [[{r['quoted_id']}]]")
            if links:
                lines.append("Entities: " + ", ".join(links))
            if r["link_domain"]:
                lines.append(f"Link domain: {r['link_domain']}")
            (v / "tweets" / f"{r['tweet_id']}.md").write_text("\n".join(lines))

        (v / "README.md").write_text(
            "# x-brain vault\n\nOpen this folder as an Obsidian vault. "
            "Graph view: entities/topics/authors/posts as nodes, wikilinks as edges.\n")

    # --- json graph ----------------------------------------------------------

    def export_json(self, g: dict) -> dict:
        rows = g["rows"]
        ent_count = g["ent_count"]
        nodes, links = [], []
        nid: dict[str, str] = {}

        def node(key: str, kind: str, label: str, **attr) -> str:
            if key in nid:
                return nid[key]
            i = f"{kind}:{len(nid)}"
            nid[key] = i
            nodes.append({"id": i, "kind": kind, "label": label, **attr})
            return i

        for r in rows:
            t = node(r["tweet_id"], "post", (r["summary"] or r["text"] or "")[:60],
                     date=r["created_iso"])
            if r["topic"]:
                links.append({"source": t, "target": node(
                    "topic:" + r["topic"], "topic", r["topic"]), "w": 1})
            if r["author_handle"]:
                links.append({"source": t, "target": node(
                    "author:" + r["author_handle"], "author", r["author_handle"]), "w": 1})
            if r["link_domain"]:
                links.append({"source": t, "target": node(
                    "domain:" + r["link_domain"], "domain", r["link_domain"]), "w": 1})
            for e in r["entities"]:
                if ent_count[e] < self.min_mentions:
                    continue
                links.append({"source": t, "target": node(
                    "ent:" + e, "entity", e), "w": 1})

        for e, co in g["ent_co"].items():
            ei = node("ent:" + e, "entity", e)
            for o, c in co.items():
                if c >= self.min_cooccur:
                    links.append({"source": ei, "target": node(
                        "ent:" + o, "entity", o), "w": c})

        for a, co in g["auth_aff"].items():
            ai = node("author:" + a, "author", a)
            for b, c in co.items():
                links.append({"source": ai, "target": node(
                    "author:" + b, "author", b), "w": c})

        doc = {"meta": {"generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                        "posts": len(rows), "nodes": len(nodes), "links": len(links),
                        "min_cooccur": self.min_cooccur},
               "nodes": nodes, "links": links}
        (self.out / "graph.json").parent.mkdir(parents=True, exist_ok=True)
        (self.out / "graph.json").write_text(json.dumps(doc, ensure_ascii=False))
        return doc

    # --- entry ----------------------------------------------------------------

    def run(self, fmt: str = "both") -> dict:
        g = self.build()
        stats = {"posts": len(g["rows"]),
                 "entities": len(g["ent_count"]),
                 "cooccur_edges": sum(1 for e, co in g["ent_co"].items()
                                      for o, c in co.items() if c >= self.min_cooccur) // 2,
                 "author_affinity": sum(len(co) for co in g["auth_aff"].values()) // 2}
        if fmt in ("obsidian", "both"):
            self.export_obsidian(g)
        if fmt in ("json", "both"):
            j = self.export_json(g)
            stats["json_nodes"] = j["meta"]["nodes"]
            stats["json_links"] = j["meta"]["links"]
        return stats
