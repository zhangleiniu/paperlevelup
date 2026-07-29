"""Keyword recall channel: search every configured source for related papers.

This is the channel that does *not* depend on the topic's papers being in any
one corpus, so it works regardless of venue. archer indexes title, abstract,
authors and venue; OpenAlex searches its full-text index across ~250M works
and reaches the venues archer does not carry (SIGIR/KDD/TKDE/ECIR/journals).

The script deliberately over-recalls and makes **no relevance judgement**. It
returns abstracts tagged with the source that found them; deciding what is
actually relevant is the agent's job, in conversation with the user.

On ranking across sources
-------------------------
Each backend ranks by its own scoring function, so `rank_in_source` is *not*
comparable between them and this script does not pretend otherwise -- it never
fuses the two orders into one score. Results are ordered by how many of your
queries hit the paper (source-independent) and then recency. A paper found by
more than one source is merged and marked, which is a real signal.

Usage:
    python search.py --topic <dir> --keywords "citation recommendation,related work generation"
    python search.py --topic <dir> --keywords "..." --year-min 2022 --venue emnlp,acl
    python search.py --topic <dir> --keywords "..." --sources openalex
    python search.py --topic <dir> --keywords "..." --include-known
"""
import argparse

from common import (Sidecar, add_topic_arg, load_config, norm_title,
                    print_json)
from sources import get_source


def dedup_key(cand: dict) -> str:
    """Deterministic identity for merging the same paper across sources.

    Normalised title first, DOI only as a fallback. DOI looks like the more
    precise key and is the wrong choice here: the same paper routinely carries
    several DOIs -- a publisher record, a Zenodo deposit, a preprint -- so
    keying on it shows the user three copies of one paper. Exact title
    collisions between genuinely different works are rarer than that.

    Still bookkeeping, not judgement: no fuzzy thresholds involved.
    """
    title = norm_title(cand.get("title") or "")
    if title:
        return "title:" + title
    doi = (cand.get("doi") or "").lower().strip()
    return ("doi:" + doi) if doi else "uid:" + cand.get("uid", "")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_topic_arg(ap)
    ap.add_argument("--keywords", required=True,
                    help="comma-separated queries; each is searched separately "
                         "and results are merged")
    ap.add_argument("--limit", type=int, default=20, help="candidates to return")
    ap.add_argument("--per-query", type=int, default=30,
                    help="raw hits to pull per query per source before merging")
    ap.add_argument("--sources", default=None,
                    help="comma-separated (default: config 'sources')")
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
    ap.add_argument("--fulltext", action="store_true",
                    help="OpenAlex only: search full text instead of title+abstract. "
                         "Far noisier -- a query is then matched by any single term "
                         "appearing anywhere. Use when the scoped query is too thin")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    queries = [q.strip() for q in args.keywords.split(",") if q.strip()]
    if not queries:
        raise SystemExit("--keywords produced no usable queries")

    sidecar = Sidecar(args.topic)
    config = load_config()
    if args.match_any:
        config = dict(config, matching_strategy="last")
    names = [s.strip() for s in (args.sources or "").split(",") if s.strip()]
    if not names:
        names = config.get("sources") or ["archer"]

    venues = [v.strip() for v in args.venue.split(",")] if args.venue else None
    cache_dir = None if args.no_cache else sidecar.cache_dir

    known_uids = set(sidecar.known_uids())
    known_titles = sidecar.known_titles()
    known_dois = sidecar.known_dois()

    merged: dict[str, dict] = {}
    errors: list[dict] = []
    per_source: dict[str, int] = {}
    stats = []

    for name in names:
        try:
            source = get_source(name, config=config, cache_dir=cache_dir)
        except Exception as exc:
            errors.append({"source": name, "error": str(exc)[:200]})
            continue
        if "search" not in source.capabilities:
            continue
        try:
            kwargs = {"per_query": args.per_query, "year_min": args.year_min,
                      "year_max": args.year_max, "venues": venues}
            if name == "openalex":
                kwargs["fulltext"] = args.fulltext
            found = source.search(queries, **kwargs)
        except Exception as exc:
            errors.append({"source": name, "error": str(exc)[:300]})
            continue
        errors.extend({"source": name, **e} for e in getattr(source, "last_errors", []))
        per_source[name] = len(found)
        stats.append(source.stats())

        for cand in found:
            slot = merged.get(dedup_key(cand))
            if slot is None:
                merged[dedup_key(cand)] = {
                    "uid": cand["uid"],
                    "ids": {cand["source"]: cand["id"]},
                    "found_by": [name],
                    "title": cand.get("title"),
                    "authors": cand.get("authors") or [],
                    "venue": cand.get("venue"),
                    "year": cand.get("year"),
                    "doi": cand.get("doi"),
                    "abstract": cand.get("abstract") or "",
                    "cited_by_count": cand.get("cited_by_count"),
                    "is_oa": cand.get("is_oa"),
                    "matched_queries": list(cand.get("matched_queries") or []),
                    "rank_in_source": {name: cand.get("best_rank", 0)},
                }
                continue
            # Same paper from a second source: union the evidence, keep the
            # richer abstract, and never average the two ranks.
            slot["found_by"].append(name)
            slot["ids"][cand["source"]] = cand["id"]
            slot["rank_in_source"][name] = cand.get("best_rank", 0)
            slot["doi"] = slot.get("doi") or cand.get("doi")
            slot["cited_by_count"] = (slot.get("cited_by_count")
                                      or cand.get("cited_by_count"))
            if len(cand.get("abstract") or "") > len(slot.get("abstract") or ""):
                slot["abstract"] = cand["abstract"]
            for q in cand.get("matched_queries") or []:
                if q not in slot["matched_queries"]:
                    slot["matched_queries"].append(q)

    candidates = []
    for cand in merged.values():
        in_topic = (any("%s:%s" % (s, i) in known_uids for s, i in cand["ids"].items())
                    or norm_title(cand.get("title")) in known_titles
                    or (cand.get("doi") or "").lower() in known_dois)
        cand["already_in_topic"] = in_topic
        if in_topic and not args.include_known:
            continue
        if args.abstract_chars and len(cand["abstract"]) > args.abstract_chars:
            cand["abstract"] = cand["abstract"][:args.abstract_chars].rstrip() + " ..."
        cand["matched_queries"].sort()
        candidates.append(cand)

    # Source-independent ordering only: how many of your queries hit it, then
    # whether more than one backend surfaced it, then recency.
    candidates.sort(key=lambda c: (-len(c["matched_queries"]), -len(c["found_by"]),
                                   -(c.get("year") or 0)))

    print_json({
        "queries": queries,
        "sources_used": names,
        "hits_per_source": per_source,
        "returned": min(len(candidates), args.limit),
        "total_after_filters": len(candidates),
        "candidates": candidates[:args.limit],
        "errors": errors,
        "source_stats": stats,
        "note": "rank_in_source is each backend's own ranking and is not "
                "comparable across sources; ordering here uses query overlap only",
        "next": "read the abstracts, judge relevance, then: python fetch.py "
                "--topic <dir> --ids <uid,uid>",
    })


if __name__ == "__main__":
    main()
