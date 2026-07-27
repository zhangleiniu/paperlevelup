"""Citation recall channel: use the library's resolved citation graph.

Only papers that resolved against the library take part -- the graph stores
edges between corpus papers, so a paper from an uncovered venue can neither
cite nor be cited inside it. Expect this channel to cover a subset of the
folder; the keyword channel in search.py covers the rest.

Three views:

  gaps      papers cited by several of your papers but missing from the folder.
            Ranked by lift, not raw count: raw count surfaces field
            infrastructure (BERT, Attention Is All You Need) that you already
            know. Lift divides by overall popularity, which surfaces papers that
            are specific to this topic -- exactly the ones easy to miss.
  forward   newer papers that cite several of yours; how the topic moved on.
  internal  in-degree inside your own folder; which of your papers the others
            build on.

Usage:
    python graph.py --topic <dir>
    python graph.py --topic <dir> --mode all --min-seeds 2 --limit 30
    python graph.py --topic <dir> --mode gaps --rank count
"""
import argparse

from common import Sidecar, add_topic_arg, print_json, run_remote

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
        item["citations_in_corpus"] = total
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


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_topic_arg(ap)
    ap.add_argument("--mode", choices=("gaps", "forward", "internal", "all"),
                    default="gaps")
    ap.add_argument("--min-seeds", type=int, default=2,
                    help="how many of your papers must share the edge")
    ap.add_argument("--limit", type=int, default=25)
    ap.add_argument("--rank", choices=("lift", "count"), default="lift",
                    help="lift favours topic-specific papers; count favours "
                         "field-wide classics you probably already know")
    ap.add_argument("--abstract-chars", type=int, default=500)
    args = ap.parse_args()

    sidecar = Sidecar(args.topic)
    seeds = sidecar.known_archer_ids()
    if not seeds:
        raise SystemExit(
            "no papers in this topic resolved against the library, so the "
            "citation channel has nothing to work with. Run collect.py first, "
            "and use search.py for keyword-based discovery.")

    result = run_remote(REMOTE_GRAPH, {
        "seeds": seeds, "mode": args.mode, "min_seeds": args.min_seeds,
        "limit": args.limit, "rank": args.rank,
        "abstract_chars": args.abstract_chars,
    })
    result["papers_in_topic"] = len(sidecar.entries)
    result["coverage_note"] = (
        "%d of %d papers in this folder are in the corpus; the rest are "
        "invisible to the citation graph." % (len(seeds), len(sidecar.entries)))
    result["next"] = ("read the abstracts, judge relevance, then: "
                      "python fetch.py --topic <dir> --ids <id,id>")
    print_json(result)


if __name__ == "__main__":
    main()
