"""Source interface, the normalised record shape, and the on-disk response cache."""
import hashlib
import json
import time
from pathlib import Path


class SourceError(RuntimeError):
    pass


# --------------------------------------------------------------------------
# identity
# --------------------------------------------------------------------------

def make_uid(source: str, ident: str) -> str:
    return "%s:%s" % (source, ident)


def split_uid(uid: str, default: str = "archer") -> tuple[str, str]:
    """`openalex:W123` -> ("openalex", "W123").

    A bare id has no prefix and belongs to `default`, so invocations written
    before sources existed keep working. Archer ids are UUIDs, which contain
    hyphens but never a colon, so the split is unambiguous.
    """
    if ":" in uid:
        head, rest = uid.split(":", 1)
        if head and rest:
            return head, rest
    return default, uid


def normalise_title(text: str) -> str:
    cleaned = "".join(c if (c.isalnum() or c.isspace()) else " "
                      for c in (text or "").lower())
    return " ".join(cleaned.split())


#: A filename title this long, appearing verbatim inside a candidate title, is
#: not a coincidence. Below it, short generic titles would false-positive.
CONTAINMENT_MIN_CHARS = 30


def title_match(query: str, candidate: str) -> tuple[float, bool]:
    """(similarity, is_contained).

    Plain sequence ratio misses a whole class of real matches: filenames get
    truncated, so `[2002 ...]Experiments with Relevance and Rhetorical Status`
    scores only 0.75 against the true title `Summarizing Scientific Articles:
    Experiments with Relevance and Rhetorical Status`. Verbatim containment of
    a long-enough string is separate evidence, reported separately so the
    caller decides what to do with it rather than having it hidden in a number.
    """
    from difflib import SequenceMatcher
    a, b = normalise_title(query), normalise_title(candidate)
    if not a or not b:
        return 0.0, False
    ratio = SequenceMatcher(None, a, b).ratio()
    shorter, longer = (a, b) if len(a) <= len(b) else (b, a)
    contained = len(shorter) >= CONTAINMENT_MIN_CHARS and shorter in longer
    return ratio, contained


# --------------------------------------------------------------------------
# record merging
# --------------------------------------------------------------------------

# Which source wins for a given field when two disagree. Not a single global
# ranking: archer is authoritative about "is this in my curated corpus and
# where is the PDF", OpenAlex about DOI, global citation counts and venues it
# actually carries. Anything unlisted falls back to first-non-empty.
FIELD_PRECEDENCE = {
    "title": ["archer", "openalex"],
    "authors": ["archer", "openalex"],
    "venue": ["archer", "openalex"],
    "year": ["archer", "openalex"],
    "abstract": ["archer", "openalex"],
    "doi": ["openalex"],
    "cited_by_count": ["openalex"],
    "is_oa": ["openalex"],
    "pdf_url": ["openalex"],
    "license": ["openalex"],
}


def merge_records(records: list[dict]) -> dict:
    """Field-level merge of the same paper seen by several sources.

    Returns the merged record plus `provenance` mapping each field to the
    source it came from, because with more than one backend "the library is
    the authority" stops being a single sentence you can put in the docs.
    """
    by_source = {r["source"]: r for r in records if r.get("source")}
    merged: dict = {}
    provenance: dict[str, str] = {}
    ids: dict[str, str] = {}

    for rec in records:
        if rec.get("source") and rec.get("id"):
            ids[rec["source"]] = rec["id"]
        # DOI is the only identifier every source can agree on, so it lives in
        # `ids` as the cross-source join key rather than as a plain field.
        if rec.get("doi") and not ids.get("doi"):
            ids["doi"] = rec["doi"]
        if rec.get("arxiv_id") and not ids.get("arxiv"):
            ids["arxiv"] = rec["arxiv_id"]

    fields = {k for r in records for k in r
              if k not in ("source", "id", "uid", "provenance")}
    for field in fields:
        order = FIELD_PRECEDENCE.get(field, [])
        chosen = None
        for name in order:
            value = (by_source.get(name) or {}).get(field)
            if value not in (None, "", [], {}):
                chosen = (name, value)
                break
        if chosen is None:
            for rec in records:
                value = rec.get(field)
                if value not in (None, "", [], {}):
                    chosen = (rec.get("source"), value)
                    break
        if chosen is not None:
            provenance[field] = chosen[0]
            merged[field] = chosen[1]

    merged["ids"] = ids
    merged["provenance"] = provenance
    return merged


# --------------------------------------------------------------------------
# cache
# --------------------------------------------------------------------------

class ResponseCache:
    """Raw API responses under `<topic>/.paperlevelup/cache/<source>/`.

    Two reasons beyond speed: it keeps repeat runs off the remote quota, and it
    makes a session replayable offline, so re-running collect.py after an edit
    does not depend on the network being up.
    """

    def __init__(self, root: Path | None, namespace: str):
        self.dir = Path(root) / namespace if root else None
        self.enabled = self.dir is not None
        self.hits = 0
        self.misses = 0

    def _path(self, key: str) -> Path:
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:40]
        return self.dir / ("%s.json" % digest)

    def get(self, key: str, max_age_days: float | None = None):
        if not self.enabled:
            return None
        path = self._path(key)
        if not path.exists():
            self.misses += 1
            return None
        if max_age_days is not None:
            age = (time.time() - path.stat().st_mtime) / 86400.0
            if age > max_age_days:
                self.misses += 1
                return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            self.misses += 1
            return None
        self.hits += 1
        return payload.get("body")

    def put(self, key: str, body):
        if not self.enabled:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        self._path(key).write_text(
            json.dumps({"key": key, "cached_at": time.time(), "body": body},
                       ensure_ascii=False),
            encoding="utf-8")


# --------------------------------------------------------------------------
# interface
# --------------------------------------------------------------------------

class Source:
    """One metadata backend.

    Subclasses declare `capabilities` and implement only those. Calling an
    unsupported capability raises rather than returning something empty and
    plausible, because a silent empty result would look like "nothing found".
    """

    name: str = "?"
    capabilities: frozenset = frozenset()

    def __init__(self, config: dict | None = None, cache_dir=None):
        self.config = config or {}
        self.cache = ResponseCache(cache_dir, self.name)

    # -- capability stubs ---------------------------------------------------

    def resolve(self, items: list[dict], threshold: float) -> dict[str, dict]:
        """[{key, title, year, venue}] -> {key: match}.

        A match is {matched, score, id, uid, near_miss, record}.
        """
        raise self._unsupported("resolve")

    def search(self, queries: list[str], per_query: int = 30,
               year_min=None, year_max=None, venues=None) -> list[dict]:
        raise self._unsupported("search")

    def references(self, uids: list[str]) -> dict[str, list[str]]:
        """uid -> uids it cites (outgoing edges)."""
        raise self._unsupported("refs")

    def cited_by(self, uids: list[str], limit_per_paper: int = 200) -> dict[str, list[str]]:
        """uid -> uids that cite it (incoming edges)."""
        raise self._unsupported("cites")

    def hydrate(self, uids: list[str]) -> dict[str, dict]:
        """uid -> normalised record."""
        raise self._unsupported("hydrate")

    def pdf(self, uid: str, dest: Path) -> dict:
        """Download if permitted. Returns {ok, reason, url, license}."""
        raise self._unsupported("pdf")

    def pdf_availability(self, record: dict) -> dict:
        """Preflight a hydrated record without downloading it."""
        raise self._unsupported("pdf")

    # -- helpers ------------------------------------------------------------

    def _unsupported(self, capability):
        return SourceError(
            "source %r does not support %r (it has: %s)"
            % (self.name, capability, ", ".join(sorted(self.capabilities)) or "none"))

    def stats(self) -> dict:
        return {"source": self.name,
                "cache_hits": self.cache.hits, "cache_misses": self.cache.misses}
