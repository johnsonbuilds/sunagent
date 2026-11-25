import asyncio
import json
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parents[1]))
sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from starlette.testclient import TestClient

from agent_runtime import diagnosis, storage
from agent_runtime.harness import list_harnesses, resolve_harness
from agent_runtime.metrics import RunMetrics, TokenUsage, append_run_metrics
from webui import server
from webui.board import BoardStore
from webui.evaluation import EvaluationNotFound, EvaluationRequestError, EvaluationStore
from webui.sessions import SessionStore


REPORT = """## 结论
这一跑失败在 apply_patch 的参数上。

## 根因
- 类别：tool_argument
- 置信度：0.8
- 影响 run：`{run_id}`
- 说明：old_text 与文件内容不匹配。

## 证据
- `{run_id}` / iteration 2 / `{event_id}` — old_text not found
- `{run_id}` / iteration 1 / `evt-111111111111` — LLM 结束（只在 trace 里）
- `{run_id}` / iteration 3 / `evt-ffffffffffff` — 未验证的引用

## Top 5 改进建议
1. **收紧 apply_patch 提示** — 影响指标：`tool_error_rate`；影响面：task；成本：low；验证：重跑同类任务
2. **在编辑前回读目标片段** — 影响指标：`schema_pass_rate`；影响面：cohort；成本：low；验证：观察 validation 占比
3. **压缩阈值上调** — 影响指标：`tokens_per_task`；影响面：cohort；成本：medium；验证：对比 tokens 与 TCR
4. **失败后限制重试** — 影响指标：`tool_error_rate`；影响面：task；成本：low；验证：重跑失败任务
5. **补充验收清单** — 影响指标：`TSR`；影响面：cohort；成本：low；验证：下一轮抽样
"""


def _record(run_id, **kwargs):
    now = time.time()
    defaults = dict(run_id=run_id, origin="webui", harness_id="coding-v1",
                    model="model-a", started_at=now - 2.0, finished_at=now,
                    duration_ms=2000.0)
    defaults.update(kwargs)
    return RunMetrics(**defaults)


def _write_trace(run_id, *, event_id="evt-aaaaaaaaaaaa", status="error"):
    path = storage.trace_path(run_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    header = {"record_type": "harness", "run_id": run_id,
              "harness_id": "coding-v1"}
    events = [
        {"run_id": run_id, "event_id": "evt-111111111111",
         "event_type": "llm.end", "timestamp": 1.0, "iteration": 1,
         "data": {"usage": {"prompt_tokens": 100, "completion_tokens": 20},
                  "duration_ms": 500.0, "finish_reason": "tool_calls"}},
        {"run_id": run_id, "event_id": event_id,
         "event_type": "tool.end", "timestamp": 2.0, "iteration": 2,
         "data": {"tool": "apply_patch", "status": "error",
                  "error_kind": "not_found",
                  "result": "old_text not found", "edited_files": []}},
        {"run_id": run_id, "event_id": "evt-222222222222",
         "event_type": "context.llm_summary", "timestamp": 3.0,
         "iteration": 2,
         "data": {"original_chars": 90_000, "request_chars": 60_000}},
    ]
    if status == "error":
        events.append({"run_id": run_id, "event_id": "evt-333333333333",
                       "event_type": "agent.error", "timestamp": 4.0,
                       "iteration": 2,
                       "data": {"stage": "llm", "error": "timeout"}})
    with path.open("w", encoding="utf-8") as stream:
        stream.write(json.dumps(header) + "\n")
        for event in events:
            stream.write(json.dumps(event) + "\n")
    return path


class BundleTests(unittest.TestCase):
    def test_a_bundle_carries_every_section(self):
        bundle = diagnosis.build_bundle(
            target={"type": "task", "id": "task-1"},
            task={"title": "t", "status": "review"},
            runs=[{"run_id": "run-1", "metrics": {"status": "error"}}],
            environment={"edited_files": ["a.py"]},
            human_feedback={"verdict": "rework"},
            aggregate={"tsr": 0.0},
            generated_at=1.0)
        self.assertEqual(bundle["target"]["id"], "task-1")
        self.assertEqual(bundle["task"]["status"], "review")
        self.assertEqual(bundle["runs"][0]["run_id"], "run-1")
        self.assertEqual(bundle["environment"]["edited_files"], ["a.py"])
        self.assertEqual(bundle["human_feedback"]["verdict"], "rework")
        self.assertEqual(bundle["aggregate"]["tsr"], 0.0)

    def test_the_hash_is_stable_and_evidence_sensitive(self):
        base = diagnosis.build_bundle(target={"type": "task", "id": "task-1"},
                                      runs=[{"run_id": "run-1"}])
        same = diagnosis.build_bundle(target={"type": "task", "id": "task-1"},
                                      runs=[{"run_id": "run-1"}])
        other = diagnosis.build_bundle(target={"type": "task", "id": "task-1"},
                                       runs=[{"run_id": "run-1", "x": 1}])
        self.assertEqual(diagnosis.compute_hash(base),
                         diagnosis.compute_hash(same))
        self.assertNotEqual(diagnosis.compute_hash(base),
                            diagnosis.compute_hash(other))

    def test_an_oversized_bundle_drops_passing_detail_first(self):
        def run(run_id, status):
            return {"run_id": run_id, "status": status,
                    "metrics": {"status": status},
                    "iterations": [{"n": index, "tools": [
                        {"tool": "apply_patch", "status": "ok",
                         "event_id": f"evt-{index:012d}"}]}
                        for index in range(300)]}

        bundle = diagnosis.build_bundle(
            target={"type": "task", "id": "task-1"},
            runs=[run("run-failed", "error"), run("run-ok", "completed")])
        on_disk = len(json.dumps(bundle, ensure_ascii=False, indent=2))
        self.assertGreater(on_disk, 40_000)
        # A budget that fits once (and only once) the passing run loses its
        # per-iteration detail.
        without_passing = json.loads(json.dumps(bundle))
        for row in without_passing["runs"]:
            if row["run_id"] == "run-ok":
                row["iterations"] = []
        fits = len(json.dumps(without_passing, ensure_ascii=False, indent=2))
        compacted = diagnosis.compact_bundle(bundle,
                                            limit=(fits + on_disk) // 2)
        self.assertTrue(compacted["truncated_input"])
        rows = {row["run_id"]: row for row in compacted["runs"]}
        # The failing run keeps its detail longest.
        self.assertTrue(rows["run-failed"]["iterations"])
        self.assertFalse(rows["run-ok"]["iterations"])
        self.assertTrue(rows["run-ok"]["detail_dropped"])

    def test_compacting_twice_keeps_the_truncation_flag(self):
        bundle = diagnosis.build_bundle(
            target={"type": "task", "id": "task-1"},
            runs=[{"run_id": "run-1", "iterations": [
                {"n": index, "tools": []} for index in range(400)]}])
        compacted = diagnosis.compact_bundle(bundle, limit=1_000)
        self.assertTrue(compacted["truncated_input"])
        again = diagnosis.compact_bundle(compacted, limit=1_000)
        self.assertTrue(again["truncated_input"])
        self.assertEqual(diagnosis.compute_hash(compacted),
                         diagnosis.compute_hash(again))

    def test_writing_the_bundle_lands_under_the_runtime_home(self):
        bundle = diagnosis.build_bundle(target={"type": "task", "id": "task-1"},
                                        runs=[{"run_id": "run-1"}])
        written = diagnosis.write_bundle(bundle, "diag-test1")
        directory = storage.runtime_home() / "diagnosis" / "diag-test1"
        self.assertEqual(Path(written["path"]), directory / "bundle.json")
        self.assertTrue((directory / "summary.md").is_file())
        self.assertEqual(written["bundle_hash"],
                         diagnosis.compute_hash(
                             json.loads((directory / "bundle.json").read_text(
                                 encoding="utf-8"))))
        self.assertIn("## 指标口径", (directory / "summary.md").read_text(
            encoding="utf-8"))

    def test_an_invalid_diagnosis_id_is_refused(self):
        for value in ("../escape", "", "a/b", ".."):
            with self.subTest(value=value):
                with self.assertRaises(diagnosis.DiagnosisError):
                    diagnosis.bundle_dir(value)


class TraceEvidenceTests(unittest.TestCase):
    def test_evidence_collects_iterations_tools_and_compactions(self):
        _write_trace("run-evidence")
        evidence = diagnosis.collect_run_evidence("run-evidence")
        self.assertTrue(evidence["trace_available"])
        self.assertEqual(evidence["failure"]["stage"], "llm")
        self.assertEqual([row["n"] for row in evidence["iterations"]], [1, 2])
        first = evidence["iterations"][0]
        self.assertEqual(first["llm"]["tokens"], 120)
        self.assertEqual(evidence["tool_errors"][0]["tool"], "apply_patch")
        self.assertEqual(evidence["tool_errors"][0]["event_id"],
                         "evt-aaaaaaaaaaaa")
        self.assertEqual(evidence["context_events"][0]["before"], 90_000)

    def test_missing_trace_degrades_honestly(self):
        evidence = diagnosis.collect_run_evidence("run-absent")
        self.assertFalse(evidence["trace_available"])
        self.assertEqual(evidence["iterations"], [])
        self.assertIsNone(evidence["failure"])

    def test_read_trace_filters_by_event_and_iteration(self):
        _write_trace("run-read")
        everything = json.loads(asyncio.run(
            diagnosis.read_trace("run-read")))
        self.assertEqual(everything["matched"], 4)
        one = json.loads(asyncio.run(
            diagnosis.read_trace("run-read", event_type="tool.end")))
        self.assertEqual([row["event_id"] for row in one["events"]],
                         ["evt-aaaaaaaaaaaa"])
        two = json.loads(asyncio.run(
            diagnosis.read_trace("run-read", iteration=2)))
        self.assertEqual(two["matched"], 3)
        missing = json.loads(asyncio.run(diagnosis.read_trace("run-absent")))
        self.assertIn("trace not found", missing["error"])

    def test_search_trace_returns_a_snippet(self):
        _write_trace("run-search")
        found = json.loads(asyncio.run(
            diagnosis.search_trace("run-search", "OLD_TEXT not")))
        self.assertEqual(len(found["matches"]), 1)
        self.assertEqual(found["matches"][0]["event_id"], "evt-aaaaaaaaaaaa")
        self.assertIn("old_text not found", found["matches"][0]["snippet"])

    def test_read_diagnosis_refuses_a_path(self):
        bundle = diagnosis.build_bundle(target={"type": "task", "id": "task-1"})
        diagnosis.write_bundle(bundle, "diag-read")
        text = asyncio.run(diagnosis.read_diagnosis("diag-read",
                                                    file="bundle.json"))
        self.assertIn("task-1", text)
        for name in ("../sessions/x.json", "bundle.json.bak", "/etc/passwd"):
            with self.subTest(name=name):
                refused = json.loads(asyncio.run(
                    diagnosis.read_diagnosis("diag-read", file=name)))
                self.assertIn("error", refused)


class ReportTests(unittest.TestCase):
    def test_a_complete_report_parses_into_four_sections(self):
        parsed = diagnosis.parse_report(REPORT.format(
            run_id="run-abcdef123456", event_id="evt-aaaaaaaaaaaa"))
        self.assertEqual(parsed["headings"], list(diagnosis.REPORT_HEADINGS))
        self.assertEqual(parsed["missing"], [])
        self.assertTrue(parsed["order_ok"])
        self.assertTrue(parsed["complete"])
        self.assertEqual(parsed["suggestion_count"], 5)
        self.assertEqual(parsed["suggestions"][0]["index"], 1)
        self.assertIn("apply_patch", parsed["sections"]["结论"])

    def test_missing_headings_are_reported_not_rejected(self):
        partial = "## 结论\n一句话。\n\n## 根因\n- 类别：other"
        parsed = diagnosis.parse_report(partial)
        self.assertEqual(parsed["headings"], ["结论", "根因"])
        self.assertEqual(parsed["missing"], ["证据", "Top 5 改进建议"])
        self.assertFalse(parsed["complete"])
        # The body is still rendered; parsing never gates the report.
        self.assertTrue(parsed["available"])

    def test_citations_are_verified_best_effort(self):
        parsed = diagnosis.parse_report(REPORT.format(
            run_id="run-abcdef123456", event_id="evt-aaaaaaaaaaaa"))
        verification = diagnosis.verify_report(
            parsed, event_ids={"evt-aaaaaaaaaaaa", "evt-111111111111"},
            run_ids={"run-abcdef123456"})
        verified = {(item["kind"], item["value"]): item["verified"]
                    for item in verification["citations"]}
        self.assertTrue(verified[("event", "evt-aaaaaaaaaaaa")])
        self.assertFalse(verified[("event", "evt-ffffffffffff")])
        self.assertTrue(verified[("run", "run-abcdef123456")])
        self.assertEqual(verification["unverified"], ["evt-ffffffffffff"])
        self.assertEqual(verification["suggestion_count"], 5)

    def test_an_empty_report_has_nothing_to_verify(self):
        parsed = diagnosis.parse_report("")
        self.assertFalse(parsed["available"])
        self.assertEqual(parsed["missing"], list(diagnosis.REPORT_HEADINGS))


class HarnessInternalTests(unittest.TestCase):
    def test_the_diagnosis_harness_stays_resolvable_but_hidden(self):
        spec = resolve_harness("evaluation-v1")
        self.assertTrue(spec.internal)
        self.assertEqual(spec.id, "evaluation-v1")
        listed = {item.id for item in list_harnesses()}
        self.assertIn("evaluation-v1", listed)
        visible = {item.id for item in list_harnesses(include_internal=False)}
        self.assertNotIn("evaluation-v1", visible)
        self.assertIn(spec.id, {item.id for item in list_harnesses()})

    def test_internal_does_not_change_the_gene_hash(self):
        from agent_runtime.harness.schema import HarnessSpec

        self.assertEqual(HarnessSpec(id="x", internal=True).genes_hash,
                         HarnessSpec(id="x", internal=False).genes_hash)

    def test_the_picker_payload_carries_the_mutation_note(self):
        rows = {row["id"]: row
                for row in server.AgentRunner().status()["harnesses"]}
        self.assertIn("context.strategy turn_fold -> llm_summary",
                      rows["coding-v2"]["mutation"])
        self.assertIsNone(rows["coding-v1"]["mutation"])
        self.assertTrue(rows["coding-v1"]["reason"])

    def test_the_diagnosis_tools_are_only_injected_for_that_harness(self):
        from webui.turn_context import _turn_tools

        diagnosis_tools = {spec.name for spec in diagnosis.diagnosis_tool_specs()}
        self.assertEqual(diagnosis_tools,
                         {"read_trace", "search_trace", "read_diagnosis"})
        own = {schema["function"]["name"] for schema in
               _turn_tools(resolve_harness("evaluation-v1")).schemas}
        self.assertTrue(diagnosis_tools <= own)
        generic = {schema["function"]["name"] for schema in
                   _turn_tools(resolve_harness("coding-v1")).schemas}
        self.assertFalse(diagnosis_tools & generic)

    def test_the_api_hides_and_refuses_the_diagnosis_harness(self):
        with TemporaryDirectory() as root:
            path = Path(root)
            sessions = SessionStore(path / "sessions")
            board = BoardStore(path)
            with patch.object(server, "credentials_from_env",
                              return_value=None):
                app = server.create_app(sessions, board,
                                        enable_executor=False)
                with TestClient(app) as client:
                    config = client.get("/api/config").json()
                    self.assertNotIn("evaluation-v1",
                                     [row["id"] for row in config["harnesses"]])
                    refused = client.post("/api/sessions", json={
                        "harness": "evaluation-v1"})
                    self.assertEqual(refused.status_code, 400)
                    self.assertIn("not selectable", refused.json()["detail"])


class DiagnosisFlowTests(unittest.TestCase):
    """Start a diagnosis through the API with a stubbed model turn."""

    def setUp(self):
        self._root = TemporaryDirectory()
        self.addCleanup(self._root.cleanup)
        self.root = Path(self._root.name)
        self.sessions = SessionStore(self.root / "sessions")
        self.board = BoardStore(self.root)
        self.board.load()
        task = self.board.create("Failing task", description="write the file",
                                 status="todo", harness="coding-v1")
        self.task_id = task["id"]
        claimed = self.board.claim_next_todo()
        self.run_id = claimed.run_id
        self.board.finish_run(self.task_id, self.run_id, status="review",
                              response="done, wrote it")
        append_run_metrics(
            _record(self.run_id, task_id=self.task_id, status="error",
                    tool_calls=3, tool_errors=1,
                    error_kinds={"not_found": 1},
                    tokens=TokenUsage(prompt=100, completion=20),
                    error={"stage": "llm", "code": "timeout"}),
            storage.metrics_path())
        _write_trace(self.run_id)
        self.report = REPORT.format(run_id=self.run_id,
                                    event_id="evt-aaaaaaaaaaaa")

    def _app(self, report=None, run_turn=None):
        body = report if report is not None else self.report

        async def fake_run_turn(message, *args, **kwargs):
            events = kwargs.get("events")
            events.emit("agent.started", 0, message=message)
            events.emit("assistant.delta", 1, content=body)
            events.emit("agent.completed", 1, answer=body, iterations=1)
            return body

        turn = run_turn if run_turn is not None else fake_run_turn
        # The patches must outlive ``create_app``: the route resolves
        # ``run_turn`` when the turn starts, not when the app is built.
        for target, replacement in (
                (server, "credentials_from_env"),
                (server, "run_turn")):
            context = (patch.object(target, replacement, return_value=None)
                       if replacement == "credentials_from_env"
                       else patch.object(target, replacement, turn))
            context.start()
            self.addCleanup(context.stop)
        app = server.create_app(self.sessions, self.board,
                                enable_executor=False)
        client = TestClient(app)
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        return client

    def _wait_report(self, client, session_id, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            detail = client.get(f"/api/sessions/{session_id}").json()
            if detail["report_segment_id"]:
                return detail
            time.sleep(0.02)
        self.fail(f"diagnosis session {session_id} never produced a report")

    def _start(self, client, target, force=False):
        response = client.post("/api/evaluation/diagnose",
                               json={"target": target, "force": force})
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_starting_a_diagnosis_opens_a_linked_session(self):
        client = self._app()
        payload = self._start(client, {"type": "task", "id": self.task_id})
        self.assertFalse(payload["duplicate"])
        session_id = payload["session_id"]
        self.assertTrue(session_id.startswith("sess-"))

        diagnosis_id = payload["diagnosis_id"]
        directory = storage.diagnosis_dir() / diagnosis_id
        bundle = json.loads((directory / "bundle.json").read_text("utf-8"))
        self.assertEqual(bundle["target"], {"type": "task", "id": self.task_id})
        self.assertEqual(bundle["task"]["description"], "write the file")
        self.assertEqual([run["run_id"] for run in bundle["runs"]],
                         [self.run_id])
        self.assertEqual(bundle["runs"][0]["metrics"]["status"], "error")
        self.assertEqual(bundle["runs"][0]["failure"]["stage"], "llm")
        self.assertTrue((directory / "summary.md").is_file())

        detail = self._wait_report(client, session_id)
        self.assertIn("## 结论", detail["segments"][-1]["content"])
        sessions = self.sessions.list()
        session = next(item for item in sessions if item.id == session_id)
        self.assertEqual(session.origin, "diagnosis")
        self.assertEqual(session.harness, "evaluation-v1")
        self.assertEqual(session.target, {"type": "task", "id": self.task_id})
        self.assertEqual(session.bundle_hash, payload["bundle_hash"])
        self.assertEqual(session.diagnosis_id, diagnosis_id)

        diagnoses = client.get(
            f"/api/evaluation/diagnoses?task_id={self.task_id}").json()
        row = diagnoses["sessions"][0]
        self.assertEqual(row["session_id"], session_id)
        self.assertTrue(row["report"]["available"])
        self.assertIn("Top 5 改进建议", row["report"]["markdown"])
        self.assertEqual(row["report"]["verification"]["missing_sections"], [])
        self.assertEqual(row["report"]["verification"]["suggestion_count"], 5)
        self.assertEqual(row["report"]["verification"]["unverified"],
                         ["evt-ffffffffffff"])
        self.assertEqual(len(row["report"]["suggestions"]), 5)

    def test_the_same_evidence_is_not_diagnosed_twice(self):
        client = self._app()
        first = self._start(client, {"type": "task", "id": self.task_id})
        self._wait_report(client, first["session_id"])
        again = self._start(client, {"type": "task", "id": self.task_id})
        self.assertTrue(again["duplicate"])
        self.assertEqual(again["existing_session_id"], first["session_id"])
        self.assertEqual(again["bundle_hash"], first["bundle_hash"])

        forced = self._start(client, {"type": "task", "id": self.task_id},
                             force=True)
        self.assertFalse(forced["duplicate"])
        self.assertNotEqual(forced["session_id"], first["session_id"])

    def test_a_follow_up_does_not_replace_the_report(self):
        client = self._app()
        payload = self._start(client, {"type": "task", "id": self.task_id})
        session_id = payload["session_id"]
        first = self._wait_report(client, session_id)
        marked = first["report_segment_id"]
        original = next(segment for segment in first["segments"]
                        if segment["id"] == marked)["content"]

        follow = client.post(f"/api/sessions/{session_id}/messages",
                            json={"message": "再解释一下根因"})
        self.assertEqual(follow.status_code, 200)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            detail = client.get(f"/api/sessions/{session_id}").json()
            if not detail["busy"] and len(detail["segments"]) > len(
                    first["segments"]):
                break
            time.sleep(0.02)
        self.assertEqual(detail["report_segment_id"], marked)
        kept = next(segment for segment in detail["segments"]
                    if segment["id"] == marked)["content"]
        self.assertEqual(kept, original)

    def test_a_run_and_a_cohort_target_diagnose_too(self):
        client = self._app()
        run_plan = self._start(client, {"type": "run", "id": self.run_id})
        self.assertFalse(run_plan["duplicate"])
        self._wait_report(client, run_plan["session_id"])
        bundle = json.loads((storage.diagnosis_dir()
                             / run_plan["diagnosis_id"]
                             / "bundle.json").read_text("utf-8"))
        self.assertEqual(bundle["runs"][0]["run_id"], self.run_id)

        cohort = self._start(client, {"type": "cohort",
                                      "filters": {"status": "review"}})
        self.assertFalse(cohort["duplicate"])
        self._wait_report(client, cohort["session_id"])
        bundle = json.loads((storage.diagnosis_dir()
                             / cohort["diagnosis_id"]
                             / "bundle.json").read_text("utf-8"))
        self.assertEqual(bundle["target"]["type"], "cohort")
        self.assertEqual([case["task_id"] for case in bundle["cases"]],
                         [self.task_id])
        self.assertEqual(bundle["aggregate"]["cohort_size"], 1)

    def test_a_bad_target_is_a_400_and_an_unknown_one_is_a_404(self):
        client = self._app()
        for target in ({"type": "nope", "id": "x"}, {"type": "task"}):
            with self.subTest(target=target):
                bad = client.post("/api/evaluation/diagnose",
                                  json={"target": target})
                self.assertEqual(bad.status_code, 400)
        # A body that is not an object is refused before the handler runs.
        self.assertEqual(client.post("/api/evaluation/diagnose",
                                     json={"target": []}).status_code, 422)
        missing = client.post("/api/evaluation/diagnose",
                              json={"target": {"type": "task",
                                               "id": "task-missing"}})
        self.assertEqual(missing.status_code, 404)

    def test_a_failed_turn_freezes_no_report_and_can_be_retried(self):
        async def failing(message, *args, **kwargs):
            events = kwargs.get("events")
            events.emit("assistant.delta", 1, content="半截结论")
            events.emit("runtime.error", 2, stage="llm", error="boom")
            return "半截结论"

        client = self._app(run_turn=failing)
        first = self._start(client, {"type": "task", "id": self.task_id})
        deadline = time.time() + 5.0
        detail = {}
        while time.time() < deadline:
            detail = client.get(
                f"/api/sessions/{first['session_id']}").json()
            if not detail["busy"]:
                break
            time.sleep(0.02)
        # The half-written answer stays in the transcript, but it is not the
        # report: only a completed turn can freeze one.
        self.assertIsNone(detail["report_segment_id"])
        self.assertIn("半截结论",
                      "".join(segment["content"]
                              for segment in detail["segments"]))
        # And a session without a report is not a duplicate: the same
        # evidence can still be diagnosed.
        again = self._start(client, {"type": "task", "id": self.task_id})
        self.assertFalse(again["duplicate"])
        self.assertNotEqual(again["session_id"], first["session_id"])

    def test_an_unusable_model_leaves_no_session_or_bundle(self):
        client = self._app()
        from fastapi import HTTPException

        with patch.object(server, "_resolve_snapshot",
                          side_effect=HTTPException(status_code=400,
                                                    detail="no credentials")):
            refused = client.post("/api/evaluation/diagnose",
                                  json={"target": {"type": "task",
                                                   "id": self.task_id}})
        self.assertEqual(refused.status_code, 400)
        self.assertEqual(self.sessions.list(), [])
        self.assertEqual(list(storage.diagnosis_dir().glob("diag-*")), [])

    def test_a_report_may_cite_an_event_only_the_trace_has(self):
        client = self._app()
        payload = self._start(client, {"type": "task", "id": self.task_id})
        self._wait_report(client, payload["session_id"])
        row = client.get(
            f"/api/evaluation/diagnoses?task_id={self.task_id}").json()[
                "sessions"][0]
        citations = {item["value"]: item["verified"]
                     for item in row["report"]["verification"]["citations"]}
        # ``llm.end`` is not in the bundle (it carries no tool detail), yet
        # the model read it from the trace: that citation is verified.
        self.assertTrue(citations["evt-111111111111"])
        self.assertFalse(citations["evt-ffffffffffff"])


class DiagnosisModelTests(unittest.TestCase):
    """``AGENT_RUNTIME_DIAGNOSIS_MODEL`` names a catalog id, not a wire name."""

    def setUp(self):
        from fastapi import HTTPException

        self.HTTPException = HTTPException
        self.runner = server.AgentRunner()

    def _model(self, value):
        with patch.dict(os.environ, {"AGENT_RUNTIME_DIAGNOSIS_MODEL": value}):
            return server._diagnosis_model(self.runner)

    def test_unset_falls_back_to_the_catalog_default(self):
        self.assertEqual(self._model(""), self.runner.catalog.default)
        self.assertEqual(self._model("  "), self.runner.catalog.default)

    def test_a_known_id_is_used_and_an_unknown_one_is_a_400(self):
        known = self.runner.catalog.default
        self.assertEqual(self._model(known), known)
        with self.assertRaises(self.HTTPException) as caught:
            self._model("model-not-in-catalog")
        self.assertEqual(caught.exception.status_code, 400)


class StoreDiagnosisTests(unittest.TestCase):
    """The store half, without the HTTP/turn machinery."""

    def setUp(self):
        self._root = TemporaryDirectory()
        self.addCleanup(self._root.cleanup)
        self.root = Path(self._root.name)
        self.sessions = SessionStore(self.root / "sessions")
        self.board = BoardStore(self.root)
        self.board.load()
        self.store = EvaluationStore(self.sessions, self.board,
                                     root=self.root,
                                     records_dir=self.root / "records")

    def test_an_unknown_task_raises_not_found(self):
        with self.assertRaises(EvaluationNotFound):
            self.store.diagnose({"type": "task", "id": "task-none"},
                                model="model-a")

    def test_an_unknown_run_raises_not_found(self):
        with self.assertRaises(EvaluationNotFound):
            self.store.diagnose({"type": "run", "id": "run-none"},
                                model="model-a")

    def test_a_bad_range_filter_is_rejected(self):
        with self.assertRaises(EvaluationRequestError):
            self.store.diagnose({"type": "cohort",
                                 "filters": {"range": "decade"}},
                                model="model-a")

    def test_a_duplicate_target_without_evidence_before_is_allowed(self):
        task = self.board.create("card", description="body", status="todo",
                                 harness="coding-v1")
        plan = self.store.diagnose({"type": "task", "id": task["id"]},
                                   model="model-a")
        self.assertFalse(plan.duplicate)
        self.assertIsNotNone(plan.session)
        self.assertTrue(plan.diagnosis_id.startswith("diag-"))
        self.assertIn(plan.diagnosis_id, plan.message)
        self.assertTrue(plan.bundle_path.endswith("bundle.json"))

    def test_a_session_without_a_report_is_not_a_duplicate(self):
        task = self.board.create("card", description="body", status="todo",
                                 harness="coding-v1")
        first = self.store.diagnose({"type": "task", "id": task["id"]},
                                    model="model-a")
        again = self.store.diagnose({"type": "task", "id": task["id"]},
                                    model="model-a")
        self.assertFalse(again.duplicate)
        self.assertNotEqual(again.diagnosis_id, first.diagnosis_id)
        # Once the report is frozen the same evidence is reused.
        session = first.session
        session.append_user_message("诊断")
        session.mark_report_segment("seg-report")
        self.sessions.save(session)
        reused = self.store.diagnose({"type": "task", "id": task["id"]},
                                     model="model-a")
        self.assertTrue(reused.duplicate)
        self.assertEqual(reused.existing_session_id, session.id)

    def test_an_oversized_bundle_keeps_its_truncation_hash(self):
        runs = [{"run_id": f"run-{index}", "status": "completed",
                 "iterations": [{"n": number,
                                  "tools": [{"tool": "apply_patch",
                                             "status": "ok"}]}
                                 for number in range(400)]}
                for index in range(4)]
        bundle = diagnosis.build_bundle(
            target={"type": "task", "id": "task-big"}, runs=runs)
        compacted = diagnosis.compact_bundle(bundle, limit=50_000)
        self.assertTrue(compacted["truncated_input"])
        fingerprint = diagnosis.compute_hash(compacted)
        written = diagnosis.write_bundle(compacted, "diag-big",
                                        self.root / "diagnosis")
        # The hash ``diagnose()`` keeps must be the hash of what is on disk,
        # otherwise duplicate detection never matches a truncated bundle.
        self.assertEqual(written["bundle_hash"], fingerprint)
        self.assertTrue(written["truncated_input"])
        self.assertLessEqual(written["bytes"], diagnosis.MAX_BUNDLE_BYTES)
        disk = json.loads(Path(written["path"]).read_text(encoding="utf-8"))
        self.assertTrue(disk["truncated_input"])

    def test_a_cohort_honours_model_and_origin_filters(self):
        runs_path = self.root / "metrics" / "runs.jsonl"
        created: dict[str, str] = {}
        for name, model, origin in (("a", "model-a", "webui"),
                                    ("b", "model-b", "cli")):
            task = self.board.create(name, description="x", status="todo",
                                     harness="coding-v1", model=model)
            claimed = self.board.claim_next_todo()
            self.board.finish_run(task["id"], claimed.run_id, status="review",
                                  response="ok")
            append_run_metrics(_record(claimed.run_id, task_id=task["id"],
                                      origin=origin, model=model,
                                      status="completed"), runs_path)
            created[name] = task["id"]

        def cases(filters):
            plan = self.store.diagnose({"type": "cohort", "filters": filters},
                                       model="model-a")
            bundle = json.loads(Path(plan.bundle_path).read_text("utf-8"))
            return [case["task_id"] for case in bundle["cases"]]

        self.assertEqual(sorted(cases({"model": "model-b"})),
                         [created["b"]])
        self.assertEqual(sorted(cases({"origin": "cli"})), [created["b"]])
        self.assertEqual(sorted(cases({"origin": "webui"})),
                         [created["a"]])
        self.assertEqual(sorted(cases({})), sorted(created.values()))


if __name__ == "__main__":
    sys.path.insert(0, str(Path(__file__).parents[1]))
    from _direct_run import isolate_runtime_home

    isolate_runtime_home()
    unittest.main()
