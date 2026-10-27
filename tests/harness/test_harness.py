import asyncio
import sys
import json
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.harness import (
    BUILTIN_HARNESS,
    DEFAULT_HARNESS_ID,
    ControlGenome,
    HarnessError,
    HarnessSpec,
    LLM_RETRY_CATEGORIES,
    LLMRetryPolicy,
    RecoveryGenome,
    default_harness,
    from_dict,
    load_harness,
    load_named_harness,
    resolve_harness,
)
from agent_runtime.trace import RunTrace


REPO_ROOT = Path(__file__).parents[2]
BASELINE_YAML = REPO_ROOT / "harnesses" / "baseline-v0.yaml"
COMMON_YAML = REPO_ROOT / "harnesses" / "common-v0.yaml"


class HarnessDefaultTests(unittest.TestCase):
    def test_builtin_default_stays_in_sync_with_baseline_manifest(self) -> None:
        loaded = load_harness(BASELINE_YAML)
        self.assertEqual(loaded.id, BUILTIN_HARNESS.id)
        self.assertEqual(loaded.genes_hash, BUILTIN_HARNESS.genes_hash)

    def test_default_harness_is_the_manifest_common_v0(self) -> None:
        self.assertEqual(DEFAULT_HARNESS_ID, "common-v0")
        spec = default_harness()
        self.assertEqual(spec.id, "common-v0")
        self.assertEqual(Path(spec.source or "").resolve(), COMMON_YAML.resolve())
        self.assertEqual(spec.genes_hash,
                         load_named_harness("common-v0").genes_hash)

    def test_genes_hash_ignores_lineage_metadata(self) -> None:
        base = HarnessSpec()
        annotated = HarnessSpec(id="baseline-v1", parent="baseline-v0",
                                mutation="enable verification",
                                reason="test the verification gene",
                                verification=base.verification)
        self.assertEqual(base.genes_hash, annotated.genes_hash)

    def test_genes_hash_changes_when_a_gene_changes(self) -> None:
        mutated = HarnessSpec(control=ControlGenome(max_iterations=20))
        self.assertNotEqual(BUILTIN_HARNESS.genes_hash, mutated.genes_hash)

    def test_to_dict_roundtrips_through_from_dict(self) -> None:
        spec = HarnessSpec(id="h1", parent="baseline-v0",
                           mutation="bump iterations",
                           control=ControlGenome(max_iterations=5))
        replica = from_dict(spec.to_dict())
        self.assertEqual(spec.genes_hash, replica.genes_hash)
        self.assertEqual(spec.prompt, replica.prompt)
        self.assertEqual(spec.control, replica.control)


class TranscriptEconomicsGenesTests(unittest.TestCase):
    """The observation/memory sizing knobs live on the genome."""

    def test_genome_defaults_match_runtime_invariants(self) -> None:
        self.assertEqual(BUILTIN_HARNESS.memory.head_chars, 200)
        self.assertEqual(BUILTIN_HARNESS.memory.context_budget, 60_000)
        self.assertEqual(BUILTIN_HARNESS.memory.window_rounds, 12)
        self.assertEqual(BUILTIN_HARNESS.memory.keep_recent_rounds, 2)
        self.assertEqual(BUILTIN_HARNESS.memory.summary_tail_rounds, 2)
        self.assertEqual(BUILTIN_HARNESS.control.max_observation_chars, 16_000)
        self.assertEqual(BUILTIN_HARNESS.control.spill_preview_chars, 4_000)

    def test_yaml_parses_new_gene_keys(self) -> None:
        spec = from_dict({
            "control": {"max_iterations": 30,
                        "max_observation_chars": 8_000,
                        "spill_preview_chars": 2_000},
            "memory": {"strategy": "compact_observations",
                       "head_chars": 500,
                       "context_budget": 30_000,
                       "window_rounds": 6,
                       "keep_recent_rounds": 3,
                       "summary_tail_rounds": 4},
        })
        self.assertEqual(spec.control.max_observation_chars, 8_000)
        self.assertEqual(spec.control.spill_preview_chars, 2_000)
        self.assertEqual(spec.memory.head_chars, 500)
        self.assertEqual(spec.memory.context_budget, 30_000)
        self.assertEqual(spec.memory.window_rounds, 6)
        self.assertEqual(spec.memory.keep_recent_rounds, 3)
        self.assertEqual(spec.memory.summary_tail_rounds, 4)

    def test_bad_transcript_economics_values_are_rejected(self) -> None:
        for section, key in (("control", "max_observation_chars"),
                             ("control", "spill_preview_chars"),
                             ("memory", "head_chars"),
                             ("memory", "context_budget"),
                             ("memory", "window_rounds"),
                             ("memory", "keep_recent_rounds"),
                             ("memory", "summary_tail_rounds")):
            with self.subTest(section=section, key=key):
                with self.assertRaisesRegex(HarnessError, f"{section}.{key}"):
                    from_dict({section: {key: 0}})
                with self.assertRaisesRegex(HarnessError, f"{section}.{key}"):
                    from_dict({section: {key: "big"}})

    def test_unknown_transcript_economics_keys_are_rejected(self) -> None:
        with self.assertRaisesRegex(HarnessError, "control"):
            from_dict({"control": {"max_observation_x": 1}})
        with self.assertRaisesRegex(HarnessError, "memory"):
            from_dict({"memory": {"head_x": 1}})


class HarnessValidationTests(unittest.TestCase):
    def assert_harness_error(self, yaml_text: str, fragment: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "h.yaml"
            path.write_text(dedent(yaml_text))
            with self.assertRaises(HarnessError) as caught:
                load_harness(path)
            self.assertIn(fragment, str(caught.exception))

    def test_unknown_manifest_key_is_rejected(self) -> None:
        self.assert_harness_error(
            """
            id: h1
            model:
              stream: env
            """,
            "unknown manifest keys: ['model']",
        )

    def test_unknown_section_key_is_rejected(self) -> None:
        self.assert_harness_error(
            """
            control:
              max_iterations: 3
              timeout: 30
            """,
            "unknown control keys: ['timeout']",
        )

    def test_bad_max_iterations_is_rejected(self) -> None:
        self.assert_harness_error(
            """
            control:
              max_iterations: 0
            """,
            "max_iterations must be an integer >= 1",
        )

    def test_unknown_recovery_strategy_is_rejected(self) -> None:
        self.assert_harness_error(
            """
            recovery:
              tool_error: panic
            """,
            "recovery.tool_error",
        )

    def test_unknown_memory_strategy_is_rejected(self) -> None:
        self.assert_harness_error(
            """
            memory:
              strategy: summarized
            """,
            "memory.strategy",
        )

    def test_blank_id_is_rejected(self) -> None:
        self.assert_harness_error(
            """
            id: ""
            """,
            "manifest.id must be non-empty text",
        )

    def test_empty_notice_is_rejected(self) -> None:
        self.assert_harness_error(
            """
            prompt:
              system: ""
              iteration_limit_notice: ""
            """,
            "iteration_limit_notice must be non-empty text",
        )

    def test_missing_file_is_rejected(self) -> None:
        with self.assertRaises(HarnessError):
            load_harness("/nonexistent/harness.yaml")

    def test_unresolvable_reference_lists_candidates(self) -> None:
        with self.assertRaisesRegex(HarnessError, "no-such-harness"):
            resolve_harness("no-such-harness")

    def test_resolve_none_returns_the_default_harness(self) -> None:
        self.assertIs(resolve_harness(None), default_harness())
        self.assertIs(resolve_harness(""), default_harness())
        self.assertEqual(resolve_harness(None).id, "common-v0")


class HarnessResolveTests(unittest.TestCase):
    def test_resolve_by_id_finds_repo_manifest(self) -> None:
        spec = resolve_harness("baseline-v0")
        self.assertEqual(spec.id, "baseline-v0")
        self.assertEqual(Path(spec.source).resolve(), BASELINE_YAML.resolve())

    def test_resolve_by_id_finds_the_default_manifest(self) -> None:
        spec = resolve_harness("common-v0")
        self.assertEqual(spec.id, "common-v0")
        self.assertEqual(Path(spec.source).resolve(), COMMON_YAML.resolve())

    def test_resolve_by_path_loads_file(self) -> None:
        self.assertEqual(resolve_harness(str(BASELINE_YAML)).id, "baseline-v0")


class TraceHarnessHeaderTests(unittest.TestCase):
    def test_jsonl_leads_with_harness_header_then_events(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            spec = HarnessSpec(id="header-check")
            trace = RunTrace(run_id="r1", output_path=path, harness=spec)
            trace.emit("agent.start")
            trace.emit("agent.end")

            self.assertEqual(len(trace.events), 2)
            asyncio.run(trace.flush())

            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records[0]["record_type"], "harness")
            self.assertEqual(records[0]["harness_id"], "header-check")
            self.assertEqual(records[0]["genes_hash"], spec.genes_hash)
            self.assertEqual(records[0]["harness"]["id"], "header-check")
            self.assertEqual([r["event_type"] for r in records[1:]],
                             ["agent.start", "agent.end"])

    def test_trace_without_harness_writes_no_header(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "run.jsonl"
            trace = RunTrace(run_id="r1", output_path=path)
            trace.emit("agent.start")

            asyncio.run(trace.flush())

            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records[0]["event_type"], "agent.start")


class RecoveryLLMErrorsParsingTests(unittest.TestCase):
    def test_default_materializes_all_categories_with_zero_retries(self) -> None:
        spec = from_dict({})
        self.assertEqual(set(spec.recovery.llm_errors),
                         set(LLM_RETRY_CATEGORIES))
        for policy in spec.recovery.llm_errors.values():
            self.assertEqual(policy.max_retries, 0)

    def test_partial_entry_fills_backoff_and_base_delay_defaults(self) -> None:
        spec = from_dict({"recovery": {"llm_errors": {
            "stream_truncated": {"max_retries": 2}}}})
        policy = spec.recovery.llm_errors["stream_truncated"]
        self.assertEqual((policy.max_retries, policy.backoff, policy.base_delay),
                         (2, "exponential", 0.5))
        untouched = [p.max_retries for category, p
                     in spec.recovery.llm_errors.items()
                     if category != "stream_truncated"]
        # One zero-retry entry per LLM_RETRY_CATEGORIES member.
        self.assertEqual(untouched, [0] * (len(LLM_RETRY_CATEGORIES) - 1))

    def test_explicit_entry_roundtrips_all_fields(self) -> None:
        spec = from_dict({"recovery": {"tool_error": "feed_error_and_continue",
                                       "llm_errors": {
                                           "provider_error": {
                                               "max_retries": 3,
                                               "backoff": "fixed",
                                               "base_delay": 1.5}}}})
        policy = spec.recovery.llm_errors["provider_error"]
        self.assertEqual((policy.max_retries, policy.backoff, policy.base_delay),
                         (3, "fixed", 1.5))

    def test_null_category_entry_falls_back_to_zero_retries(self) -> None:
        spec = from_dict({"recovery": {"llm_errors": {"stream_empty": None}}})
        self.assertEqual(spec.recovery.llm_errors["stream_empty"].max_retries, 0)

    def test_unknown_llm_error_category_is_rejected(self) -> None:
        with self.assertRaisesRegex(HarnessError, "unknown recovery.llm_errors"):
            from_dict({"recovery": {"llm_errors": {
                "network_flap": {"max_retries": 1}}}})

    def test_unknown_entry_key_is_rejected(self) -> None:
        with self.assertRaisesRegex(
                HarnessError,
                "unknown recovery.llm_errors.stream_truncated keys"):
            from_dict({"recovery": {"llm_errors": {
                "stream_truncated": {"max_retries": 1, "jitter": True}}}})

    def test_negative_max_retries_is_rejected(self) -> None:
        with self.assertRaisesRegex(HarnessError,
                                    "max_retries must be an integer >= 0"):
            from_dict({"recovery": {"llm_errors": {
                "stream_truncated": {"max_retries": -1}}}})

    def test_unknown_backoff_is_rejected(self) -> None:
        with self.assertRaisesRegex(HarnessError,
                                    "backoff must be one of"):
            from_dict({"recovery": {"llm_errors": {
                "stream_truncated": {"max_retries": 1,
                                     "backoff": "fibonacci"}}}})

    def test_bad_base_delay_is_rejected(self) -> None:
        with self.assertRaisesRegex(HarnessError,
                                    "base_delay must be a non-negative number"):
            from_dict({"recovery": {"llm_errors": {
                "stream_truncated": {"base_delay": "soon"}}}})


class LLMRetryPolicyDelayTests(unittest.TestCase):
    def test_fixed_backoff_keeps_delay_constant(self) -> None:
        policy = LLMRetryPolicy(max_retries=3, backoff="fixed", base_delay=0.25)
        self.assertEqual([policy.delay(attempt) for attempt in (1, 2, 3)],
                         [0.25, 0.25, 0.25])

    def test_exponential_backoff_doubles_each_attempt(self) -> None:
        policy = LLMRetryPolicy(max_retries=3, backoff="exponential",
                                base_delay=0.5)
        self.assertEqual([policy.delay(attempt) for attempt in (1, 2, 3)],
                         [0.5, 1.0, 2.0])

    def test_none_backoff_ignores_base_delay(self) -> None:
        policy = LLMRetryPolicy(max_retries=1, backoff="none", base_delay=9.9)
        self.assertEqual(policy.delay(1), 0.0)

    def test_retry_gene_change_is_visible_in_genes_hash(self) -> None:
        parent = HarnessSpec()
        child = HarnessSpec(recovery=RecoveryGenome(
            tool_error=parent.recovery.tool_error,
            llm_errors={**parent.recovery.llm_errors,
                        "stream_truncated": LLMRetryPolicy(
                            max_retries=2, backoff="fixed", base_delay=0.25)}))
        self.assertNotEqual(parent.genes_hash, child.genes_hash)


if __name__ == "__main__":
    unittest.main()
