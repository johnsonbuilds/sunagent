import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.tools.schema_gate import (
    nested_problem,
    shape_of,
    value_problem,
)

EDIT_SCHEMA = {
    "type": "object",
    "properties": {"old_text": {"type": "string", "minLength": 1},
                   "new_text": {"type": "string"}},
    "required": ["old_text", "new_text"],
    "additionalProperties": False,
}

EDITS_SCHEMA = {"type": "array", "minItems": 1, "items": EDIT_SCHEMA}

PARAMETERS = {"type": "object", "properties": {"path": {"type": "string"},
                                              "edits": EDITS_SCHEMA},
              "required": ["path", "edits"], "additionalProperties": False}


def problem(value, schema=EDITS_SCHEMA, path="edits", tool="apply_patch"):
    return value_problem(value, schema, path, tool=tool)


class ShapeRenderingTests(unittest.TestCase):
    def test_shape_lists_required_keys_in_schema_order(self) -> None:
        self.assertEqual(shape_of(EDIT_SCHEMA),
                         "{'old_text': ..., 'new_text': ...}")

    def test_shape_falls_back_to_properties_without_required(self) -> None:
        schema = {"type": "object", "properties": {"a": {}, "b": {}}}
        self.assertEqual(shape_of(schema), "{'a': ..., 'b': ...}")


class ObjectViolationTests(unittest.TestCase):
    def test_one_key_names_the_shape_and_the_keys_that_arrived(self) -> None:
        message = problem([{"old_text": "x = 1"}])
        self.assertEqual(
            message,
            "apply_patch: edits[1] must be exactly one object "
            "{'old_text': ..., 'new_text': ...}; got {'old_text'}")

    def test_split_edit_reports_the_first_object(self) -> None:
        message = problem([{"old_text": "x = 1"}, {"new_text": "x = 2"}])
        self.assertIn("edits[1] must be exactly one object", message)
        self.assertIn("got {'old_text'}", message)

    def test_non_object_item_names_the_type(self) -> None:
        message = problem(["x = 1"])
        self.assertIn("edits[1] must be exactly one object", message)
        self.assertIn("got a string", message)

    def test_additional_properties_is_rejected(self) -> None:
        message = problem([{"old_text": "a", "new_text": "b",
                            "start_line": 3}])
        self.assertIn("got {'new_text', 'old_text', 'start_line'}", message)

    def test_unknown_keys_are_sorted_and_quoted(self) -> None:
        message = problem([{"old_text": "a", "new_text": "b", "z": 1,
                            "c": 2}])
        self.assertIn("got {'c', 'new_text', 'old_text', 'z'}", message)


class EchoBoundingTests(unittest.TestCase):
    def test_values_are_never_echoed(self) -> None:
        secret = "classify the hidden token ALPHA-SECRET here"
        message = problem([{"old_text": secret}])
        self.assertNotIn("ALPHA-SECRET", message)
        self.assertIn("got {'old_text'}", message)

    def test_long_key_names_are_clipped(self) -> None:
        message = problem([{"k" * 100: 1}])
        self.assertNotIn("k" * 41, message)
        self.assertIn("k" * 40 + "...", message)

    def test_extra_keys_collapse_to_a_count(self) -> None:
        message = problem([{f"k{index:02d}": index for index in range(20)}])
        self.assertIn("'k07', and 12 more}", message)

    def test_message_length_does_not_grow_with_the_payload(self) -> None:
        small = problem([{"old_text": "x"}])
        large = problem([{"old_text": "x" * 200_000}])
        self.assertEqual(small, large)


class ArrayViolationTests(unittest.TestCase):
    def test_empty_array_is_named_non_empty(self) -> None:
        self.assertEqual(
            problem([]),
            "apply_patch: edits must be a non-empty array of "
            "{'old_text': ..., 'new_text': ...} objects")

    def test_non_array_value_names_the_type(self) -> None:
        message = problem('{"old_text": "x"}')
        self.assertIn("edits must be an array of "
                      "{'old_text': ..., 'new_text': ...} objects", message)
        self.assertIn("got a string", message)

    def test_higher_min_items_is_stated_as_a_count(self) -> None:
        schema = {"type": "array", "minItems": 3,
                  "items": {"type": "string"}}
        self.assertIn("must hold at least 3 items", problem([], schema))


class ScalarViolationTests(unittest.TestCase):
    def test_min_length_one_reads_as_non_empty(self) -> None:
        message = problem([{"old_text": "", "new_text": "x"}])
        self.assertEqual(
            message, "apply_patch: edits[1].old_text must be a non-empty string")

    def test_longer_min_length_reads_as_a_length(self) -> None:
        schema = {"type": "object", "properties": {"code": {"type": "string",
                                                            "minLength": 4}},
                  "required": ["code"]}
        self.assertIn("must be at least 4 characters",
                      problem({"code": "ab"}, schema))

    def test_wrong_type_names_the_type_that_arrived(self) -> None:
        cases = [(1, "an integer"), (1.5, "a number"), (True, "a boolean"),
                 (None, "null"), (["a"], "an array")]
        for value, phrase in cases:
            with self.subTest(value=value):
                message = problem([{"old_text": value, "new_text": "x"}])
                self.assertIn(f"edits[1].old_text must be a string; got {phrase}",
                              message)


class NestedDispatchTests(unittest.TestCase):
    def test_valid_arguments_pass(self) -> None:
        arguments = {"path": "a.py",
                     "edits": [{"old_text": "x", "new_text": "y"}]}
        self.assertIsNone(nested_problem(arguments, PARAMETERS,
                                         "apply_patch"))

    def test_nested_violation_is_found_from_the_schema(self) -> None:
        arguments = {"path": "a.py", "edits": [{"old_text": "x"}]}
        self.assertIn("edits[1] must be exactly one object",
                      nested_problem(arguments, PARAMETERS, "apply_patch"))

    def test_flat_alternating_edits_are_reported_before_execution(self) -> None:
        arguments = {"path": "a.py",
                     "edits": [{"old_text": "x"}, {"new_text": "y"}]}
        self.assertIn("edits[1] must be exactly one object",
                      nested_problem(arguments, PARAMETERS, "apply_patch"))

    def test_tool_name_prefixes_the_message(self) -> None:
        arguments = {"path": "a.py", "edits": []}
        self.assertTrue(nested_problem(arguments, PARAMETERS, "apply_patch")
                        .startswith("apply_patch: "))


if __name__ == "__main__":
    unittest.main()
