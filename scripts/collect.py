"""Gather everything the agent needs to read, for every PDF in a topic folder.

Configured sources are queried independently, then their answers are merged:

  1. The library host (archer). Authoritative title/authors/venue/year/abstract
     for the 15 venues it carries, and the only source with the PDFs. It also
     silently corrects filename typos, so its answer beats the filename.
  2. OpenAlex. Broad venue coverage including SIGIR/KDD/TKDE/ECIR, journals
     and preprints, plus DOI and global citation counts that archer lacks.
  3. Local text extraction. Whatever neither identified still needs to be
     readable, so pypdf over the first few pages.

Metadata from several sources is merged field by field with a fixed precedence
(see sources/base.py), and each record keeps a `provenance` map saying which
source each field came from.

Usage:
    python collect.py --topic ~/Downloads/Papers/Sentific_literature
    python collect.py --topic <dir> --force              # re-resolve everything
    python collect.py --topic <dir> --sources archer     # library only
    python collect.py --topic <dir> --digest             # print reading material
"""
import argparse
import re
import sys

from common import (Sidecar, add_topic_arg, filename_ids,
                    guess_title_from_text, load_config, looks_like_title,
                    parse_filename, print_json, scan_pdfs)
from metadata_overrides import apply_overrides
from sources import get_source, merge_records

ABSTRACT_RE = re.compile(r"(?im)^[^\S\n]*abstract[^\n]{0,40}$")


def pdf_text(path, pages: int) -> tuple[str, str | None]:
    """Raw text of the first `pages` pages, or ("", reason)."""
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
        return text, None
    except Exception as exc:
        return "", str(exc)


def extract_local(path, pages: int, max_chars: int) -> tuple[str, str | None]:
    text, error = pdf_text(path, pages)
    if not text:
        return "", error
    return focus_excerpt(text, max_chars), None


#: page-1 text kept for verifying candidates against the printed title
HEAD_CHARS = 4000


def choose_title_hint(rec: dict | None, hints: dict, ids: dict, path):
    """(title to search with, where it came from, page-1 text or None).

    A filename is only a usable query when it is a title. Publisher download
    names (`2026.emnlp-main.589.pdf`, `s41586-025-10072-4.pdf`) are
    identifiers: matched as titles they fail every time, and the closest title
    a source offers back is an unrelated paper. For those, the query is a
    title guessed from the first page, and the page text travels with it so a
    source can accept a candidate whose real title is printed there.
    """
    rec = rec or {}
    from_override = (rec.get("provenance") or {}).get("title") == "metadata_override"
    if rec.get("title") and (rec.get("ids") or from_override):
        return rec["title"], "override" if from_override else "record", None
    if looks_like_title(hints["title"]) and not ids:
        return hints["title"], "filename", None
    text, _error = pdf_text(path, 1)
    head = text[:HEAD_CHARS] or None
    guess = guess_title_from_text(text)
    if guess:
        return guess, "pdf_text", head
    return hints["title"], "filename", head


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
        ids = rec.get("ids") or {}
        where = ("in-corpus" if ids.get("archer")
                 else "openalex" if ids.get("openalex") else "local-only")
        print("source: %s  %s" % (rec.get("source", "?"), where))
        if (rec.get("provenance") or {}).get("title") == "pdf_text_guess":
            print("title: guessed from page 1, unconfirmed by any source")
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


UNRESOLVED_WARNING = (
    "No source identified the papers in `unresolved`. This is not evidence they "
    "are absent: a near_miss is only the closest title a source returned, and "
    "below the threshold it is usually an unrelated paper. Check `searched_as` "
    "against the PDF's first page; if it is not the real title, fix it with "
    "metadata.py --set and rerun with --retry-unresolved. Before telling the "
    "user a paper is not in the library, run exact_search on its real title.")


def resolve_sources(todo, names, config, cache_dir, threshold):
    """Query every configured source so records can be enriched and merged."""
    per_source: dict[str, int] = {}
    source_errors: list[dict] = []
    hits: dict[str, list[dict]] = {}
    scores: dict[str, dict] = {}

    for name in names:
        try:
            source = get_source(name, config=config, cache_dir=cache_dir)
        except Exception as exc:
            source_errors.append({"source": name, "error": str(exc)[:200]})
            continue
        if "resolve" not in source.capabilities:
            continue

        print("resolving %d paper(s) via %s..." % (len(todo), name),
              file=sys.stderr)
        try:
            answers = source.resolve(todo, threshold)
        except Exception as exc:
            source_errors.append({"source": name, "error": str(exc)[:300]})
            continue

        for item in todo:
            answer = answers.get(item["key"]) or {}
            scores.setdefault(item["key"], {})[name] = {
                "score": answer.get("score"),
                "match_kind": answer.get("match_kind") if answer.get("matched") else None,
                "near_miss": answer.get("near_miss"),
                "error": answer.get("error"),
            }
            if answer.get("matched") and answer.get("record"):
                hits.setdefault(item["key"], []).append(answer["record"])
                per_source[name] = per_source.get(name, 0) + 1
    return hits, scores, per_source, source_errors


def records_from_sidecar(rec: dict) -> list[dict]:
    """Reconstruct source records without inventing new provenance.

    This lets enrichment survive a temporary backend failure. Fields already
    carrying provenance stay attached to that source; legacy records without
    provenance are attributed to their primary source.
    """
    ids = rec.get("ids") or {}
    source_ids = {name: ident for name, ident in ids.items()
                  if name not in ("doi", "arxiv") and ident}
    if not source_ids:
        return []
    primary = (
        "archer" if source_ids.get("archer")
        else next((s for s in rec.get("resolved_by") or [] if source_ids.get(s)), None)
        or next(iter(source_ids))
    )
    records = {
        name: {"source": name, "id": ident}
        for name, ident in source_ids.items()
    }
    provenance = rec.get("provenance") or {}
    fields = (
        "title", "authors", "venue", "year", "abstract", "doi", "url",
        "cited_by_count", "is_oa", "pdf_url", "license", "library_pdf_path",
    )
    for field in fields:
        value = rec.get(field)
        if value in (None, "", [], {}):
            continue
        # Overrides are re-applied after the merge, and they must sit on top of
        # what the sources say now, not on a copy of themselves -- otherwise
        # withdrawing one later would restore the override, not the source.
        if provenance.get(field) in (
                "metadata_override", "withdrawn_override", "pdf_text_guess"):
            continue
        owner = provenance.get(field) or primary
        records.setdefault(owner, {
            "source": owner,
            "id": source_ids.get(owner),
        })[field] = value
    return list(records.values())


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    add_topic_arg(ap)
    ap.add_argument("--force", action="store_true",
                    help="re-resolve papers that were already collected")
    ap.add_argument("--retry-unresolved", action="store_true",
                    help="re-attempt only the papers no source has identified "
                         "yet, including sources tried on an earlier run")
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
    ap.add_argument("--known-ids", action="store_true",
                    help="print the archer ids of the papers already in the folder "
                         "(for paper-retrieval's filters.exclude_ids) instead of collecting")
    ap.add_argument("--sources", default=None,
                    help="comma-separated, in query/primary-source order "
                         "(default: config 'sources', else archer,openalex)")
    ap.add_argument("--no-cache", action="store_true",
                    help="bypass the cached API responses under .paperlevelup/cache")
    args = ap.parse_args()

    sidecar = Sidecar(args.topic)

    if args.digest:
        cmd_digest(sidecar, args.digest_chars)
        return
    if args.known_ids:
        ids = sidecar.known_archer_ids()
        out = {"exclude_ids": ids, "count": len(ids),
               "papers_in_folder": len(sidecar.entries),
               "note": "archer ids are the paper_ids paper-retrieval returns; "
                       "papers no archer record matched are not in this list"}
        if not sidecar.entries:
            out["warning"] = ("no papers recorded for this folder yet; run collect.py "
                              "--topic <dir> first, or this list excludes nothing")
        print_json(out)
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
    identified_before = {
        key for key, rec in sidecar.entries.items() if rec.get("ids")
    }
    config = load_config()
    names = [s.strip() for s in (args.sources or "").split(",") if s.strip()]
    if not names:
        names = config.get("sources") or ["archer", "openalex"]

    for paper in papers:
        rec = sidecar.entries.get(paper["key"])
        sidecar.upsert(paper["key"], path=paper["path"])
        unresolved = not (rec or {}).get("ids")
        ids = (rec or {}).get("ids") or {}
        tried = set((rec or {}).get("tried_sources") or [])
        missing_untried_source = any(name not in ids and name not in tried
                                     for name in names)
        if (rec is None or args.force or not rec.get("source")
                or (args.retry_unresolved and unresolved)
                or missing_untried_source):
            hints = parse_filename(paper["key"])
            file_ids = filename_ids(paper["key"])
            title, hint_from, head = choose_title_hint(
                rec, hints, file_ids, sidecar.topic / paper["path"])
            sidecar.upsert(paper["key"], filename_title=hints["title"],
                           filename_venue=hints["venue"], filename_year=hints["year"],
                           title_hint=title, title_hint_from=hint_from)
            todo.append({
                "key": paper["key"],
                "title": title,
                "title_hint_from": hint_from,
                "head": head,
                "doi": file_ids.get("doi"),
                "url": file_ids.get("url"),
                "year": (rec or {}).get("year") or hints["year"],
                "venue": (rec or {}).get("venue") or hints["venue"],
            })

    threshold = args.threshold or config.get("match_threshold") or 0.85
    cache_dir = None if args.no_cache else sidecar.cache_dir

    per_source: dict[str, int] = {}
    source_errors: list[dict] = []
    hits: dict[str, list[dict]] = {}
    scores: dict[str, dict] = {}
    failed = backfilled = 0

    if todo:
        hits, scores, per_source, source_errors = resolve_sources(
            todo, names, config, cache_dir, threshold)

        for item in todo:
            key = item["key"]
            path = sidecar.topic / sidecar.entries[key]["path"]
            records = hits.get(key) or []
            existing = sidecar.entries[key]
            # Preserve previously resolved fields if a backend is temporarily
            # unavailable while a new source is enriching the record.
            records[:0] = records_from_sidecar(existing)

            if records:
                merged = merge_records(records)
                abstract = (merged.get("abstract") or "").strip()
                excerpt, error = "", None
                if len(abstract) < 40:
                    # Abstract coverage is ~99.9% from 2020 on in the library
                    # but only ~83% before that, and OpenAlex sits near 76%.
                    # Without this a paper can resolve and still leave nothing
                    # to read.
                    excerpt, error = extract_local(path, args.pages, args.max_chars)
                    backfilled += 1
                primary = next((name for name in names
                                if merged["ids"].get(name)), records[0]["source"])
                attempted = {
                    name for name, result in scores.get(key, {}).items()
                    if not result.get("error")
                }
                tried_sources = sorted(
                    set(existing.get("tried_sources") or []) | attempted)
                resolved_sources = [
                    name for name in names if merged["ids"].get(name)
                ]
                resolved_sources += sorted(
                    name for name in merged["ids"]
                    if name not in ("doi", "arxiv")
                    and name not in resolved_sources
                )
                primary_score = (
                    (scores.get(key, {}).get(primary) or {}).get("score")
                )
                if primary_score is None and existing.get("ids", {}).get(primary):
                    primary_score = existing.get("match_score")
                primary_kind = (scores.get(key, {}).get(primary) or {}).get("match_kind")
                sidecar.upsert(
                    key,
                    source="library" if primary == "archer" else primary,
                    match_kind=primary_kind or existing.get("match_kind"),
                    resolved_by=resolved_sources,
                    ids=merged["ids"], archer_id=merged["ids"].get("archer"),
                    provenance=merged["provenance"],
                    tried_sources=tried_sources,
                    match_score=primary_score,
                    title=merged.get("title"),
                    authors=merged.get("authors") or [],
                    venue=merged.get("venue"), year=merged.get("year"),
                    abstract=abstract or None,
                    doi=merged.get("doi"), url=merged.get("url"),
                    cited_by_count=merged.get("cited_by_count"),
                    is_oa=merged.get("is_oa"), pdf_url=merged.get("pdf_url"),
                    license=merged.get("license"),
                    library_pdf_path=merged.get("library_pdf_path"),
                    excerpt=excerpt or None, collect_error=error, near_miss=None)
                if error:
                    failed += 1
                continue

            hints = parse_filename(key)
            text, error = extract_local(path, args.pages, args.max_chars)
            attempts = scores.get(key, {})
            best = max(attempts.items(),
                       key=lambda kv: (kv[1].get("score") or 0), default=(None, {}))
            sidecar.upsert(
                key, source="local", ids={}, archer_id=None, resolved_by=[],
                match_score=best[1].get("score"), near_miss=best[1].get("near_miss"),
                match_kind=None,
                tried_sources=sorted(
                    name for name, result in attempts.items()
                    if not result.get("error")),
                # a title guessed from page 1 reads better in the digest than
                # an identifier, and is labelled as a guess in provenance
                title=item["title"],
                provenance=({"title": "pdf_text_guess"}
                            if item["title_hint_from"] == "pdf_text" else {}),
                venue=hints["venue"], year=hints["year"],
                authors=[], abstract=None, excerpt=text or None,
                collect_error=error or best[1].get("error"))
            if error:
                failed += 1

    # Evidence-bearing local corrections are the final metadata authority and
    # are deliberately re-applied after every source refresh.
    applied_overrides = apply_overrides(sidecar)
    sidecar.save()

    unresolved = []
    for item in todo:
        if sidecar.entries[item["key"]].get("ids"):
            continue
        unresolved.append({
            "key": item["key"],
            "searched_as": item["title"],
            "searched_as_from": item["title_hint_from"],
            "closest_by_source": {
                name: {"score": result.get("score"),
                       "near_miss": result.get("near_miss"),
                       **({"error": result["error"]} if result.get("error") else {})}
                for name, result in scores.get(item["key"], {}).items()},
        })

    summary = {
        "topic": str(sidecar.topic),
        "pdfs_on_disk": len(papers),
        "processed": len(todo),
        "newly_resolved": sum(
            1 for item in todo
            if item["key"] not in identified_before
            and sidecar.entries[item["key"]].get("ids")),
        "resolved_by_source": per_source,
        "still_local_only": sum(
            1 for item in todo
            if not (sidecar.entries[item["key"]].get("ids") or {})),
        "matched_but_abstract_backfilled_from_pdf": backfilled,
        "text_extraction_failed": failed,
        "no_readable_text": sorted(
            k for k, r in sidecar.entries.items()
            if not (r.get("abstract") or "").strip() and not (r.get("excerpt") or "").strip()),
        "total_in_sidecar": len(sidecar.entries),
        "identified_total": sum(1 for r in sidecar.entries.values() if r.get("ids")),
        "in_library_total": sum(1 for r in sidecar.entries.values()
                                if (r.get("ids") or {}).get("archer")),
        "with_doi": sum(1 for r in sidecar.entries.values()
                        if (r.get("ids") or {}).get("doi") or r.get("doi")),
        "dropped_missing_from_disk": removed,
        "unresolved": unresolved,
        "source_errors": source_errors,
        "problems": problems,
        "metadata_overrides_applied": applied_overrides,
        **({"unresolved_warning": UNRESOLVED_WARNING} if unresolved else {}),
        "next": "python collect.py --topic <dir> --digest   # read everything, then design the taxonomy",
    }
    print_json(summary)


if __name__ == "__main__":
    main()
