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
        self.assertNotIn("warning", result)

    def test_known_ids_warns_on_uncollected_folder(self):
        with tempfile.TemporaryDirectory() as raw:
            result = run_main(collect, ["--topic", raw, "--known-ids"])
        self.assertEqual(result["exclude_ids"], [])
        self.assertIn("warning", result)


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

    def test_removing_an_override_restores_the_source_value(self):
        """2026-11 session: --remove left the truncated author list in
        papers.jsonl, still labelled metadata_override."""
        from metadata_overrides import remove_entries, withdraw_overrides
        with tempfile.TemporaryDirectory() as raw:
            sidecar = Sidecar(Path(raw))
            sidecar.entries["paper.pdf"] = {
                "key": "paper.pdf", "ids": {"openalex": "W1"},
                "authors": ["A", "B", "C"], "venue": "Nature",
                "provenance": {"authors": "openalex", "venue": "openalex"},
            }
            data = {"version": 1, "records": {}}
            set_entries(sidecar, data, [{"key": "paper.pdf", "authors": ["A"],
                                         "venue": "Wrong", "evidence": "memory"}])
            apply_overrides(sidecar, data)
            # applying twice must not record the override as its own source
            apply_overrides(sidecar, data)
            self.assertEqual(sidecar.entries["paper.pdf"]["authors"], ["A"])

            # dropping one field keeps the other overridden
            set_entries(sidecar, data, [{"key": "paper.pdf", "venue": "Wrong",
                                         "evidence": "memory"}])
            apply_overrides(sidecar, data)
            rec = sidecar.entries["paper.pdf"]
            self.assertEqual(rec["authors"], ["A", "B", "C"])
            self.assertEqual(rec["provenance"]["authors"], "openalex")
            self.assertEqual(rec["provenance"]["venue"], "metadata_override")

            remove_entries(data, ["paper.pdf"])
            withdrawn = withdraw_overrides(sidecar, data)
            rec = sidecar.entries["paper.pdf"]
            self.assertEqual(withdrawn, [{"key": "paper.pdf", "restored": ["venue"]}])
            self.assertEqual(rec["venue"], "Nature")
            self.assertEqual(rec["provenance"]["venue"], "openalex")
            self.assertNotIn("metadata_override", rec)

    def test_withdrawing_a_legacy_override_marks_it_stale(self):
        from metadata_overrides import withdraw_overrides
        with tempfile.TemporaryDirectory() as raw:
            sidecar = Sidecar(Path(raw))
            sidecar.entries["paper.pdf"] = {
                "key": "paper.pdf", "authors": ["A"],
                "provenance": {"authors": "metadata_override"},
                "metadata_override": {"fields": ["authors"], "evidence": "x"},
            }
            withdrawn = withdraw_overrides(sidecar, {"version": 1, "records": {}})
            rec = sidecar.entries["paper.pdf"]
        self.assertEqual(withdrawn, [{"key": "paper.pdf", "restored": [],
                                      "stale": ["authors"]}])
        self.assertEqual(rec["provenance"]["authors"], "withdrawn_override")

    def test_source_refresh_does_not_feed_overrides_back_into_the_merge(self):
        records = collect.records_from_sidecar({
            "ids": {"openalex": "W1"}, "authors": ["A"], "url": "u",
            "provenance": {"authors": "metadata_override", "url": "openalex"},
        })
        [record] = records
        self.assertNotIn("authors", record)
        self.assertEqual(record["url"], "u")

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


SCIRAG_PAGE_1 = """Proceedings of the 19th Conference of the European Chapter of the Association for Computational Linguistics
V olume 1: Long Papers, pages 6440-6460
March 24-29, 2026 \u00a92026 Association for Computational Linguistics
SciRAG: Adaptive, Citation-Aware, and Outline-Guided Retrieval and
Synthesis for Scientific Literature
Hang Ding S Yilun ZhaoY Tiansheng Hu N Zihang WangN
Abstract
"""


class IdentifierFilenameTests(unittest.TestCase):
    """Publisher download names are identifiers, not titles (2026-11 session:
    three such PDFs all fell through to local-only, and the agent reported a
    paper that was in the library as absent)."""

    def test_identifier_filenames_yield_lookups(self):
        from common import filename_ids, looks_like_title
        self.assertEqual(filename_ids("2026.emnlp-main.589.pdf"), {
            "kind": "acl", "doi": "10.18653/v1/2026.emnlp-main.589",
            "url": "https://aclanthology.org/2026.emnlp-main.589/"})
        self.assertEqual(filename_ids("s41586-025-10072-4.pdf")["doi"],
                         "10.1038/s41586-025-10072-4")
        self.assertEqual(filename_ids("2401.12345v2.pdf")["arxiv"], "2401.12345")
        self.assertEqual(filename_ids("10.1145_3539618.3591717.pdf")["doi"],
                         "10.1145/3539618.3591717")
        self.assertEqual(filename_ids("Attention Is All You Need.pdf"), {})
        self.assertFalse(looks_like_title("2026.emnlp-main.589"))
        self.assertFalse(looks_like_title("s41586-025-10072-4"))
        self.assertTrue(looks_like_title("Attention Is All You Need"))

    def test_title_guess_skips_running_head_and_joins_wrapped_line(self):
        from common import guess_title_from_text
        self.assertEqual(
            guess_title_from_text(SCIRAG_PAGE_1),
            "SciRAG: Adaptive, Citation-Aware, and Outline-Guided Retrieval and "
            "Synthesis for Scientific Literature")

    def test_title_printed_on_page_one_is_accepted_from_a_rough_query(self):
        from sources.base import title_in_head
        title = ("SciRAG: Adaptive, Citation-Aware, and Outline-Guided "
                 "Retrieval and Synthesis for Scientific Literature")
        self.assertTrue(title_in_head(title, SCIRAG_PAGE_1))
        self.assertFalse(title_in_head("ACL2026 Industry Track: Overview",
                                       SCIRAG_PAGE_1))
        self.assertFalse(title_in_head("Abstract", SCIRAG_PAGE_1))

    def test_openalex_tries_the_filename_doi_before_title_search(self):
        source = OpenAlexSource(config={"openalex": {}}, cache_dir=None)
        paths = []

        def fake_get(path, _params, use_cache=True):
            paths.append(path)
            return {"id": "https://openalex.org/W7", "title": "Real Title",
                    "doi": "https://doi.org/10.1038/s41586-025-10072-4"}

        with mock.patch.object(source, "_get", side_effect=fake_get):
            out = source.resolve([{"key": "s41586-025-10072-4.pdf",
                                   "title": "s41586-025-10072-4",
                                   "doi": "10.1038/s41586-025-10072-4"}], 0.85)
        self.assertEqual(paths, ["works/doi:10.1038/s41586-025-10072-4"])
        self.assertTrue(out["s41586-025-10072-4.pdf"]["matched"])
        self.assertEqual(out["s41586-025-10072-4.pdf"]["match_kind"], "doi")

    def test_openalex_accepts_candidate_whose_title_is_on_page_one(self):
        source = OpenAlexSource(config={"openalex": {}}, cache_dir=None)
        real = ("SciRAG: Adaptive, Citation-Aware, and Outline-Guided "
                "Retrieval and Synthesis for Scientific Literature")
        with mock.patch.object(source, "_get", return_value={"results": [
                {"id": "https://openalex.org/W1", "title": "Something else"},
                {"id": "https://openalex.org/W2", "title": real}]}):
            out = source.resolve([{"key": "x.pdf", "title": "SciRAG Adaptive",
                                   "head": SCIRAG_PAGE_1}], 0.85)
        self.assertTrue(out["x.pdf"]["matched"])
        self.assertEqual(out["x.pdf"]["match_kind"], "page_title")
        self.assertEqual(out["x.pdf"]["id"], "W2")

    def test_collect_searches_by_page_title_and_reports_unresolved(self):
        calls = {}

        class Unmatched:
            capabilities = frozenset({"resolve"})

            def __init__(self, name):
                self.name = name

            def resolve(self, items, threshold):
                calls[self.name] = [dict(i) for i in items]
                return {i["key"]: {"matched": False, "score": 0.31,
                                   "near_miss": "ACL2026 Industry Track: Overview"}
                        for i in items}

        with tempfile.TemporaryDirectory() as raw:
            (Path(raw) / "2026.eacl-long.303.pdf").write_bytes(b"%PDF-1.4")
            with mock.patch.object(collect, "get_source",
                                   side_effect=lambda name, **_k: Unmatched(name)), \
                    mock.patch.object(collect, "pdf_text",
                                      return_value=(SCIRAG_PAGE_1, None)):
                result = run_main(collect, ["--topic", raw,
                                            "--sources", "archer,openalex"])
            rec = Sidecar(Path(raw)).entries["2026.eacl-long.303.pdf"]

        sent = calls["archer"][0]
        self.assertTrue(sent["title"].startswith("SciRAG: Adaptive"))
        self.assertEqual(sent["doi"], "10.18653/v1/2026.eacl-long.303")
        self.assertEqual(sent["url"], "https://aclanthology.org/2026.eacl-long.303/")
        self.assertIn("Zihang", sent["head"])
        [unresolved] = result["unresolved"]
        self.assertEqual(unresolved["searched_as_from"], "pdf_text")
        self.assertEqual(
            unresolved["closest_by_source"]["archer"]["near_miss"],
            "ACL2026 Industry Track: Overview")
        self.assertIn("exact_search", result["unresolved_warning"])
        self.assertTrue(rec["title"].startswith("SciRAG: Adaptive"))
        self.assertEqual(rec["provenance"], {"title": "pdf_text_guess"})


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
