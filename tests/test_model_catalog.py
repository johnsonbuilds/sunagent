import json
import os
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from agent_runtime.model_catalog import (
    ModelCatalogError,
    catalog_candidates,
    catalog_from_dict,
    load_model_catalog,
)


def sample(api_key: str = "literal-key") -> dict:
    return {
        "default": "main",
        "models": [
            {
                "id": "main",
                "label": "Main",
                "base_url": "https://main.example/v1",
                "api_key": api_key,
                "model_id": "main-model",
            },
            {
                "id": "fast",
                "base_url": "https://fast.example/v1",
                "api_key": "fast-key",
                "model_id": "fast-model",
            },
        ],
    }


class CatalogFromDictTests(unittest.TestCase):
    def test_parses_entries_and_defaults_the_label_to_the_id(self) -> None:
        catalog = catalog_from_dict(sample())

        self.assertEqual(catalog.default, "main")
        self.assertEqual([entry.id for entry in catalog.entries],
                         ["main", "fast"])
        fast = catalog.require("fast")
        self.assertEqual(fast.label, "fast")
        self.assertEqual(fast.model_id, "fast-model")
        self.assertEqual(fast.base_url, "https://fast.example/v1")

    def test_default_falls_back_to_the_first_entry(self) -> None:
        data = sample()
        del data["default"]

        catalog = catalog_from_dict(data)

        self.assertEqual(catalog.default, "main")

    def test_env_reference_is_expanded(self) -> None:
        with patch.dict(os.environ, {"CATALOG_TEST_KEY": "from-env"}):
            catalog = catalog_from_dict(sample(api_key="${CATALOG_TEST_KEY}"))

        self.assertEqual(catalog.require("main").api_key, "from-env")

    def test_env_prefix_is_also_accepted(self) -> None:
        with patch.dict(os.environ, {"CATALOG_TEST_KEY": "from-env"}):
            catalog = catalog_from_dict(sample(api_key="env:CATALOG_TEST_KEY"))

        self.assertEqual(catalog.require("main").api_key, "from-env")

    def test_unset_env_reference_is_an_error(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ModelCatalogError):
                catalog_from_dict(sample(api_key="${DEFINITELY_NOT_SET}"))

    def test_duplicate_ids_are_rejected(self) -> None:
        data = sample()
        data["models"].append(dict(data["models"][0]))

        with self.assertRaises(ModelCatalogError):
            catalog_from_dict(data)

    def test_unknown_default_is_rejected(self) -> None:
        data = sample()
        data["default"] = "missing"

        with self.assertRaises(ModelCatalogError):
            catalog_from_dict(data)

    def test_required_fields_are_enforced(self) -> None:
        data = sample()
        del data["models"][0]["base_url"]

        with self.assertRaises(ModelCatalogError):
            catalog_from_dict(data)

    def test_unknown_keys_are_rejected(self) -> None:
        data = sample()
        data["models"][0]["temperature"] = 0.2

        with self.assertRaises(ModelCatalogError):
            catalog_from_dict(data)

    def test_public_view_hides_endpoint_and_secret(self) -> None:
        catalog = catalog_from_dict(sample(api_key="super-secret"))

        public = catalog.public()

        self.assertEqual(public[0], {"id": "main", "label": "Main",
                                     "model_id": "main-model"})
        self.assertNotIn("super-secret", json.dumps(public))
        self.assertNotIn("base_url", json.dumps(public))

    def test_require_reports_unknown_models(self) -> None:
        catalog = catalog_from_dict(sample())

        with self.assertRaises(ModelCatalogError):
            catalog.require("nope")


class LoadCatalogTests(unittest.TestCase):
    def test_explicit_path_must_exist(self) -> None:
        with TemporaryDirectory() as root:
            with self.assertRaises(ModelCatalogError):
                load_model_catalog(Path(root) / "missing.json")

    def test_loads_a_file(self) -> None:
        with TemporaryDirectory() as root:
            path = Path(root) / "model.json"
            path.write_text(json.dumps(sample()), encoding="utf-8")

            catalog = load_model_catalog(path)

            self.assertEqual(catalog.default, "main")

    def test_missing_file_falls_back_to_the_environment(self) -> None:
        with TemporaryDirectory() as root:
            with patch.dict(os.environ, {
                "MODEL_ID": "env-model",
                "LLM_BASE_URL": "https://env.example/v1",
                "LLM_API_KEY": "env-key",
            }):
                with patch(
                    "agent_runtime.model_catalog.catalog_candidates",
                    return_value=[Path(root) / "absent.json"],
                ):
                    catalog = load_model_catalog()

        self.assertEqual(catalog.entries[0].model_id, "env-model")
        self.assertEqual(catalog.entries[0].base_url,
                         "https://env.example/v1")
        self.assertEqual(catalog.entries[0].api_key, "env-key")

    def test_candidates_put_explicit_env_first(self) -> None:
        with patch.dict(os.environ,
                        {"AGENT_RUNTIME_MODEL_CATALOG": "/tmp/x.json"}):
            self.assertEqual(catalog_candidates(), [Path("/tmp/x.json")])


if __name__ == "__main__":
    unittest.main()
