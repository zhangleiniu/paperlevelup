"""Gather everything the agent needs to read, for every PDF in a topic folder.

Two sources, in priority order:

  1. The library host. If the paper is in the corpus we take its authoritative
     title/authors/venue/year/abstract. This also silently fixes typos in the
     filename, so treat the library's answer as the truth.
  2. Local text extraction. Papers from venues the corpus does not cover fall
     back to pypdf over the first few pages, which is enough to classify from.

Usage:
    python collect.py --topic ~/Downloads/Papers/Sentific_literature
    python collect.py --topic <dir> --force          # re-resolve everything
    python collect.py --topic <dir> --digest         # print the reading material
"""
import argparse
import re
import sys

from common import (Sidecar, add_topic_arg, parse_filename, print_json,
                    run_remote, scan_pdfs)

ABSTRACT_RE = re.compile(r"(?im)^[^\S\n]*abstract[^\n]{0,40}$")

REMOTE_RESOLVE = r'''
items = PAYLOAD["items"]
threshold = PAYLOAD["threshold"]
results = []
for item in items:
    query = item.get("title") or ""
    try:
        hits = meili_search(query, limit=8)
    except Exception as exc:
        results.append({"key": item["key"], "matched": False, "error": str(exc)})
        continue
    best, best_score = None, 0.0
    for hit in hits:
        score = ratio(query, hit.get("title", ""))
        if score > best_score:
            best, best_score = hit, score
    if best and best_score >= threshold:
        results.append({"key": item["key"], "matched": True,
                        "score": round(best_score, 4), "id": best["id"]})
    else:
        results.append({"key": item["key"], "matched": False,
                        "score": round(best_score, 4),
                        "near_miss": (best or {}).get("title")})
emit({"results": results,
      "meta": hydrate([r["id"] for r in results if r.get("matched")])})
'''


def extract_local(path, pages: int, max_chars: int) -> tuple[str, str | None]:
    try:
        import logging

        from pypdf import PdfReader
        # pypdf narrates every malformed xref it walks past; real papers trip
        # this constantly and it is never actionable.
        logging.getLogger("pypdf").setLevel(logging.ERROR)
    except ImportError:
        return "", "pypdf not installed (pip install pypdf)"
    try:
        reader = PdfReader(str(path))
        limit = min(pages, len(reader.pages))
        chunks = []
        for i in range(limit):
            try:
                chunks.append(reader.pages[i].extract_text() or "")
            except Exception as exc:
                chunks.append("[page %d failed: %s]" % (i + 1, exc))
        text = "\n".join(chunks).strip()
        if not text:
            return "", "no extractable text (scanned PDF?)"
        return focus_excerpt(text, max_chars), None
    except Exception as exc:
        return "", str(exc)


def focus_excerpt(text: str, max_chars: int) -> str:
    """Keep the head (title/authors) but skip ahead to the abstract if it is
    buried. Theses and reports put pages of front matter first, and a budget
    spent on acknowledgements is a budget not spent on content."""
    match = ABSTRACT_RE.search(text)
    if match and match.start() > 600:
        return (text[:600].rstrip() + "\n\n[...]\n\n" + text[match.start():])[:max_chars]
    return text[:max_chars]


def cmd_digest(sidecar: Sidecar, chars: int):
    """Print title + venue/year + abstract for every paper, in one pass.

    This is deliberately one flat dump: the agent is meant to read the whole
    topic at once and design a taxonomy from the actual distribution, rather
    than classify papers one at a time against a taxonomy guessed up front.
    """
    records = [sidecar.entries[k] for k in sorted(sidecar.entries)]
    print("# Topic digest: %s" % sidecar.topic.name)
    print("# %d papers\n" % len(records))
    for i, rec in enumerate(records, 1):
        venue_year = " ".join(str(x) for x in (rec.get("venue"), rec.get("year")) if x)
        print("## [%d] %s" % (i, rec.get("title") or rec["key"]))
        print("key: %s" % rec["key"])
        print("source: %s%s" % (rec.get("source", "?"),
                                "  in-corpus" if rec.get("archer_id") else "  local-only"))
        if venue_year:
            print("venue/year: %s" % venue_year)
        if rec.get("authors"):
            print("authors: %s" % ", ".join(rec["authors"][:8]))
        if rec.get("category"):
            print("current category: %s" % rec["category"])
        body = rec.get("abstract") or rec.get("excerpt") or ""
        if body:
            print("\n%s" % body[:chars].strip())
        elif rec.get("collect_error"):
            print("\n[no text: %s]" % rec["collect_error"])
        print()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_topic_arg(ap)
    ap.add_argument("--force", action="store_true",
                    help="re-resolve papers that were already collected")
    ap.add_argument("--threshold", type=float, default=None,
                    help="title similarity required to accept a library match")
    ap.add_argument("--pages", type=int, default=5,
                    help="pages of local text to extract when not in the corpus")
    ap.add_argument("--max-chars", type=int, default=8000,
                    help="cap on locally extracted text per paper")
    ap.add_argument("--digest", action="store_true",
                    help="print collected abstracts instead of collecting")
    ap.add_argument("--digest-chars", type=int, default=2500,
                    help="per-paper text budget in --digest output")
    args = ap.parse_args()

    sidecar = Sidecar(args.topic)

    if args.digest:
        cmd_digest(sidecar, args.digest_chars)
        return

    papers, problems = scan_pdfs(sidecar.topic)
    if not papers:
        raise SystemExit("no PDFs found under %s" % sidecar.topic)

    # Keep the sidecar in step with what is actually on disk.
    on_disk = {p["key"] for p in papers}
    removed = [k for k in sidecar.entries if k not in on_disk]
    for key in removed:
        del sidecar.entries[key]

    todo = []
    for paper in papers:
        rec = sidecar.entries.get(paper["key"])
        sidecar.upsert(paper["key"], path=paper["path"])
        if rec is None or args.force or not rec.get("source"):
            hints = parse_filename(paper["key"])
            sidecar.upsert(paper["key"], filename_title=hints["title"],
                           filename_venue=hints["venue"], filename_year=hints["year"])
            todo.append({"key": paper["key"], "title": hints["title"]})

    matched = unmatched = failed = backfilled = 0
    if todo:
        config_threshold = None
        try:
            from common import load_config
            config_threshold = load_config().get("match_threshold")
        except Exception:
            pass
        threshold = args.threshold or config_threshold or 0.85

        print("resolving %d paper(s) against the library host..." % len(todo),
              file=sys.stderr)
        response = run_remote(REMOTE_RESOLVE,
                              {"items": todo, "threshold": threshold})
        meta = response["meta"]

        for result in response["results"]:
            key = result["key"]
            path = sidecar.topic / sidecar.entries[key]["path"]

            if result.get("matched"):
                info = meta.get(result["id"], {})
                abstract = (info.get("abstract") or "").strip()
                excerpt, error = "", None
                if len(abstract) < 40:
                    # Abstract coverage is ~99.9% from 2020 on but only ~83%
                    # before that. Without this fallback an older paper can
                    # match the library and still leave nothing to read.
                    excerpt, error = extract_local(path, args.pages, args.max_chars)
                    backfilled += 1
                sidecar.upsert(
                    key, source="library", archer_id=result["id"],
                    match_score=result["score"], title=info.get("title"),
                    authors=info.get("authors") or [], venue=info.get("venue"),
                    year=info.get("year"), abstract=abstract or None,
                    library_pdf_path=info.get("pdf_path"), url=info.get("url"),
                    excerpt=excerpt or None, collect_error=error)
                matched += 1
                if error:
                    failed += 1
                continue

            hints = parse_filename(key)
            text, error = extract_local(path, args.pages, args.max_chars)
            sidecar.upsert(
                key, source="local", archer_id=None,
                match_score=result.get("score"), near_miss=result.get("near_miss"),
                title=hints["title"], venue=hints["venue"], year=hints["year"],
                authors=[], abstract=None, excerpt=text or None,
                collect_error=error or result.get("error"))
            unmatched += 1
            if error:
                failed += 1

    sidecar.save()

    summary = {
        "topic": str(sidecar.topic),
        "pdfs_on_disk": len(papers),
        "newly_resolved": len(todo),
        "matched_in_library": matched,
        "local_only": unmatched,
        "matched_but_abstract_backfilled_from_pdf": backfilled,
        "text_extraction_failed": failed,
        "no_readable_text": sorted(
            k for k, r in sidecar.entries.items()
            if not (r.get("abstract") or "").strip() and not (r.get("excerpt") or "").strip()),
        "total_in_sidecar": len(sidecar.entries),
        "in_library_total": sum(1 for r in sidecar.entries.values() if r.get("archer_id")),
        "dropped_missing_from_disk": removed,
        "problems": problems,
        "next": "python collect.py --topic <dir> --digest   # read everything, then design the taxonomy",
    }
    print_json(summary)


if __name__ == "__main__":
    main()
