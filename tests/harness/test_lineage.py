import sys
import tempfile
import unittest
from pathlib import Path
from textwrap import dedent

import yaml


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.harness import HarnessError, from_dict
from agent_runtime.lineage import (
    changed_genes,
    derive,
    flatten_genes,
    format_gene_diff,
    format_tree,
    load_all_harnesses,
    manifest_text,
    validate_lineage,
)


BASELINE = """
id: baseline-v0
parent: null
mutation: null
reason: extract current hardcoded behavior
"""


def write_manifest(directory: Path, text: str) -> Path:
    data = yaml.safe_load(dedent(text))
    path = directory / f"{data['id']}.yaml"
    path.write_text(dedent(text))
    return path


class TempHarnessDir:
    def __init__(self, *manifests: str) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        for manifest in manifests:
            write_manifest(self.dir, manifest)

    def __enter__(self) -> Path:
        return self.dir

    def __exit__(self, *exc: object) -> None:
        self._tmp.cleanup()


CHILD_ONE_GENE = """
id: baseline-v1
parent: baseline-v0
mutation: enable verification
reason: test
verification:
  enabled: true
"""

CHILD_TWO_GENES = """
id: greedy-v1
parent: baseline-v0
mutation: everything at once
reason: test
verification:
  enabled: true
control:
  max_iterations: 20
"""


class LoadAllHarnessesTests(unittest.TestCase):
    def test_loads_every_yaml_keyed_by_id(self) -> None:
        with TempHarnessDir(BASELINE, CHILD_ONE_GENE) as directory:
            specs = load_all_harnesses(directory)
            self.assertEqual(sorted(specs), ["baseline-v0", "baseline-v1"])
            self.assertEqual(specs["baseline-v1"].parent, "baseline-v0")

    def test_duplicate_ids_are_rejected(self) -> None:
        with TempHarnessDir(BASELINE, CHILD_ONE_GENE) as directory:
            (directory / "baseline-v0-copy.yaml").write_text(
                dedent(BASELINE).replace("reason: extract current hardcoded behavior",
                                         "reason: copy"))
            with self.assertRaisesRegex(HarnessError, "duplicate harness id"):
                load_all_harnesses(directory)

    def test_missing_directory_is_rejected(self) -> None:
        with self.assertRaisesRegex(HarnessError, "harnesses directory not found"):
            load_all_harnesses("/nonexistent/harnesses")


class ValidateLineageTests(unittest.TestCase):
    def test_sound_lineage_has_no_problems(self) -> None:
        with TempHarnessDir(BASELINE, CHILD_ONE_GENE) as directory:
            specs = load_all_harnesses(directory)
            self.assertEqual(validate_lineage(specs), [])

    def test_missing_parent_is_reported(self) -> None:
        orphan = """
        id: orphan-v1
        parent: no-such-parent
        mutation: something
        reason: test
        verification:
          enabled: true
        """
        with TempHarnessDir(BASELINE, orphan) as directory:
            specs = load_all_harnesses(directory)
            problems = validate_lineage(specs)
            self.assertTrue(any("parent 'no-such-parent' not found" in p
                                for p in problems))

    def test_id_filename_mismatch_is_reported(self) -> None:
        with TempHarnessDir(BASELINE, CHILD_ONE_GENE) as directory:
            specs = load_all_harnesses(directory)
            path = Path(specs["baseline-v1"].source)
            path.rename(path.with_name("wrong-name.yaml"))
            specs = load_all_harnesses(directory)
            problems = validate_lineage(specs)
            self.assertTrue(any("does not match filename" in p for p in problems))

    def test_multi_gene_mutation_is_flagged(self) -> None:
        with TempHarnessDir(BASELINE, CHILD_TWO_GENES) as directory:
            specs = load_all_harnesses(directory)
            problems = validate_lineage(specs)
            self.assertTrue(any("touches 2 genes" in p for p in problems))

    def test_no_op_mutation_is_flagged(self) -> None:
        noop = """
        id: noop-v1
        parent: baseline-v0
        mutation: claimed change
        reason: test
        """
        with TempHarnessDir(BASELINE, noop) as directory:
            specs = load_all_harnesses(directory)
            problems = validate_lineage(specs)
            self.assertTrue(any("genes are identical" in p for p in problems))


class TreeTests(unittest.TestCase):
    def test_tree_renders_parents_before_children(self) -> None:
        grandchild = """
        id: baseline-v2
        parent: baseline-v1
        mutation: add stderr-aware recovery
        reason: test
        recovery:
          tool_error: feed_error_and_continue
        """
        with TempHarnessDir(BASELINE, CHILD_ONE_GENE, grandchild) as directory:
            specs = load_all_harnesses(directory)
            tree = format_tree(specs)
        lines = tree.splitlines()
        self.assertEqual(lines[0].strip(), "baseline-v0  (root)")
        self.assertEqual(lines[1].strip(), "baseline-v1 <- baseline-v0  enable verification")
        self.assertEqual(lines[2].strip(),
                         "baseline-v2 <- baseline-v1  add stderr-aware recovery")


class GeneDiffTests(unittest.TestCase):
    def test_flatten_uses_dotted_paths(self) -> None:
        flat = flatten_genes(from_dict({}))
        self.assertIn("control.max_iterations", flat)
        self.assertEqual(flat["verification.enabled"], False)

    def test_changed_genes_counts_top_level_genes(self) -> None:
        parent = from_dict({})
        child = from_dict({"verification": {"enabled": True},
                           "control": {"max_iterations": 20}})
        self.assertEqual(changed_genes(parent, child),
                         [("control", 1), ("verification", 1)])

    def test_format_gene_diff_shows_field_changes(self) -> None:
        parent = from_dict({})
        child = from_dict({"verification": {"enabled": True}})
        diff = format_gene_diff(parent, child)
        self.assertIn("verification.enabled: False -> True", diff)


class ManifestTextTests(unittest.TestCase):
    FULL_GENES = {
        "prompt": {"system": "You are a shell agent.",
                   "iteration_limit_notice": "Stop and summarize now."},
        "tools": {"enabled": ["run_command", "read_file"]},
        "control": {
            "max_iterations": 7,
            "max_observation_chars": 1234,
            "spill_preview_chars": 99,
            "finish_violation_limit": 2,
            "budget_reminder": {"enabled": True,
                                "at_fractions": [0.5, 0.8]},
        },
        "context": {
            "strategy": "compact_observations",
            "budget_unit": "tokens",
            "head_chars": 111,
            "context_budget": 5000,
            "window_rounds": 7,
            "keep_recent_rounds": 3,
            "summary_tail_rounds": 4,
            "user_preserve_budget": 1200,
            "summary_max_tokens": 512,
            "retrieval_exempt_tools": ["grep_search", "read_file"],
        },
        "recovery": {
            "tool_error": "feed_error_and_continue",
            "tool_error_max_retries": 2,
            "llm_errors": {"stream_truncated": {
                "max_retries": 2, "backoff": "fixed", "base_delay": 0.25}},
        },
        "verification": {
            "enabled": True,
            "mode": "task_result",
            "require": ["solution_description", "evidence",
                        "command_to_verify"],
        },
        "skills": ["python", "shell"],
    }

    def test_manifest_text_roundtrips_every_gene(self) -> None:
        spec = from_dict(self.FULL_GENES)
        reloaded = from_dict(yaml.safe_load(manifest_text(spec)))
        self.assertEqual(reloaded.genes_dict(), spec.genes_dict())
        self.assertEqual(reloaded.genes_hash, spec.genes_hash)

    def test_derive_write_and_reload_keeps_non_default_genes(self) -> None:
        parent = from_dict(self.FULL_GENES)
        child, _ = derive(parent, "child-v1", "bump iterations", None,
                          [("control.max_iterations", "9")])
        reloaded = from_dict(yaml.safe_load(manifest_text(child)))
        self.assertEqual(reloaded.genes_dict(), child.genes_dict())
        self.assertEqual(reloaded.context.head_chars, 111)
        self.assertEqual(reloaded.control.spill_preview_chars, 99)

    def test_derived_outputs_are_not_written_as_genes(self) -> None:
        spec = from_dict({})
        data = yaml.safe_load(manifest_text(spec))
        self.assertNotIn("source", data)
        self.assertNotIn("genes_hash", data)
        self.assertEqual(list(data)[:4],
                         ["id", "parent", "mutation", "reason"])


class DeriveTests(unittest.TestCase):
    def test_derive_single_gene_change(self) -> None:
        parent = from_dict({})
        child, diff = derive(parent, "baseline-v1", "enable verification",
                             "testing", [("verification.enabled", "true")])
        self.assertEqual(parent.id, "baseline-v0")
        self.assertEqual(child.id, "baseline-v1")
        self.assertEqual(child.parent, parent.id)
        self.assertTrue(child.verification.enabled)
        self.assertIn("verification.enabled: False -> True", diff)
        self.assertNotEqual(child.genes_hash, parent.genes_hash)

    def test_derive_parses_scalars_and_lists(self) -> None:
        parent = from_dict({})
        child, _ = derive(parent, "v1", "bump and extend tools", None, [
            ("control.max_iterations", "20"),
            ("tools.enabled", "run_command, read_file"),
            ("prompt.system", "You are a shell agent."),
        ])
        self.assertEqual(child.control.max_iterations, 20)
        self.assertEqual(child.tools.enabled, ("run_command", "read_file"))
        self.assertEqual(child.prompt.system, "You are a shell agent.")

    def test_derive_mutates_nested_llm_retry_policy(self) -> None:
        parent = from_dict({})
        child, diff = derive(parent, "retry-v1", "retry truncated streams",
                             None,
                             [("recovery.llm_errors.stream_truncated.max_retries",
                               "2")])
        policy = child.recovery.llm_errors["stream_truncated"]
        self.assertEqual(policy.max_retries, 2)
        self.assertEqual(policy.backoff, "exponential")
        self.assertIn(
            "recovery.llm_errors.stream_truncated.max_retries: 0 -> 2", diff)

    def test_manifest_text_roundtrips_llm_retry_policies(self) -> None:
        spec = from_dict({"recovery": {"llm_errors": {
            "stream_empty": {"max_retries": 1, "backoff": "none"},
            "stream_truncated": {"max_retries": 2, "base_delay": 0.25}}}})
        rebuilt = from_dict(yaml.safe_load(manifest_text(spec)))
        self.assertEqual(spec.recovery.tool_error, rebuilt.recovery.tool_error)
        self.assertEqual(spec.recovery.llm_errors, rebuilt.recovery.llm_errors)

    def test_derive_unknown_path_is_rejected(self) -> None:
        parent = from_dict({})
        with self.assertRaisesRegex(HarnessError, "unknown gene path"):
            derive(parent, "v1", "bad", None, [("control.timeout", "30")])

    def test_manifest_text_roundtrips_through_from_dict(self) -> None:
        parent = from_dict({})
        child, _ = derive(parent, "roundtrip-v1", "bump iterations", None,
                          [("control.max_iterations", "3")])
        replica = from_dict(yaml.safe_load(manifest_text(child)))
        self.assertEqual(replica.genes_hash, child.genes_hash)
        self.assertEqual(replica.parent, "baseline-v0")
        self.assertEqual(replica.mutation, "bump iterations")


if __name__ == "__main__":
    unittest.main()
