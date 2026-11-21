"""Billed-token coercion: every provider spelling of the cache hit count."""

import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.agent.tokens import response_usage


class ResponseUsageTests(unittest.TestCase):
    def test_reads_the_flat_shape_the_provider_adapter_emits(self) -> None:
        response = {"usage": {"prompt_tokens": 100, "completion_tokens": 20,
                              "cached_tokens": 60}}

        self.assertEqual(response_usage(response), (100, 20, 60))

    def test_reads_the_openai_nested_shape(self) -> None:
        response = {"usage": {"prompt_tokens": 100, "completion_tokens": 20,
                              "prompt_tokens_details": {"cached_tokens": 40}}}

        self.assertEqual(response_usage(response), (100, 20, 40))

    def test_reads_the_deepseek_shape(self) -> None:
        response = {"usage": {"prompt_tokens": 100, "completion_tokens": 20,
                              "prompt_cache_hit_tokens": 80}}

        self.assertEqual(response_usage(response), (100, 20, 80))

    def test_missing_or_junk_usage_is_zero(self) -> None:
        self.assertEqual(response_usage({}), (0, 0, 0))
        self.assertEqual(response_usage({"usage": None}), (0, 0, 0))
        self.assertEqual(
            response_usage({"usage": {"prompt_tokens": "x",
                                      "prompt_tokens_details": "junk"}}),
            (0, 0, 0))


if __name__ == "__main__":
    unittest.main()
