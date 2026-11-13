import contextlib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import collect
from common import Sidecar
from metadata_overrides import (apply_overrides, load_overrides, save_overrides,
                                set_entries)
from report import render
from sources import merge_records
from sources.openalex import OpenAlexSource


class FakeResolveSource:
    capabilities = frozenset({"resolve"})

    def __init__(self, name, calls):
        self.name = name
        self.calls = calls

    def resolve(self, items, threshold):
        self.calls[self.name] = [dict(item) for item in items]
        return {
            item["key"]: {
                "matched": True,
                "score": 1.0,
                "record": {
                    "source": self.name,
                    "id": "%s-id" % self.name,
                    "title": "Authoritative title",
                    "doi": "10.1/example" if self.name == "openalex" else None,
                },
            }
            for item in items
        }


class MultiSourceCollectionTests(unittest.TestCase):
    def test_every_source_receives_the_full_item_with_year_hint(self):
        calls = {}

        def fake_get_source(name, **_kwargs):
            return FakeResolveSource(name, calls)

        todo = [{"key": "paper.pdf", "title": "A paper",
                 "year": 2024, "venue": "ACL"}]
        with mock.patch.object(collect, "get_source", side_effect=fake_get_source):
            hits, _scores, counts, errors = collect.resolve_sources(
                todo, ["archer", "openalex"], {}, None, 0.85)

        self.assertEqual(errors, [])
        self.assertEqual(counts, {"archer": 1, "openalex": 1})
        self.assertEqual([r["source"] for r in hits["paper.pdf"]],
                         ["archer", "openalex"])
        self.assertEqual(calls["openalex"][0]["year"], 2024)

    def test_merge_keeps_archer_metadata_and_openalex_identity(self):
        merged = merge_records([
            {"source": "archer", "id": "a1", "title": "Curated",
             "authors": ["A"], "abstract": "archer abstract"},
            {"source": "openalex", "id": "W1", "title": "Other",
             "doi": "10.1/example", "cited_by_count": 12},
        ])
        self.assertEqual(
            merged["ids"],
            {"archer": "a1", "openalex": "W1", "doi": "10.1/example"},
        )
        self.assertEqual(merged["title"], "Curated")
        self.assertEqual(merged["cited_by_count"], 12)
        self.assertEqual(merged["provenance"]["title"], "archer")
        self.assertEqual(merged["provenance"]["doi"], "openalex")

    def test_existing_provenance_survives_enrichment_reconstruction(self):
        records = collect.records_from_sidecar({
            "ids": {
                "archer": "a1",
                "openalex": "W1",
                "doi": "10.1/example",
            },
            "resolved_by": ["archer", "openalex"],
            "title": "Curated",
            "cited_by_count": 12,
            "doi": "10.1/example",
            "provenance": {
                "title": "archer",
                "cited_by_count": "openalex",
                "doi": "openalex",
            },
        })
        by_source = {record["source"]: record for record in records}
        self.assertEqual(by_source["archer"]["title"], "Curated")
        self.assertNotIn("cited_by_count", by_source["archer"])
        self.assertEqual(by_source["openalex"]["cited_by_count"], 12)
        self.assertEqual(by_source["openalex"]["doi"], "10.1/example")


class FakeSearchSource:
    capabilities = frozenset({"search"})
    last_errors = []

    def search(self, queries, **_kwargs):
        return [{"source": "openalex", "id": "W1", "uid": "openalex:W1",
                 "title": "Found paper", "year": 2025, "abstract": "text",
                 "matched_queries": list(queries), "best_rank": 1}]

    def stats(self):
        return {"source": "openalex"}


def run_main(module, argv):
    out = io.StringIO()
    with mock.patch.object(sys, "argv", [module.__name__ + ".py"] + argv), \
            contextlib.redirect_stdout(out):
        module.main()
    return json.loads(out.getvalue())


class RetrievalChannelTests(unittest.TestCase):
    def test_search_runs_without_a_topic_folder(self):
        import search
        with mock.patch.object(search, "get_source",
                               side_effect=lambda *_a, **_k: FakeSearchSource()):
            result = run_main(search, ["--keywords", "a query", "--sources", "openalex"])
        self.assertEqual([c["title"] for c in result["candidates"]], ["Found paper"])
        self.assertFalse(result["candidates"][0]["already_in_topic"])

    def test_same_source_duplicate_title_is_not_a_two_source_hit(self):
        import search

        class TwoWorksSameTitle(FakeSearchSource):
            def search(self, queries, **_kwargs):
                return [dict(uid="openalex:W1", source="openalex", id="W1", title="Same Title",
                             best_rank=3, matched_queries=["q"]),
                        dict(uid="openalex:W2", source="openalex", id="W2", title="Same Title",
                             best_rank=1, matched_queries=["q"])]

        with mock.patch.object(search, "get_source",
                               side_effect=lambda *_a, **_k: TwoWorksSameTitle()):
            result = run_main(search, ["--keywords", "q", "--sources", "openalex"])
        [cand] = result["candidates"]
        self.assertEqual(cand["found_by"], ["openalex"])
        self.assertEqual(cand["ids"], {"openalex": "W1"})
        self.assertEqual(cand["uid"], "openalex:W1")
        self.assertEqual(cand["other_ids"], ["openalex:W2"])
        self.assertEqual(cand["rank_in_source"], {"openalex": 1})

    def test_known_ids_lists_archer_ids_for_exclusion(self):
        with tempfile.TemporaryDirectory() as raw:
            sidecar = Sidecar(Path(raw))
            sidecar.entries = {
                "a.pdf": {"key": "a.pdf", "ids": {"archer": "uuid-a"}},
                "b.pdf": {"key": "b.pdf", "ids": {"openalex": "W2"}},
            }
            sidecar.save()
            result = run_main(collect, ["--topic", raw, "--known-ids"])
        self.assertEqual(result["exclude_ids"], ["uuid-a"])
        self.assertEqual(result["papers_in_folder"], 2)


class ReportTests(unittest.TestCase):
    def test_openalex_only_paper_is_identified_not_local(self):
        with tempfile.TemporaryDirectory() as raw:
            sidecar = Sidecar(Path(raw))
            sidecar.entries["paper.pdf"] = {
                "key": "paper.pdf",
                "title": "Paper",
                "ids": {"openalex": "W1"},
            }
            output = render(sidecar)

        self.assertIn("1 identified (0 archer, 1 OpenAlex), 0 local only", output)


class MetadataOverrideTests(unittest.TestCase):
    def test_override_is_evidence_bearing_and_preserves_identity(self):
        with tempfile.TemporaryDirectory() as raw:
            sidecar = Sidecar(Path(raw))
            sidecar.entries["paper.pdf"] = {
                "key": "paper.pdf",
                "ids": {"openalex": "W1"},
                "authors": [],
                "venue": None,
                "provenance": {"title": "openalex"},
            }
            data = {"version": 1, "records": {}}
            set_entries(sidecar, data, [{
                "key": "paper.pdf",
                "authors": ["A. Author", "B. Author"],
                "venue": "Example Venue",
                "evidence": "PDF first page",
            }])
            save_overrides(sidecar, data)
            applied = apply_overrides(sidecar)

            rec = sidecar.entries["paper.pdf"]
            self.assertEqual(rec["ids"], {"openalex": "W1"})
            self.assertEqual(rec["authors"], ["A. Author", "B. Author"])
            self.assertEqual(rec["venue"], "Example Venue")
            self.assertEqual(rec["provenance"]["authors"], "metadata_override")
            self.assertEqual(rec["metadata_override"]["evidence"],
                             "PDF first page")
            self.assertEqual(applied, [{
                "key": "paper.pdf", "fields": ["authors", "venue"]}])

    def test_override_ledger_round_trips(self):
        with tempfile.TemporaryDirectory() as raw:
            sidecar = Sidecar(Path(raw))
            sidecar.entries["paper.pdf"] = {"key": "paper.pdf"}
            data = {"version": 1, "records": {}}
            set_entries(sidecar, data, [{
                "key": "paper.pdf",
                "authors": ["A. Author"],
                "evidence": "PDF title page",
            }])
            save_overrides(sidecar, data)
            self.assertEqual(load_overrides(sidecar), data)

    def test_override_rejects_unknown_pdf_key(self):
        with tempfile.TemporaryDirectory() as raw:
            sidecar = Sidecar(Path(raw))
            with self.assertRaisesRegex(ValueError, "exact topic PDF basename"):
                set_entries(sidecar, {"version": 1, "records": {}}, [{
                    "key": "missing.pdf",
                    "authors": ["A. Author"],
                    "evidence": "PDF first page",
                }])


class OpenAlexPdfTests(unittest.TestCase):
    def test_availability_rejects_unlicensed_pdf(self):
        source = OpenAlexSource(config={"openalex": {}}, cache_dir=None)
        result = source.pdf_availability({
            "is_oa": True,
            "pdf_url": "https://example.test/paper.pdf",
            "license": None,
        })
        self.assertFalse(result["ok"])
        self.assertIn("unknown", result["reason"])

    def test_failed_download_does_not_leave_partial_destination(self):
        source = OpenAlexSource(config={"openalex": {}}, cache_dir=None)
        record = {
            "source": "openalex",
            "id": "W1",
            "uid": "openalex:W1",
            "is_oa": True,
            "pdf_url": "https://example.test/paper.pdf",
            "license": "cc-by",
        }

        class BrokenResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self, size=-1):
                if size == 5:
                    return b"%PDF-"
                raise OSError("connection lost")

        with tempfile.TemporaryDirectory() as raw:
            dest = Path(raw) / "paper.pdf"
            with mock.patch.object(source, "hydrate",
                                   return_value={"openalex:W1": record}), \
                    mock.patch("urllib.request.urlopen",
                               return_value=BrokenResponse()):
                result = source.pdf("openalex:W1", dest)

            self.assertFalse(result["ok"])
            self.assertFalse(dest.exists())
            self.assertEqual(list(Path(raw).glob("*.part")), [])


if __name__ == "__main__":
    unittest.main()
