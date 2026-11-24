"""OpenAlex source: open, keyless, ~250M works, global citation graph.

What it is good for (measured on Sentific_literature, 25 archer-misses):
    matched 25/25   DOI 21   abstract 19   references 12   OA pdf 10

So it closes the *identity and venue* gap almost completely, closes the
citation-graph gap only about halfway, and is a poor PDF source.

Two field-tested traps, both encoded below:

1. `search=` is full-text relevance ranking, not lookup. Querying it with the
   exact title "Recommending Missed Citations Identified by Reviewers: A New
   Task, Dataset and Baselines" returns the PRISMA 2020 guidance paper. Use
   `filter=title.search:` for resolution and `search=` only for discovery.

2. Commas are the AND separator in OpenAlex filter syntax, so a title
   containing one makes `filter=title.search:` return HTTP 400. Punctuation is
   stripped before it goes into a filter.

Reference lists come from Crossref/publisher deposits, and arXiv does not
deposit them -- which is why the ~half of a preprint-heavy folder that lacks
edges is missing them systematically rather than at random. Callers should say
so rather than presenting a thin graph as a complete one.
"""
import json
import os
import re
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from . import register
from .base import (Source, SourceError, make_uid, split_uid, title_in_head,
                   title_match)

API = "https://api.openalex.org"

WORK_FIELDS = ",".join([
    "id", "doi", "title", "display_name", "publication_year", "type",
    "primary_location", "best_oa_location", "open_access",
    "authorships", "abstract_inverted_index", "referenced_works",
    "referenced_works_count", "cited_by_count",
])

# OpenAlex venue strings are display names, which make poor filenames
# ("arXiv (Cornell University)", "Lecture notes in computer science").
VENUE_CLEAN = [
    (re.compile(r"^arxiv\b.*", re.I), "arXiv"),
    (re.compile(r"^lecture notes in computer science.*", re.I), "LNCS"),
    (re.compile(r"^proceedings of the .*", re.I), None),  # keep, just strip prefix
]

# Licences we will download a PDF under. Anything else -- including a missing
# licence field -- we record the URL and leave the file alone.
OPEN_LICENCES = {
    "cc-by", "cc-by-sa", "cc-by-nc", "cc-by-nc-sa", "cc-by-nd",
    "cc-by-nc-nd", "cc0", "public-domain",
}


def _clean_venue(name: str | None) -> str | None:
    if not name:
        return None
    for pattern, replacement in VENUE_CLEAN:
        if pattern.match(name):
            if replacement:
                return replacement
            name = re.sub(r"^proceedings of the\s*", "", name, flags=re.I)
    return name.strip() or None


def _abstract_from_index(index: dict | None) -> str | None:
    """OpenAlex ships abstracts as {word: [positions]} for copyright reasons."""
    if not index:
        return None
    positions: dict[int, str] = {}
    for word, spots in index.items():
        for spot in spots:
            positions[spot] = word
    if not positions:
        return None
    return " ".join(positions[i] for i in sorted(positions)).strip() or None


def _short_id(url_or_id: str) -> str:
    return (url_or_id or "").rstrip("/").rsplit("/", 1)[-1]


@register
class OpenAlexSource(Source):
    name = "openalex"
    capabilities = frozenset({"resolve", "search", "refs", "cites", "hydrate", "pdf"})

    #: polite pool allows 10 req/s; stay under it with a little headroom
    min_interval = 0.15
    cache_max_age_days = 30.0

    def __init__(self, config=None, cache_dir=None):
        super().__init__(config=config, cache_dir=cache_dir)
        settings = (self.config or {}).get("openalex") or {}
        self.mailto = (settings.get("mailto")
                       or os.environ.get("OPENALEX_MAILTO") or "")
        self.timeout = settings.get("timeout", 30)
        self.allow_pdf = settings.get("allow_pdf_download", True)
        self._last_call = 0.0
        self.calls = 0

    # -- transport ----------------------------------------------------------

    def _url(self, path: str, params: dict) -> str:
        params = {k: v for k, v in params.items() if v not in (None, "")}
        if self.mailto:
            params["mailto"] = self.mailto
        return "%s/%s?%s" % (API, path.lstrip("/"),
                             urllib.parse.urlencode(params, safe=":<>|/"))

    def _get(self, path: str, params: dict, use_cache: bool = True):
        url = self._url(path, params)
        cache_key = url.split("&mailto=")[0]
        if use_cache:
            hit = self.cache.get(cache_key, max_age_days=self.cache_max_age_days)
            if hit is not None:
                return hit

        delay = self.min_interval
        last_error = None
        for attempt in range(4):
            gap = time.monotonic() - self._last_call
            if gap < self.min_interval:
                time.sleep(self.min_interval - gap)
            req = urllib.request.Request(url, headers={
                "User-Agent": "paperlevelup/0.2 (%s)" % (self.mailto or "no-mailto"),
                "Accept": "application/json",
            })
            try:
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                self._last_call = time.monotonic()
                self.calls += 1
                if use_cache:
                    self.cache.put(cache_key, body)
                return body
            except urllib.error.HTTPError as exc:
                self._last_call = time.monotonic()
                last_error = "HTTP %s" % exc.code
                if exc.code in (429, 500, 502, 503, 504):
                    time.sleep(delay)
                    delay *= 2
                    continue
                # 400 usually means the filter value still has syntax in it;
                # retrying verbatim will not help.
                raise SourceError("OpenAlex %s for %s" % (last_error, url[:160]))
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                self._last_call = time.monotonic()
                last_error = str(exc)[:100]
                time.sleep(delay)
                delay *= 2
        raise SourceError("OpenAlex unreachable after retries (%s)" % last_error)

    # -- normalisation ------------------------------------------------------

    def _record(self, work: dict) -> dict:
        ident = _short_id(work.get("id"))
        location = work.get("primary_location") or {}
        source_obj = location.get("source") or {}
        oa = work.get("best_oa_location") or {}
        authors = [(a.get("author") or {}).get("display_name")
                   for a in (work.get("authorships") or [])]
        doi = (work.get("doi") or "").replace("https://doi.org/", "") or None
        return {
            "source": self.name,
            "id": ident,
            "uid": make_uid(self.name, ident),
            "title": work.get("title") or work.get("display_name"),
            "authors": [a for a in authors if a],
            "venue": _clean_venue(source_obj.get("display_name")),
            "venue_full": source_obj.get("display_name"),
            "year": work.get("publication_year"),
            "abstract": _abstract_from_index(work.get("abstract_inverted_index")),
            "doi": doi,
            "url": ("https://doi.org/%s" % doi) if doi else work.get("id"),
            "cited_by_count": work.get("cited_by_count") or 0,
            "referenced_works_count": work.get("referenced_works_count") or 0,
            "is_oa": bool((work.get("open_access") or {}).get("is_oa")),
            "pdf_url": oa.get("pdf_url"),
            "license": oa.get("license"),
            "type": work.get("type"),
        }

    # -- capabilities -------------------------------------------------------

    def _resolve_doi(self, doi: str) -> dict | None:
        try:
            work = self._get("works/doi:%s" % doi, {"select": WORK_FIELDS})
        except SourceError:
            return None
        return work if work and work.get("id") else None

    def resolve(self, items: list[dict], threshold: float) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for item in items:
            # A DOI read off a publisher filename is an exact lookup; try it
            # before any fuzzy title matching.
            if item.get("doi"):
                work = self._resolve_doi(item["doi"])
                if work:
                    record = self._record(work)
                    out[item["key"]] = {
                        "matched": True, "score": 1.0, "match_kind": "doi",
                        "id": record["id"], "uid": record["uid"], "record": record}
                    continue
            title = (item.get("title") or "").strip()
            if not title:
                out[item["key"]] = {"matched": False, "score": 0.0,
                                    "error": "no title hint"}
                continue
            # Strip punctuation: commas break filter syntax outright, and the
            # rest only adds noise to a fuzzy title match.
            probe = re.sub(r"[^\w\s]", " ", title)
            probe = " ".join(probe.split())[:180]
            params = {"filter": "title.search:%s" % probe,
                      "per-page": 5, "select": WORK_FIELDS}
            try:
                data = self._get("works", params)
            except SourceError as exc:
                out[item["key"]] = {"matched": False, "score": 0.0, "error": str(exc)}
                continue

            best, best_score, best_contained = None, 0.0, False
            on_page = None
            for work in data.get("results", []):
                cand_title = work.get("title") or work.get("display_name") or ""
                score, contained = title_match(title, cand_title)
                if on_page is None and title_in_head(cand_title, item.get("head")):
                    on_page = (work, score)
                # A year hint that contradicts by more than a year is usually a
                # different paper with a near-identical title (v1 vs v2, or a
                # workshop/journal pair). Nudge rather than exclude.
                hint_year = item.get("year")
                if hint_year and work.get("publication_year"):
                    if abs(int(hint_year) - int(work["publication_year"])) > 1:
                        score -= 0.05
                if score > best_score:
                    best, best_score, best_contained = work, score, contained

            accepted = best is not None and (best_score >= threshold or best_contained)
            kind = "similarity" if best_score >= threshold else "containment"
            if not accepted and on_page:
                # The query was a guess, but this candidate's whole title is
                # printed on the PDF's first page.
                (best, best_score), accepted, kind = on_page, True, "page_title"
            if accepted:
                record = self._record(best)
                out[item["key"]] = {
                    "matched": True, "score": round(best_score, 4),
                    "match_kind": kind,
                    "id": record["id"], "uid": record["uid"], "record": record}
            else:
                out[item["key"]] = {
                    "matched": False, "score": round(best_score, 4),
                    "near_miss": (best or {}).get("title")}
        return out

    def search(self, queries, per_query=30, year_min=None, year_max=None,
               venues=None, fulltext=False) -> list[dict]:
        """Keyword recall.

        Default is `title_and_abstract.search`, not `search=`. The latter
        queries the *full text*, so "local citation recommendation" matches any
        paper containing the word "recommendation" anywhere -- measured: 177,500
        hits, whose tail is CPR guidelines and wildfire smoke reviews. Scoped to
        title and abstract the same query returns 319, and the head is on-topic.
        Pass fulltext=True only when the scoped query comes back too thin.
        """
        base = ["type:article|preprint|book-chapter|conference-paper"]
        if year_min and year_max:
            base.append("publication_year:%d-%d" % (year_min, year_max))
        elif year_min:
            base.append("publication_year:>%d" % (year_min - 1))
        elif year_max:
            base.append("publication_year:<%d" % (year_max + 1))

        merged: dict[str, dict] = {}
        errors = []
        for query in queries:
            params = {"per-page": min(per_query, 200), "select": WORK_FIELDS}
            if fulltext:
                params["search"] = query
                params["filter"] = ",".join(base)
            else:
                # Commas are the filter separator, so they cannot appear in a
                # filter value; strip punctuation as in resolve().
                probe = " ".join(re.sub(r"[^\w\s]", " ", query).split())
                params["filter"] = ",".join(
                    base + ["title_and_abstract.search:%s" % probe])
            try:
                data = self._get("works", params)
            except SourceError as exc:
                errors.append({"query": query, "error": str(exc)})
                continue
            for position, work in enumerate(data.get("results", [])):
                record = self._record(work)
                slot = merged.setdefault(record["uid"], record)
                slot.setdefault("matched_queries", [])
                if query not in slot["matched_queries"]:
                    slot["matched_queries"].append(query)
                slot["best_rank"] = min(slot.get("best_rank", 10 ** 6), position)

        results = list(merged.values())
        if venues:
            wanted = {v.lower() for v in venues}
            results = [r for r in results
                       if any(w in (r.get("venue_full") or r.get("venue") or "").lower()
                              for w in wanted)]
        self.last_errors = errors
        return results

    def references(self, uids: list[str]) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for uid in uids:
            _, ident = split_uid(uid, default=self.name)
            try:
                work = self._get("works/%s" % ident,
                                 {"select": "id,referenced_works"})
            except SourceError:
                out[uid] = []
                continue
            out[uid] = [make_uid(self.name, _short_id(w))
                        for w in (work.get("referenced_works") or [])]
        return out

    def cited_by(self, uids, limit_per_paper: int = 200) -> dict[str, list[str]]:
        out: dict[str, list[str]] = {}
        for uid in uids:
            _, ident = split_uid(uid, default=self.name)
            collected, cursor = [], "*"
            while cursor and len(collected) < limit_per_paper:
                params = {"filter": "cites:%s" % ident, "select": "id",
                          "per-page": min(200, limit_per_paper - len(collected)),
                          "cursor": cursor}
                try:
                    data = self._get("works", params)
                except SourceError:
                    break
                for work in data.get("results", []):
                    collected.append(make_uid(self.name, _short_id(work.get("id"))))
                cursor = (data.get("meta") or {}).get("next_cursor")
                if not data.get("results"):
                    break
            out[uid] = collected
        return out

    def hydrate(self, uids: list[str]) -> dict[str, dict]:
        """Batch metadata. OpenAlex takes an OR filter of up to 100 ids."""
        out: dict[str, dict] = {}
        idents = []
        for uid in uids:
            _, ident = split_uid(uid, default=self.name)
            idents.append(ident)
        for start in range(0, len(idents), 80):
            chunk = idents[start:start + 80]
            params = {"filter": "openalex_id:%s" % "|".join(
                "https://openalex.org/%s" % i for i in chunk),
                "per-page": len(chunk), "select": WORK_FIELDS}
            try:
                data = self._get("works", params)
            except SourceError:
                continue
            for work in data.get("results", []):
                record = self._record(work)
                out[record["uid"]] = record
        return out

    def pdf_availability(self, record: dict) -> dict:
        if not self.allow_pdf:
            return {"ok": False, "reason": "pdf download disabled in config"}
        if not record.get("is_oa") or not record.get("pdf_url"):
            return {"ok": False, "reason": "not open access",
                    "doi": record.get("doi"), "url": record.get("url")}
        licence = (record.get("license") or "").lower()
        if licence not in OPEN_LICENCES:
            return {
                "ok": False,
                "reason": "licence %r not in the permitted set"
                          % (licence or "unknown"),
                "url": record.get("pdf_url"), "doi": record.get("doi"),
            }
        return {"ok": True, "url": record["pdf_url"], "license": licence}

    def pdf(self, uid: str, dest: Path) -> dict:
        """Download only an open-access PDF under a permissive licence.

        Deliberately conservative: we do not follow a landing page, do not
        retry behind a paywall, and refuse anything whose licence we cannot
        read. A non-OA paper is reported with its DOI so the user can get it
        through their own institutional access.
        """
        record = self.hydrate([uid]).get(
            make_uid(self.name, split_uid(uid, default=self.name)[1]))
        if not record:
            return {"ok": False, "reason": "not found"}
        availability = self.pdf_availability(record)
        if not availability.get("ok"):
            return availability
        licence = availability["license"]

        req = urllib.request.Request(record["pdf_url"], headers={
            "User-Agent": "paperlevelup/0.2 (%s)" % (self.mailto or "no-mailto")})
        if dest.exists():
            return {"ok": False, "reason": "destination already exists"}
        temp_path = None
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with urllib.request.urlopen(req, timeout=60) as resp:
                head = resp.read(5)
                if head[:4] != b"%PDF":
                    return {"ok": False, "reason": "URL did not serve a PDF",
                            "url": record["pdf_url"]}
                with tempfile.NamedTemporaryFile(
                        prefix=dest.name + ".", suffix=".part",
                        dir=dest.parent, delete=False) as fh:
                    temp_path = Path(fh.name)
                    fh.write(head)
                    while True:
                        block = resp.read(1 << 16)
                        if not block:
                            break
                        fh.write(block)
                temp_path.replace(dest)
        except Exception as exc:
            if temp_path:
                temp_path.unlink(missing_ok=True)
            return {"ok": False, "reason": str(exc)[:120], "url": record["pdf_url"]}
        return {"ok": True, "url": record["pdf_url"], "license": licence,
                "record": record}

    def stats(self) -> dict:
        base = super().stats()
        base.update({"api_calls": self.calls, "polite_pool": bool(self.mailto)})
        return base
