import json
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[2]))
sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from starlette.testclient import TestClient

from agent_runtime.metrics import RunMetrics, TokenUsage, append_run_metrics
from webui import server
from webui.board import BoardStore
from webui.evaluation import (
    EvaluationNotFound,
    EvaluationRequestError,
    EvaluationStore,
    _day,
)
from webui.sessions import SessionStore


def _record(run_id, **kwargs):
    now = time.time()
    defaults = dict(run_id=run_id, origin="webui", harness_id="coding-v1",
                    model="model-a", started_at=now - 1.0, finished_at=now,
                    duration_ms=1000.0)
    defaults.update(kwargs)
    return RunMetrics(**defaults)


class EvaluationStoreTests(unittest.TestCase):
    """Index, metric口径, and degradation over real files."""

    def setUp(self):
        self._root = TemporaryDirectory()
        self.addCleanup(self._root.cleanup)
        self.root = Path(self._root.name)
        self.sessions = SessionStore(self.root / "sessions")
        self.board = BoardStore(self.root)
        self.board.load()
        self.runs_path = self.root / "metrics" / "runs.jsonl"

    def _store(self, records_dir=None):
        # Default to an absent directory: the repo's own records must not
        # leak into a unit test.
        return EvaluationStore(
            self.sessions, self.board, root=self.root,
            records_dir=records_dir or (self.root / "records"))

    def _append(self, *records):
        for record in records:
            append_run_metrics(record, self.runs_path)

    def _raw(self, *lines):
        self.runs_path.parent.mkdir(parents=True, exist_ok=True)
        with self.runs_path.open("a", encoding="utf-8") as stream:
            for line in lines:
                stream.write(line + "\n")

    # -- empty ----------------------------------------------------------

    def test_empty_sources_report_none_not_zero(self):
        store = self._store()
        overview = store.overview()
        self.assertEqual(overview["kpis"]["runs"], 0)
        # An empty denominator is "not measured", never "0% success".
        self.assertIsNone(overview["kpis"]["tcr"])
        self.assertIsNone(overview["kpis"]["duration_p95_ms"])
        self.assertEqual(overview["trends"], [])

        board = store.board_overview()
        self.assertEqual(board["funnel"], {"created": 0, "executed": 0,
                                           "run_completed": 0, "review": 0,
                                           "done": 0})
        self.assertIsNone(board["kpis"]["tsr"])
        self.assertIsNone(board["kpis"]["vcr"])
        self.assertIsNone(board["artifact"]["hallucinated_rate"])
        self.assertEqual(store.benchmarks()["records"], [])
        self.assertEqual(store.diagnoses()["sessions"], [])

    # -- run level ------------------------------------------------------

    def test_overview_histograms_use_the_fixed_buckets(self):
        self._append(
            _record("run-a", iterations=0, duration_ms=400.0),
            _record("run-b", iterations=2, duration_ms=1_000.0),
            _record("run-c", iterations=4, duration_ms=9_000.0),
            _record("run-d", status="error", iterations=40,
                    duration_ms=900_000.0),
        )
        histograms = self._store().overview()["histograms"]
        self.assertEqual(
            histograms["iterations"],
            [{"label": "0–1", "count": 1}, {"label": "2", "count": 1},
             {"label": "3–4", "count": 1}, {"label": "5–8", "count": 0},
             {"label": "9–16", "count": 0}, {"label": "17–32", "count": 0},
             {"label": "33+", "count": 1}])
        # Every run lands in exactly one duration bucket, edges included.
        rows = histograms["duration_ms"]
        self.assertEqual([row["count"] for row in rows], [1, 1, 1, 0, 0, 1])
        self.assertEqual(sum(row["count"] for row in rows), 4)

    def test_overview_slices_by_origin_and_keeps_diagnosis_out(self):
        self._append(
            _record("run-a", status="completed", duration_ms=1000.0,
                    ttft_ms=200.0, tool_calls=4, tool_errors=1,
                    error_kinds={"validation": 1},
                    tokens=TokenUsage(prompt=100, completion=10,
                                      cached=40),
                    summary_attempts=2, summary_invalid=1),
            _record("run-b", origin="cli", status="error", harness_id="terminal-v1",
                    duration_ms=3000.0, retries=2,
                    error={"stage": "llm", "code": "timeout"}),
            _record("run-c", origin="diagnosis", status="completed"),
        )
        store = self._store()
        overview = store.overview()
        kpis = overview["kpis"]
        self.assertEqual(kpis["runs"], 2)
        self.assertEqual(overview["excluded"]["runs_by_origin"], 1)
        self.assertEqual(kpis["tcr"], 0.5)
        self.assertEqual(kpis["error_rate"], 0.5)
        self.assertEqual(kpis["tool_error_rate"], 0.25)
        self.assertEqual(kpis["schema_pass_rate"], 0.75)
        self.assertEqual(kpis["retry_rate"], 0.5)
        self.assertEqual(kpis["summary_invalid_rate"], 0.5)
        # ``cached`` is a subset of ``prompt``: 100 + 10 + 0.
        self.assertEqual(kpis["tokens_total"], 110)
        self.assertEqual(kpis["ttft_p50_ms"], 200.0)
        self.assertEqual(overview["errors"],
                         [{"stage": "llm", "code": "timeout", "count": 1}])
        self.assertEqual(overview["error_kinds"],
                         [{"kind": "validation", "count": 1}])
        slices = {row["key"]: row for row in overview["slices"]["origin"]}
        self.assertEqual(slices["cli"]["runs"], 1)
        self.assertNotIn("diagnosis", slices)
        self.assertEqual(overview["trends"][0]["runs"], 2)

    def test_trends_bucket_runs_by_local_day(self):
        now = time.time()
        self._append(
            _record("run-a", finished_at=now - 2 * 86_400,
                    started_at=now - 2 * 86_400, status="completed"),
            _record("run-b", finished_at=now - 2 * 86_400 + 60,
                    started_at=now - 2 * 86_400 + 60, status="error"),
            _record("run-c", finished_at=now, started_at=now,
                    status="completed"))
        trends = self._store().overview(range_="all")["trends"]
        self.assertEqual([row["day"] for row in trends],
                         sorted({_day(now - 2 * 86_400), _day(now)}))
        first = trends[0]
        self.assertEqual((first["runs"], first["errors"], first["tcr"]),
                         (2, 1, 0.5))

    def test_the_day_cache_matches_strftime_across_midnight(self):
        midnight = time.mktime((2026, 3, 1, 0, 0, 0, 0, 0, -1))
        for offset in (-3600.5, -60.5, -0.5, 0.0, 0.5, 60.5, 3600.5):
            stamp = midnight + offset
            self.assertEqual(_day(stamp),
                             time.strftime("%Y-%m-%d",
                                           time.localtime(stamp)))

    def test_an_explicit_origin_filter_narrows_the_scope(self):
        self._append(_record("run-a", origin="webui"),
                     _record("run-b", origin="harbor"))
        store = self._store()
        overview = store.overview(origins=["harbor"])
        self.assertEqual(overview["kpis"]["runs"], 1)
        self.assertEqual(overview["origins"], ["harbor"])
        with self.assertRaises(EvaluationRequestError):
            store.overview(origins=["nope"])

    def test_range_and_harness_filters(self):
        now = time.time()
        self._append(_record("run-old", finished_at=now - 10 * 86_400,
                             started_at=now - 10 * 86_400),
                     _record("run-new", finished_at=now, started_at=now,
                             harness_id="terminal-v1"))
        store = self._store()
        self.assertEqual(store.overview()["kpis"]["runs"], 1)
        self.assertEqual(store.overview(range_="all")["kpis"]["runs"], 2)
        self.assertEqual(
            store.overview(range_="all", harness="terminal-v1")["kpis"]["runs"], 1)
        with self.assertRaises(EvaluationRequestError):
            store.overview(range_="yesterday")

    def test_incremental_refresh_reads_only_new_bytes(self):
        self._append(_record("run-a", status="completed"))
        store = self._store()
        self.assertEqual(store.overview()["kpis"]["runs"], 1)
        self._append(_record("run-b", status="error"))
        refreshed = store.refresh()
        self.assertEqual(refreshed["runs"], 2)
        self.assertEqual(refreshed["runs_added"], 1)
        self.assertEqual(store.overview()["kpis"]["runs"], 2)

    def test_a_half_written_line_is_read_once_it_is_complete(self):
        self._append(_record("run-a"))
        store = self._store()
        store.refresh()
        partial = json.dumps(_record("run-b").to_dict())
        marker = partial.index('"status"')
        with self.runs_path.open("a", encoding="utf-8") as stream:
            stream.write(partial[:marker])
        store.refresh()
        self.assertEqual(store.overview()["kpis"]["runs"], 1)
        with self.runs_path.open("a", encoding="utf-8") as stream:
            stream.write(partial[marker:] + "\n")
        store.refresh()
        self.assertEqual(store.overview()["kpis"]["runs"], 2)

    def test_a_truncated_file_is_reloaded(self):
        self._append(_record("run-a"), _record("run-b"))
        store = self._store()
        self.assertEqual(store.overview()["kpis"]["runs"], 2)
        append_run_metrics(_record("run-c"), self.runs_path)
        self.assertEqual(store.refresh()["runs"], 3)
        # Rotation: the file shrank, so the index must not keep stale rows.
        self.runs_path.write_text(json.dumps(_record("run-d").to_dict()) + "\n",
                                  encoding="utf-8")
        self.assertEqual(store.refresh()["runs"], 1)
        self.assertEqual(store.overview()["kpis"]["runs"], 1)

    def test_unusable_lines_are_skipped_not_counted(self):
        self._append(_record("run-a"))
        self._raw("not json", "null", "[1, 2]", '"text"',
                  json.dumps({"status": "completed"}))
        store = self._store()
        summary = store.refresh()
        self.assertEqual(summary["runs"], 1)
        self.assertEqual(summary["skipped_lines"], 5)
        self.assertEqual(store.overview()["kpis"]["runs"], 1)

    def test_ten_thousand_runs_index_quickly(self):
        lines = [json.dumps(_record(f"run-{index}").to_dict())
                 for index in range(10_000)]
        self._raw(*lines)
        store = self._store()
        started = time.monotonic()
        store.refresh()
        elapsed = time.monotonic() - started
        self.assertEqual(store.run_count, 10_000)
        self.assertLess(elapsed, 3.0, "startup index is too slow")
        query_started = time.monotonic()
        self.assertEqual(store.overview(range_="all")["kpis"]["runs"], 10_000)
        elapsed_query = time.monotonic() - query_started
        # The budget guards against a return to per-KPI walks (which took
        # ~0.4 s here); the best of a few reads keeps a scheduler hiccup
        # from failing it.
        for _ in range(3):
            started = time.monotonic()
            store.overview(range_="all")
            elapsed_query = min(elapsed_query, time.monotonic() - started)
        self.assertLess(elapsed_query, 0.2,
                        "an indexed overview must stay interactive")

    # -- board level ----------------------------------------------------

    def _executed_task(self, title, *, harness="coding-v1", expects=True):
        # Callers finish each card before creating the next one, so the
        # claim always returns the card this helper just queued.
        task = self.board.create(title, description=f"{title} body",
                                 status="todo", harness=harness,
                                 expects_artifact=expects)
        claimed = self.board.claim_next_todo()
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.id, task["id"])
        return task, claimed

    def test_board_overview_counts_tsr_first_pass_and_rework(self):
        accepted, claimed = self._executed_task("accepted")
        self.board.finish_run(accepted["id"], claimed.run_id, status="review",
                              response="finished")
        self.board.move(accepted["id"], "done")

        rejected, claimed2 = self._executed_task("rejected")
        self.board.finish_run(rejected["id"], claimed2.run_id, status="review",
                              response="finished too")

        # Left running: it proves a card without a finished run still
        # counts as executed and still drags TSR down.
        self._executed_task("running", harness=None, expects=None)
        # Last, so no other card competes for the claim above.
        self.board.move(rejected["id"], "todo")

        store = self._store()
        payload = store.board_overview()
        self.assertEqual(payload["funnel"]["created"], 3)
        self.assertEqual(payload["funnel"]["executed"], 3)
        self.assertEqual(payload["funnel"]["run_completed"], 2)
        self.assertEqual(payload["funnel"]["review"], 2)
        self.assertEqual(payload["funnel"]["done"], 1)
        self.assertEqual(payload["kpis"]["tsr"], round(1 / 3, 4))
        self.assertEqual(payload["kpis"]["first_pass_rate"], round(1 / 3, 4))
        self.assertEqual(payload["kpis"]["rework_rate"], 0.5)
        self.assertEqual(payload["counts"]["reworked"], 1)
        self.assertEqual([row["reason"] for row in payload["rework_reasons"]],
                         ["review_to_todo"])
        self.assertIsNotNone(payload["kpis"]["queue_wait_p50_ms"])
        self.assertEqual(payload["top_offenders"]["rework"][0]["task_id"],
                         rejected["id"])

    def test_artifact_metrics_use_explicit_and_harness_rules(self):
        # Declares an artifact and produced nothing.
        empty, claimed = self._executed_task("empty")
        self.board.finish_run(empty["id"], claimed.run_id, status="review",
                              response="all set")
        self._append(_record(claimed.run_id, status="completed", has_answer=True,
                             task_id=empty["id"], edited_files=[]))

        # Declares an artifact and actually edited a file.
        real, claimed2 = self._executed_task("real")
        self.board.finish_run(real["id"], claimed2.run_id, status="review",
                              response="see patch")
        self._append(_record(claimed2.run_id, status="completed",
                             has_answer=True, task_id=real["id"],
                             edited_files=["src/app.py"]))

        # No artifact semantics at all: not judged.
        other, claimed3 = self._executed_task("chat", harness="general-v1",
                                              expects=None)
        self.board.finish_run(other["id"], claimed3.run_id, status="review",
                              response="text answer")
        # The human sends the empty one back: that is the hallucination.
        self.board.move(empty["id"], "todo")

        store = self._store()
        artifact = store.board_overview()["artifact"]
        self.assertEqual(artifact["judged_tasks"], 2)
        self.assertEqual(artifact["vcr_runs"], 2)
        self.assertEqual(artifact["vcr_passed"], 1)
        self.assertEqual(artifact["vcr"], 0.5)
        self.assertEqual(artifact["hallucinated_count"], 1)
        self.assertEqual(artifact["hallucinated_tasks"][0]["task_id"],
                         empty["id"])

    def test_a_task_without_artifact_semantics_reports_null(self):
        chat, claimed = self._executed_task("chat", harness="general-v1",
                                            expects=None)
        self.board.finish_run(chat["id"], claimed.run_id, status="review",
                              response="answer")
        self._append(_record(claimed.run_id, task_id=chat["id"]))
        artifact = self._store().board_overview()["artifact"]
        self.assertEqual(artifact["judged_tasks"], 0)
        self.assertIsNone(artifact["vcr"])
        self.assertIsNone(artifact["hallucinated_rate"])

    def test_tasks_table_paginates_filters_and_sorts(self):
        first, claimed = self._executed_task("alpha")
        self.board.finish_run(first["id"], claimed.run_id, status="review")
        self._append(_record(claimed.run_id, task_id=first["id"],
                             tokens=TokenUsage(prompt=500, completion=100),
                             duration_ms=2000.0))
        second = self.board.create("beta", description="second",
                                   status="backlog")

        store = self._store()
        page = store.tasks(page=1, page_size=1, sort="tokens")
        self.assertEqual(page["total"], 2)
        self.assertEqual(len(page["rows"]), 1)
        self.assertEqual(page["rows"][0]["task_id"], first["id"])
        self.assertEqual(page["rows"][0]["tokens"], 600)
        self.assertEqual(page["rows"][0]["wall_ms"], 2000.0)
        narrowed = store.tasks(page_size=10, status="backlog")
        self.assertEqual([row["task_id"] for row in narrowed["rows"]],
                         [second["id"]])
        self.assertEqual(store.tasks(query="alph")["total"], 1)
        self.assertEqual(store.tasks(query="nothing")["total"], 0)
        with self.assertRaises(EvaluationRequestError):
            store.tasks(page=0)
        with self.assertRaises(EvaluationRequestError):
            store.tasks(page_size=1000)
        with self.assertRaises(EvaluationRequestError):
            store.tasks(sort="bogus")

    def test_task_detail_joins_runs_evidence_and_diagnoses(self):
        task, claimed = self._executed_task("detail")
        self.board.finish_run(task["id"], claimed.run_id, status="review",
                              response="report text")
        self._append(_record(claimed.run_id, task_id=task["id"],
                             edited_files=["a.py"]))
        session = self.sessions.create(origin="diagnosis",
                                       target={"type": "task",
                                               "id": task["id"]})
        session.append_segment({"id": "s1", "type": "assistant",
                                "content": "## 结论"})
        session.mark_report_segment("s1")

        store = self._store()
        detail = store.task_detail(task["id"])
        self.assertEqual(detail["title"], "detail")
        self.assertEqual(detail["runs"][0]["run_id"], claimed.run_id)
        self.assertEqual(detail["runs"][0]["evidence"], "present")
        self.assertEqual(detail["runs"][0]["edited_files"], ["a.py"])
        self.assertEqual(detail["expects_artifact_source"], "explicit")
        self.assertEqual([event["type"] for event in detail["timeline"]],
                         ["task_created", "run_claimed", "run_finished"])
        self.assertEqual([row["session_id"] for row in detail["diagnoses"]],
                         [session.id])
        self.assertFalse(detail["runs"][0]["trace_available"])
        with self.assertRaises(EvaluationNotFound):
            store.task_detail("task-missing")

    def test_a_run_without_metrics_is_not_called_a_hallucination(self):
        task, claimed = self._executed_task("historic")
        self.board.finish_run(task["id"], claimed.run_id, status="review",
                              response="done, no trace")
        self.board.move(task["id"], "todo")
        artifact = self._store().board_overview()["artifact"]
        self.assertEqual(artifact["judged_tasks"], 1)
        # No ``run.metrics`` row: the claim cannot be judged either way.
        self.assertEqual(artifact["claiming_tasks"], 0)
        self.assertEqual(artifact["unmeasured_claiming_tasks"], 1)
        self.assertEqual(artifact["hallucinated_count"], 0)
        self.assertIsNone(artifact["hallucinated_rate"])

    def test_a_card_sent_back_is_no_longer_accepted(self):
        task, claimed = self._executed_task("bounced")
        self.board.finish_run(task["id"], claimed.run_id, status="review",
                              response="ok")
        self.board.move(task["id"], "done")
        accepted = self._store().board_overview()
        self.assertEqual(accepted["funnel"]["done"], 1)
        self.assertEqual(accepted["kpis"]["tsr"], 1.0)
        # A done card dragged back to Todo is work in progress again, even
        # though the event stream still holds its trip through Done.
        self.board.move(task["id"], "todo")
        current = self._store().board_overview()
        self.assertEqual(current["funnel"]["done"], 0)
        self.assertEqual(current["kpis"]["tsr"], 0.0)

    def test_a_comment_on_review_counts_as_rework(self):
        task, claimed = self._executed_task("commented")
        self.board.finish_run(task["id"], claimed.run_id, status="review",
                              response="first")
        self.board.add_comment(task["id"], "please tighten the diff")
        payload = self._store().board_overview()
        self.assertEqual([row["reason"] for row in payload["rework_reasons"]],
                         ["comment_on_review"])
        self.assertEqual(payload["counts"]["rework_events"], 1)

    def test_a_comment_during_a_run_counts_as_rework(self):
        task = self.board.create("busy", description="body", status="todo",
                                 harness="coding-v1")
        claimed = self.board.claim_next_todo()
        self.assertEqual(claimed.id, task["id"])
        _, deferred = self.board.add_comment(task["id"], "changed my mind")
        self.assertTrue(deferred)
        payload = self._store().board_overview()
        self.assertEqual([row["reason"] for row in payload["rework_reasons"]],
                         ["comment_during_run"])

    def test_board_kpis_include_tokens_and_runs_per_task(self):
        task, claimed = self._executed_task("cost")
        self.board.finish_run(task["id"], claimed.run_id, status="review")
        self._append(_record(claimed.run_id, task_id=task["id"],
                             tokens=TokenUsage(prompt=100, completion=20)))
        kpis = self._store().board_overview()["kpis"]
        self.assertEqual(kpis["tokens_per_task"], 120)
        self.assertEqual(kpis["runs_per_task"], 1)

    # -- incremental index edge cases -----------------------------------

    def test_duplicate_run_lines_are_counted_once(self):
        record = _record("run-dup", status="completed")
        self._append(record)
        self._append(record)
        store = self._store()
        self.assertEqual(store.refresh()["runs"], 1)
        self.assertEqual(store.overview()["kpis"]["runs"], 1)

    def test_a_forced_refresh_restarts_the_skipped_counter(self):
        self._append(_record("run-a"))
        self._raw("not json")
        store = self._store()
        self.assertEqual(store.refresh()["skipped_lines"], 1)
        # A rebuild re-reads the same bad line once, not twice.
        self.assertEqual(store.refresh(force=True)["skipped_lines"], 1)

    def test_an_atomically_replaced_file_is_reloaded(self):
        self._append(_record("run-a"), _record("run-b"))
        store = self._store()
        self.assertEqual(store.refresh()["runs"], 2)
        replacement = self.runs_path.with_suffix(".new")
        for run_id in ("run-c", "run-d", "run-e"):
            append_run_metrics(_record(run_id), replacement)
        os.replace(replacement, self.runs_path)
        self.assertEqual(store.refresh()["runs"], 3)

    def test_an_explicit_origin_filter_drops_unmatched_tasks(self):
        task, claimed = self._executed_task("webui only")
        self.board.finish_run(task["id"], claimed.run_id, status="review")
        self._append(_record(claimed.run_id, task_id=task["id"]))
        self.board.create("backlog card", description="b")
        store = self._store()
        self.assertEqual(store.tasks()["total"], 2)
        self.assertEqual(store.tasks(origins=["cli"])["total"], 0)
        self.assertEqual(store.tasks(origins=["webui"])["rows"][0]["task_id"],
                         task["id"])

    def test_a_run_id_that_cannot_name_a_file_is_refused(self):
        self._append(_record("run-a"))
        store = self._store()
        self.assertIsNone(store._trace_path("../escape"))
        self.assertIsNone(store._trace_path(".."))
        self.assertIsNone(store._trace_path(None))
        self.assertIn("run-a.jsonl", store.run_detail("run-a")["trace_path"])

    def test_a_run_without_metrics_is_reported_as_missing_evidence(self):
        task, claimed = self._executed_task("no metrics")
        self.board.finish_run(task["id"], claimed.run_id, status="review",
                              response="claimed")
        detail = self._store().task_detail(task["id"])
        self.assertEqual(detail["runs"][0]["evidence"], "missing")
        self.assertIsNone(detail["runs"][0]["metrics"])

    # -- run detail -----------------------------------------------------

    def _trace(self, run_id, events):
        path = self.root / "traces" / f"{run_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps({"record_type": "harness", "run_id": run_id})]
        lines += [json.dumps({"run_id": run_id, "event_id": f"evt-{index}",
                              "event_type": event_type, "timestamp": 1.0,
                              "iteration": iteration, "data": data})
                  for index, (event_type, iteration, data) in
                  enumerate(events)]
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path

    def test_run_detail_digests_the_trace_without_payloads(self):
        self._append(_record("run-a"))
        self._trace("run-a", [
            ("run.config", 0, {"model": "model-a", "harness_id": "coding-v1",
                               "tools": [{"name": "read_file"}]}),
            ("tool.end", 1, {"tool": "read_file", "status": "success",
                             "exec_ms": 12.5}),
            ("context.llm_summary.invalid", 1,
             {"request_chars": 4000, "duration_ms": 8.0}),
            ("context.strategy", 1, {"request_chars": 4000}),
            ("tool.end", 2, {"tool": "apply_patch", "status": "error",
                             "error_kind": "schema", "exec_ms": 3.0}),
            ("llm.end", 2, {"finish_reason": "tool_calls", "content": "x"}),
            ("llm.retry", 2, {"error": "timeout"}),
        ])
        store = self._store()
        detail = store.run_detail("run-a")
        self.assertEqual(detail["metrics"]["run_id"], "run-a")
        digest = detail["digest"]
        self.assertTrue(digest["available"])
        self.assertEqual(digest["tool_calls"], 2)
        self.assertEqual(digest["tool_errors"], 1)
        # A failed summary is not also a compaction.
        self.assertEqual(digest["context"], {"compactions": 1,
                                            "summary_invalid": 1})
        self.assertEqual(digest["llm"]["requests"], 1)
        self.assertEqual(digest["llm"]["retries"], 1)
        self.assertEqual(digest["llm"]["finish_reasons"], {"tool_calls": 1})
        self.assertEqual(digest["config"]["model"], "model-a")
        self.assertEqual(digest["error_samples"][0]["error_kind"], "schema")
        # Payloads stay in the trace; the digest never carries them.
        self.assertNotIn("content", json.dumps(digest))
        self.assertEqual(digest["event_counts"]["tool.end"], 2)

    def test_run_detail_is_cached_until_the_trace_changes(self):
        self._append(_record("run-a"))
        path = self._trace("run-a", [("tool.end", 1, {"tool": "read_file"})])
        store = self._store()
        first = store.run_detail("run-a")["digest"]
        second = store.run_detail("run-a")["digest"]
        self.assertIs(first, second)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"run_id": "run-a", "event_id": "evt-9",
                                     "event_type": "tool.end",
                                     "timestamp": 2.0, "iteration": 2,
                                     "data": {"tool": "shell"}}) + "\n")
        third = store.run_detail("run-a")["digest"]
        self.assertIsNot(third, first)
        self.assertEqual(third["tool_calls"], 2)

    def test_run_detail_degrades_when_the_trace_is_gone(self):
        self._append(_record("run-a"))
        digest = self._store().run_detail("run-a")["digest"]
        self.assertEqual(digest, {"available": False,
                                  "reason": "trace missing"})
        with self.assertRaises(EvaluationNotFound):
            self._store().run_detail("run-missing")

    # -- benchmarks -----------------------------------------------------

    def test_benchmarks_read_records_and_degrade_when_absent(self):
        records_dir = self.root / "records"
        records_dir.mkdir()
        (records_dir / "one.json").write_text(json.dumps({
            "record_id": "one", "created_at": "2026-11-20T10:00:00",
            "harness_id": "tb-v1", "genes_hash": "g1", "model": "model-a",
            "benchmark": "terminal-bench-2",
            "score": {"passed": 8, "total": 10, "rate": 0.8},
            "tasks": [{"task": "a"}],
        }), encoding="utf-8")
        payload = self._store(records_dir=records_dir).benchmarks()
        self.assertEqual(payload["records"][0]["accuracy"], 0.8)
        self.assertEqual(payload["latest_accuracy"], {"tb-v1": 0.8})
        self.assertEqual(self._store(
            records_dir=self.root / "absent").benchmarks()["records"], [])

    def test_a_record_rewritten_in_place_is_reloaded(self):
        records_dir = self.root / "records"
        records_dir.mkdir()
        path = records_dir / "one.json"

        def write(rate):
            path.write_text(json.dumps({"record_id": "one",
                                        "harness_id": "tb-v1",
                                        "created_at": "2026-11-20T10:00:00",
                                        "score": {"rate": rate}}),
                            encoding="utf-8")

        write(0.25)
        store = self._store(records_dir=records_dir)
        self.assertEqual(store.benchmarks()["records"][0]["accuracy"], 0.25)
        # Same name, new contents: the signature must notice the rewrite.
        write(0.5)
        self.assertEqual(store.benchmarks()["records"][0]["accuracy"], 0.5)

    def test_a_cached_trace_is_not_rescanned_for_a_missing_citation(self):
        """An id the trace never had must not defeat the trace-id cache."""
        from agent_runtime import diagnosis as diagnosis_mod

        run_id = "run-cccccccccccc"
        traces = self.root / "traces"
        traces.mkdir(parents=True, exist_ok=True)
        (traces / f"{run_id}.jsonl").write_text(
            json.dumps({"run_id": run_id, "event_id": "evt-cccccccccccc",
                        "event_type": "tool.end"}) + "\n",
            encoding="utf-8")
        session = self.sessions.create(
            origin="diagnosis", target={"type": "run", "id": run_id})
        # ``evt-…`` that no file carries: the citation stays unverified, but
        # the scan must not repeat on every ``/diagnoses`` refresh.
        session.append_segment({"id": "seg-report", "type": "assistant",
                                "content": f"## 证据\n`{run_id}` "
                                             "`evt-999999999999`"})
        self.assertTrue(session.mark_report_segment("seg-report"))

        scans: list[Path] = []
        real = diagnosis_mod.iter_trace

        def counting(path):
            scans.append(path)
            return real(path)

        store = self._store()
        with patch("webui.evaluation.diagnosis_mod.iter_trace", counting):
            first = store.diagnoses(task_id=run_id)["sessions"][0]
            second = store.diagnoses(task_id=run_id)["sessions"][0]

        self.assertIn("evt-999999999999",
                      first["report"]["verification"]["unverified"])
        self.assertEqual(second["report"]["verification"],
                         first["report"]["verification"])
        self.assertEqual(len(scans), 1)


class EvaluationApiTests(unittest.TestCase):
    """The HTTP surface the evaluation page talks to."""

    def setUp(self):
        self._root = TemporaryDirectory()
        self.addCleanup(self._root.cleanup)
        self.root = Path(self._root.name)
        self.env = patch.dict("os.environ",
                              {"AGENT_RUNTIME_HOME": str(self.root)})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.sessions = SessionStore(self.root / "sessions")
        self.board = BoardStore(self.root)
        self.board.load()
        append_run_metrics(_record("run-a", status="completed",
                                   task_id="task-1"),
                           self.root / "metrics" / "runs.jsonl")
        append_run_metrics(_record("run-b", origin="diagnosis"),
                           self.root / "metrics" / "runs.jsonl")
        task = self.board.create("api task", description="body",
                                 status="todo", harness="coding-v1")
        self.task_id = task["id"]
        claimed = self.board.claim_next_todo()
        self.run_id = claimed.run_id
        self.board.finish_run(self.task_id, self.run_id, status="review",
                              response="ok")
        append_run_metrics(_record(self.run_id, task_id=self.task_id),
                           self.root / "metrics" / "runs.jsonl")
        self._app_ctx = patch.object(server, "credentials_from_env",
                                     return_value=None)
        self._app_ctx.start()
        self.addCleanup(self._app_ctx.stop)
        app = server.create_app(self.sessions, self.board,
                                enable_executor=False)
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_overview_endpoint(self):
        payload = self.client.get("/api/evaluation/overview?range=all").json()
        self.assertEqual(payload["kpis"]["runs"], 2)
        self.assertEqual(payload["excluded"]["runs_by_origin"], 1)
        narrowed = self.client.get(
            "/api/evaluation/overview?range=all&origin=webui,cli").json()
        self.assertEqual(narrowed["kpis"]["runs"], 2)
        bad = self.client.get("/api/evaluation/overview?range=bogus")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("range must be", bad.json()["detail"])

    def test_board_overview_and_benchmarks_endpoints(self):
        board = self.client.get("/api/evaluation/board-overview?range=all").json()
        self.assertEqual(board["funnel"]["executed"], 1)
        self.assertIsNotNone(board["kpis"]["tsr"])
        benchmarks = self.client.get("/api/evaluation/benchmarks").json()
        self.assertIn("records", benchmarks)

    def test_task_and_run_endpoints(self):
        tasks = self.client.get("/api/evaluation/tasks?range=all").json()
        self.assertEqual(tasks["total"], 1)
        self.assertEqual(tasks["rows"][0]["task_id"], self.task_id)

        detail = self.client.get(
            f"/api/evaluation/tasks/{self.task_id}").json()
        self.assertEqual(detail["status"], "review")
        self.assertEqual(self.client.get(
            "/api/evaluation/tasks/task-missing").status_code, 404)

        run = self.client.get(f"/api/evaluation/runs/{self.run_id}").json()
        self.assertEqual(run["metrics"]["run_id"], self.run_id)
        self.assertIn("digest", run)
        self.assertEqual(self.client.get(
            "/api/evaluation/runs/run-missing").status_code, 404)

    def test_diagnoses_endpoint_lists_sessions_by_target(self):
        session = self.sessions.create(origin="diagnosis",
                                       target={"type": "task",
                                               "id": self.task_id})
        session.append_segment({"id": "s1", "type": "assistant",
                                "content": "report"})
        payload = self.client.get(
            f"/api/evaluation/diagnoses?task_id={self.task_id}").json()
        self.assertEqual([row["session_id"] for row in payload["sessions"]],
                         [session.id])
        self.sessions.delete(session.id)
        self.assertEqual(self.client.get(
            "/api/evaluation/diagnoses").json()["sessions"], [])

    def test_refresh_endpoint_rebuilds_the_index(self):
        append_run_metrics(_record("run-c"), self.root / "metrics" / "runs.jsonl")
        payload = self.client.post("/api/evaluation/refresh").json()
        self.assertGreaterEqual(payload["runs"], 3)


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    # A direct run skips ``conftest.py``: isolate the runtime home here too.
    _sys.path.insert(0, str(next(parent for parent in
                                 _Path(__file__).resolve().parents
                                 if (parent / "conftest.py").is_file())))
    from _direct_run import isolate_runtime_home

    isolate_runtime_home()
    unittest.main()
