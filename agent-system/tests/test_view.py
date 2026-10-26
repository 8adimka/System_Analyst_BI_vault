import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from orchestrator.access import AccessDenied
from orchestrator.inputs import discover
from orchestrator.main import create_state
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

    def new_view(self):
        view = PreparedView(self.workspace, self.run, [self.source], include_corporate=False,
                            auth_home=self.workspace / "no-auth")
        self.addCleanup(view.close)
        return view

    def run_view(self, view, script, role="reviewer"):
        completed = subprocess.run(view.command(role, "/usr/bin/python3", ["-c", script]),
                                   capture_output=True, text=True, timeout=15)
        if completed.returncode and "Creating new namespace failed" in completed.stderr:
            self.skipTest("host disables unprivileged namespaces")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return completed.stdout

    def analytics_and_developer_files(self):
        analytics = {
            "agent-system/runtime/AGENTS.md": "synthetic read-only analytics instructions",
            "agent-system/standards/review-protocol.md": "synthetic review protocol",
            ".codex/agents/analyst.toml": "synthetic analyst role",
            ".codex/agents/reviewer.toml": "synthetic reviewer role",
        }
        for skill in ("codebase-investigator", "requirements-clarifier", "jira-task-writer",
                      "task-reviser", "consistency-reviewer", "evidence-reviewer"):
            analytics[f".agents/skills/{skill}/SKILL.md"] = "synthetic research skill: " + skill
        developer = {
            "AGENTS.md": "synthetic full-stack default instructions",
            "AGENT.md": "synthetic developer entrypoint",
            ".codex/config.toml": 'sandbox_mode = "workspace-write"',
            ".codex/agents/fullstack.toml": "synthetic full-stack role",
            ".agents/skills/portal-fullstack/SKILL.md": "synthetic implementation skill",
            ".agents/skills/portal-html-mockup/SKILL.md": "synthetic HTML mockup skill",
            ".agents/skills/codebase-wiki/SKILL.md": "synthetic Wiki-writing skill",
            ".private/development/topic/notes.md": "synthetic developer working notes",
            ".private/mockups/topic/index.html": "synthetic developer HTML mockup",
        }
        for relative, value in (analytics | developer).items():
            path = self.workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value)
        return analytics, developer

    def test_instruction_mounts_use_analytics_entrypoint_and_research_allowlist(self):
        analytics, developer = self.analytics_and_developer_files()
        view = self.new_view()
        for role in ("analyst", "reviewer"):
            with self.subTest(role=role), patch.dict(os.environ, {}, clear=True):
                command = view.command(role, "/usr/bin/true")
            mounts = {Path(command[index+2]): Path(command[index+1])
                      for index, value in enumerate(command) if value == "--ro-bind"}
            runtime = self.workspace / "agent-system/runtime/AGENTS.md"
            self.assertEqual(mounts[self.workspace / "AGENTS.md"], runtime)
            self.assertNotIn(self.workspace / "AGENTS.md", mounts.values())
            for relative in analytics:
                if relative != "agent-system/runtime/AGENTS.md":
                    self.assertEqual(mounts[self.workspace / relative], self.workspace / relative)
            for relative in developer:
                if relative != "AGENTS.md":
                    self.assertNotIn(self.workspace / relative, mounts)

    def test_missing_or_symlink_runtime_instructions_never_use_interactive_fallback(self):
        interactive = self.workspace / "AGENTS.md"
        interactive.write_text("synthetic full-stack default instructions")
        runtime = self.workspace / "agent-system/runtime/AGENTS.md"
        runtime.parent.mkdir(parents=True)
        view = self.new_view()
        for kind in ("missing", "symlink"):
            if kind == "symlink":
                runtime.symlink_to(interactive)
            with self.subTest(kind=kind), patch.dict(os.environ, {}, clear=True):
                command = view.command("reviewer", "/usr/bin/true")
            mounts = [(command[index+1], command[index+2])
                      for index, value in enumerate(command) if value == "--ro-bind"]
            self.assertFalse(any(str(interactive) in mount or str(runtime) in mount for mount in mounts))

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is not installed")
    def test_physical_analytics_view_excludes_developer_context_and_artifacts(self):
        analytics, developer = self.analytics_and_developer_files()
        paths = [str(self.workspace / relative) for relative in analytics | developer]
        script = ("import json,pathlib; paths=" + repr(paths) +
                  "; print(json.dumps({p: pathlib.Path(p).read_text() if pathlib.Path(p).is_file() else None for p in paths}))")
        view = self.new_view()
        for role in ("analyst", "reviewer"):
            with self.subTest(role=role), patch.dict(os.environ, {}, clear=True):
                result = json.loads(self.run_view(view, script, role=role))
                self.assertEqual(result[str(self.workspace / "AGENTS.md")],
                                 analytics["agent-system/runtime/AGENTS.md"])
                for relative, value in analytics.items():
                    expected = None if relative == "agent-system/runtime/AGENTS.md" else value
                    self.assertEqual(result[str(self.workspace / relative)], expected)
                for relative in developer:
                    if relative != "AGENTS.md":
                        self.assertIsNone(result[str(self.workspace / relative)])
                denied = subprocess.run(view.command(role, "/usr/bin/python3", ["-c",
                    "from pathlib import Path; Path('AGENTS.md').write_text('changed')"]),
                    capture_output=True, text=True, timeout=15)
                self.assertNotEqual(denied.returncode, 0)
        self.assertEqual((self.workspace / "AGENTS.md").read_text(), developer["AGENTS.md"])
        self.assertEqual((self.workspace / "agent-system/runtime/AGENTS.md").read_text(),
                         analytics["agent-system/runtime/AGENTS.md"])

    def test_network_environment_uses_only_proxy_and_certificate_allowlist(self):
        certificates = self.workspace.parent / "certificates"
        certificates.mkdir()
        bundle = certificates / "bundle.pem"
        bundle.write_text("synthetic public CA")
        certdir = certificates / "certdir"
        certdir.mkdir()
        (certdir / "root.crt").write_text("synthetic public CA")
        second_certdir = certificates / "second-certdir"
        second_certdir.mkdir()
        (second_certdir / "other.crt").write_text("synthetic public CA")
        environment = {name: "http://synthetic.invalid:8080" for name in (
            "HTTP_PROXY", "http_proxy", "HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy")}
        environment.update({"NO_PROXY": "localhost", "no_proxy": "127.0.0.1",
                            "SSL_CERT_FILE": str(bundle), "SSL_CERT_DIR": str(certdir) + os.pathsep + str(second_certdir),
                            "CODEX_CA_CERTIFICATE": str(bundle),
                            "OPENAI_API_KEY": "synthetic-api-token", "OPENAI_BASE_URL": "https://invalid.test",
                            "AWS_SECRET_ACCESS_KEY": "synthetic-aws-token", "UNLISTED_TOKEN": "synthetic-token"})
        with patch.dict(os.environ, environment, clear=True):
            command = self.new_view().command("analyst", "/usr/bin/true")
        forwarded = {command[index+1]: command[index+2] for index, value in enumerate(command)
                     if value == "--setenv"}
        for name, value in environment.items():
            if name.startswith("OPENAI_") or name in {"AWS_SECRET_ACCESS_KEY", "UNLISTED_TOKEN"}:
                self.assertNotIn(name, forwarded)
            else:
                self.assertEqual(forwarded[name], value)
        self.assertIn("--clearenv", command)
        readonly = {command[index+1] for index, value in enumerate(command) if value == "--ro-bind"}
        writable = {command[index+1] for index, value in enumerate(command) if value == "--bind"}
        self.assertIn(str(bundle), readonly)
        self.assertIn(str(certdir), readonly)
        self.assertIn(str(second_certdir), readonly)
        self.assertNotIn(str(bundle), writable)
        self.assertNotIn(str(certdir), writable)
        self.assertNotIn(str(certificates), readonly)

    def test_custom_certificate_paths_refuse_secrets_history_symlinks_and_unrelated_files(self):
        certificates = self.workspace.parent / "certificates"
        certificates.mkdir()
        valid = certificates / "root.pem"
        valid.write_text("synthetic public CA")
        alias = certificates / "alias.pem"
        alias.symlink_to(valid)
        directory_alias = self.workspace.parent / "cert-alias"
        directory_alias.symlink_to(certificates, target_is_directory=True)
        bad = [certificates / "missing.pem", alias, directory_alias / "root.pem",
               certificates / "auth.json", certificates / "private.key",
               self.run / "history.pem", self.workspace / "topic/.agent/ca.pem",
               self.workspace / "old/ca.pem", self.workspace.parent / ".codex/ca.pem"]
        for path in bad[3:]:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic prohibited fixture")
        view = self.new_view()
        for path in bad:
            with self.subTest(path=path), patch.dict(os.environ, {"SSL_CERT_FILE": str(path)}, clear=True):
                with self.assertRaises(AccessDenied):
                    view.command("reviewer", "/usr/bin/true")
        for value in ("relative.pem", "~/root.pem"):
            with self.subTest(value=value), patch.dict(os.environ, {"SSL_CERT_FILE": value}, clear=True):
                with self.assertRaises(AccessDenied):
                    view.command("reviewer", "/usr/bin/true")
        # A cert directory grant must not make an adjacent auth or key readable.
        with patch.dict(os.environ, {"SSL_CERT_DIR": str(certificates)}, clear=True):
            with self.assertRaises(AccessDenied):
                view.command("reviewer", "/usr/bin/true")

    def test_wrapper_policy_updates_create_immutable_content_addressed_versions(self):
        view = self.new_view()
        legacy = view.gateway.write_text("view-files/analyst-codex", "synthetic old wrapper\n")
        probe = subprocess.CompletedProcess([], 0, stdout=b"", stderr=b"")
        with patch("orchestrator.view.subprocess.run", return_value=probe), patch.dict(os.environ, {}, clear=True):
            original = view.prepare("analyst", "/usr/bin/true")
            original_text = original.read_text()
            self.assertEqual(view.prepare("analyst", "/usr/bin/true"), original)
            with patch.dict(os.environ, {"HTTPS_PROXY": "http://synthetic.invalid:8080"}):
                updated = view.prepare("analyst", "/usr/bin/true")
        self.assertNotEqual(original, updated)
        self.assertRegex(original.name, r"^analyst-codex-[0-9a-f]{64}$")
        self.assertEqual(original.read_text(), original_text)
        self.assertEqual(legacy.read_text(), "synthetic old wrapper\n")
        self.assertNotEqual(original_text, updated.read_text())
        original.write_text("synthetic tampered wrapper\n")
        with patch("orchestrator.view.subprocess.run", return_value=probe), patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(AccessDenied):
                view.prepare("analyst", "/usr/bin/true")

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is not installed")
    def test_physical_view_preserves_proxy_and_ca_without_other_environment_or_writes(self):
        certificate = self.workspace.parent / "custom-ca.pem"
        certificate.write_text("synthetic public CA")
        unlisted = self.workspace.parent / "unlisted-secret.txt"
        unlisted.write_text("synthetic unrelated file")
        script = ("import json,os,pathlib; p=pathlib.Path(" + repr(str(certificate)) + "); "
                  "denied=False\ntry: p.write_text('changed')\nexcept OSError: denied=True\n"
                  "print(json.dumps({'proxy':os.getenv('HTTPS_PROXY'), 'ca':os.getenv('SSL_CERT_FILE'),"
                  "'token':os.getenv('OPENAI_API_KEY'), 'content':p.read_text(), 'write_denied':denied,"
                  "'unlisted':pathlib.Path(" + repr(str(unlisted)) + ").exists()}))")
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://synthetic.invalid:8080",
                                    "SSL_CERT_FILE": str(certificate), "OPENAI_API_KEY": "synthetic-token"}, clear=True):
            result = json.loads(self.run_view(self.new_view(), script))
        self.assertEqual(result, {"proxy": "http://synthetic.invalid:8080", "ca": str(certificate),
                                  "token": None, "content": "synthetic public CA",
                                  "write_denied": True, "unlisted": False})
        self.assertEqual(certificate.read_text(), "synthetic public CA")

    @unittest.skipUnless(shutil.which("bwrap") and shutil.which("openssl"), "bubblewrap and openssl are required")
    def test_physical_system_trust_store_symlink_targets_are_read_only_and_loadable(self):
        system = self.workspace.parent / "synthetic-system"
        certs = system / "ssl/certs"
        extracted = system / "ca-certificates/extracted"
        certs.mkdir(parents=True)
        extracted.mkdir(parents=True)
        bundle = extracted / "tls-ca-bundle.pem"
        subprocess.run([shutil.which("openssl"), "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                        "-keyout", "/dev/null", "-out", str(bundle), "-days", "1",
                        "-subj", "/CN=Synthetic View Test CA"], capture_output=True, check=True, timeout=15)
        alias = certs / "ca-certificates.crt"
        alias.symlink_to("../../ca-certificates/extracted/tls-ca-bundle.pem")
        top_alias = system / "ssl/cert.pem"
        top_alias.symlink_to("../ca-certificates/extracted/tls-ca-bundle.pem")
        private = system / "private/ca.key"
        private.parent.mkdir()
        private.write_text("synthetic private key-shaped fixture")
        script = ("import json,pathlib,ssl; p=pathlib.Path(" + repr(str(bundle)) + "); "
                  "count=ssl.create_default_context(cafile=" + repr(str(alias)) + ").cert_store_stats()['x509_ca']; "
                  "denied=False\ntry: p.write_text('changed')\nexcept OSError: denied=True\n"
                  "print(json.dumps({'ca_count':count, 'write_denied':denied,"
                  "'private_visible':pathlib.Path(" + repr(str(private)) + ").exists()}))")
        with patch("orchestrator.view._SYSTEM_CA_PATHS", (str(certs), str(top_alias), str(extracted))), \
                patch.dict(os.environ, {"SSL_CERT_FILE": str(alias), "SSL_CERT_DIR": str(certs)}, clear=True):
            result = json.loads(self.run_view(self.new_view(), script))
        self.assertGreater(result["ca_count"], 0)
        self.assertTrue(result["write_denied"])
        self.assertFalse(result["private_visible"])

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is not installed")
    def test_physical_default_system_trust_store_loads_without_network(self):
        with patch.dict(os.environ, {}, clear=True):
            output = self.run_view(self.new_view(),
                                   "import ssl; print(ssl.create_default_context().cert_store_stats()['x509_ca'])")
        self.assertGreater(int(output.strip()), 0)

    def test_history_sources_are_refused(self):
        for relative in ("topic/.agent/draft.txt", ".private/runtime/old/draft.txt",
                         ".private/development/topic/notes.md", ".private/mockups/topic/index.html",
                         ".agents/skills/portal-fullstack/SKILL.md", ".codex/agents/fullstack.toml",
                         "AGENTS.md", "AGENT.md", "Доступы.txt", ".env"):
            path = self.workspace / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic")
            with self.subTest(relative=relative), self.assertRaises(AccessDenied):
                PreparedView(self.workspace, self.run, [path], auth_home=self.workspace / "no-auth")

    @unittest.skipUnless(shutil.which("bwrap"), "bubblewrap is not installed")
    def test_physical_analyst_source_is_hidden_even_when_auto_discovered(self):
        analyst_source = self.workspace / "topic/previous_context.txt"
        sentinel = "synthetic analyst-only sentinel"
        analyst_source.write_text(sentinel)
        script = ("import json,pathlib; p=pathlib.Path(" + repr(str(analyst_source)) + "); "
                  "content=None; denied=False\ntry: content=p.read_text()\nexcept OSError: denied=True\n"
                  "print(json.dumps({'content':content, 'read_denied':denied, 'primary':"
                  "pathlib.Path(" + repr(str(self.source)) + ").read_text()}))")
        for discovered in (False, True):
            with self.subTest(auto_discovered=discovered):
                sources = [self.source]
                if discovered:
                    prompt = "Источник: topic/input.txt\nМатериал: topic/previous_context.txt"
                    self.assertEqual(discover(self.workspace, prompt), [self.source, analyst_source])
                    state = create_state(self.workspace, "discovered", prompt, [self.source],
                                         analyst_sources=[analyst_source])
                    self.assertIn("topic/previous_context.txt", state.sources)
                    self.assertEqual(state.analyst_sources, ["topic/previous_context.txt"])
                    sources = [self.workspace / relative for relative in state.sources]
                view = PreparedView(self.workspace,
                                    self.workspace / f".private/runtime/analyst-source-{discovered}",
                                    sources, analyst_sources=[analyst_source], include_corporate=False,
                                    auth_home=self.workspace / "no-auth")
                self.addCleanup(view.close)
                with patch.dict(os.environ, {}, clear=True):
                    reviewer = json.loads(self.run_view(view, script))
                    analyst = json.loads(self.run_view(view, script, role="analyst"))
                self.assertEqual(reviewer, {"content": None, "read_denied": True,
                                            "primary": "synthetic input"})
                self.assertEqual(analyst, {"content": sentinel, "read_denied": False,
                                           "primary": "synthetic input"})

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
            ".private/knowledge/sources.json": '{"registry": "research sources"}',
            ".private/knowledge/fullstack-sources.json": '{"registry": "development sources"}',
            ".private/knowledge/unlisted-draft.json": '{"draft": "hidden"}',
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
            for relative in list(files)[:4] + [".private/knowledge/legacy/old.md",
                                               ".private/knowledge/unlisted-draft.json"]:
                self.assertIsNone(result[str(self.workspace / relative)])
            self.assertEqual(result[str(self.workspace / ".private/knowledge/architecture/overview.md")], "current wiki")
            self.assertEqual(result[str(self.workspace / ".private/knowledge/legacy/domain-checks.md")], "normative checks")
            self.assertEqual(result[str(self.workspace / ".private/repositories/project/src.py")], "synthetic code")
            self.assertIsNone(result[str(self.workspace / ".private/repositories/project/.env")])
            self.assertEqual(result[str(self.workspace / ".private/repositories/project/.git/config")], "")
            manifests = [self.workspace / ".private/knowledge" / name
                         for name in ("sources.json", "fullstack-sources.json")]
            for manifest in manifests:
                self.assertEqual(result[str(manifest)], files[str(manifest.relative_to(self.workspace))])
            for path in [self.source, *manifests]:
                expected = path.read_text()
                write_script = "from pathlib import Path; Path(" + repr(str(path)) + ").write_text('changed')"
                denied = subprocess.run(view.command("reviewer", "/usr/bin/python3", ["-c", write_script]),
                                        capture_output=True, text=True, timeout=15)
                self.assertNotEqual(denied.returncode, 0)
                self.assertEqual(path.read_text(), expected)
        finally:
            view.close()

    def test_git_config_surrogate_rejects_content_symlink_and_hardlink_tampering(self):
        for kind in ("content", "symlink", "hardlink", "directory"):
            view = PreparedView(self.workspace, self.workspace / f".private/runtime/{kind}",
                                [self.source], include_corporate=False, auth_home=self.workspace / "no-auth")
            self.addCleanup(view.close)
            surrogate = view.gateway.path("view-files/empty-git-config")
            surrogate.parent.mkdir(exist_ok=True)
            if kind == "content":
                surrogate.write_text("synthetic tampered config")
            elif kind == "symlink":
                surrogate.symlink_to(self.source)
            elif kind == "hardlink":
                empty = self.workspace / "empty.txt"
                empty.write_text("")
                os.link(empty, surrogate)
            else:
                surrogate.mkdir()
            with self.subTest(kind=kind), self.assertRaises(AccessDenied):
                view._empty_git_config()

    @unittest.skipUnless(shutil.which("bwrap") and shutil.which("git"), "bubblewrap and git are required")
    def test_physical_git_reads_work_with_empty_config_and_no_remote_or_write_access(self):
        repository = self.workspace / ".private/repositories/project"
        repository.mkdir(parents=True)
        git = shutil.which("git")
        environment = {"PATH": os.defpath, "HOME": str(self.workspace / "empty-home"),
                       "GIT_CONFIG_NOSYSTEM": "1", "GIT_CONFIG_GLOBAL": "/dev/null"}

        def host_git(*args):
            return subprocess.run([git, "-c", "core.hooksPath=/dev/null", "-C", str(repository), *args],
                                  capture_output=True, text=True, check=True, env=environment).stdout.strip()

        host_git("init", "-q")
        (repository / "source.txt").write_text("synthetic implementation\n")
        host_git("add", "source.txt")
        host_git("-c", "user.name=Synthetic Test", "-c", "user.email=test@example.invalid",
                 "commit", "-qm", "Synthetic baseline")
        revision = host_git("rev-parse", "HEAD")
        host_git("config", "remote.origin.url", "https://synthetic-secret@example.invalid/project.git")
        view = PreparedView(self.workspace, self.run, [self.source], auth_home=self.workspace / "no-auth")
        self.addCleanup(view.close)

        def isolated_git(*args):
            # Coordinator construction must never inspect the actual config.
            with patch.object(Path, "read_text", side_effect=AssertionError("Do not read repository config")):
                command = view.command("reviewer", git, ["-c", "safe.directory=" + str(repository),
                                                        "-C", str(repository), *args])
            completed = subprocess.run(command, capture_output=True, text=True, timeout=15)
            if completed.returncode and "Creating new namespace failed" in completed.stderr:
                self.skipTest("host disables unprivileged namespaces")
            return completed

        for args, expected in ((("rev-parse", "HEAD"), revision), (("status", "--porcelain"), ""),
                               (("log", "-1", "--format=%H"), revision),
                               (("show", "HEAD:source.txt"), "synthetic implementation")):
            with self.subTest(args=args):
                completed = isolated_git(*args)
                self.assertEqual(completed.returncode, 0, completed.stderr)
                self.assertEqual(completed.stdout.strip(), expected)
        remote = isolated_git("config", "--get", "remote.origin.url")
        self.assertEqual(remote.returncode, 1)
        self.assertEqual(remote.stdout, "")
        denied = isolated_git("config", "remote.origin.url", "https://changed.invalid/project.git")
        self.assertNotEqual(denied.returncode, 0)
        self.assertEqual(host_git("config", "--get", "remote.origin.url"),
                         "https://synthetic-secret@example.invalid/project.git")
        config = repository / ".git/config"
        script = ("import pathlib; p=pathlib.Path(" + repr(str(config)) + "); "
                  "assert p.is_file() and p.read_text() == ''; denied=False\n"
                  "try: p.write_text('changed')\nexcept OSError: denied=True\nassert denied")
        self.run_view(view, script)

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
