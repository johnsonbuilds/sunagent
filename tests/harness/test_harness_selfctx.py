import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.harness import (
    from_dict,
    load_harness,
    MEMORY_STRATEGIES,
)


REPO_ROOT = Path(__file__).parents[2]


class SelfManagedGeneTests(unittest.TestCase):
    def test_strategy_registered(self):
        self.assertIn("self_managed_file", MEMORY_STRATEGIES)

    def test_new_gene_defaults(self):
        from agent_runtime.harness import MemoryGenome
        genome = MemoryGenome(strategy="self_managed_file")
        self.assertEqual(genome.reserve_chars, 12000)
        self.assertEqual(genome.mirror_path, ".live_ctx/LIVE_CTX_MAIN.md")
        self.assertEqual(genome.protect_turns, 2)
        self.assertEqual(genome.nudge_ratios, (0.25, 0.5, 0.75))
        self.assertEqual(genome.urgent_mult, 2.0)
        self.assertEqual(genome.retry_margin_chars, 6000)
        self.assertEqual(genome.max_rollback_retries, 8)
        self.assertEqual(genome.edit_gate, "fit")

    def test_new_harness_loads(self):
        spec = load_harness(REPO_ROOT / "harnesses" / "common-v1-selfctx.yaml")
        self.assertEqual(spec.id, "common-v1-selfctx")
        self.assertEqual(spec.parent, "common-v0")
        self.assertEqual(spec.memory.strategy, "self_managed_file")
        self.assertIn("CONTEXT FILE", spec.prompt.system)

    def test_old_manifests_still_load(self):
        spec = load_harness(REPO_ROOT / "harnesses" / "common-v0.yaml")
        self.assertEqual(spec.memory.strategy, "llm_summary")

    def test_genes_hash_covers_new_fields(self):
        base = from_dict({"id": "h", "memory": {"strategy": "self_managed_file"}})
        other = from_dict({"id": "h", "memory": {"strategy": "self_managed_file",
                                                 "reserve_chars": 1}})
        self.assertNotEqual(base.genes_hash, other.genes_hash)

    def test_bad_edit_gate_rejected(self):
        with self.assertRaises(Exception):
            from_dict({"memory": {"strategy": "self_managed_file",
                                  "edit_gate": "grow"}})


if __name__ == "__main__":
    unittest.main()
