import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from orchestrator.access import AccessDenied, ArtifactGateway


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.repo = self.workspace / ".private/repositories/bi_react"
        self.repo.mkdir(parents=True)
        (self.repo / "README.md").write_text("unchanged")
        self.run = self.workspace / ".private/runtime/run-one"
        self.gateway = ArtifactGateway(self.workspace, self.run)

    def tearDown(self):
        self.gateway.close()
        self.temp.cleanup()

    def test_artifacts_are_private_and_immutable_state_is_atomic(self):
        self.gateway.write_json("research/first.json", {"synthetic": True})
        self.assertEqual(self.gateway.read_json("research/first.json"), {"synthetic": True})
        self.assertEqual((self.run / "research/first.json").stat().st_mode & 0o777, 0o600)
        with self.assertRaises(FileExistsError):
            self.gateway.write_json("research/first.json", {})
        self.gateway.write_json("state.json", {"version": 1}, replace=True)
        self.gateway.write_json("state.json", {"version": 2}, replace=True)
        self.assertEqual(self.gateway.read_json("state.json"), {"version": 2})
        with self.assertRaises(AccessDenied):
            self.gateway.write_text("research/first.json", "replace", replace=True)

    def test_all_repository_path_spellings_are_refused(self):
        for path in ("../repositories/bi_react/README.md", self.repo / "README.md",
                     "../../repositories/bi_react/README.md", "a/../../README.md",
                     "a\\..\\README.md", "/tmp/output", ".", "a//b"):
            with self.subTest(path=path), self.assertRaises(AccessDenied):
                self.gateway.write_text(path, "forbidden")
        self.assertEqual((self.repo / "README.md").read_text(), "unchanged")

    def test_symlink_directory_and_file_are_refused(self):
        (self.run / "escape").symlink_to(self.repo, target_is_directory=True)
        (self.run / "state.json").symlink_to(self.repo / "README.md")
        with self.assertRaises(AccessDenied):
            self.gateway.write_text("escape/README.md", "forbidden")
        with self.assertRaises(AccessDenied):
            self.gateway.write_json("state.json", {}, replace=True)
        with self.assertRaises(AccessDenied):
            self.gateway.read_json("state.json")
        self.assertEqual((self.repo / "README.md").read_text(), "unchanged")

    def test_symlink_run_root_is_refused(self):
        linked = self.workspace / ".private/runtime/linked"
        linked.symlink_to(self.repo, target_is_directory=True)
        with self.assertRaises(AccessDenied):
            ArtifactGateway(self.workspace, linked)

    def test_parent_symlink_swapped_between_resolve_and_open_is_refused(self):
        (self.run / "safe").mkdir()
        original_open = os.open
        swapped = False

        def racing_open(path, flags, *args, **kwargs):
            nonlocal swapped
            if path == "safe" and not swapped:
                swapped = True
                (self.run / "safe").rename(self.run / "original-safe")
                (self.run / "safe").symlink_to(self.repo, target_is_directory=True)
            return original_open(path, flags, *args, **kwargs)

        with patch("orchestrator.access.os.open", racing_open):
            with self.assertRaises(AccessDenied):
                self.gateway.write_text("safe/README.md", "forbidden")
        self.assertTrue(swapped)
        self.assertEqual((self.repo / "README.md").read_text(), "unchanged")

    def test_hardlinked_artifact_cannot_be_overwritten(self):
        os.link(self.repo / "README.md", self.run / "state.json")
        with self.assertRaises(AccessDenied):
            self.gateway.write_json("state.json", {}, replace=True)
        self.assertEqual((self.repo / "README.md").read_text(), "unchanged")

    def test_hardlinked_create_only_artifact_is_refused(self):
        os.link(self.repo / "README.md", self.run / "artifact.json")
        with self.assertRaises(AccessDenied):
            self.gateway.write_json("artifact.json", {"forbidden": True})
        self.assertEqual((self.repo / "README.md").read_text(), "unchanged")
        self.assertEqual((self.run / "artifact.json").read_text(), "unchanged")
        self.assertFalse(list(self.run.glob(".pending-*")))

    def test_concurrent_artifact_creation_does_not_overwrite_history(self):
        outcomes = []

        def write(value):
            try:
                self.gateway.write_json("race.json", {"value": value})
                outcomes.append((value, "created"))
            except (FileExistsError, AccessDenied):
                outcomes.append((value, "refused"))
            except Exception as exc:
                outcomes.append((value, type(exc).__name__))

        threads = [threading.Thread(target=write, args=(i,)) for i in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertCountEqual([status for _, status in outcomes], ["created", "refused"])
        winner = next(value for value, status in outcomes if status == "created")
        self.assertEqual(self.gateway.read_json("race.json"), {"value": winner})
        self.assertFalse(list(self.run.glob(".pending-*")))

    def test_concurrent_create_refuses_the_transient_publication_hardlink(self):
        installed = threading.Event()
        release = threading.Event()
        outcomes = []
        original_link = os.link

        def paused_link(source, target, *args, **kwargs):
            original_link(source, target, *args, **kwargs)
            if target == "race.json":
                installed.set()
                if not release.wait(timeout=5):
                    raise TimeoutError("Publication was not released by the test")

        def publish():
            try:
                self.gateway.write_json("race.json", {"value": "winner"})
                outcomes.append("created")
            except Exception as exc:
                outcomes.append(type(exc).__name__)

        with patch("orchestrator.access.os.link", paused_link):
            writer = threading.Thread(target=publish)
            writer.start()
            try:
                self.assertTrue(installed.wait(timeout=5))
                self.assertEqual((self.run / "race.json").stat().st_nlink, 2)
                # A separate gateway may see a complete file while publication
                # still owns the temporary name. It must refuse it safely.
                with ArtifactGateway(self.workspace, self.run) as competitor:
                    with self.assertRaises(AccessDenied):
                        competitor.write_json("race.json", {"value": "loser"})
            finally:
                release.set()
                writer.join(timeout=5)
        self.assertFalse(writer.is_alive())
        self.assertEqual(outcomes, ["created"])
        self.assertEqual(self.gateway.read_json("race.json"), {"value": "winner"})
        self.assertEqual((self.run / "race.json").stat().st_nlink, 1)
        self.assertFalse(list(self.run.glob(".pending-*")))
        with self.assertRaises(FileExistsError):
            self.gateway.write_json("race.json", {"value": "later"})
        self.assertEqual(self.gateway.read_json("race.json"), {"value": "winner"})


if __name__ == "__main__":
    unittest.main()
