"""Keyword recall channel: search the whole library for papers related to a topic.

This is the channel that does *not* depend on the topic's papers being in the
corpus, so it works for every paper in the folder regardless of venue. The
library indexes title, abstract, authors and venue, so queries match against
full abstracts, not just titles.

The script deliberately over-recalls and makes no relevance judgement. It
returns abstracts; deciding what is actually relevant is the agent's job, in
conversation with the user.

Usage:
    python search.py --topic <dir> --keywords "citation recommendation,related work generation"
    python search.py --topic <dir> --keywords "..." --year-min 2022 --venue emnlp,acl
    python search.py --topic <dir> --keywords "..." --include-known
"""
import argparse

from common import Sidecar, add_topic_arg, print_json, run_remote

REMOTE_SEARCH = r'''
queries = PAYLOAD["queries"]
per_query = PAYLOAD["per_query"]
known_ids = set(PAYLOAD["known_ids"])
known_titles = set(PAYLOAD["known_titles"])

strategy = PAYLOAD["matching_strategy"]
hit_terms = {}
best_rank = {}
errors = []
for query in queries:
    try:
        hits = meili_search(query, limit=per_query, matching_strategy=strategy)
    except Exception as exc:
        errors.append({"query": query, "error": str(exc)})
        continue
    for position, hit in enumerate(hits):
        pid = hit.get("id")
        if not pid:
            continue
        hit_terms.setdefault(pid, set()).add(query)
        best_rank[pid] = min(best_rank.get(pid, 10 ** 6), position)

meta = hydrate(list(hit_terms))
candidates = []
for pid, terms in hit_terms.items():
    info = meta.get(pid)
    if not info:
        continue
    if pid in known_ids or norm(info.get("title")) in known_titles:
        if not PAYLOAD["include_known"]:
            continue
    candidates.append({
        "id": pid,
        "title": info.get("title"),
        "authors": info.get("authors") or [],
        "venue": info.get("venue"),
        "year": info.get("year"),
        "abstract": info.get("abstract") or "",
        "matched_queries": sorted(terms),
        "best_rank": best_rank[pid],
        "already_in_topic": pid in known_ids,
    })

emit({"candidates": candidates, "errors": errors})
'''


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_topic_arg(ap)
    ap.add_argument("--keywords", required=True,
                    help="comma-separated queries; each is searched separately "
                         "and results are merged")
    ap.add_argument("--limit", type=int, default=20, help="candidates to return")
    ap.add_argument("--per-query", type=int, default=30,
                    help="raw hits to pull per query before merging")
    ap.add_argument("--venue", default=None,
                    help="comma-separated venue filter, e.g. emnlp,acl,neurips")
    ap.add_argument("--year-min", type=int, default=None)
    ap.add_argument("--year-max", type=int, default=None)
    ap.add_argument("--abstract-chars", type=int, default=900,
                    help="truncate abstracts in the output (0 = full)")
    ap.add_argument("--include-known", action="store_true",
                    help="do not filter out papers already in the topic folder")
    ap.add_argument("--match-any", action="store_true",
                    help="loosen matching so a query's terms need not all appear; "
                         "much noisier, use only when a strict query returns nothing")
    args = ap.parse_args()

    queries = [q.strip() for q in args.keywords.split(",") if q.strip()]
    if not queries:
        raise SystemExit("--keywords produced no usable queries")

    sidecar = Sidecar(args.topic)
    response = run_remote(REMOTE_SEARCH, {
        "queries": queries,
        "per_query": args.per_query,
        "known_ids": sidecar.known_archer_ids(),
        "known_titles": sorted(sidecar.known_titles()),
        "include_known": args.include_known,
        "matching_strategy": "last" if args.match_any else "all",
    })

    venues = {v.strip().lower() for v in args.venue.split(",")} if args.venue else None
    candidates = []
    for cand in response["candidates"]:
        year = cand.get("year")
        if venues and (cand.get("venue") or "").lower() not in venues:
            continue
        if args.year_min and (year or 0) < args.year_min:
            continue
        if args.year_max and (year or 9999) > args.year_max:
            continue
        if args.abstract_chars and len(cand["abstract"]) > args.abstract_chars:
            cand["abstract"] = cand["abstract"][:args.abstract_chars].rstrip() + " ..."
        candidates.append(cand)

    # More query terms hitting the same paper is the strongest signal we have
    # without reading it. Within that, defer to the library's own relevance
    # ranking before falling back to recency.
    candidates.sort(key=lambda c: (-len(c["matched_queries"]), c["best_rank"],
                                   -(c.get("year") or 0)))

    print_json({
        "queries": queries,
        "returned": min(len(candidates), args.limit),
        "total_after_filters": len(candidates),
        "candidates": candidates[:args.limit],
        "errors": response.get("errors", []),
        "next": "read the abstracts, judge relevance, then: python fetch.py "
                "--topic <dir> --ids <id,id>",
    })


if __name__ == "__main__":
    main()
