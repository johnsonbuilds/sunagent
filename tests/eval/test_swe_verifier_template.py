"""Guardrails for the SWE-bench verifier template.

Three silent grading bugs once zeroed a whole 100-task run (every new
task scored 0 no matter the fix):
  1. `--no-header` in the pytest command aborts ancient pytest (exit 2).
  2. Generated test.sh missed `f2p/p2p = json.loads(...)`, so totals were
     string lengths and nothing ever matched.
  3. pytest 3.x prints no `PASSED <id>` summary lines; only verbose
     `<id> PASSED` exists there.
These tests pin the fixes so no future template edit can reintroduce them.
"""

import re
import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "evaluation" / "swe_bench"))

import build_swe_dataset as builder


def make_row(repo: str, f2p: list[str], p2p: list[str],
             test_file: str) -> dict:
    import json
    return {
        "instance_id": "org__repo-1",
        "repo": repo,
        "base_commit": "abc123",
        "problem_statement": "boom",
        "patch": "diff --git a/x.py b/x.py\n",
        "test_patch": f"diff --git a/{test_file} b/{test_file}\n"
                      f"--- a/{test_file}\n+++ b/{test_file}\n@@ +1\n",
        "FAIL_TO_PASS": json.dumps(f2p),
        "PASS_TO_PASS": json.dumps(p2p),
        "environment_setup_commit": "def456",
    }


PYTEST_ROW = make_row(
    "psf/requests",
    ["tests/test_x.py::test_new"],
    ["tests/test_x.py::test_old"],
    "tests/test_x.py")


def extract_pytest_ok(test_sh: str):
    match = re.search(r"(def pytest_ok.*?)(?=\ndef )", test_sh, re.S)
    assert match, "pytest_ok missing from generated test.sh"
    namespace: dict = {"re": re, "clean_log": ""}
    exec(match.group(1), namespace)
    return namespace["pytest_ok"]


class VerifierTemplateTests(unittest.TestCase):
    def test_pytest_command_has_no_forbidden_flags(self) -> None:
        plan = builder.plan_for(PYTEST_ROW)
        self.assertEqual(plan["runner"], "pytest")
        self.assertNotIn("--no-header", plan["command"])
        self.assertIn(" -v ", " " + plan["command"] + " ")

    def test_django_and_sympy_runners_unchanged(self) -> None:
        django = make_row("django/django", ["a.tests.test_x"],
                          [], "tests/app/tests.py")
        sympy = make_row("sympy/sympy", [], ["sympy/x/test_y.py"],
                           "sympy/x/test_y.py")
        self.assertEqual(builder.plan_for(django)["runner"], "django")
        self.assertEqual(builder.plan_for(sympy)["runner"], "sympy")

    def test_generated_test_sh_decodes_id_lists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            builder.build_task(Path(directory), PYTEST_ROW, "latest")
            text = (Path(directory) / "org__repo-1" / "tests"
                    / "test.sh").read_text()
        self.assertIn("json.loads(f2p)", text)
        self.assertIn("json.loads(p2p)", text)
        self.assertNotIn("--no-header", text)

    def test_pytest_ok_matches_modern_summary_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            builder.build_task(Path(directory), PYTEST_ROW, "latest")
            text = (Path(directory) / "org__repo-1" / "tests"
                    / "test.sh").read_text()
        check = extract_pytest_ok(text)
        check.__globals__["clean_log"] = (
            "PASSED tests/test_x.py::test_new\n"
            "PASSED tests/test_x.py::test_old\n")
        self.assertTrue(check("tests/test_x.py::test_new"))
        self.assertFalse(check("tests/test_x.py::test_missing"))

    def test_pytest_ok_matches_legacy_verbose_format(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            builder.build_task(Path(directory), PYTEST_ROW, "latest")
            text = (Path(directory) / "org__repo-1" / "tests"
                    / "test.sh").read_text()
        check = extract_pytest_ok(text)
        check.__globals__["clean_log"] = (
            "tests/test_x.py::test_new PASSED\n"
            "tests/test_x.py::test_old PASSED [ 50%]\n")
        self.assertTrue(check("tests/test_x.py::test_new"))
        self.assertTrue(check("tests/test_x.py::test_old"))
        self.assertFalse(check("tests/test_x.py::test_missing"))


if __name__ == "__main__":
    unittest.main()
