"""Pull a discovered paper's PDF into the topic folder.

This closes the discovery loop: search.py and graph.py return ids, and this
copies the matching PDFs in, named to the `[year VENUE]title.pdf` convention
and registered in the sidecar so the next collect.py run sees them as already
resolved.

Ids are namespaced -- `archer:<uuid>`, `openalex:W123` -- and a bare id is read
as archer's, so invocations written before sources existed keep working.

What will and will not be downloaded
------------------------------------
archer serves the PDF for anything in its corpus. OpenAlex only yields a file
when the work is open access *and* carries a licence in the permitted set; a
paywalled paper comes back as a pointer with its DOI so you can go through your
own institutional access instead. We do not follow landing pages, retry past a
paywall, or guess at unlabelled licences. Measured on this folder, about 43% of
OpenAlex matches produce a file -- the rest are meant to be a reference, not a
download, and are reported as `no-pdf` rather than silently dropped.

Usage:
    python fetch.py --topic <dir> --ids "openalex:W123,archer:<uuid>"
    python fetch.py --topic <dir> --ids "id1" --category "1 Retrieval/1.4 Doc Rep"
    python fetch.py --topic <dir> --ids "id1,id2" --dry-run
"""
import argparse

from common import (Sidecar, add_topic_arg, build_filename, load_config,
                    print_json, sanitize_dirname)
from sources import get_source, split_uid


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_topic_arg(ap)
    ap.add_argument("--ids", required=True,
                    help="comma-separated ids, optionally namespaced "
                         "(openalex:W123); a bare id means archer")
    ap.add_argument("--category", default=None,
                    help="drop the PDFs straight into this category subfolder")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-cache", action="store_true")
    args = ap.parse_args()

    uids = [i.strip() for i in args.ids.split(",") if i.strip()]
    if not uids:
        raise SystemExit("--ids produced nothing usable")

    sidecar = Sidecar(args.topic)
    config = load_config()
    cache_dir = None if args.no_cache else sidecar.cache_dir

    subdir = sanitize_dirname(args.category) if args.category else None
    target_dir = sidecar.topic / subdir if subdir else sidecar.topic

    by_source: dict[str, list[str]] = {}
    for uid in uids:
        name, _ = split_uid(uid)
        by_source.setdefault(name, []).append(uid)

    results = []
    for name, group in by_source.items():
        try:
            source = get_source(name, config=config, cache_dir=cache_dir)
        except Exception as exc:
            results.extend({"uid": u, "status": "error", "detail": str(exc)[:160]}
                           for u in group)
            continue

        try:
            records = source.hydrate(group)
        except Exception as exc:
            results.extend({"uid": u, "status": "error", "detail": str(exc)[:200]}
                           for u in group)
            continue

        for uid in group:
            record = records.get(uid)
            if not record:
                results.append({"uid": uid, "status": "error",
                                "detail": "not found in %s" % name})
                continue

            filename = build_filename(record.get("title"), record.get("venue"),
                                      record.get("year"))
            if filename in sidecar.entries:
                results.append({"uid": uid, "status": "already-present",
                                "file": filename})
                continue
            rel = ("%s/%s" % (subdir, filename)) if subdir else filename

            if args.dry_run:
                availability = source.pdf_availability(record)
                if availability.get("ok"):
                    results.append({
                        "uid": uid, "status": "would-fetch", "file": rel,
                        "title": record.get("title"),
                        "license": availability.get("license"),
                    })
                else:
                    results.append({
                        "uid": uid, "status": "no-pdf",
                        "detail": availability.get("reason"),
                        "title": record.get("title"), "doi": record.get("doi"),
                        "url": availability.get("url") or record.get("url"),
                    })
                continue

            outcome = source.pdf(uid, target_dir / filename)
            if not outcome.get("ok"):
                # An identified paper we cannot download is still worth
                # reporting: it is a pointer, not a failure to forget.
                results.append({
                    "uid": uid, "status": "no-pdf",
                    "detail": outcome.get("reason"), "title": record.get("title"),
                    "doi": record.get("doi"),
                    "url": outcome.get("url") or record.get("url"),
                })
                continue

            ids = {name: record["id"]}
            if record.get("doi"):
                ids["doi"] = record["doi"]
            sidecar.upsert(
                filename, path=rel,
                source="library" if name == "archer" else name,
                resolved_by=[name], ids=ids,
                archer_id=record["id"] if name == "archer" else None,
                match_score=1.0, title=record.get("title"),
                authors=record.get("authors") or [], venue=record.get("venue"),
                year=record.get("year"), abstract=record.get("abstract"),
                doi=record.get("doi"), url=record.get("url"),
                cited_by_count=record.get("cited_by_count"),
                is_oa=record.get("is_oa"), license=outcome.get("license"),
                library_pdf_path=record.get("library_pdf_path"),
                category=args.category, excerpt=None, collect_error=None)
            results.append({"uid": uid, "status": "fetched", "file": rel,
                            "title": record.get("title"),
                            "license": outcome.get("license")})

    if not args.dry_run:
        sidecar.save()

    print_json({
        "dry_run": args.dry_run,
        "requested": len(uids),
        "fetched": sum(1 for r in results if r["status"] == "fetched"),
        "no_pdf": sum(1 for r in results if r["status"] == "no-pdf"),
        "results": results,
        "next": "python organize.py --topic <dir> --assign '[...]'   # classify the new papers",
    })


if __name__ == "__main__":
    main()
