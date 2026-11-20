import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

import generate_eval_cases
import iforest_scorer
import log_analytics_sink
import run_evals
from search_evidence import MAX_DOCUMENT_CHARS, format_search_result
from observability.shared.features import RETRIEVAL_FEATURE_NAMES, retrieval_from_row


class SampleObservabilityTests(unittest.TestCase):
    def test_partial_generation_does_not_pass_for_a_document(self):
        incomplete = '{"cases":[{"id":"sample","question":"What?","expected_keywords":["one"]}]}'
        doc = next(generate_eval_cases.DOCS_DIR.rglob("*.txt"))
        with patch.object(generate_eval_cases, "chat_complete", return_value=incomplete):
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(generate_eval_cases.generate_cases_for_doc(doc), [])

    def test_complete_generation_accepts_five_keywords(self):
        doc = next(generate_eval_cases.DOCS_DIR.rglob("*.txt"))
        cases = [
            {"id": f"case-{i}", "question": "What?", "expected_keywords": ["a", "b", "c", "d", "e"],
             "expected_sources": [doc.name]}
            for i in range(2)
        ]
        with patch.object(generate_eval_cases, "chat_complete", return_value=json.dumps({"cases": cases})):
            self.assertEqual(generate_eval_cases.generate_cases_for_doc(doc), cases)

    def test_short_document_evidence_keeps_details_missing_from_caption(self):
        rendered = format_search_result({
            "metadata_storage_name": "sample.txt",
            "source_url": "https://example.test/sample.txt",
            "content": "Weekly checklist: check belt tracking and inspect the rollers.",
            "@search.captions": [SimpleNamespace(text="Weekly checklist")],
        })
        self.assertIn("[sample.txt](https://example.test/sample.txt)", rendered)
        self.assertIn("check belt tracking and inspect the rollers", rendered)

    def test_long_document_evidence_keeps_a_relevant_caption_and_is_bounded(self):
        rendered = format_search_result({
            "content": "a" * MAX_DOCUMENT_CHARS + "Weekly checklist",
            "@search.captions": [SimpleNamespace(text="Weekly checklist")],
        })
        self.assertTrue(rendered.startswith("Weekly checklist"))
        self.assertEqual(len(rendered), MAX_DOCUMENT_CHARS)
    def test_empty_completed_response_fails_the_suite(self):
        cases = [{"id": "empty", "question": "What happened?"}]
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(run_evals, "FOUNDRY_PROJECT_ENDPOINT", "https://example.test"),
            patch.object(run_evals, "EVAL_WARMUP_ENABLED", False),
            patch.object(run_evals, "responses_post", return_value={"status": "completed", "output": []}) as post,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = run_evals.run_evals(cases, False, Path(directory) / "results.json")
        self.assertFalse(result["passed"])
        self.assertIn("case_errors", result["failures"])
        self.assertIn("status='completed' with empty output", result["results"][0]["error"])
        self.assertNotIn("model", post.call_args.args[0])

    def test_failed_managed_memory_recall_cannot_pass_on_aggregate_scores(self):
        cases = [{
            "id": "memory-recall", "category": "memory", "expected_keywords": ["orchid", "winner"],
            "turns": [{"question": "Remember orchid winner"}, {"question": "What was my preference?"}],
        }]
        with (
            tempfile.TemporaryDirectory() as directory,
            patch.object(run_evals, "MEMORY_EVAL_ENABLED", True),
            patch.object(run_evals, "EVAL_WARMUP_ENABLED", False),
            patch.object(run_evals, "EVAL_MEMORY_SETTLE_SECONDS", 0),
            patch.object(run_evals, "call_agent", side_effect=[("OK", [], 20), ("orchid", [], 20)]) as call,
            patch.object(run_evals, "THRESHOLDS", {**run_evals.THRESHOLDS, "keyword_recall": 0.4}),
            patch.object(run_evals, "_log_telemetry_la"),
            contextlib.redirect_stdout(io.StringIO()),
        ):
            result = run_evals.run_evals(cases, False, Path(directory) / "results.json")
        self.assertEqual(result["failures"], ["memory_recall"])
        self.assertEqual(call.call_args_list[0].kwargs["extra_headers"], call.call_args_list[1].kwargs["extra_headers"])

    def test_runtime_telemetry_matches_dcr_and_excludes_query_text(self):
        with patch.object(log_analytics_sink.threading, "Thread") as thread:
            log_analytics_sink.log_async(
                request_id="sample", query="private query", source="memory",
                memory_enabled=True, memory_read_count=2, memory_status="read_ok",
            )
        row, = thread.call_args.kwargs["args"]
        self.assertNotIn("private query", row.values())
        self.assertEqual(row["MemoryReadCount_d"], 2)
        self.assertTrue(row["MemoryEnabled_b"])
        self.assertEqual(row["Source_s"], "memory")

    def test_training_rows_match_hosted_scorer_input(self):
        class CaptureModel:
            def decision_function(self, values):
                self.features = values
                return [0.2]

            def predict(self, values):
                return [1]

        model = CaptureModel()
        with patch.object(iforest_scorer, "_load_model", return_value=model):
            self.assertEqual(iforest_scorer.score([1.0, 1.0], [3.0, 2.0], 24.0), (0.2, False))
        kql_row = dict(zip(RETRIEVAL_FEATURE_NAMES, [1.0, 0.0, np.log(2), 2, 2.5, 24.0], strict=True))
        np.testing.assert_allclose(retrieval_from_row(kql_row), model.features[0])
