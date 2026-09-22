"""Offline tests: temporary databases only, no live human annotations or model requests."""

import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from starlette.applications import Starlette
from starlette.testclient import TestClient
from research_assistant.web.lab_api import make_lab_routes
from research_assistant.retrieval.embeddings.remote_embeddings import RemoteQwenEmbeddings
from research_assistant.evaluation.retrieval_evaluation import (EvaluationStore, corpus_snapshot, display_ranking, human_summary,
                                  score_ranking, summarize, validate_dataset, run_evaluation)
from research_assistant.storage.table_store import TableStore, parse_table_html


class TableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.library = self.root / "library"
        self.library.mkdir()
        self.pdf = self.library / "sample.PDF"
        self.pdf.write_bytes(b"fixture")
        self.cache = self.root / "cache"
        self.cache.mkdir()
        self.content = self.cache / "content_list.json"
        self.extractor = SimpleNamespace(cache_dir=self.cache, cache_path=lambda _: self.cache,
                                        has_cache=lambda _: self.content.exists(), _find_content_list=lambda _: self.content)
        self.store = TableStore(self.root / "tables.sqlite3")
        self.write_block("<table><tr><th>Group</th><th>Score</th></tr><tr><td>A</td><td>0.8</td></tr></table>")

    def tearDown(self):
        self.tmp.cleanup()

    def write_block(self, html):
        self.content.write_text(json.dumps([{"type": "table", "page_idx": 6, "table_body": html,
                                           "table_caption": ["Performance"], "bbox": [0, 0, 100, 100]}]), encoding="utf-8")

    def sync(self):
        return self.store.sync(self.library, self.extractor)

    def test_spans_preserve_anchor_without_duplicating_values(self):
        p = parse_table_html('<table><tr><th rowspan="2">A</th><td colspan="2">2</td></tr><tr><td>3</td><td>4</td></tr></table>')
        self.assertEqual((p["status"], p["rows"], p["columns"], len(p["cells"])), ("ok", 2, 3, 4))
        self.assertEqual(p["cells"][2]["column_index"], 1)
        self.assertEqual(sum(c["numeric_value"] or 0 for c in p["cells"]), 9)

    def test_missing_invalid_ragged_and_numeric(self):
        self.assertEqual(parse_table_html("")["status"], "missing_html")
        self.assertEqual(parse_table_html('<table><tr><td rowspan="-1">x</td></tr></table>')["status"], "invalid_html")
        self.assertEqual(parse_table_html('<table><tr><td><table></table></td></tr></table>')["status"], "invalid_html")
        self.assertEqual(parse_table_html('<table><tr><td>A</td><td>B</td></tr><tr><td>C</td></tr></table>')["status"], "ragged")
        values = ["40%", "5 mg", "1,000", "9" * 400, "-0.75", "&lt;script&gt;"]
        p = parse_table_html("<table><tr>" + "".join(f"<td>{v}</td>" for v in values) + "</tr></table>")
        self.assertEqual([c["numeric_value"] for c in p["cells"]], [None, None, None, None, -0.75, None])
        self.assertEqual(p["cells"][-1]["text"], "<script>")

    def test_incremental_replace_remove_and_page(self):
        self.assertEqual(self.sync()["cells"], 4)
        first = self.store.search()[0]
        self.assertEqual(first["page"], 7)
        self.assertEqual(self.sync()["skipped_sources"], 1)
        self.write_block("<table><tr><td>B</td></tr></table>")
        self.assertEqual(self.sync()["cells"], 1)
        self.assertIsNone(self.store.get(first["table_id"]))
        self.pdf.unlink()
        self.assertEqual(self.sync()["tables"], 0)

    def test_missing_cache_explicitly_invalidates_old_tables(self):
        self.sync()
        self.content.unlink()
        self.assertEqual(self.sync()["tables"], 0)
        self.assertEqual(self.store.stats()["sources"][0]["status"], "not_parsed")

    def test_sql_readonly_and_literal_keywords(self):
        self.sync()
        self.assertEqual(self.store.query("SELECT count(*) FROM research_tables WHERE source LIKE 'sample%'")["rows"], [[1]])
        self.assertEqual(self.store.query("WITH x AS (SELECT numeric_value FROM table_cells) SELECT sum(numeric_value) FROM x")["rows"], [[0.8]])
        self.assertEqual(self.store.query("SELECT 'drop table' FROM research_tables")["rows"], [["drop table"]])
        attacks = ["DELETE FROM table_cells", "SELECT 1; SELECT 2", "PRAGMA table_info(table_cells)",
                   "SELECT name FROM sqlite_master", "ATTACH DATABASE 'x' AS x", "SELECT load_extension('x')",
                   "WITH x AS (SELECT 1) DELETE FROM table_cells", "SELECT randomblob(1000000000)",
                   "SELECT * FROM pragma_table_info('table_cells')"]
        for sql in attacks:
            with self.subTest(sql=sql), self.assertRaises(ValueError): self.store.query(sql)
        self.assertEqual(self.store.stats()["cells"], 4)

    def test_sql_bounded_rows_and_keyword_search(self):
        self.sync()
        result = self.store.query("SELECT * FROM table_cells", limit=1)
        self.assertEqual(len(result["rows"]), 1)
        self.assertTrue(result["truncated"])
        self.assertEqual(len(self.store.query("SELECT * FROM table_cells", limit=-1)["rows"]), 1)
        self.assertEqual(len(self.store.search("performance")), 1)
        self.assertEqual(self.store.search(offset=1), [])
        # The token 'or' legitimately matches 'Performance'; it is not executed as SQL.
        self.assertEqual(self.store.search("' OR 1=1"), self.store.search("or"))
        self.assertEqual(self.store.search(source="' OR 1=1"), [])

    def test_expensive_sql_is_interrupted(self):
        self.sync()
        sql = "SELECT count(*) FROM " + " CROSS JOIN ".join(f"table_cells t{i}" for i in range(8))
        with patch('research_assistant.storage.table_store.time.monotonic', side_effect=[0, 3]):
            with self.assertRaisesRegex(ValueError, "interrupted"):
                self.store.query(sql)

    def test_api_sql_and_traversal(self):
        self.sync()
        def safe(source):
            path = (self.library / source).resolve()
            if not path.is_relative_to(self.library): raise ValueError("Invalid path")
            return path
        routes = make_lab_routes(self.root, lambda _: None, safe)
        with patch('research_assistant.storage.table_store.MinerUExtractor', return_value=self.extractor), TestClient(Starlette(routes=routes)) as client:
            response = client.get("/api/tables")
            self.assertEqual(response.status_code, 200)
            table_id = response.json()["tables"][0]["table_id"]
            self.assertEqual(client.get("/api/tables/" + table_id).json()["cells"][3]["numeric_value"], 0.8)
            self.assertEqual(client.post("/api/tables/sql", json={"sql": "SELECT count(*) FROM table_cells"}).json()["rows"], [[4]])
            self.assertEqual(client.post("/api/tables/sql", json={"sql": "DELETE FROM table_cells"}).status_code, 400)
            self.assertEqual(client.post("/api/tables/sql", json={"sql": "SELECT 1"}, headers={"Origin": "https://untrusted.invalid"}).status_code, 400)
            self.assertEqual(client.get("/api/document", params={"source": "../secret.pdf"}).status_code, 404)
            (self.library / "note.md").write_text("<script>unsafe()</script>", encoding="utf-8")
            response = client.get("/api/document", params={"source": "note.md"})
            self.assertEqual(response.status_code, 200)
            self.assertIn("text/plain", response.headers["content-type"])


def fixture_case():
    return {"id": "C1", "query_type": "mixed", "input_modality": "text", "question": "test", "split": "development",
            "expected": [{"source": "a.pdf", "page": 1, "modality": "text", "any_of": ["t1", "t2"]},
                         {"source": "a.pdf", "page": 2, "modality": "image", "any_of": ["i1"]}]}


def hit(pid, page=1, source="a.pdf", score=.8):
    return {"point_id": pid, "source": source, "page": page, "score": score}


class EvaluationTests(unittest.TestCase):
    def test_groups_mrr_page_and_empty_ranking(self):
        c = fixture_case()
        metrics = score_ranking(c, [hit("wrong"), hit("t1"), hit("t2"), hit("i1", 2)])
        self.assertEqual(metrics["recall@3"], .5)
        self.assertEqual(metrics["recall@5"], 1)
        self.assertEqual(metrics["mrr@10"], .5)
        self.assertEqual(metrics["page_recall@1"], .5)
        self.assertEqual(score_ranking(c, [hit("wrong", 1, "other.pdf")])["page_recall@1"], 0)
        self.assertEqual(score_ranking(c, [])["mrr@10"], 0)

    def test_source_cap_is_separate_from_raw_recall(self):
        ranking = [hit("wrong1"), hit("wrong2"), hit("wrong3"), hit("t1"), hit("i1", 2)]
        self.assertEqual(score_ranking(fixture_case(), ranking)["recall@10"], 1)
        self.assertEqual(score_ranking(fixture_case(), display_ranking(ranking, .35))["recall@10"], 0)

    def test_no_silent_gold_failures(self):
        c = fixture_case()
        catalog = {"t1": {"modality": "text", "source": "a.pdf", "page": 1},
                   "t2": {"modality": "text", "source": "a.pdf", "page": 1},
                   "i1": {"modality": "image", "source": "a.pdf", "page": 2}}
        data = {"schema": 1, "corpus": {"test": 1}, "cases": [c]}
        with patch('research_assistant.evaluation.retrieval_evaluation.evidence_catalog', return_value=catalog), patch('research_assistant.evaluation.retrieval_evaluation.corpus_snapshot', return_value={"test": 1}):
            validate_dataset(data, Path("."))
            for change in (lambda d: d.update(corpus={}), lambda d: d["cases"][0].update(expected=[]),
                           lambda d: d["cases"][0]["expected"][0].update(any_of=["missing"]),
                           lambda d: d["cases"][0].update(input_modality="image")):
                modified = copy.deepcopy(data); change(modified)
                with self.assertRaises(ValueError): validate_dataset(modified, Path("."))

    def test_refuses_fallback(self):
        with patch('research_assistant.evaluation.retrieval_evaluation.validate_dataset', return_value={}):
            with self.assertRaisesRegex(ValueError, "fallback"):
                run_evaluation(Path("."), SimpleNamespace(unified_index=None), {"cases": []})

    def test_missing_judgments_are_not_zero_and_probe_excluded(self):
        c = fixture_case()
        c["rankings"] = {"raw": [hit("t1"), hit("i1", 2)], "displayed": [hit("t1"), hit("i1", 2)]}
        c["metrics"] = {m: score_ranking(c, ranks) for m, ranks in c["rankings"].items()}
        self.assertIsNone(human_summary([c], [])["all"]["mean_grade"])
        h = human_summary([c], [{"case_id": "C1", "point_id": "t1", "grade": 3}])["all"]
        self.assertEqual((h["mean_grade"], h["coverage"]), (3, .5))
        probe = {**c, "id": "probe", "split": "self_match_probe"}
        self.assertEqual(summarize([c, probe])["draft_development"]["all"]["count"], 1)
        self.assertEqual(summarize([c])["human_approved_development"]["all"]["count"], 0)

    def test_judgments_persist_and_approvals_are_versioned(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = EvaluationStore(Path(tmp) / "eval.sqlite3")
            c = fixture_case(); ranks = [hit("t1")]
            c["rankings"] = {"raw": ranks, "displayed": ranks}
            c["metrics"] = {m: score_ranking(c, ranks) for m in c["rankings"]}
            run = {"run_id": "r1", "created_at": "today", "dataset_hash": "v1", "cases": [c]}
            store.save(run)
            with self.assertRaises(ValueError): store.judge("r1", {"case_id": "C1", "point_id": "t1", "grade": 3})
            store.judge("r1", {"case_id": "C1", "point_id": "t1", "grade": 3, "reviewer": "test-fixture"})
            store.judge("r1", {"case_id": "C1", "action": "approve_gold", "reviewer": "test-fixture"})
            self.assertEqual(store.get("r1")["human_relevance"]["raw"]["all"]["mean_grade"], 3)
            store.save({**run, "run_id": "r2", "dataset_hash": "v2"})
            self.assertEqual(store.get("r2")["approvals"], [])
            with self.assertRaises(ValueError): store.judge("r1", {"case_id": "C1", "point_id": "unknown", "grade": 3, "reviewer": "test-fixture"})
            with self.assertRaises(ValueError): store.judge("r1", {"case_id": "C1", "point_id": "t1", "grade": True, "reviewer": "test-fixture"})

    def test_pure_image_omits_empty_text_and_mixed_is_one_input(self):
        model = RemoteQwenEmbeddings.__new__(RemoteQwenEmbeddings)
        requests = []
        model._embed = lambda items: requests.append(items) or [[1.0]]
        with tempfile.TemporaryDirectory() as tmp:
            image = Path(tmp) / "query.png"; image.write_bytes(b"fixture")
            model.embed_query("", image)
            model.embed_query("scientific diagram", image)
        self.assertNotIn("text", requests[0][0])
        self.assertEqual(requests[1][0]["text"], "scientific diagram")
        self.assertIn("image_base64", requests[1][0])
        self.assertEqual(len(requests[1]), 1)


if __name__ == "__main__":
    unittest.main()
