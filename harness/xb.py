#!/usr/bin/env python3
"""xb — x-brain harness CLI (v1: auth, enum, stats, doctor)."""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from xbrain import protocol
from xbrain import session as sess
from xbrain.enum import Enumerator
from xbrain.enrich import Enricher
from xbrain.lane import FxLane, GraphQLLane
from xbrain.llm import LlmWorker, STUDY_PROMPT, STUDY_SCHEMA
from xbrain.store import Store

# Brain dir resolves per-invocation so --brain-dir / $XBRAIN_DIR / .env win.
# Do not read it at import time — session.get_brain_dir() handles .env.
def _brain_dir(args) -> Path:
    return sess.get_brain_dir(getattr(args, "brain_dir", None))


def cmd_auth(args) -> int:
    bd = _brain_dir(args)
    # --user-id optional but recommended; saves to config.json
    if getattr(args, "user_id", None):
        sess.save_user_id(args.user_id, bd)
        print(f"user_id saved: {args.user_id} -> {sess.config_path(bd)}")
    p = sess.save_creds(args.auth_token, args.ct0, bd)
    print(f"creds saved (0600): {p}")
    if not getattr(args, "user_id", None) and not sess.load_user_id(bd):
        print("tip: add --user-id <numeric id> or set X_USER_ID in .env (see .env.example)")
    return 0


def _make_lane(args) -> GraphQLLane:
    bd = _brain_dir(args)
    creds = sess.load_creds(bd)
    if not creds:
        sys.exit("no creds. get auth_token + ct0 from DevTools → Application → Cookies → x.com, then:\n"
                 "  python3 xb.py auth --auth-token <tok> --ct0 <ct0>  (add --user-id <id> or set X_USER_ID in .env)")
    return GraphQLLane(creds, bd / "limits.json", bd / "cache")


def cmd_doctor(args) -> int:
    bd = _brain_dir(args)
    lane = _make_lane(args)
    store = Store(bd)
    uid = sess.require_user_id(bd, getattr(args, "user_id", None))
    print(f"creds: ok | user_id: {uid} | brain: {bd}")
    print(lane.status())
    try:
        items, cursor = lane.fetch_page(uid, None, 1)
        print(f"probe page: {len(items)} item(s), cursor={'yes' if cursor else 'no'}")
        if items:
            it = items[0]
            print(f"  sample: @{it['author_handle']}: {it['text'][:60]!r}")
        print("doctor: PASS")
        return 0
    except Exception as e:
        print(f"doctor: FAIL — {e}")
        return 1


def cmd_enum(args) -> int:
    bd = _brain_dir(args)
    lane = _make_lane(args)
    store = Store(bd)
    uid = sess.require_user_id(bd, getattr(args, "user_id", None))
    en = Enumerator(lane, store, uid, count=args.count)
    result = en.run(resume=args.resume, max_pages=args.max_pages)
    print(result)
    return 0


def cmd_enrich(args) -> int:
    bd = _brain_dir(args)
    lane = _make_lane(args)
    fx = FxLane() if not args.no_fx else None
    store = Store(bd)
    en = Enricher(lane, fx, store, batch=args.batch)
    total = {"done": 0, "errors": 0}
    rounds = 0
    while True:
        r = en.run(max_items=args.max if args.max else args.batch)
        total["done"] += r["done"]; total["errors"] += r["errors"]
        rounds += 1
        if r["done"] == 0 or (args.max and total["done"] >= args.max):
            print(f"enrich loop end after {rounds} rounds: {total}")
            break
    print(store.stats())
    return 0


def _resolve_key(env_names: list[str], filenames: list[str]) -> Path | None:
    import os as _os
    for k in env_names:
        v = _os.environ.get(k)
        if v and v.strip():
            # materialise as temp file in brain dir for backends that expect a file
            bd = sess.get_brain_dir()
            bd.mkdir(parents=True, exist_ok=True)
            p = bd / f".{k.lower()}.key"
            if not p.exists():
                p.write_text(v.strip()); _os.chmod(p, 0o600)
            else:
                # refresh if changed
                if p.read_text().strip() != v.strip():
                    p.write_text(v.strip())
            return p
    for fn in filenames:
        for base in (Path.home(), Path.cwd(), Path(__file__).resolve().parents[1]):
            p = base / fn
            if p.exists():
                return p
    return None

def cmd_llm(args) -> int:
    bd = _brain_dir(args)
    store = Store(bd)
    from xbrain.backends import InferXBackend, OllamaBackend, OpenRouterBackend, LiteLLMBackend
    from xbrain.router import ModelRouter
    orkey = _resolve_key(["OPENROUTER_API_KEY","OPENROUTER_KEY","OR_KEY"], ["openrouterkey"])
    ixkey = _resolve_key(["INFERX_API_KEY","INFERX_KEY","IX_KEY"], ["inferxkey","inferx2key","inferx3key","inferx4key"])
    backends = []
    router = None
    if args.route:
        ext_chain = []
        # InferX chain: env or first existing inferx* key file
        ix_src = _resolve_key(["INFERX_API_KEY","INFERX_KEY"], ["inferx4key","inferx3key","inferx2key","inferxkey"])
        if ix_src:
            ext_chain.append(InferXBackend(ix_src, model="Qwen3-Coder-Next-FP8", min_interval=0.8))
        if orkey:
            ext_chain.append(OpenRouterBackend("z-ai/glm-5.2:free", orkey, min_interval=1.5))
            ext_chain.append(OpenRouterBackend("nvidia/nemotron-3-ultra-550b-a55b:free", orkey, min_interval=1.5))
        judge = None
        if args.mode == "int-ext-int":
            if args.litellm:
                judge = [LiteLLMBackend(base=args.litellm_base, model=args.judge_model,
                                        api_key=args.litellm_key),
                         LiteLLMBackend(base=args.litellm_base,
                                        model=args.judge_backup, api_key=args.litellm_key)]
            else:
                judge = [OllamaBackend(model=args.judge_model),
                         OllamaBackend(model=args.judge_backup)]
        router = ModelRouter(catalog=Path(args.catalog) if args.catalog else None,
                             external=ext_chain, mode=args.mode,
                             judge=judge, judge_model=args.judge_model)
        # model drop-in check: every tier model must exist in the local runtime;
        # missing ones fall back to the standard tier at routing time.
        try:
            import requests as _rq
            tags = {m["name"] for m in _rq.get(
                "http://localhost:11434/api/tags", timeout=5).json().get("models", [])}
            for tier in ("fast", "standard", "deep", "uncensored"):
                models = {m for c, m in (router.tiers.get(tier) or {}).items()}
                for m in sorted(models):
                    print(f"route: tier {tier:11s} model {m:32s} "
                          f"{'AVAILABLE' if m in tags or ':' in m and m.split(':')[0] in {t.split(':')[0] for t in tags} else 'MISSING (falls back to standard)'}")
        except Exception:
            pass
        print(f"route: mode={router.mode} | external = "
              f"{[be.name() for be in ext_chain] or 'NONE'}"
              f"{' | judges = ' + args.judge_model + ' -> ' + args.judge_backup if judge else ''}")
    if args.litellm:
        backends.append(LiteLLMBackend(base=args.litellm_base, model=args.model,
                                       rel_model=args.rel_model, api_key=args.litellm_key))
    for name in (args.backends or "inferx,ollama").split(","):
        name = name.strip().lower()
        if name == "inferx":
            ix_src = _resolve_key(["INFERX_API_KEY","INFERX_KEY"], ["inferx4key","inferx3key","inferx2key","inferxkey"])
            if ix_src:
                backends.append(InferXBackend(ix_src, model="Qwen3-Coder-Next-FP8", min_interval=args.pace))
        elif name == "or-glm" and orkey:
            backends.append(OpenRouterBackend("z-ai/glm-5.2:free", orkey))
        elif name == "or-nemotron" and orkey:
            backends.append(OpenRouterBackend("nvidia/nemotron-3-ultra-550b-a55b:free", orkey))
        elif name == "ollama":
            backends.append(OllamaBackend(model=args.model))
    if not backends:
        sys.exit("no usable backends")
    # --model single-model focus: overrides backends chain (keep providers DISTINCT)
    if args.model and "/" in args.model:
        if args.model.startswith("opencode/") or args.model.startswith("zen/") or args.model.startswith("x-preview"):
            from xbrain.backends import OpencodeZenBackend as _ZB, InferXBackend as _IX
            zen_model = args.model.split("/")[-1]
            try:
                zen = _ZB(model=zen_model)
                ix_src = _resolve_key(["INFERX_API_KEY","INFERX_KEY"], ["inferxkey","inferx2key","inferx3key","inferx4key"])
                if not ix_src:
                    raise RuntimeError("no InferX key (set INFERX_API_KEY in .env)")
                inferx = _IX(ix_src)
                backends = []
                for _ in range(5):
                    backends.extend([zen, inferx])
                print(f"single-model mode (ZEN<->InferX alternating): {args.model} -> {zen_model} + {inferx.model} x5 rounds")
            except Exception as e:
                print(f"zen init failed ({e}), falling back to inferx only")
                ix_fallback = _resolve_key(["INFERX_API_KEY","INFERX_KEY"], ["inferxkey"])
                if ix_fallback:
                    backends = [_IX(ix_fallback)]
        else:
            from xbrain.backends import OpenRouterBackend as _OR
            if not orkey:
                sys.exit("OpenRouter key required for --model <provider/model> (set OPENROUTER_API_KEY in .env or ~/openrouterkey)")
            single = _OR(args.model, orkey)
            backends = [single]
            print(f"single-model mode (OpenRouter): {args.model}")
    worker = LlmWorker(store, bd / "quarantine", backends=backends,
                       card_models={"self_relevance": args.rel_model},
                       router=router)
    cards = args.cards.split(",") if args.cards else ["tag_topic", "extract_entities", "self_relevance", "summarize"]
    if args.workers > 1:
        store.expire_llm_leases()
        from xbrain.llm import run_parallel
        print(run_parallel(store, backends, cards, n_workers=args.workers,
                           quarantine_dir=bd / "quarantine",
                           card_models={"self_relevance": args.rel_model},
                           router=router))
    else:
        print(worker.run(cards=cards, limit=args.limit))
        print("backend usage:", worker.usage)
    if router:
        print("router usage:", router.usage)
    print(store.stats())
    return 0


def cmd_links(args) -> int:
    bd = _brain_dir(args)
    store = Store(bd)
    from xbrain.links import LinkResolver
    import json as _json
    lr = LinkResolver()
    rows = store.pending_links(limit=args.limit or 100000)
    done = errs = 0
    for tid, text in rows:
        info = lr.resolve_tweet(text)
        if info is None:
            info = {"link_url": "in-network", "link_domain": None, "link_title": None,
                    "link_desc": None, "link_content": None, "link_error": None}
        store.save_link_info(tid, info)
        if info.get("link_error"):
            errs += 1
        else:
            done += 1
        if (done + errs) % 50 == 0:
            print(f"links: {done+errs} processed, {errs} errors")
    print(f"links done: {done}, errors: {errs}")
    return 0


def cmd_deep(args) -> int:
    bd = _brain_dir(args)
    store = Store(bd)
    from xbrain.backends import OpenRouterBackend, default_backends
    backends = default_backends(None)
    orkey = _resolve_key(["OPENROUTER_API_KEY","OPENROUTER_KEY"], ["openrouterkey"])
    if orkey:
        backends.insert(1, OpenRouterBackend("z-ai/glm-5.2:free", orkey))
        backends.append(OpenRouterBackend("nvidia/nemotron-3-ultra-550b-a55b:free", orkey))
    worker = LlmWorker(store, bd / "quarantine", backends=backends)
    rows = store.pending_deep(limit=args.limit or 100000)
    done = quarantined = 0
    for tid, text, ltitle, ldesc, lcontent in rows:
        prompt = STUDY_PROMPT.format(text=(text or "")[:900],
                                     title=ltitle or "(no link)",
                                     desc=ldesc or "—",
                                     content=(lcontent or "—")[:1500])
        row, errs = None, ["no backend answered"]
        for attempt in range(3):
            for be in worker.backends:
                try:
                    cand = be.chat(prompt, STUDY_SCHEMA)
                except Exception:
                    continue
                if not isinstance(cand, dict):
                    continue
                if (cand.get("deep_reason") and cand.get("reference_value") in
                        ("essential", "high", "medium", "low")
                        and isinstance(cand.get("study_topics"), list)):
                    store.save_deep(tid, cand)
                    worker.usage[be.name()] = worker.usage.get(be.name(), 0) + 1
                    row = cand
                    break
            if row:
                break
            time.sleep(3)
        if row:
            done += 1
        else:
            worker._quarantine("study_value", tid, (text or "")[:200], row or {}, errs)
            quarantined += 1
        if (done + quarantined) % 25 == 0:
            print(f"deep: {done} done, {quarantined} quarantined | {worker.usage}")
    print(f"deep curated: {done}, quarantined: {quarantined}")
    print("usage:", worker.usage)
    return 0


def cmd_fuse(args) -> int:
    bd = _brain_dir(args)
    store = Store(bd)
    from xbrain.backends import default_backends
    from xbrain.llm import fuse_row
    backends = default_backends(None)
    orkey = _resolve_key(["OPENROUTER_API_KEY","OPENROUTER_KEY"], ["openrouterkey"])
    if orkey:
        from xbrain.backends import OpenRouterBackend
        backends.insert(1, OpenRouterBackend("z-ai/glm-5.2:free", orkey))
    done = fail = 0
    while True:
        rows = store.pending_fuse(limit=100)
        if not rows:
            break
        for row in rows:
            d = fuse_row(row, backends)
            if d:
                store.save_fusion(row["tweet_id"], d)
                done += 1
            else:
                fail += 1
        print(f"fused: {done}, failed: {fail}")
        if fail > len(rows) * 0.9:  # sustained outage
            print("backends saturated — stopping (resumable)")
            break
    print(f"fuse complete: {done} fused, {fail} failed")
    return 0


def cmd_vision(args) -> int:
    bd = _brain_dir(args)
    store = Store(bd)
    from xbrain.vision import VisionWorker
    ixkey = _resolve_key(["INFERX_API_KEY","INFERX_KEY"], ["inferx3key","inferx2key","inferxkey","inferx4key","inferx5key"])
    orkey = _resolve_key(["OPENROUTER_API_KEY","OPENROUTER_KEY"], ["openrouterkey"])
    if not orkey:
        sys.exit("OpenRouter key required for vision (set OPENROUTER_API_KEY in .env)")
    w = VisionWorker(orkey, store, bd / "quarantine", inferx_key_file=ixkey)
    print(w.run(limit=args.limit))
    print("usage:", w.usage)
    return 0


def cmd_graph(args) -> int:
    bd = _brain_dir(args)
    store = Store(bd)
    from xbrain.graph import GraphExporter
    out = Path(args.out) if args.out else bd / "vault"
    ge = GraphExporter(store, out, min_cooccur=args.min_cooccur,
                       topic=args.topic, limit=args.limit,
                       min_mentions=args.min_mentions)
    print(ge.run(fmt=args.format))
    print(f"vault written to {out} — open it in Obsidian for the network graph")
    return 0


def cmd_stats(args) -> int:
    bd = _brain_dir(args)
    store = Store(bd)
    print(store.stats())
    cur = store.kv_get("enum_cursor")
    print(f"cursor checkpoint: {'set' if cur else 'none'} | done: {store.kv_get('enum_done')}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(prog="xb")
    ap.add_argument("--brain-dir", default=None, help="brain directory (default $XBRAIN_DIR or ~/.xbrain)")
    ap.add_argument("--user-id", default=None, help="target X user id (numeric, overrides env/config)")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("auth", help="store credentials")
    p.add_argument("--auth-token", required=True)
    p.add_argument("--ct0", required=True)
    p.add_argument("--user-id", required=False, default=None, help="target X user id (numeric)")
    p.set_defaults(fn=cmd_auth)

    p = sub.add_parser("doctor", help="verify creds + one live page")
    p.set_defaults(fn=cmd_doctor)

    p = sub.add_parser("enum", help="enumerate reposts (L1)")
    p.add_argument("--resume", action="store_true", help="continue from checkpoint")
    p.add_argument("--max-pages", type=int, default=0, help="0 = until end")
    p.add_argument("--count", type=int, default=20, help="items per page (try 50)")
    p.set_defaults(fn=cmd_enum)

    p = sub.add_parser("enrich", help="L2 thread enrichment for discovered tweets")
    p.add_argument("--batch", type=int, default=50)
    p.add_argument("--max", type=int, default=0, help="0 = until queue empty")
    p.add_argument("--no-fx", action="store_true", help="disable FxTwitter fallback")
    p.set_defaults(fn=cmd_enrich)

    p = sub.add_parser("llm-run", help="run task cards on llm_queued tweets")
    p.add_argument("--cards", default="", help="comma list: tag_topic,extract_entities,self_relevance,summarize")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--model", default="nemotron3-nano", help="ollama model tag")
    p.add_argument("--rel-model", default="granite-4.2-3b-q8", help="ollama model for self_relevance card")
    p.add_argument("--litellm", action="store_true", help="route local models through the LiteLLM proxy (litellm_drain.yaml)")
    p.add_argument("--litellm-base", default="http://127.0.0.1:4000", help="LiteLLM proxy base URL")
    p.add_argument("--litellm-key", default="sk-local-xbrain", help="LiteLLM proxy master key")
    p.add_argument("--backends", default="ollama,inferx,or-nemotron", help="ordered: inferx,or-nemotron,ollama")
    p.add_argument("--route", action="store_true",
                   help="per-row dynamic model routing (hybrid heuristic + inferx judgment)")
    p.add_argument("--mode", default="hybrid",
                   choices=["hybrid", "ext-int-int", "int-ext-int"],
                   help="routing strategy: hybrid=heuristic fast-path+ext on ambiguous; "
                        "ext-int-int=external judges EVERY row (1 ext req/row); "
                        "int-ext-int=local judge every row, ext arbitration only on "
                        "low-confidence/nsfw/refusal-risk/uncensored rows")
    p.add_argument("--judge-model", default="nemotron3-nano",
                   help="local ollama model used as router judge in int-ext-int mode")
    p.add_argument("--judge-backup", default="granite-4.2-3b-q8",
                   help="backup local judge if the primary judge fails")
    p.add_argument("--catalog", default="",
                   help="path to a models JSON catalog (default ~/models_recommendation.json); "
                        "edit routing_tiers there to drop in different local models — "
                        "picked up on next start, no code changes")
    p.add_argument("--pace", type=float, default=1.0, help="min seconds between inferx calls")
    p.add_argument("--workers", type=int, default=1, help="parallel workers (>1 = parallel mode)")
    p.set_defaults(fn=cmd_llm)

    p = sub.add_parser("links-run", help="resolve + scrape t.co links")
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(fn=cmd_links)

    p = sub.add_parser("deep-run", help="curate why-reposted/study-value per tweet")
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(fn=cmd_deep)

    p = sub.add_parser("fuse-run", help="synthesis: unify text+vision+links per post")
    p.set_defaults(fn=cmd_fuse)

    p = sub.add_parser("vision-run", help="understand media (OCR/describe) via free VL models")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--workers", type=int, default=3)
    p.set_defaults(fn=cmd_vision)

    p = sub.add_parser("stats", help="dataset stats")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("graph-export", help="export mind-graph (Obsidian vault + graph.json)")
    p.add_argument("--out", default=None, help="output vault directory (default <brain-dir>/vault)")
    p.add_argument("--min-cooccur", type=int, default=2,
                   help="minimum entity co-occurrence count for an edge")
    p.add_argument("--topic", default="", help="restrict to one topic enum")
    p.add_argument("--limit", type=int, default=0, help="0 = all processed rows")
    p.add_argument("--format", default="both", choices=["obsidian", "json", "both"])
    p.add_argument("--min-mentions", type=int, default=2,
                   help="suppress entities mentioned fewer than N times (noise filter)")
    p.set_defaults(fn=cmd_graph)

    args = ap.parse_args()
    return args.fn(args)


if __name__ == "__main__":
    raise SystemExit(main())
