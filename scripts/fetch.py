"""Pull a discovered paper's PDF down from the library into the topic folder.

This closes the discovery loop: search.py and graph.py return library ids, and
this copies the matching PDFs in, named to the `[year VENUE]title.pdf`
convention and registered in the sidecar so the next collect.py run sees them
as already resolved.

Usage:
    python fetch.py --topic <dir> --ids "id1,id2"
    python fetch.py --topic <dir> --ids "id1" --category Benchmark
    python fetch.py --topic <dir> --ids "id1,id2" --dry-run
"""
import argparse

from common import (Sidecar, add_topic_arg, build_filename, load_config,
                    print_json, run_remote, sanitize_dirname, scp_from_remote)

REMOTE_LOOKUP = r'''
meta = hydrate(PAYLOAD["ids"])
out = []
for pid in PAYLOAD["ids"]:
    info = meta.get(pid)
    if not info:
        out.append({"id": pid, "error": "no such paper in the library"})
        continue
    pdf_path = info.get("pdf_path")
    full = os.path.join(CONFIG["pdf_root"], pdf_path) if pdf_path else None
    out.append({
        "id": pid, "title": info.get("title"), "authors": info.get("authors") or [],
        "venue": info.get("venue"), "year": info.get("year"),
        "abstract": info.get("abstract"), "url": info.get("url"),
        "pdf_path": pdf_path, "remote_path": full,
        "exists": bool(full and os.path.exists(full)),
    })
emit({"papers": out})
'''


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_topic_arg(ap)
    ap.add_argument("--ids", required=True, help="comma-separated library paper ids")
    ap.add_argument("--category", default=None,
                    help="drop the PDFs straight into this category subfolder")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    if not ids:
        raise SystemExit("--ids produced no usable ids")

    config = load_config()
    sidecar = Sidecar(args.topic)
    response = run_remote(REMOTE_LOOKUP, {"ids": ids}, config=config)

    subdir = sanitize_dirname(args.category) if args.category else None
    target_dir = sidecar.topic / subdir if subdir else sidecar.topic
    results = []

    for paper in response["papers"]:
        if paper.get("error"):
            results.append({"id": paper["id"], "status": "error",
                            "detail": paper["error"]})
            continue
        if not paper["exists"]:
            results.append({"id": paper["id"], "status": "error", "title": paper["title"],
                            "detail": "PDF not on the library host: %s"
                                      % (paper.get("remote_path") or "no path recorded")})
            continue

        name = build_filename(paper["title"], paper["venue"], paper["year"])
        if name in sidecar.entries:
            results.append({"id": paper["id"], "status": "already-present",
                            "file": name})
            continue

        rel = ("%s/%s" % (subdir, name)) if subdir else name
        results.append({"id": paper["id"], "status": "would-fetch" if args.dry_run
                        else "fetched", "file": rel, "title": paper["title"]})
        if args.dry_run:
            continue

        target_dir.mkdir(parents=True, exist_ok=True)
        scp_from_remote(paper["remote_path"], target_dir / name, config=config)
        sidecar.upsert(
            name, path=rel, source="library", archer_id=paper["id"],
            match_score=1.0, title=paper["title"], authors=paper["authors"],
            venue=paper["venue"], year=paper["year"], abstract=paper["abstract"],
            library_pdf_path=paper["pdf_path"], url=paper["url"],
            category=args.category, excerpt=None, collect_error=None)

    if not args.dry_run:
        sidecar.save()

    print_json({
        "dry_run": args.dry_run,
        "requested": len(ids),
        "fetched": sum(1 for r in results if r["status"] == "fetched"),
        "results": results,
        "next": "python organize.py --topic <dir> --assign '[...]'   # classify the new papers",
    })


if __name__ == "__main__":
    main()
