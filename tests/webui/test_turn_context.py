import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2]))
sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.model_catalog import ModelCatalogError, catalog_from_dict
from agent_runtime.runtime_config import RuntimeConfig
from webui.turn_context import resolve_turn


CATALOG = {
    "default": "main",
    "models": [
        {
            "id": "main",
            "label": "Main",
            "base_url": "https://main.example/v1",
            "api_key": "main-key",
            "model_id": "main-model",
        },
        {
            "id": "other",
            "label": "Other",
            "base_url": "https://other.example/v1",
            "api_key": "other-key",
            "model_id": "other-model",
        },
    ],
}


class ResolveTurnTests(unittest.TestCase):
    def setUp(self) -> None:
        self.catalog = catalog_from_dict(CATALOG)
        self.base = RuntimeConfig(model="env-model", base_url=None,
                                  api_key="env-key", streaming=False)

    def test_snapshot_binds_the_catalog_endpoint(self) -> None:
        snapshot = resolve_turn(catalog=self.catalog, model_id="main",
                                harness_id="general-v1",
                                base_runtime=self.base)

        self.assertEqual(snapshot.model, "main")
        self.assertEqual(snapshot.runtime.model, "main-model")
        self.assertEqual(snapshot.runtime.base_url, "https://main.example/v1")
        self.assertEqual(snapshot.runtime.api_key, "main-key")
        self.assertFalse(snapshot.stream)
        self.assertEqual(snapshot.llm.model, "main-model")
        self.assertEqual(snapshot.harness.id, "general-v1")

    def test_snapshots_are_independent(self) -> None:
        first = resolve_turn(catalog=self.catalog, model_id="main",
                             harness_id="general-v1",
                             base_runtime=self.base)
        second = resolve_turn(catalog=self.catalog, model_id="other",
                              harness_id="terminal-v1",
                              base_runtime=self.base)

        self.assertEqual(first.runtime.model, "main-model")
        self.assertEqual(second.runtime.model, "other-model")
        self.assertNotEqual(first.harness.id, second.harness.id)

    def test_unknown_model_is_reported(self) -> None:
        with self.assertRaises(ModelCatalogError):
            resolve_turn(catalog=self.catalog, model_id="nope",
                         harness_id="general-v1", base_runtime=self.base)

    def test_summarizer_uses_the_same_endpoint(self) -> None:
        snapshot = resolve_turn(catalog=self.catalog, model_id="main",
                                harness_id="general-v1",
                                base_runtime=self.base)

        summarizer = snapshot.summarizer_factory()

        self.assertEqual(summarizer.model, "main-model")
        self.assertEqual(summarizer.base_url, "https://main.example/v1")

    def test_explicit_summary_model_wins(self) -> None:
        base = RuntimeConfig(model="env-model", base_url=None,
                             api_key="env-key", streaming=False,
                             summary_model="summary-model")
        snapshot = resolve_turn(catalog=self.catalog, model_id="main",
                                harness_id="general-v1", base_runtime=base)

        self.assertEqual(snapshot.summarizer_factory().model, "summary-model")


if __name__ == "__main__":
    unittest.main()
