import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from orchestrator.access import AccessDenied
from orchestrator.view import PreparedView


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name) / "work"
        self.workspace.mkdir()
        self.run = self.workspace / ".private/runtime/current"
        self.source = self.workspace / "topic/input.txt"
        self.source.parent.mkdir()
        self.source.write_text("synthetic input")

    def tearDown(self):
        self.temp.cleanup()

    def test_history_sources_are_refused(self):
        for relative in ("topic/.agent/draft.txt", ".private/runtime/old/draft.txt", "Доступы.txt", ".env"):
            path = self.workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic")
            with self.subTest(relative=relative), self.assertRaises(AccessDenied):
                PreparedView(self.workspace, self.run, [path], auth_home=self.workspace / "no-auth")

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is not installed")
    def test_physical_view_hides_all_other_drafts_and_role_history(self):
        files = {
            "topic/.agent/old-draft.txt": "history",
            "topic/unlisted-draft.txt": "history",
            ".private/runtime/previous/draft.txt": "history",
            ".private/runtime/current/codex-analyst/sessions/secret.txt": "analyst history",
            ".private/knowledge/architecture/overview.md": "current wiki",
            ".private/knowledge/legacy/old.md": "old wiki",
            ".private/knowledge/legacy/domain-checks.md": "normative checks",
            ".private/repositories/project/src.py": "synthetic code",
            ".private/repositories/project/.env": "synthetic secret",
            ".private/repositories/project/.git/config": "synthetic credentials",
        }
        for relative, value in files.items():
            path = self.workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        view = PreparedView(self.workspace, self.run, [self.source], auth_home=self.workspace / "no-auth")
        paths = [str(self.workspace / relative) for relative in files]
        script = ("import json,pathlib; paths=" + repr(paths) +
                  "; print(json.dumps({p: pathlib.Path(p).read_text() if pathlib.Path(p).is_file() else None for p in paths}))")
        try:
            completed = subprocess.run(view.command("reviewer", "/usr/bin/python3", ["-c", script]),
                                       capture_output=True, text=True, timeout=15)
            if completed.returncode and "Creating new namespace failed" in completed.stderr:
                self.skipTest("host disables unprivileged namespaces")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            for relative in list(files)[:4] + [".private/knowledge/legacy/old.md"]:
                self.assertIsNone(result[str(self.workspace / relative)])
            self.assertEqual(result[str(self.workspace / ".private/knowledge/architecture/overview.md")], "current wiki")
            self.assertEqual(result[str(self.workspace / ".private/knowledge/legacy/domain-checks.md")], "normative checks")
            self.assertEqual(result[str(self.workspace / ".private/repositories/project/src.py")], "synthetic code")
            self.assertIsNone(result[str(self.workspace / ".private/repositories/project/.env")])
            self.assertIsNone(result[str(self.workspace / ".private/repositories/project/.git/config")])
            write_script = "from pathlib import Path; Path(" + repr(str(self.source)) + ").write_text('changed')"
            denied = subprocess.run(view.command("reviewer", "/usr/bin/python3", ["-c", write_script]),
                                    capture_output=True, text=True, timeout=15)
            self.assertNotEqual(denied.returncode, 0)
            self.assertEqual(self.source.read_text(), "synthetic input")
        finally:
            view.close()

    def test_synthetic_mode_does_not_mount_corporate_trees(self):
        view = PreparedView(self.workspace, self.run, [self.source], include_corporate=False,
                            auth_home=self.workspace / "no-auth")
        try:
            command = view.command("analyst", "/usr/bin/true")
            self.assertNotIn("--die-with-parent", command)
            self.assertNotIn(str(self.workspace / ".private/repositories"), command)
            self.assertIn(str(self.source), command)
            self.assertNotIn(str(self.source.parent), [command[i+1] for i, item in enumerate(command[:-1]) if item == "--ro-bind"])
        finally:
            view.close()

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is not installed")
    def test_documented_auth_limitation_uses_only_synthetic_credentials(self):
        auth_home = self.workspace.parent / "synthetic-auth"
        auth_home.mkdir()
        fake_auth = auth_home / "auth.json"
        fake_auth.write_text('{"synthetic": true}')
        (auth_home / "sessions").mkdir()
        (auth_home / "sessions/old.jsonl").write_text("unrelated history")
        view = PreparedView(self.workspace, self.run, [self.source], include_corporate=False, auth_home=auth_home)
        role_auth = self.run / "codex-reviewer/auth.json"
        script = ("import json,pathlib; p=pathlib.Path(" + repr(str(role_auth)) + "); "
                  "print(json.dumps({'synthetic_auth_readable': json.loads(p.read_text())['synthetic'], "
                  "'global_sessions_visible': pathlib.Path(" + repr(str(auth_home / "sessions")) + ").exists()}))")
        try:
            completed = subprocess.run(view.command("reviewer", "/usr/bin/python3", ["-c", script]),
                                       capture_output=True, text=True, timeout=15)
            if completed.returncode and "Creating new namespace failed" in completed.stderr:
                self.skipTest("host disables unprivileged namespaces")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            result = json.loads(completed.stdout)
            # This is explicitly a limitation, not an asserted credential-read
            # boundary. Official read_only permits reads of its role auth mount.
            self.assertTrue(result["synthetic_auth_readable"])
            self.assertFalse(result["global_sessions_visible"])
            denied = subprocess.run(view.command("reviewer", "/usr/bin/python3", ["-c",
                "from pathlib import Path; Path(" + repr(str(role_auth)) + ").write_text('changed')"]),
                capture_output=True, text=True, timeout=15)
            self.assertNotEqual(denied.returncode, 0)
            self.assertEqual(fake_auth.read_text(), '{"synthetic": true}')
            # Only an empty mount point may remain in the role home; no copy.
            self.assertEqual(role_auth.read_text(), "")
        finally:
            view.close()


if __name__ == "__main__":
    unittest.main()
