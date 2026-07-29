"""The curated library host, reached over SSH.

Authoritative for: PDFs (all of them), curated metadata, and 3.8M *internal*
citation edges. Blind to anything outside its 15 venues.

Note on scope: this class exposes the per-paper capabilities. The topic-level
graph analytics in `graph.py` keep their own remote body, because those are SQL
aggregations best evaluated on the host rather than shipped back edge by edge.
"""
from pathlib import Path
import tempfile

from . import register
from .base import Source, SourceError, make_uid, split_uid

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

REMOTE_SEARCH = r'''
queries = PAYLOAD["queries"]
per_query = PAYLOAD["per_query"]
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
out = []
for pid, terms in hit_terms.items():
    info = meta.get(pid)
    if not info:
        continue
    info = dict(info)
    info["matched_queries"] = sorted(terms)
    info["best_rank"] = best_rank[pid]
    out.append(info)
emit({"candidates": out, "errors": errors})
'''

REMOTE_HYDRATE = r'''
emit({"meta": hydrate(PAYLOAD["ids"])})
'''

REMOTE_EDGES = r'''
ids = PAYLOAD["ids"]
direction = PAYLOAD["direction"]
placeholders = ",".join("?" * len(ids))
out = {}
if direction == "refs":
    rows = db().execute(
        "select source_paper_id, target_paper_id from paper_references "
        "where source_paper_id in (%s)" % placeholders, ids).fetchall()
else:
    rows = db().execute(
        "select target_paper_id, source_paper_id from paper_references "
        "where target_paper_id in (%s)" % placeholders, ids).fetchall()
for anchor, other in rows:
    out.setdefault(anchor, []).append(other)
emit({"edges": out})
'''


@register
class ArcherSource(Source):
    name = "archer"
    capabilities = frozenset({"resolve", "search", "refs", "cites", "hydrate", "pdf"})

    def _run(self, body, payload):
        from common import run_remote  # imported lazily: scripts/ must be on sys.path
        return run_remote(body, payload, config=self.config)

    def _record(self, info: dict) -> dict:
        return {
            "source": self.name,
            "id": info.get("id"),
            "uid": make_uid(self.name, info.get("id")),
            "title": info.get("title"),
            "authors": info.get("authors") or [],
            "venue": info.get("venue"),
            "year": info.get("year"),
            "abstract": info.get("abstract"),
            "url": info.get("url"),
            "library_pdf_path": info.get("pdf_path"),
        }

    def resolve(self, items, threshold):
        if not items:
            return {}
        response = self._run(REMOTE_RESOLVE,
                             {"items": [{"key": i["key"], "title": i.get("title")}
                                        for i in items],
                              "threshold": threshold})
        meta = response.get("meta") or {}
        out = {}
        for result in response.get("results", []):
            key = result["key"]
            if result.get("matched"):
                record = self._record(meta.get(result["id"], {"id": result["id"]}))
                out[key] = {"matched": True, "score": result.get("score"),
                            "id": result["id"], "uid": record["uid"],
                            "record": record}
            else:
                out[key] = {"matched": False, "score": result.get("score"),
                            "near_miss": result.get("near_miss"),
                            "error": result.get("error")}
        return out

    def search(self, queries, per_query=30, year_min=None, year_max=None,
               venues=None):
        response = self._run(REMOTE_SEARCH, {
            "queries": queries, "per_query": per_query,
            "matching_strategy": (self.config or {}).get("matching_strategy", "all"),
        })
        self.last_errors = response.get("errors", [])
        results = []
        for info in response.get("candidates", []):
            record = self._record(info)
            record["matched_queries"] = info.get("matched_queries", [])
            record["best_rank"] = info.get("best_rank", 0)
            year = record.get("year")
            if year_min and (year or 0) < year_min:
                continue
            if year_max and (year or 9999) > year_max:
                continue
            if venues and (record.get("venue") or "").lower() not in {
                    v.lower() for v in venues}:
                continue
            results.append(record)
        return results

    def hydrate(self, uids):
        idents = [split_uid(u, default=self.name)[1] for u in uids]
        if not idents:
            return {}
        response = self._run(REMOTE_HYDRATE, {"ids": idents})
        return {make_uid(self.name, pid): self._record(info)
                for pid, info in (response.get("meta") or {}).items()}

    def _edges(self, uids, direction):
        idents = [split_uid(u, default=self.name)[1] for u in uids]
        if not idents:
            return {}
        response = self._run(REMOTE_EDGES, {"ids": idents, "direction": direction})
        edges = response.get("edges") or {}
        return {make_uid(self.name, anchor): [make_uid(self.name, o) for o in others]
                for anchor, others in edges.items()}

    def references(self, uids):
        return self._edges(uids, "refs")

    def cited_by(self, uids, limit_per_paper=200):
        out = self._edges(uids, "cites")
        return {k: v[:limit_per_paper] for k, v in out.items()}

    def pdf_availability(self, record: dict) -> dict:
        remote_path = record.get("library_pdf_path")
        if not remote_path:
            return {"ok": False, "reason": "no PDF path recorded on the host"}
        return {"ok": True, "url": remote_path}

    def pdf(self, uid: str, dest: Path) -> dict:
        from common import scp_from_remote
        record = self.hydrate([uid]).get(
            make_uid(self.name, split_uid(uid, default=self.name)[1]))
        if not record:
            return {"ok": False, "reason": "not found in the library"}
        availability = self.pdf_availability(record)
        if not availability.get("ok"):
            return availability
        remote_path = availability["url"]
        root = (self.config or {}).get("pdf_root")
        if root and not str(remote_path).startswith("/"):
            remote_path = "%s/%s" % (root.rstrip("/"), remote_path)
        if dest.exists():
            return {"ok": False, "reason": "destination already exists"}
        temp_path = None
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                    prefix=dest.name + ".", suffix=".part",
                    dir=dest.parent, delete=False) as fh:
                temp_path = Path(fh.name)
            scp_from_remote(remote_path, temp_path, config=self.config)
            temp_path.replace(dest)
        except Exception as exc:
            if temp_path:
                temp_path.unlink(missing_ok=True)
            return {"ok": False, "reason": str(exc)[:200]}
        return {"ok": True, "url": remote_path, "record": record}
