"""Input/evidence boundary tests on temporary synthetic workspaces only."""
from __future__ import annotations

from pathlib import Path
import subprocess
import tempfile
import unittest

from orchestrator.inputs import digest, discover, repositories, source_path, verify_evidence
from orchestrator.models import Evidence


class InputsTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="modus-inputs-")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.input = self.file("Тема/Исходник.txt", "Synthetic required behavior.\n")

    def file(self, relative, content="Synthetic material.\n"):
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return path

    def evidence(self, path, revision):
        return Evidence(path=path, revision=revision, symbol="synthetic-requirement",
                        observation="The referenced input establishes required behavior.")

    def git(self, root, *args):
        return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(root), *args],
                              capture_output=True, text=True, check=True).stdout.strip()

    def repository(self, name):
        root = self.workspace / ".private/repositories" / name
        root.mkdir(parents=True)
        self.git(root, "init", "-q")
        (root / "source.txt").write_text("Synthetic implementation.\n")
        self.git(root, "add", "source.txt")
        self.git(root, "-c", "user.name=Synthetic Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "Synthetic baseline")
        return root

    def test_primary_input_is_available_and_versioned_by_content(self):
        self.assertEqual(source_path(self.workspace, "Тема/Исходник.txt"), self.input)
        before = digest(self.input)
        self.assertTrue(before.startswith("sha256:"))
        self.input.write_text("Changed requirement.\n")
        self.assertNotEqual(digest(self.input), before)

    def test_discovery_handles_unicode_explicit_and_quoted_references_without_urls(self):
        second = self.file("topic/with spaces.md")
        third = self.file("topic/plain.pdf")
        prompt = "\n".join(["Материал: Тема/Исходник.txt", "`topic/with spaces.md`",
                            "See topic/plain.pdf", "Source: Тема/Исходник.txt",
                            "`https://example.invalid/topic/plain.pdf`", "`topic/missing.txt`"])
        found = discover(self.workspace, prompt)
        self.assertEqual(set(found), {self.input, second, third})
        self.assertEqual(len(found), 3)

    def test_history_private_runtime_and_credential_inputs_are_rejected(self):
        for relative in ["topic/.agent/research.json", ".private/knowledge/index.md",
                         ".private/runtime/old/source.txt", ".codex/agents/analyst.toml",
                         ".agents/skills/role/SKILL.md", "topic/Доступы.txt", "topic/auth.json",
                         "topic/.env.local", "topic/final.txt", "topic/draft.jira.txt",
                         "topic/notes.review.md"]:
            with self.subTest(path=relative):
                self.file(relative)
                with self.assertRaises(ValueError):
                    source_path(self.workspace, relative)

    def test_credential_paths_are_rejected_before_inventory_reads_them(self):
        for relative in [".aws/config", ".aws/credentials", "topic/.netrc", "topic/id_rsa",
                         "topic/id_ed25519", "topic/server.key", "topic/certificate.pem"]:
            with self.subTest(path=relative):
                self.file(relative, "Synthetic credential-shaped fixture; no secret.\n")
                with self.assertRaises(ValueError):
                    source_path(self.workspace, relative)

    def test_traversal_symlink_outside_workspace_and_directory_are_rejected(self):
        self.file("topic/inside.txt")
        outside = tempfile.TemporaryDirectory(prefix="modus-outside-")
        self.addCleanup(outside.cleanup)
        outside_file = Path(outside.name) / "outside.txt"
        outside_file.write_text("Unrelated private fixture.\n")
        (self.workspace / "topic/linked.txt").symlink_to(outside_file)
        for value in [str(outside_file), "topic/linked.txt", "topic", "../outside.txt"]:
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    source_path(self.workspace, value)

    def test_oversized_input_is_rejected_without_loading_its_content(self):
        path = self.workspace / "topic/large.pdf"
        path.parent.mkdir()
        with path.open("wb") as stream:
            stream.truncate(20_000_001)
        with self.assertRaises(ValueError):
            source_path(self.workspace, "topic/large.pdf")

    def test_evidence_requires_an_inventoried_source_and_exact_revision(self):
        relative = str(self.input.relative_to(self.workspace))
        version = digest(self.input)
        manifest = {"sources": {relative: version}, "repositories": {}}
        verify_evidence(self.workspace, [self.evidence(relative, version)], manifest)
        with self.assertRaises(ValueError):
            verify_evidence(self.workspace, [self.evidence(relative, "sha256:wrong")], manifest)
        self.file("topic/not-in-input.txt")
        for relative in ("topic/not-in-input.txt", "topic/missing.txt", "../escape.txt"):
            with self.subTest(path=relative):
                with self.assertRaises(ValueError):
                    verify_evidence(self.workspace, [self.evidence(relative, version)], manifest)

    def test_repository_evidence_uses_commit_and_rejects_credentials_or_git_data(self):
        version = "b" * 40
        relative_root = ".private/repositories/bi_react"
        manifest = {"sources": {}, "repositories": {relative_root: version}}
        relative = relative_root + "/src/entry.txt"
        self.file(relative)
        verify_evidence(self.workspace, [self.evidence(relative, version)], manifest)
        with self.assertRaises(ValueError):
            verify_evidence(self.workspace, [self.evidence(relative, "c" * 40)], manifest)
        for name in (".env.production", "auth.json", "Доступы.txt", ".git/config"):
            path = relative_root + "/" + name
            self.file(path)
            with self.subTest(path=path):
                with self.assertRaises(ValueError):
                    verify_evidence(self.workspace, [self.evidence(path, version)], manifest)

    def test_repository_inventory_records_plugins_and_commit_changes(self):
        primary = self.repository("bi_react")
        plugin = self.repository("plugins/example")
        before = repositories(self.workspace)
        self.assertEqual(set(before), {".private/repositories/bi_react", ".private/repositories/plugins/example"})
        self.assertEqual(before[".private/repositories/bi_react"], self.git(primary, "rev-parse", "HEAD"))
        (plugin / "source.txt").write_text("Synthetic second implementation.\n")
        self.git(plugin, "add", "source.txt")
        self.git(plugin, "-c", "user.name=Synthetic Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "Synthetic update")
        self.assertNotEqual(repositories(self.workspace), before)

    def test_dirty_repository_cannot_be_claimed_as_a_clean_commit(self):
        repository = self.repository("modusbi")
        (repository / "source.txt").write_text("Uncommitted implementation.\n")
        with self.assertRaises(ValueError):
            repositories(self.workspace)


if __name__ == "__main__":
    unittest.main()
