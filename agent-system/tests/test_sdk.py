import inspect
import os
from pathlib import Path
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from orchestrator.sdk_adapter import BackendError, EmptyThreadError, RoutingUnavailable, SDKBackend


class FakeThread:
    def __init__(self, thread_id):
        self.id = thread_id
        self.calls = []
        self.response = '{"synthetic": true}'

    def run(self, prompt, **kwargs):
        self.calls.append((prompt, kwargs))
        return SimpleNamespace(final_response=self.response)


class FakeClient:
    instances = []

    def __init__(self, config):
        self.config = config
        self.started = []
        self.resumed = []
        self.closed = False
        self.instances.append(self)

    def thread_start(self, **kwargs):
        self.started.append(kwargs)
        return FakeThread("thread-" + str(len(self.instances)))

    def thread_resume(self, thread_id, **kwargs):
        self.resumed.append((thread_id, kwargs))
        return FakeThread(thread_id)

    def close(self):
        self.closed = True

    def account(self, *, refresh_token):
        if refresh_token:
            raise AssertionError("Account check must not refresh authorization")
        return SimpleNamespace(account=object())


class FakeView:
    def __init__(self, *args, **kwargs):
        self.prepared = []

    def prepare(self, role, binary):
        self.prepared.append(role)
        return Path("/synthetic/wrapper-" + role)

    def close(self):
        pass


class SDKTests(unittest.TestCase):
    def setUp(self):
        FakeClient.instances = []
        self.patch = patch("orchestrator.sdk_adapter.PreparedView", FakeView)
        self.patch.start()
        self.backend = SDKBackend("/tmp/synthetic-work", "/tmp/synthetic-work/.private/runtime/run",
                                  codex_bin="/usr/bin/true", client_factory=FakeClient)

    def tearDown(self):
        self.backend.close()
        self.patch.stop()

    def test_official_installed_public_api_contract(self):
        import openai_codex
        for name in ("thread_start", "thread_resume"):
            parameters = inspect.signature(getattr(openai_codex.Codex, name)).parameters
            self.assertTrue({"sandbox", "developer_instructions", "cwd", "approval_mode"} <= set(parameters))
        self.assertIn("output_schema", inspect.signature(openai_codex.Thread.run).parameters)
        self.assertEqual(openai_codex.Sandbox.read_only.value, "read-only")
        self.assertIn("refresh_token", inspect.signature(openai_codex.Codex.account).parameters)

    def test_account_diagnostic_does_not_create_threads_or_refresh_or_return_identity(self):
        self.assertIs(self.backend.check_account(), True)
        self.assertEqual(FakeClient.instances[0].started, [])
        self.assertEqual(self.backend._threads, {})
        with patch.object(FakeClient, "account", return_value=SimpleNamespace(account=None)):
            self.assertIs(self.backend.check_account(), False)

    def test_account_diagnostic_classifies_known_routing_failure_without_raw_errors(self):
        from openai_codex.errors import InternalRpcError
        error = InternalRpcError(-32603, "Workspace routing discovery failed")
        with patch.object(FakeClient, "account", side_effect=error), self.assertRaises(RoutingUnavailable):
            self.backend.check_account()
        with patch.object(FakeClient, "account", side_effect=RuntimeError("synthetic-private-provider-detail")):
            with self.assertRaises(BackendError) as caught:
                self.backend.check_account()
            self.assertNotIn("synthetic-private-provider-detail", str(caught.exception))

    def test_default_binary_is_pinned_distribution_not_vscode_path(self):
        from codex_cli_bin import bundled_codex_path
        with SDKBackend("/tmp/synthetic-work", "/tmp/synthetic-work/.private/runtime/pinned",
                        client_factory=FakeClient) as backend:
            backend.start("analyst", "role")
            self.assertEqual(backend.codex_bin, str(bundled_codex_path()))

    def test_roles_have_distinct_threads_homes_and_read_only_calls(self):
        import openai_codex
        analyst = self.backend.start("analyst", "analyst instructions")
        reviewer = self.backend.start("reviewer", "reviewer instructions")
        self.assertNotEqual(analyst, reviewer)
        self.assertEqual(len(FakeClient.instances), 2)
        for client in FakeClient.instances:
            self.assertEqual(client.started[0]["sandbox"], openai_codex.Sandbox.read_only)
            self.assertEqual(client.started[0]["approval_mode"], openai_codex.ApprovalMode.deny_all)
            config = client.started[0]["config"]
            self.assertFalse(config["agents"]["enabled"])
            self.assertTrue(all(value is False for value in config["features"].values()))
        self.assertNotEqual(FakeClient.instances[0].config.codex_bin, FakeClient.instances[1].config.codex_bin)
        self.assertEqual(self.backend.run("reviewer", "input", {"type": "object"}), {"synthetic": True})
        options = self.backend._threads["reviewer"].calls[0][1]
        self.assertEqual(options["sandbox"], openai_codex.Sandbox.read_only)
        self.assertEqual(options["approval_mode"], openai_codex.ApprovalMode.deny_all)

    def test_resume_is_idempotent_and_cannot_merge_roles(self):
        self.backend.resume("analyst", "saved-analyst", "role")
        self.backend.resume("analyst", "saved-analyst", "role")
        self.assertEqual(len(FakeClient.instances[0].resumed), 1)
        with self.assertRaises(BackendError):
            self.backend.resume("reviewer", "saved-analyst", "role")
        with self.assertRaises(BackendError):
            self.backend.resume("analyst", "different", "role")

    def test_only_exact_typed_missing_rollout_error_is_specialized(self):
        from openai_codex.errors import InvalidRequestError
        exact = InvalidRequestError(-32600, "no rollout found for thread id saved-empty")
        with patch.object(FakeClient, "thread_resume", side_effect=exact):
            with self.assertRaises(EmptyThreadError):
                self.backend.resume("analyst", "saved-empty", "role")
        self.assertNotIn("analyst", self.backend._threads)
        for error in (InvalidRequestError(-32600, "different invalid request"),
                      InvalidRequestError(-32600, "no rollout found for thread id another-thread"),
                      RuntimeError("no rollout found for thread id saved-empty")):
            with self.subTest(error=type(error).__name__), patch.object(FakeClient, "thread_resume", side_effect=error):
                with self.assertRaises(BackendError) as caught:
                    self.backend.resume("analyst", "saved-empty", "role")
                self.assertNotIsInstance(caught.exception, EmptyThreadError)

    def test_malformed_oversized_and_nonobject_output_is_rejected(self):
        self.backend.start("analyst", "role")
        for response in ('not-json', '[]', '{"x": NaN}', '"text"', None, '{"x": "' + "x" * 200 + '"}'):
            self.backend.max_response_bytes = 100
            self.backend._threads["analyst"].response = response
            with self.subTest(response=str(response)[:30]), self.assertRaises(BackendError):
                self.backend.run("analyst", "input", {})

    def test_timeout_closes_runtime_and_returns_control(self):
        self.backend.start("analyst", "role")
        self.backend.timeout = 0.01
        self.backend._threads["analyst"].run = lambda *args, **kwargs: time.sleep(0.1)
        with self.assertRaises(BackendError):
            self.backend.run("analyst", "input", {})
        self.assertTrue(FakeClient.instances[0].closed)

    def test_startup_timeout_and_late_client_cleanup(self):
        def slow_factory(config):
            time.sleep(0.04)
            return FakeClient(config)

        with SDKBackend("/tmp/synthetic-work", "/tmp/synthetic-work/.private/runtime/slow",
                        codex_bin="/usr/bin/true", client_factory=slow_factory, startup_timeout=0.01) as backend:
            with self.assertRaises(BackendError):
                backend.start("analyst", "role")
            time.sleep(0.06)
            self.assertTrue(FakeClient.instances[0].closed)


@unittest.skipUnless(os.environ.get("MODUS_REAL_SDK_TEST") == "1", "explicit opt-in real SDK metadata check")
class RealSDKMetadataTests(unittest.TestCase):
    def test_runtime_survives_construction_worker_and_empty_rollout_is_explicit(self):
        # Uses the official local app-server metadata API only: no model turn,
        # corporate files, or credential reads by this test.
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary) / "synthetic"
            workspace.mkdir()
            source = workspace / "input.txt"
            source.write_text("Synthetic metadata contract test; no model call.")
            run_root = workspace / ".private/runtime/metadata"
            ids = {}
            with SDKBackend(workspace, run_root, [source], include_corporate=False,
                            timeout=30, startup_timeout=30) as backend:
                import openai_codex
                for role in ("analyst", "reviewer"):
                    ids[role] = backend.start(role, "Synthetic metadata test. Do not call tools.")
                    # Start returned after its construction thread exited. A
                    # further public RPC proves the app-server is still alive.
                    backend._threads[role].read(include_turns=False)
                    # Native 0.163.0 does not persist a rollout until its first
                    # turn, even when resume is requested on this live client.
                    with self.assertRaises(openai_codex.InvalidRequestError) as caught:
                        backend._clients[role].thread_resume(ids[role],
                            cwd=str(workspace), sandbox=openai_codex.Sandbox.read_only,
                            approval_mode=openai_codex.ApprovalMode.deny_all,
                            developer_instructions="Synthetic metadata test. Do not call tools.")
                    self.assertEqual(caught.exception.message, "no rollout found for thread id " + ids[role])
                    backend._threads[role].read(include_turns=False)
                self.assertNotEqual(ids["analyst"], ids["reviewer"])
            # Persistence across app-server restarts requires a first turn in
            # runtime 0.163.0. The full synthetic smoke checks that separately;
            # this metadata-only test must not pretend it persisted a rollout.


if __name__ == "__main__":
    unittest.main()
