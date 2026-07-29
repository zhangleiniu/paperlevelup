"""Citation recall channel.

Each source contributes the edges it actually has, and they are **not** the
same shape:

  archer    3.8M edges strictly *between corpus papers*. Dense and clean inside
            the 15 venues, blind outside them.
  openalex  global edges, but reference lists come from Crossref/publisher
            deposits and arXiv does not deposit them. Measured on this folder:
            only about half the archer-misses have any outgoing references, and
            the gap is systematic (preprints), not random.

Three views:

  gaps      papers cited by several of yours but missing from the folder.
            Ranked by lift, not raw count: raw count surfaces field
            infrastructure (BERT, Attention Is All You Need) you already know.
            Lift divides by overall popularity, surfacing papers specific to
            this topic -- exactly the ones easy to miss.
  forward   newer papers citing several of yours; how the topic moved on.
  internal  in-degree inside your own folder.

Why results are reported per source
-----------------------------------
Lift needs a popularity denominator, and the two sources do not share one.
archer can only count citations *within its corpus*; OpenAlex knows the global
count, which is what lift actually wants. Mixing archer's edges with archer's
denominator and OpenAlex's edges with OpenAlex's denominator into a single
ranked list would produce numbers that look comparable and are not. So each
source reports its own block, with the denominator it used stated.

Usage:
    python graph.py --topic <dir>
    python graph.py --topic <dir> --mode all --min-seeds 2 --limit 30
    python graph.py --topic <dir> --sources openalex --mode gaps
    python graph.py --topic <dir> --mode gaps --rank count
"""
import argparse
from collections import Counter

from common import Sidecar, add_topic_arg, load_config, print_json, run_remote
from sources import get_source

REMOTE_GRAPH = r'''
seeds = PAYLOAD["seeds"]
mode = PAYLOAD["mode"]
min_seeds = PAYLOAD["min_seeds"]
limit = PAYLOAD["limit"]
placeholders = ",".join("?" * len(seeds))
out = {"seed_count": len(seeds)}

row = db().execute(
    "select count(*), count(distinct source_paper_id) from paper_references "
    "where source_paper_id in (%s)" % placeholders, seeds).fetchone()
out["edges_from_seeds"] = {"edges": row[0], "seeds_with_references": row[1]}

def rows_to_papers(rows, count_field):
    ids = [r[0] for r in rows]
    meta = hydrate(ids)
    items = []
    for pid, count in rows:
        info = meta.get(pid)
        if not info:
            continue
        items.append({
            "id": pid, count_field: count,
            "title": info.get("title"), "venue": info.get("venue"),
            "year": info.get("year"), "authors": (info.get("authors") or [])[:6],
            "abstract": (info.get("abstract") or "")[:PAYLOAD["abstract_chars"]],
        })
    return items

if mode in ("gaps", "all"):
    rows = db().execute(
        "select r.target_paper_id, count(distinct r.source_paper_id) c "
        "from paper_references r "
        "where r.source_paper_id in (%s) and r.target_paper_id not in (%s) "
        "group by r.target_paper_id having c >= ? "
        "order by c desc limit 400" % (placeholders, placeholders),
        seeds + seeds + [min_seeds]).fetchall()
    items = rows_to_papers([(r[0], r[1]) for r in rows], "cited_by_n_seeds")
    for item in items:
        total = db().execute(
            "select count(distinct source_paper_id) from paper_references "
            "where target_paper_id = ?", (item["id"],)).fetchone()[0]
        item["popularity"] = total
        # Popularity is heavy-tailed, so damp it with a square root rather than
        # dividing outright, which would over-reward one-off references.
        item["lift"] = round(item["cited_by_n_seeds"] / (total ** 0.5 + 1), 4)
    key = "lift" if PAYLOAD["rank"] == "lift" else "cited_by_n_seeds"
    items.sort(key=lambda x: (-x[key], -(x.get("year") or 0)))
    out["gaps"] = items[:limit]

if mode in ("forward", "all"):
    rows = db().execute(
        "select r.source_paper_id, count(distinct r.target_paper_id) c "
        "from paper_references r "
        "where r.target_paper_id in (%s) and r.source_paper_id not in (%s) "
        "group by r.source_paper_id having c >= ? "
        "order by c desc limit ?" % (placeholders, placeholders),
        seeds + seeds + [min_seeds, limit]).fetchall()
    items = rows_to_papers([(r[0], r[1]) for r in rows], "cites_n_seeds")
    items.sort(key=lambda x: (-x["cites_n_seeds"], -(x.get("year") or 0)))
    out["forward"] = items

if mode in ("internal", "all"):
    rows = db().execute(
        "select r.target_paper_id, count(distinct r.source_paper_id) c "
        "from paper_references r "
        "where r.source_paper_id in (%s) and r.target_paper_id in (%s) "
        "group by r.target_paper_id order by c desc" % (placeholders, placeholders),
        seeds + seeds).fetchall()
    meta = hydrate([r[0] for r in rows])
    out["internal"] = [{
        "id": pid, "cited_by_n_seeds": count,
        "title": (meta.get(pid) or {}).get("title"),
        "year": (meta.get(pid) or {}).get("year"),
    } for pid, count in rows]

emit(out)
'''


def archer_block(sidecar, args):
    seeds = sidecar.known_archer_ids()
    if not seeds:
        return {"seed_count": 0,
                "note": "no paper in this folder resolved against the library"}
    block = run_remote(REMOTE_GRAPH, {
        "seeds": seeds, "mode": args.mode, "min_seeds": args.min_seeds,
        "limit": args.limit, "rank": args.rank,
        "abstract_chars": args.abstract_chars,
    })
    block["lift_denominator"] = "citations within archer's corpus"
    return block


def openalex_block(sidecar, args, config, cache_dir):
    """Same three views over OpenAlex edges.

    Lift here divides by the *global* citation count, which is the quantity
    lift was always reaching for -- archer can only approximate it with counts
    inside its own 15 venues.
    """
    source = get_source("openalex", config=config, cache_dir=cache_dir)
    seed_uids = ["openalex:%s" % i for i in
                 (r.get("ids", {}).get("openalex") for r in sidecar.entries.values())
                 if i]
    block = {"seed_count": len(seed_uids),
             "lift_denominator": "global citation count (cited_by_count)"}
    if not seed_uids:
        block["note"] = "no paper in this folder has an OpenAlex id yet"
        return block

    seed_set = set(seed_uids)
    known_titles = sidecar.known_titles()

    def hydrate_and_pack(counter, count_field, exclude_seeds=True):
        ranked = [(uid, n) for uid, n in counter.most_common()
                  if n >= args.min_seeds and (not exclude_seeds or uid not in seed_set)]
        # Popularity is part of the lift formula, so every candidate meeting
        # min_seeds must be hydrated before ranking. Pre-truncating by raw
        # seed count would silently turn this into "lift among popular items".
        meta = source.hydrate([uid for uid, _ in ranked])
        items = []
        # OpenAlex carries duplicate work records for the same paper (two
        # RefSeer entries differing only in capitalisation, for instance).
        # Collapse them on normalised title, keeping the higher seed count.
        seen_titles: dict[str, dict] = {}
        for uid, n in ranked:
            info = meta.get(uid)
            if not info:
                continue
            from common import norm_title
            title_key = norm_title(info.get("title"))
            # OpenAlex carries stub records with no usable title (withdrawn
            # entries, deposit errors). They are unjudgeable, so drop them.
            if not title_key or title_key in known_titles:
                continue
            if title_key in seen_titles:
                twin = seen_titles[title_key]
                twin[count_field] = max(twin[count_field], n)
                twin["duplicate_records"] = twin.get("duplicate_records", 1) + 1
                continue
            item = {
                "id": info["id"], "uid": uid, count_field: n,
                "title": info.get("title"), "venue": info.get("venue"),
                "year": info.get("year"), "authors": (info.get("authors") or [])[:6],
                "doi": info.get("doi"),
                "popularity": info.get("cited_by_count") or 0,
                "abstract": (info.get("abstract") or "")[:args.abstract_chars],
            }
            item["lift"] = round(n / ((item["popularity"]) ** 0.5 + 1), 4)
            seen_titles[title_key] = item
            items.append(item)
        for item in items:
            item["lift"] = round(item[count_field] / ((item["popularity"]) ** 0.5 + 1), 4)
        key = "lift" if args.rank == "lift" else count_field
        items.sort(key=lambda x: (-x[key], -(x.get("year") or 0)))
        return items[:args.limit]

    if args.mode in ("gaps", "internal", "all"):
        refs = source.references(seed_uids)
        with_refs = sum(1 for v in refs.values() if v)
        block["edges_from_seeds"] = {
            "edges": sum(len(v) for v in refs.values()),
            "seeds_with_references": with_refs,
        }
        block["reference_coverage_note"] = (
            "%d of %d seeds have a reference list in OpenAlex; the missing ones "
            "often include preprints whose references were not deposited by a "
            "publisher or Crossref" % (with_refs, len(seed_uids)))
        outgoing = Counter()
        internal = Counter()
        for targets in refs.values():
            for target in set(targets):
                if target in seed_set:
                    internal[target] += 1
                else:
                    outgoing[target] += 1
        if args.mode in ("gaps", "all"):
            block["gaps"] = hydrate_and_pack(outgoing, "cited_by_n_seeds")
        if args.mode in ("internal", "all"):
            meta = source.hydrate(list(internal))
            block["internal"] = sorted(
                ({"uid": uid, "cited_by_n_seeds": n,
                  "title": (meta.get(uid) or {}).get("title"),
                  "year": (meta.get(uid) or {}).get("year")}
                 for uid, n in internal.items()),
                key=lambda x: -x["cited_by_n_seeds"])

    if args.mode in ("forward", "all"):
        cites = source.cited_by(seed_uids, limit_per_paper=args.cites_cap)
        incoming = Counter()
        for citers in cites.values():
            for citer in set(citers):
                incoming[citer] += 1
        block["forward"] = hydrate_and_pack(incoming, "cites_n_seeds")

    block["source_stats"] = source.stats()
    return block


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_topic_arg(ap)
    ap.add_argument("--mode", choices=("gaps", "forward", "internal", "all"),
                    default="gaps")
    ap.add_argument("--sources", default=None,
                    help="comma-separated (default: config 'sources')")
    ap.add_argument("--min-seeds", type=int, default=2,
                    help="how many of your papers must share the edge")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--rank", choices=("lift", "count"), default="lift",
                    help="lift favours topic-specific papers; count favours "
                         "field-wide classics you probably already know")
    ap.add_argument("--abstract-chars", type=int, default=500)
    ap.add_argument("--cites-cap", type=int, default=200,
                    help="max forward citations to pull per seed (OpenAlex)")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    sidecar = Sidecar(args.topic)
    config = load_config()
    names = [s.strip() for s in (args.sources or "").split(",") if s.strip()]
    if not names:
        names = config.get("sources") or ["archer"]
    cache_dir = None if args.no_cache else sidecar.cache_dir

    blocks, errors = {}, []
    for name in names:
        try:
            if name == "archer":
                blocks["archer"] = archer_block(sidecar, args)
            elif name == "openalex":
                blocks["openalex"] = openalex_block(sidecar, args, config, cache_dir)
            else:
                errors.append({"source": name, "error": "no graph support"})
        except Exception as exc:
            errors.append({"source": name, "error": str(exc)[:300]})

    total = len(sidecar.entries)
    identified = {
        name: sum(1 for r in sidecar.entries.values()
                  if (r.get("ids") or {}).get(name))
        for name in ("archer", "openalex")
    }
    print_json({
        "mode": args.mode,
        "papers_in_topic": total,
        "identified_per_source": identified,
        "by_source": blocks,
        "errors": errors,
        "note": "blocks are reported separately because lift uses a different "
                "popularity denominator per source and the numbers are not "
                "comparable across them",
        "next": "read the abstracts, judge relevance, then: "
                "python fetch.py --topic <dir> --ids <uid,uid>",
    })


if __name__ == "__main__":
    main()
