"""Thin official openai-codex SDK adapter; no CLI output parsing."""
from __future__ import annotations

import json
from pathlib import Path
import queue
import threading
from typing import Any, Callable, Sequence

from .view import IsolationUnavailable, PreparedView


class BackendError(RuntimeError):
    """A safe, content-free diagnostic suitable for coordinator state."""


class EmptyThreadError(BackendError):
    """Runtime has no persisted rollout for the exact requested Thread ID.

    The coordinator, which knows whether the role completed any work, decides
    whether a new empty thread is permissible. The adapter never changes IDs.
    """


class RoutingUnavailable(BackendError):
    """ChatGPT workspace discovery is unavailable before model execution."""


def _safe_failure(exc: BaseException, fallback: str) -> BackendError:
    message = getattr(exc, "message", str(exc))
    if isinstance(message, str) and message.strip().casefold() == "workspace routing discovery failed":
        return RoutingUnavailable("Workspace routing discovery failed; restore ChatGPT account routing before running smoke")
    return BackendError(fallback)


def _bounded(call: Callable[[], Any], seconds: float, *, timeout_cleanup: Callable[[], None] | None = None,
             late_cleanup: Callable[[Any], None] | None = None) -> Any:
    """Keep SDK initialization/RPC calls from blocking coordinator indefinitely.

    Public SDK construction starts the subprocess internally. If construction
    completes after cancellation, close that client immediately. Existing clients
    are closed at timeout so their pending transport requests are interrupted.
    """
    outcomes: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)
    cancelled = False
    lock = threading.Lock()

    def work() -> None:
        try:
            outcome = (True, call())
        except BaseException as exc:
            outcome = (False, exc)
        with lock:
            should_cleanup = cancelled
            if not should_cleanup:
                outcomes.put(outcome)
        if should_cleanup and outcome[0] and late_cleanup:
            late_cleanup(outcome[1])

    threading.Thread(target=work, name="codex-sdk-request", daemon=True).start()
    try:
        succeeded, value = outcomes.get(timeout=seconds)
    except (queue.Empty, KeyboardInterrupt, SystemExit) as exc:
        with lock:
            cancelled = True
            try:
                completed = outcomes.get_nowait()
            except queue.Empty:
                completed = None
        if completed and completed[0] and late_cleanup:
            late_cleanup(completed[1])
        if timeout_cleanup:
            timeout_cleanup()
        if isinstance(exc, queue.Empty):
            raise BackendError("Codex SDK startup/request exceeded its deadline") from exc
        raise
    if not succeeded:
        raise value
    return value


class SDKBackend:
    """Independent persistent SDK clients, homes and read-only threads per role.

    Sources are explicit workspace files; the prompt itself is supplied in
    memory. Repositories/Wiki are available only with include_corporate=True.
    A coordinator must verify input/source versions when resuming a run.
    """

    def __init__(self, workspace: str | Path, run_root: str | Path,
                 sources: Sequence[Path] = (), *, model: str | None = None,
                 timeout: float = 900, codex_bin: str | None = None,
                 include_corporate: bool = True, max_response_bytes: int = 2_097_152,
                 client_factory: Callable[..., Any] | None = None, startup_timeout: float = 60):
        if timeout <= 0 or startup_timeout <= 0 or max_response_bytes <= 0:
            raise ValueError("Timeout and response size limit must be positive")
        self.workspace = Path(workspace).absolute()
        self.run_root = Path(run_root).absolute()
        self.sources = tuple(Path(p) for p in sources)
        self.model = model
        self.timeout = timeout
        self.startup_timeout = startup_timeout
        self.codex_bin = codex_bin
        self.include_corporate = include_corporate
        self.max_response_bytes = max_response_bytes
        self._client_factory = client_factory
        self._view: PreparedView | None = None
        self._clients: dict[str, Any] = {}
        self._threads: dict[str, Any] = {}
        self._sdk: Any = None

    def __enter__(self) -> "SDKBackend":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        for client in self._clients.values():
            try:
                client.close()
            except Exception:
                pass
        self._clients.clear()
        self._threads.clear()
        if self._view is not None:
            self._view.close()
            self._view = None

    def _client(self, role: str) -> Any:
        if role not in ("analyst", "reviewer"):
            raise ValueError("Unknown role")
        if role in self._clients:
            return self._clients[role]
        try:
            import openai_codex
        except ImportError as exc:
            raise BackendError("Install the pinned official openai-codex dependency") from exc
        self._sdk = openai_codex
        if self.codex_bin is None:
            try:
                from codex_cli_bin import bundled_codex_path
                self.codex_bin = str(bundled_codex_path())
            except (ImportError, FileNotFoundError) as exc:
                raise BackendError("Install the pinned official openai-codex-cli-bin runtime") from exc
        if self._view is None:
            self._view = PreparedView(self.workspace, self.run_root, self.sources,
                                      include_corporate=self.include_corporate)
        wrapper = self._view.prepare(role, self.codex_bin)
        config = self._sdk.CodexConfig(codex_bin=str(wrapper), cwd=str(self.workspace))
        try:
            client = _bounded(lambda: (self._client_factory or self._sdk.Codex)(config),
                              self.startup_timeout, late_cleanup=lambda value: value.close())
        except BackendError:
            raise
        except Exception as exc:
            raise BackendError("Official Codex SDK could not start its isolated runtime") from exc
        self._clients[role] = client
        return client

    def _options(self, instructions: str) -> dict[str, Any]:
        result = {"cwd": str(self.workspace), "developer_instructions": instructions,
                  "sandbox": self._sdk.Sandbox.read_only,
                  "approval_mode": self._sdk.ApprovalMode.deny_all,
                  "config": {"web_search": "disabled", "agents": {"enabled": False},
                             "features": {"apps": False, "plugins": False, "memories": False,
                                          "request_permissions_tool": False}}}
        if self.model:
            result["model"] = self.model
        return result

    def check_account(self) -> bool:
        """Public account metadata only: no refresh, model, identity or token output."""
        client = self._client("reviewer")
        try:
            response = _bounded(lambda: client.account(refresh_token=False),
                                self.startup_timeout, timeout_cleanup=client.close)
        except BackendError:
            raise
        except Exception as exc:
            raise _safe_failure(exc, "Codex account check failed; check authentication or connectivity") from exc
        return response.account is not None

    def start(self, role: str, instructions: str) -> str:
        if role in self._threads:
            return self._threads[role].id
        client = self._client(role)
        try:
            thread = _bounded(lambda: client.thread_start(**self._options(instructions)),
                              self.startup_timeout, timeout_cleanup=client.close)
        except BackendError:
            self._clients.pop(role, None)
            raise
        except Exception as exc:
            raise BackendError("Codex thread start failed; check authentication and account availability") from exc
        other_ids = {value.id for value in self._threads.values()}
        if thread.id in other_ids:
            raise BackendError("Independent roles must have distinct Codex Thread identifiers")
        self._threads[role] = thread
        return thread.id

    def resume(self, role: str, thread_id: str, instructions: str) -> None:
        if role in self._threads:
            if self._threads[role].id != thread_id:
                raise BackendError("Role is already attached to a different thread")
            return
        if thread_id in {value.id for value in self._threads.values()}:
            raise BackendError("Independent roles cannot share a Codex Thread")
        client = self._client(role)
        try:
            self._threads[role] = _bounded(lambda: client.thread_resume(thread_id, **self._options(instructions)),
                                          self.startup_timeout, timeout_cleanup=client.close)
        except BackendError:
            self._clients.pop(role, None)
            raise
        except self._sdk.InvalidRequestError as exc:
            if exc.code == -32600 and exc.message == f"no rollout found for thread id {thread_id}":
                raise EmptyThreadError("Codex has no persisted rollout for this Thread ID") from exc
            raise BackendError("Codex thread resume failed; use the original run and role home") from exc
        except Exception as exc:
            raise BackendError("Codex thread resume failed; use the original run and role home") from exc

    def run(self, role: str, prompt: str, output_schema: dict[str, Any]) -> dict[str, Any]:
        if role not in self._threads:
            raise BackendError("Start or resume the role before running it")
        # The SDK exposes structured output_schema; coordinator Pydantic models
        # perform strict field validation after this transport/JSON boundary.
        outcomes: queue.Queue[tuple[bool, Any]] = queue.Queue(maxsize=1)

        def work() -> None:
            try:
                result = self._threads[role].run(prompt, output_schema=output_schema,
                         cwd=str(self.workspace), sandbox=self._sdk.Sandbox.read_only,
                         approval_mode=self._sdk.ApprovalMode.deny_all)
                outcomes.put((True, result))
            except BaseException as exc:
                outcomes.put((False, exc))

        worker = threading.Thread(target=work, name="codex-" + role, daemon=True)
        worker.start()
        try:
            succeeded, result = outcomes.get(timeout=self.timeout)
        except queue.Empty as exc:
            self._clients[role].close()
            self._clients.pop(role, None)
            self._threads.pop(role, None)
            raise BackendError("Codex turn exceeded the configured deadline; resume the saved run") from exc
        except BaseException:
            self.close()
            raise
        if not succeeded:
            if isinstance(result, (KeyboardInterrupt, SystemExit)):
                raise result
            raise _safe_failure(result, "Codex turn failed; check account limits, authentication or connectivity") from result
        raw = result.final_response
        if not isinstance(raw, str) or len(raw.encode("utf-8")) > self.max_response_bytes:
            raise BackendError("Codex returned missing or oversized structured output")
        try:
            data = json.loads(raw, parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
        except (ValueError, TypeError, RecursionError) as exc:
            raise BackendError("Codex returned malformed JSON output") from exc
        if not isinstance(data, dict):
            raise BackendError("Codex structured output must be a JSON object")
        return data
