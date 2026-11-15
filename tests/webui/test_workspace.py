import contextlib
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[2]))
sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from starlette.testclient import TestClient

from webui import server
from webui.board import BoardStore
from webui.sessions import SessionStore
from webui.workspace import (
    InvalidPath,
    ProtectedFile,
    WorkspaceBrowser,
    WorkspaceNotFound,
    _parse_diff,
    _parse_status,
    is_protected_name,
)

GIT = shutil.which("git")


def git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
    )


@contextlib.contextmanager
def repo(files: dict[str, bytes] | None = None):
    """A temporary repository with one commit."""
    with TemporaryDirectory() as root:
        path = Path(root)
        git(path, "init", "-q")
        git(path, "config", "user.email", "test@example.com")
        git(path, "config", "user.name", "Test")
        for name, content in (files or {}).items():
            target = path / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(content)
        git(path, "add", "-A")
        git(path, "commit", "-qm", "init")
        yield path


@unittest.skipUnless(GIT, "git is not installed")
class WorkspaceTreeTests(unittest.TestCase):
    def test_lists_one_level_and_hides_fallback_ignored(self) -> None:
        with TemporaryDirectory() as root:
            base = Path(root)
            (base / "src").mkdir()
            (base / "src" / "a.py").write_text("x = 1\n")
            (base / "README.md").write_text("hi\n")
            (base / "workspace").mkdir()
            (base / "workspace" / "data.json").write_text("{}")
            (base / "jobs").mkdir()
            (base / "node_modules").mkdir()
            (base / ".git").mkdir()
            browser = WorkspaceBrowser(base)

            tree = browser.list_dir("")

            self.assertEqual([entry["name"] for entry in tree["entries"]],
                             ["src", "README.md"])
            self.assertTrue(tree["entries"][0]["type"] == "dir")
            self.assertEqual(tree["hidden_ignored"], 2)
            self.assertFalse(tree["is_git"])
            self.assertFalse(tree["truncated"])

    def test_marks_protected_files_and_keeps_them_listed(self) -> None:
        with TemporaryDirectory() as root:
            base = Path(root)
            for name in (".env", ".env.local", "model.json", "cert.pem",
                         "id_rsa", "credentials.json", "notes.txt"):
                (base / name).write_text("secret")
            entries = {
                entry["name"]: entry
                for entry in WorkspaceBrowser(base).list_dir("")["entries"]
            }

            for name in (".env", ".env.local", "model.json", "cert.pem",
                         "id_rsa", "credentials.json"):
                self.assertTrue(entries[name]["protected"], name)
            self.assertFalse(entries["notes.txt"]["protected"])

    def test_truncates_a_large_directory(self) -> None:
        with TemporaryDirectory() as root:
            base = Path(root)
            for index in range(6):
                (base / f"file{index}.txt").write_text(str(index))
            with patch("webui.workspace.MAX_TREE_ENTRIES", 3):
                tree = WorkspaceBrowser(base).list_dir("")
            self.assertTrue(tree["truncated"])
            self.assertEqual(len(tree["entries"]), 3)

    def test_shows_ignored_entries_on_request(self) -> None:
        with TemporaryDirectory() as root:
            base = Path(root)
            (base / "workspace").mkdir()
            (base / "keep.txt").write_text("keep")
            browser = WorkspaceBrowser(base)

            hidden = browser.list_dir("")
            self.assertEqual(hidden["hidden_ignored"], 1)
            shown = browser.list_dir("", include_ignored=True)
            names = {entry["name"]: entry for entry in shown["entries"]}
            self.assertIn("workspace", names)
            self.assertTrue(names["workspace"]["ignored"])
            self.assertEqual(shown["hidden_ignored"], 0)

    def test_rejects_escapes_hard_skips_and_wrong_kinds(self) -> None:
        with TemporaryDirectory() as root:
            base = Path(root)
            (base / "file.txt").write_text("hi")
            browser = WorkspaceBrowser(base)

            for path in ("..", "../etc", "/etc", ".git", "node_modules"):
                with self.assertRaises((InvalidPath, WorkspaceNotFound)):
                    browser.list_dir(path)
            with self.assertRaises(InvalidPath):
                browser.list_dir("file.txt")
            with self.assertRaises(WorkspaceNotFound):
                browser.list_dir("missing")

    def test_names_protected_patterns(self) -> None:
        for name in (".env", ".env.production", "model.json", "a.pem",
                     "b.key", "c.p12", "id_rsa", "id_rsa.pub",
                     "credentials", "credentials.json", ".netrc"):
            self.assertTrue(is_protected_name(name), name)
        for name in ("environment.py", "main.py", "keyboard.ts", ".envrc"):
            self.assertFalse(is_protected_name(name), name)


@unittest.skipUnless(GIT, "git is not installed")
class WorkspaceReadTests(unittest.TestCase):
    def test_preserves_crlf_and_counts_lines(self) -> None:
        with TemporaryDirectory() as root:
            base = Path(root)
            (base / "a.txt").write_bytes(b"one\r\ntwo\r\n")
            data = WorkspaceBrowser(base).read_file("a.txt")

            self.assertFalse(data["binary"])
            self.assertEqual(data["content"], "one\r\ntwo\r\n")
            self.assertEqual(data["lines"], 2)
            self.assertEqual(data["encoding"], "utf-8")
            self.assertFalse(data["truncated"])

    def test_empty_and_binary_files(self) -> None:
        with TemporaryDirectory() as root:
            base = Path(root)
            (base / "empty.txt").write_bytes(b"")
            (base / "blob.bin").write_bytes(b"abc\x00def")
            browser = WorkspaceBrowser(base)

            empty = browser.read_file("empty.txt")
            self.assertEqual(empty["content"], "")
            self.assertEqual(empty["lines"], 0)

            blob = browser.read_file("blob.bin")
            self.assertTrue(blob["binary"])
            self.assertIsNone(blob["content"])
            self.assertIsNone(blob["lines"])

    def test_truncates_by_line_count(self) -> None:
        with TemporaryDirectory() as root:
            base = Path(root)
            (base / "many.txt").write_text("a\nb\nc\nd\n")
            with patch("webui.workspace.MAX_PREVIEW_LINES", 2):
                data = WorkspaceBrowser(base).read_file("many.txt")
            self.assertTrue(data["truncated"])
            self.assertEqual(data["lines"], 2)
            self.assertEqual(data["content"], "a\nb\n")

    def test_truncates_large_preview_to_byte_budget(self) -> None:
        with TemporaryDirectory() as root:
            base = Path(root)
            (base / "big.txt").write_bytes(b"0123456789abcdef")
            with patch("webui.workspace.MAX_PREVIEW_BYTES", 8), \
                    patch("webui.workspace.PREVIEW_BYTES", 4):
                data = WorkspaceBrowser(base).read_file("big.txt")
            self.assertTrue(data["truncated"])
            self.assertEqual(data["content"], "0123")

    def test_protected_directory_missing_and_symlink(self) -> None:
        with TemporaryDirectory() as root:
            base = Path(root)
            (base / ".env").write_text("KEY=secret")
            (base / "dir").mkdir()
            (base / "target.txt").write_text("target")
            os.symlink(base / "target.txt", base / "link.txt")
            browser = WorkspaceBrowser(base)

            with self.assertRaises(ProtectedFile):
                browser.read_file(".env")
            with self.assertRaises(InvalidPath):
                browser.read_file("dir")
            with self.assertRaises(WorkspaceNotFound):
                browser.read_file("missing.txt")
            with self.assertRaises(InvalidPath):
                browser.read_file("link.txt")
            with self.assertRaises(InvalidPath):
                browser.read_file("../outside.txt")


@unittest.skipUnless(GIT, "git is not installed")
class WorkspaceChangesTests(unittest.TestCase):
    def test_modified_unstaged_file(self) -> None:
        with repo({"README.md": b"one\ntwo\n"}) as base:
            (base / "README.md").write_text("one\nchanged\n")
            data = WorkspaceBrowser(base).changes()

            self.assertTrue(data["is_git"])
            self.assertEqual(data["repo_root"], str(base.resolve()))
            self.assertEqual(data["ahead"], 0)
            self.assertEqual(data["behind"], 0)
            self.assertIsNone(data["upstream"])
            self.assertRegex(data["head"] or "", r"^[0-9a-f]{7}$")
            entry = data["entries"][0]
            self.assertEqual(entry["path"], "README.md")
            self.assertEqual(entry["status"], "modified")
            self.assertTrue(entry["unstaged"])
            self.assertFalse(entry["staged"])
            self.assertEqual((entry["additions"], entry["deletions"]), (1, 1))
            self.assertFalse(entry["binary"])
            self.assertEqual(data["counts"]["total"], 1)
            self.assertEqual(data["counts"]["modified"], 1)

    def test_staged_and_unstaged_at_once(self) -> None:
        with repo({"a.txt": b"base\n"}) as base:
            (base / "a.txt").write_text("staged\n")
            git(base, "add", "a.txt")
            (base / "a.txt").write_text("worktree\n")
            entry = WorkspaceBrowser(base).changes()["entries"][0]

            self.assertEqual(entry["status"], "modified")
            self.assertTrue(entry["staged"])
            self.assertTrue(entry["unstaged"])

    def test_added_deleted_and_untracked(self) -> None:
        with repo({"keep.txt": b"keep\n", "gone.txt": b"gone\n"}) as base:
            (base / "new.txt").write_text("new\n")
            git(base, "add", "new.txt")
            (base / "gone.txt").unlink()
            (base / "untracked.txt").write_text("one\ntwo\n")
            data = WorkspaceBrowser(base).changes()
            by_path = {entry["path"]: entry for entry in data["entries"]}

            self.assertEqual(by_path["new.txt"]["status"], "added")
            self.assertTrue(by_path["new.txt"]["staged"])
            self.assertEqual(by_path["gone.txt"]["status"], "deleted")
            self.assertTrue(by_path["gone.txt"]["unstaged"])
            self.assertEqual(by_path["untracked.txt"]["status"], "untracked")
            self.assertEqual(by_path["untracked.txt"]["additions"], 2)
            self.assertEqual(by_path["untracked.txt"]["deletions"], 0)
            self.assertEqual(data["counts"], {
                "total": 3,
                "modified": 0,
                "added": 1,
                "deleted": 1,
                "renamed": 0,
                "untracked": 1,
                "conflicted": 0,
            })

    def test_rename_reports_old_path_and_stats(self) -> None:
        with repo({"old.txt": b"one\ntwo\nthree\n"}) as base:
            git(base, "mv", "old.txt", "new.txt")
            entry = WorkspaceBrowser(base).changes()["entries"][0]

            self.assertEqual(entry["status"], "renamed")
            self.assertEqual(entry["path"], "new.txt")
            self.assertEqual(entry["old_path"], "old.txt")
            self.assertTrue(entry["staged"])
            self.assertEqual((entry["additions"], entry["deletions"]), (0, 0))

        with repo({"old.txt": b"one\ntwo\n"}) as base:
            git(base, "mv", "old.txt", "new.txt")
            (base / "new.txt").write_text("one\nchanged\n")
            entry = WorkspaceBrowser(base).changes()["entries"][0]
            self.assertEqual(entry["status"], "renamed")
            self.assertEqual(entry["old_path"], "old.txt")
            self.assertEqual((entry["additions"], entry["deletions"]), (1, 1))

    def test_gitignore_hides_entries_and_tree_can_show_them(self) -> None:
        with repo({".gitignore": b"build/\n*.log\n", "app.py": b"print(1)\n"}) as base:
            (base / "build").mkdir()
            (base / "build" / "out.txt").write_text("x")
            (base / "debug.log").write_text("x")
            browser = WorkspaceBrowser(base)

            tree = browser.list_dir("")
            self.assertEqual([entry["name"] for entry in tree["entries"]],
                             [".gitignore", "app.py"])
            self.assertEqual(tree["hidden_ignored"], 2)
            shown = browser.list_dir("", include_ignored=True)
            self.assertEqual(
                {entry["name"] for entry in shown["entries"]},
                {".gitignore", "app.py", "build", "debug.log"},
            )

    def test_tracked_runtime_directory_stays_hidden_by_default(self) -> None:
        with repo({"jobs/run/config.json": b"{}\n",
                   "app.py": b"print(1)\n"}) as base:
            browser = WorkspaceBrowser(base)
            hidden = browser.list_dir("")
            self.assertNotIn("jobs",
                             {entry["name"] for entry in hidden["entries"]})
            shown = browser.list_dir("", include_ignored=True)
            self.assertIn("jobs",
                          {entry["name"] for entry in shown["entries"]})

    def test_binary_change_has_no_line_counts(self) -> None:
        with repo({"blob.bin": b"a\x00b"}) as base:
            (base / "blob.bin").write_bytes(b"a\x00c")
            entry = WorkspaceBrowser(base).changes()["entries"][0]
            self.assertTrue(entry["binary"])
            self.assertIsNone(entry["additions"])
            self.assertIsNone(entry["deletions"])

    def test_changes_truncate_but_keep_the_total(self) -> None:
        with repo({"a.txt": b"a\n", "b.txt": b"b\n"}) as base:
            (base / "a.txt").write_text("a2\n")
            (base / "b.txt").write_text("b2\n")
            with patch("webui.workspace.MAX_CHANGES", 1):
                data = WorkspaceBrowser(base).changes()
            self.assertTrue(data["truncated"])
            self.assertEqual(len(data["entries"]), 1)
            self.assertEqual(data["counts"]["total"], 2)

    def test_protected_untracked_file_is_marked(self) -> None:
        with repo({"keep.txt": b"keep\n"}) as base:
            (base / ".env").write_text("KEY=secret")
            entry = WorkspaceBrowser(base).changes()["entries"][0]
            self.assertEqual(entry["path"], ".env")
            self.assertTrue(entry["protected"])

    def test_non_git_workspace_is_not_an_error(self) -> None:
        with TemporaryDirectory() as root:
            base = Path(root)
            (base / "a.txt").write_text("a")
            browser = WorkspaceBrowser(base)

            data = browser.changes()
            self.assertFalse(data["is_git"])
            self.assertEqual(data["entries"], [])
            self.assertEqual(data["counts"]["total"], 0)
            with self.assertRaises(InvalidPath):
                browser.diff("a.txt")

    def test_repository_without_a_commit(self) -> None:
        with TemporaryDirectory() as root:
            base = Path(root)
            git(base, "init", "-q")
            git(base, "config", "user.email", "test@example.com")
            git(base, "config", "user.name", "Test")
            (base / "first.txt").write_text("one\ntwo\n")
            browser = WorkspaceBrowser(base)

            data = browser.changes()
            self.assertEqual(data["entries"][0]["status"], "untracked")
            self.assertEqual(data["entries"][0]["additions"], 2)
            diff = browser.diff("first.txt")
            self.assertEqual(diff["status"], "added")
            self.assertEqual(diff["hunks"][0]["new_count"], 2)

    def test_workspace_may_be_a_repository_subdirectory(self) -> None:
        with repo({"sub/app.py": b"one\n", "top.txt": b"top\n"}) as base:
            sub = base / "sub"
            (sub / "app.py").write_text("changed\n")
            browser = WorkspaceBrowser(sub)

            data = browser.changes()
            self.assertEqual(str(data["repo_root"]), str(base.resolve()))
            self.assertEqual([entry["path"] for entry in data["entries"]],
                             ["app.py"])
            tree = browser.list_dir("")
            self.assertEqual([entry["path"] for entry in tree["entries"]],
                             ["app.py"])
            diff = browser.diff("app.py")
            self.assertEqual(diff["status"], "modified")
            self.assertTrue(diff["hunks"])


@unittest.skipUnless(GIT, "git is not installed")
class WorkspaceDiffTests(unittest.TestCase):
    def test_modified_file_hunk_line_numbers(self) -> None:
        with repo({"a.txt": b"one\ntwo\nthree\nfour\n"}) as base:
            (base / "a.txt").write_text("one\nTWO\nthree\nfour\n")
            data = WorkspaceBrowser(base).diff("a.txt")

            self.assertEqual(data["status"], "modified")
            self.assertFalse(data["binary"])
            self.assertFalse(data["truncated"])
            hunk = data["hunks"][0]
            self.assertEqual(hunk["old_start"], 1)
            self.assertEqual(hunk["new_start"], 1)
            kinds = [line["kind"] for line in hunk["lines"]]
            self.assertIn("del", kinds)
            self.assertIn("add", kinds)
            deleted = next(line for line in hunk["lines"] if line["kind"] == "del")
            added = next(line for line in hunk["lines"] if line["kind"] == "add")
            self.assertEqual((deleted["old"], deleted["new"], deleted["text"]),
                             (2, None, "two"))
            self.assertEqual((added["old"], added["new"], added["text"]),
                             (None, 2, "TWO"))

    def test_untracked_file_is_all_additions(self) -> None:
        with repo({"keep.txt": b"keep\n"}) as base:
            (base / "new.txt").write_text("one\ntwo")
            data = WorkspaceBrowser(base).diff("new.txt")

            self.assertEqual(data["status"], "added")
            hunk = data["hunks"][0]
            self.assertEqual(hunk["header"], "@@ -0,0 +1,2 @@")
            self.assertEqual([line["new"] for line in hunk["lines"]
                              if line["kind"] == "add"], [1, 2])
            self.assertEqual(hunk["lines"][-1]["kind"], "note")

    def test_deleted_file_is_all_deletions(self) -> None:
        with repo({"gone.txt": b"one\ntwo\n"}) as base:
            (base / "gone.txt").unlink()
            data = WorkspaceBrowser(base).diff("gone.txt")

            self.assertEqual(data["status"], "deleted")
            self.assertEqual([line["kind"] for line in data["hunks"][0]["lines"]],
                             ["del", "del"])

    def test_rename_uses_old_path_and_reports_status(self) -> None:
        with repo({"old.txt": b"one\ntwo\n"}) as base:
            git(base, "mv", "old.txt", "new.txt")
            (base / "new.txt").write_text("one\nchanged\n")
            data = WorkspaceBrowser(base).diff("new.txt", "old.txt")

            self.assertEqual(data["status"], "renamed")
            self.assertEqual(data["old_path"], "old.txt")
            self.assertTrue(data["hunks"])

    def test_binary_file_has_no_hunks(self) -> None:
        with repo({"blob.bin": b"a\x00b"}) as base:
            (base / "blob.bin").write_bytes(b"a\x00c")
            data = WorkspaceBrowser(base).diff("blob.bin")

            self.assertTrue(data["binary"])
            self.assertEqual(data["hunks"], [])

    def test_protected_file_cannot_be_diffed(self) -> None:
        with repo({".env": b"KEY=1\n"}) as base:
            (base / ".env").write_text("KEY=2\n")
            with self.assertRaises(ProtectedFile):
                WorkspaceBrowser(base).diff(".env")

    def test_protected_rename_source_is_rejected(self) -> None:
        with repo({".env": b"KEY=1\n"}) as base:
            git(base, "mv", ".env", "env.txt")
            with self.assertRaises(ProtectedFile):
                WorkspaceBrowser(base).diff("env.txt", ".env")

    def test_unknown_or_missing_file(self) -> None:
        with repo({"a.txt": b"a\n"}) as base:
            with self.assertRaises(WorkspaceNotFound):
                WorkspaceBrowser(base).diff("missing.txt")


class StatusAndPatchParserTests(unittest.TestCase):
    def test_status_parser_handles_branch_renames_and_conflicts(self) -> None:
        raw = "\0".join([
            "# branch.oid 0123456789abcdef",
            "# branch.head main",
            "# branch.upstream origin/main",
            "# branch.ab +2 -3",
            "1 .M N... 100644 100644 100644 aaa bbb a.txt",
            "2 R. N... 100644 100644 100644 aaa bbb R100 new.txt",
            "old.txt",
            "u UU N... 100644 100644 100644 100644 aaa bbb ccc conflict.txt",
            "? untracked.txt",
            "",
        ])
        branch, head, upstream, ahead, behind, entries = _parse_status(raw)

        self.assertEqual(branch, "main")
        self.assertEqual(head, "0123456")
        self.assertEqual(upstream, "origin/main")
        self.assertEqual((ahead, behind), (2, 3))
        by_path = {entry["path"]: entry for entry in entries}
        self.assertEqual(by_path["a.txt"]["status"], "modified")
        self.assertEqual(by_path["new.txt"]["status"], "renamed")
        self.assertEqual(by_path["new.txt"]["old_path"], "old.txt")
        self.assertEqual(by_path["conflict.txt"]["status"], "conflicted")
        self.assertTrue(by_path["conflict.txt"]["staged"])
        self.assertTrue(by_path["conflict.txt"]["unstaged"])
        self.assertEqual(by_path["untracked.txt"]["status"], "untracked")
        self.assertFalse(by_path["untracked.txt"]["staged"])

    def test_status_parser_detached_and_initial(self) -> None:
        raw = "\0".join([
            "# branch.oid abcdef123456",
            "# branch.head (detached)",
            "",
        ])
        branch, head, _, _, _, _ = _parse_status(raw)
        self.assertEqual(branch, "abcdef1")
        self.assertEqual(head, "abcdef1")

        raw = "\0".join([
            "# branch.oid (initial)",
            "# branch.head main",
            "",
        ])
        branch, initial_head, _, _, _, _ = _parse_status(raw)
        self.assertEqual(branch, "main")
        self.assertIsNone(initial_head)

    def test_patch_parser_counts_and_truncates(self) -> None:
        patch = "\n".join([
            "diff --git a/a.txt b/a.txt",
            "--- a/a.txt",
            "+++ b/a.txt",
            "@@ -1,2 +1,3 @@",
            " one",
            "-two",
            "+TWO",
            "+extra",
            "\\ No newline at end of file",
            "",
        ])
        hunks, binary, truncated = _parse_diff(patch)
        self.assertFalse(binary)
        self.assertFalse(truncated)
        self.assertEqual(hunks[0]["old_count"], 2)
        self.assertEqual(hunks[0]["new_count"], 3)
        self.assertEqual(hunks[0]["lines"][-1]["kind"], "note")

        hunks, _, truncated = _parse_diff(patch, max_lines=2)
        self.assertTrue(truncated)
        self.assertEqual(len(hunks[0]["lines"]), 2)

    def test_patch_parser_detects_binary(self) -> None:
        hunks, binary, _ = _parse_diff(
            "Binary files a/blob.bin and b/blob.bin differ\n")
        self.assertTrue(binary)
        self.assertEqual(hunks, [])


@unittest.skipUnless(GIT, "git is not installed")
class WorkspaceApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self._root = TemporaryDirectory()
        self._base = Path(self._root.name)
        self._repo = Path(self._root.name) / "repo"
        self._repo.mkdir()
        git(self._repo, "init", "-q")
        git(self._repo, "config", "user.email", "test@example.com")
        git(self._repo, "config", "user.name", "Test")
        (self._repo / "README.md").write_text("one\n")
        (self._repo / ".env").write_text("KEY=secret\n")
        git(self._repo, "add", "README.md")
        git(self._repo, "commit", "-qm", "init")
        (self._repo / "README.md").write_text("one\nchanged\n")
        (self._repo / "new.txt").write_text("new\n")

        self.sessions = SessionStore(self._base / "sessions")
        self.board = BoardStore(self._base)
        self.browser = WorkspaceBrowser(self._repo)
        with patch.object(server, "credentials_from_env", return_value=None):
            app = server.create_app(self.sessions, self.board,
                                    workspace=self.browser,
                                    enable_executor=False)
        self.client = TestClient(app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.addCleanup(self._root.cleanup)

    def test_tree_file_changes_and_diff(self) -> None:
        tree = self.client.get("/api/workspace/tree")
        self.assertEqual(tree.status_code, 200)
        names = {entry["name"] for entry in tree.json()["entries"]}
        self.assertIn("README.md", names)
        self.assertIn(".env", names)

        file = self.client.get("/api/workspace/file",
                               params={"path": "README.md"})
        self.assertEqual(file.status_code, 200)
        self.assertEqual(file.json()["content"], "one\nchanged\n")

        changes = self.client.get("/api/workspace/changes")
        self.assertEqual(changes.status_code, 200)
        self.assertTrue(changes.json()["is_git"])
        # README.md is modified and both .env and new.txt are untracked.
        self.assertEqual(changes.json()["counts"]["total"], 3)
        self.assertEqual(changes.json()["counts"]["modified"], 1)
        self.assertEqual(changes.json()["counts"]["untracked"], 2)

        diff = self.client.get("/api/workspace/diff",
                                params={"path": "README.md"})
        self.assertEqual(diff.status_code, 200)
        self.assertTrue(diff.json()["hunks"])

    def test_error_statuses(self) -> None:
        self.assertEqual(
            self.client.get("/api/workspace/file",
                            params={"path": ".env"}).status_code, 403)
        self.assertEqual(
            self.client.get("/api/workspace/file",
                            params={"path": "nope"}).status_code, 404)
        self.assertEqual(
            self.client.get("/api/workspace/tree",
                            params={"path": ".."}).status_code, 400)
        self.assertEqual(
            self.client.get("/api/workspace/file",
                            params={"path": "missing"}).status_code, 404)
        self.assertEqual(
            self.client.get("/api/workspace/diff").status_code, 422)

    def test_ignored_switch(self) -> None:
        (self._repo / ".gitignore").write_text("ignored.txt\n")
        (self._repo / "ignored.txt").write_text("x")
        hidden = self.client.get("/api/workspace/tree").json()
        self.assertNotIn("ignored.txt",
                         {entry["name"] for entry in hidden["entries"]})
        shown = self.client.get("/api/workspace/tree",
                                params={"ignored": "true"}).json()
        self.assertIn("ignored.txt",
                      {entry["name"] for entry in shown["entries"]})


if __name__ == "__main__":
    unittest.main()
