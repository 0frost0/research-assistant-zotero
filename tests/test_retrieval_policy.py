"""Policy regressions use synthetic evidence, no external models or human judgments."""

import copy
import unittest
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from langchain_core.documents import Document
from research_assistant.retrieval.knowledge_base import LiteratureIndex, SearchResult, split_documents
from research_assistant.agents.research_agent import research_prompt
from research_assistant.core.models import ResearchContext
from research_assistant.evaluation.retrieval_evaluation import compare_policies, run_evaluation, score_context_ranking, score_ranking
from research_assistant.retrieval.retrieval_policy import CONTEXT_LIMIT, EvidenceContext, select_evidence
from research_assistant.retrieval.unified_index import UnifiedSearchHit


def hit(pid, text="Evidence", kind="text", modality="text", score=.8, page=3, bbox=None):
    return {"point_id": pid, "source": "paper.pdf", "file_type": ".pdf", "page": page,
            "text": text, "content_type": kind, "modality": modality, "score": score,
            "chunk_index": 99, "bbox": bbox, "image_path": None}


def case():
    return {"id": "C1", "question": "What is the finding?", "query_type": "text", "input_modality": "text",
            "expected": [{"source": "paper.pdf", "page": 3, "modality": "text", "any_of": ["answer"]}]}


class RetrievalPolicyTests(unittest.TestCase):
    def setUp(self):
        self.text = " ".join(f"Clinical observation {i}." for i in range(35)) + " Missing-value indicators are retained."
        self.document = Document(page_content=self.text, metadata={"source": "paper.pdf", "file_type": ".pdf",
            "page": 3, "content_type": "text", "block_index": 7, "bbox": [10, 20, 80, 90]})
        self.chunks = split_documents([self.document])
        self.context = EvidenceContext([self.document], self.chunks)

    def candidate(self, i, pid):
        return hit(pid, self.chunks[i].page_content, bbox=self.document.metadata["bbox"])

    def test_expands_exact_anchor_to_complete_source_block(self):
        results = select_evidence([self.candidate(0, "answer")], .35, context=self.context)
        self.assertEqual(results[0]["retrieval_policy"], "research-context-v2")
        self.assertEqual(results[0]["text"], self.text)
        self.assertNotIn("Missing-value indicators", results[0]["retrieved_text"])
        self.assertTrue(results[0]["context_span"]["complete_block"])
        self.assertEqual(results[0]["context_span"]["block_index"], 7)
        self.assertEqual(results[0]["point_id"], "answer")

    def test_dedup_preserves_only_actually_delivered_candidate_ids(self):
        candidates = [self.candidate(0, "first"), hit("elsewhere", "Other evidence", page=9), self.candidate(1, "answer")]
        results = select_evidence(candidates, .35, limit=1, context=self.context)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["context_evidence_ids"], ["first", "answer"])
        self.assertEqual(score_ranking(case(), results)["recall@1"], 0)
        self.assertEqual(score_context_ranking(case(), results)["context_recall@1"], 1)
        self.assertNotIn("elsewhere", results[0]["context_evidence_ids"])

    def test_headers_removed_without_capping_source(self):
        candidates = [hit("header", "Repeated title", kind="header"), hit("footer", kind="footer")]
        candidates += [hit(str(i), f"Different finding {i}") for i in range(8)]
        results = select_evidence(candidates, .35, limit=6)
        self.assertEqual([r["point_id"] for r in results], [str(i) for i in range(6)])

    def test_visual_transcript_not_sent_as_quote_image_still_returned(self):
        candidates = [hit("transcript", "A --> B", kind="image"),
                      hit("image", "A --> B; ~999", kind="image", modality="image"),
                      hit("chart", "~123", kind="chart"), hit("body", "Actual method.")]
        results = select_evidence(candidates, .35)
        self.assertEqual([r["point_id"] for r in results], ["image", "body"])
        self.assertEqual(results[0]["text"], "")
        self.assertIn("~999", results[0]["retrieved_text"])
        self.assertTrue(results[0]["quality_warnings"])

    def test_table_uncertainty_is_explicit(self):
        results = select_evidence([hit("table", "<table><tr><td>42</td></tr></table>", kind="table")], .35)
        self.assertIn("42", results[0]["text"])
        self.assertIn("数值", results[0]["quality_warnings"][0])

    def test_source_page_bbox_and_content_must_match(self):
        for changes in ({"source": "other.pdf"}, {"page": 2}, {"bbox": [0, 0, 0, 0]}, {"text": "changed"}):
            value = {**self.candidate(0, "answer"), **changes}
            text, span = self.context.expand(value)
            self.assertEqual(text, value["text"])
            self.assertIsNone(span)

    def test_context_never_uses_stale_global_chunk_ordinal(self):
        value = self.candidate(0, "answer")
        value["chunk_index"] = 1000000
        self.assertEqual(self.context.expand(value)[0], self.text)

    def test_ambiguous_chunk_does_not_expand(self):
        other = Document(page_content=self.document.page_content, metadata={**self.document.metadata, "block_index": 8})
        context = EvidenceContext([self.document, other], self.chunks + split_documents([other]))
        self.assertIsNone(context.expand(self.candidate(0, "answer"))[1])

    def test_long_context_is_bounded_and_marked(self):
        document = Document(page_content=" ".join(f"Observed result {i}." for i in range(400)), metadata=self.document.metadata)
        chunks = split_documents([document])
        value = hit("long", chunks[8].page_content, bbox=self.document.metadata["bbox"])
        result = select_evidence([value], .35, context=EvidenceContext([document], chunks))[0]
        self.assertLessEqual(len(result["text"]), CONTEXT_LIMIT)
        self.assertIn(value["text"], result["text"])
        self.assertFalse(result["context_span"]["complete_block"])
        self.assertTrue(result["quality_warnings"])

    def test_input_metadata_and_raw_ranking_are_unchanged(self):
        ranking = [self.candidate(0, "answer")]
        original = copy.deepcopy(ranking)
        metadata = copy.deepcopy(self.chunks[0].metadata)
        select_evidence(ranking, .35, context=self.context)
        self.assertEqual(ranking, original)
        self.assertEqual(self.chunks[0].metadata, metadata)

    def test_threshold_applies_to_grouped_ids_too(self):
        low = self.candidate(1, "answer"); low["score"] = .1
        results = select_evidence([self.candidate(0, "first"), low], .35, context=self.context)
        self.assertEqual(results[0]["context_evidence_ids"], ["first"])

    def test_production_queries_only_once_and_propagates_filters(self):
        index = LiteratureIndex.__new__(LiteratureIndex)
        index.evidence_context = self.context
        index.unified_index = SimpleNamespace(search=Mock(return_value=[UnifiedSearchHit(**self.candidate(0, "answer"))]))
        results = index._unified_search_results("method", k=5, min_score=.35, sources=["paper.pdf"], file_types=[".pdf"])
        self.assertEqual(index.unified_index.search.call_count, 1)
        self.assertEqual(index.unified_index.search.call_args.kwargs["sources"], ["paper.pdf"])
        self.assertEqual(results[0].quote, self.text)
        self.assertEqual(results[0].context_evidence_ids, ["answer"])

    def test_format_does_not_leak_unverified_transcription_into_generation(self):
        result = SearchResult(source="paper.pdf", page=3, quote="", score=.9, backend="multimodal-unified", chunk_id=0,
            modality="image", retrieved_text="fabricated graph ~999", quality_warnings=["Unverified visual transcription"])
        prompt = LiteratureIndex.format_results([result])
        self.assertNotIn("~999", prompt)
        self.assertIn("Unverified visual transcription", prompt)
        self.assertIn("MODALITY: image", prompt)
        self.assertIn("并未看到图像", research_prompt(ResearchContext(research_topic="test", weekly_hours=1)))

    def test_evaluator_uses_current_policy_and_saves_frozen_baseline(self):
        ranked = [UnifiedSearchHit(**hit("header", "Repeated header", kind="header")),
                  UnifiedSearchHit(**self.candidate(0, "answer"))]
        search = Mock(return_value=ranked)
        unified = SimpleNamespace(search=search, _contains_points=lambda ids: True, model_name="fixture", dimension=2,
                                  collection_name="fixture", embeddings=SimpleNamespace(profile={}))
        index = SimpleNamespace(unified_index=unified, _expanded_query=lambda q: q, evidence_context=self.context)
        dataset = {"id": "test", "corpus": {}, "cases": [case()]}
        with patch('research_assistant.evaluation.retrieval_evaluation.validate_dataset', return_value={}), patch('research_assistant.evaluation.retrieval_evaluation.corpus_snapshot', return_value={}):
            run = run_evaluation(Path("."), index, dataset)
        self.assertEqual(search.call_count, 1)
        self.assertEqual(run["cases"][0]["rankings"]["raw"][0]["point_id"], "header")
        self.assertEqual(run["cases"][0]["baseline"]["ranking"][0]["point_id"], "header")
        self.assertEqual(run["cases"][0]["rankings"]["displayed"][0]["text"], self.text)
        self.assertEqual(run["policy_comparison"]["status"], "no_context_recall_regression")
        self.assertEqual(run["suitability"], "not_established")

    def test_comparison_flags_missing_evidence_without_human_approval(self):
        c = case()
        c["rankings"] = {"raw": [hit("answer")], "displayed": []}
        c["metrics"] = {"raw": score_ranking(c, [hit("answer")]),
                        "displayed": {**score_ranking(c, []), **score_context_ranking(c, [])}}
        c["baseline"] = {"ranking": [hit("answer")], "metrics": score_ranking(c, [hit("answer")])}
        result = compare_policies([c])
        self.assertEqual(result["regressions"], ["C1"])
        self.assertEqual(result["status"], "needs_review")


if __name__ == "__main__":
    unittest.main()
