"""Bidirectional ACP (Agent Client Protocol) transport over stdio.

ACP is JSON-RPC 2.0 with one JSON object per line (NDJSON), *not* LSP
``Content-Length`` framing.

The important difference from the one-way CLI providers in this package: an
ACP connection is bidirectional.  While a ``session/prompt`` request is still
in flight the agent keeps sending ``session/update`` notifications and may
send its own ``session/request_permission`` request that expects a reply.
Writing a request and then stopping the stdout read loop would therefore
deadlock the moment the agent asks for permission ("agent waits for
authorization, client waits for the agent").  The reader task here keeps
draining stdout for the whole lifetime of the process and dispatches three
kinds of frames:

* a response to one of our requests        -> resolve the pending future
* a notification (no ``id``)               -> ``on_notification`` callback
* a request from the agent (``id`` + method) -> ``on_request`` callback, then
  send the result back on the same connection

Only ``session/close`` / stdin EOF end the process; this module never kills
the child itself — the provider owns process-group teardown via
``_cli_common.kill_process_tree``.
"""

import asyncio
import json
import logging
import subprocess
import sys
from typing import Any, Awaitable, Callable, Optional

_log = logging.getLogger(__name__)

STREAM_LIMIT = 10 * 1024 * 1024
STDERR_CAP = 64 * 1024
DEFAULT_REQUEST_TIMEOUT_S = 60.0
# How long request() waits between idle-timeout re-checks.
_POLL_S = 1.0

# JSON-RPC error codes used by this transport for locally detected failures.
CODE_LOCAL_TIMEOUT = -32001
CODE_PROTOCOL = -32603
CODE_METHOD_NOT_FOUND = -32601


class AcpError(RuntimeError):
    """A JSON-RPC error returned by the agent, or a local protocol failure."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.data = data

    def with_context(self, context: str) -> "AcpError":
        return AcpError(self.code, f"{context}: {self.message}", self.data)


class AcpProcessError(RuntimeError):
    """The ACP child process exited or its output stream broke."""


NotificationHandler = Callable[[str, dict], Awaitable[None]]
RequestHandler = Callable[[str, dict], Awaitable[Any]]


class AcpTransport:
    def __init__(
        self,
        argv: list[str],
        *,
        cwd: str,
        env: dict,
        on_notification: Optional[NotificationHandler] = None,
        on_request: Optional[RequestHandler] = None,
    ) -> None:
        self.argv = list(argv)
        self.cwd = cwd
        self.env = env
        self._on_notification = on_notification
        self._on_request = on_request

        self._proc: Optional[asyncio.subprocess.Process] = None
        self._pending: dict[int, asyncio.Future] = {}
        self._next_id = 0
        self._write_lock = asyncio.Lock()
        self._reader_task: Optional[asyncio.Task] = None
        self._stderr_task: Optional[asyncio.Task] = None
        self._stderr: list[bytes] = []
        self._stderr_bytes = 0
        # Any frame from the agent counts as activity for idle timeouts.
        self.last_activity = 0.0
        self.closed_reason = ""
        # Non-protocol lines seen on stdout.  stdout belongs to ACP, so this
        # is a bug worth surfacing rather than silently dropping.
        self.protocol_noise: list[str] = []

    # ---------------------------------------------------------------- process

    @property
    def proc(self) -> Optional[asyncio.subprocess.Process]:
        return self._proc

    @property
    def returncode(self) -> Optional[int]:
        return self._proc.returncode if self._proc is not None else None

    def stderr_text(self) -> str:
        return b"".join(self._stderr).decode("utf-8", "replace").strip()

    async def start(self) -> None:
        kwargs: dict = {
            "stdout": asyncio.subprocess.PIPE,
            "stderr": asyncio.subprocess.PIPE,
            "stdin": asyncio.subprocess.PIPE,
            "cwd": self.cwd,
            "limit": STREAM_LIMIT,
            "env": self.env,
        }
        if sys.platform == "win32":
            kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
        else:
            # Own process group so a hard stop can kill the whole tree.
            kwargs["start_new_session"] = True
        self.last_activity = asyncio.get_running_loop().time()
        self._proc = await asyncio.create_subprocess_exec(*self.argv, **kwargs)
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="comfytv-acp-reader")
        self._stderr_task = asyncio.create_task(
            self._drain_stderr(), name="comfytv-acp-stderr")

    async def _drain_stderr(self) -> None:
        proc = self._proc
        if proc is None or proc.stderr is None:
            return
        while True:
            try:
                chunk = await proc.stderr.read(65536)
            except (OSError, ValueError):
                return
            if not chunk:
                return
            if self._stderr_bytes < STDERR_CAP:
                self._stderr.append(chunk)
                self._stderr_bytes += len(chunk)

    def _note_noise(self, text: str) -> None:
        if len(self.protocol_noise) < 20:
            self.protocol_noise.append(text[:400])
        if self._stderr_bytes < STDERR_CAP:
            note = f"[non-protocol stdout] {text[:400]}\n".encode("utf-8")
            self._stderr.append(note)
            self._stderr_bytes += len(note)

    async def _read_loop(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        try:
            while True:
                try:
                    line = await proc.stdout.readline()
                except ValueError:
                    # Line exceeded STREAM_LIMIT; the buffer is unusable.
                    _log.warning("[ComfyTV/acp] oversized stdout line skipped")
                    self.last_activity = asyncio.get_running_loop().time()
                    continue
                except (OSError, asyncio.LimitOverrunError):
                    break
                if not line:
                    break
                self.last_activity = asyncio.get_running_loop().time()
                text = line.decode("utf-8", "replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except ValueError:
                    self._note_noise(text)
                    continue
                if isinstance(msg, dict):
                    await self._dispatch(msg)
                else:
                    self._note_noise(text)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # pragma: no cover - defensive
            self.closed_reason = f"ACP reader failed: {type(e).__name__}: {e}"
            _log.exception("[ComfyTV/acp] reader failed")
        finally:
            if not self.closed_reason:
                rc = proc.returncode
                self.closed_reason = (
                    "ACP connection closed" if rc in (None, 0)
                    else f"ACP process exited with code {rc}")
            self._fail_pending(self.closed_reason)

    # -------------------------------------------------------------- dispatch

    async def _dispatch(self, msg: dict) -> None:
        has_id = "id" in msg
        if has_id and ("result" in msg or "error" in msg):
            fut = self._pending.pop(msg["id"], None)
            if fut is None or fut.done():
                return
            error = msg.get("error")
            if error is not None:
                if isinstance(error, dict):
                    fut.set_exception(AcpError(
                        int(error.get("code") or CODE_PROTOCOL),
                        str(error.get("message") or "ACP error"),
                        error.get("data"),
                    ))
                else:
                    fut.set_exception(AcpError(CODE_PROTOCOL, str(error)))
            else:
                fut.set_result(msg.get("result"))
            return

        method = msg.get("method")
        if not isinstance(method, str):
            return

        if has_id:
            await self._answer_server_request(msg["id"], method,
                                              msg.get("params") or {})
            return

        if self._on_notification is not None:
            try:
                await self._on_notification(method, msg.get("params") or {})
            except Exception:
                # A failing consumer must not take down the read loop.
                _log.exception("[ComfyTV/acp] notification handler failed "
                               "for %s", method)

    async def _answer_server_request(self, rid: Any, method: str,
                                     params: dict) -> None:
        if self._on_request is None:
            await self._safe_send({
                "jsonrpc": "2.0", "id": rid,
                "error": {"code": CODE_METHOD_NOT_FOUND,
                          "message": f"unsupported client method: {method}"},
            })
            return
        try:
            result = await self._on_request(method, params)
        except AcpError as e:
            await self._safe_send({
                "jsonrpc": "2.0", "id": rid,
                "error": {"code": e.code, "message": e.message, "data": e.data},
            })
        except Exception as e:  # pragma: no cover - defensive
            _log.exception("[ComfyTV/acp] request handler failed for %s", method)
            await self._safe_send({
                "jsonrpc": "2.0", "id": rid,
                "error": {"code": CODE_PROTOCOL,
                          "message": f"{type(e).__name__}: {e}"},
            })
        else:
            await self._safe_send({
                "jsonrpc": "2.0", "id": rid,
                "result": {} if result is None else result,
            })

    # ----------------------------------------------------------------- write

    async def _safe_send(self, obj: dict) -> None:
        try:
            await self._send(obj)
        except Exception:
            _log.warning("[ComfyTV/acp] reply could not be written")

    async def _send(self, obj: dict) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.returncode is not None:
            raise AcpProcessError(self.closed_reason or "ACP process is not running")
        data = (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")
        async with self._write_lock:
            try:
                proc.stdin.write(data)
                await proc.stdin.drain()
            except (OSError, ConnectionError, RuntimeError) as e:
                raise AcpProcessError(f"ACP write failed: {e}") from e

    async def request(
        self,
        method: str,
        params: dict,
        *,
        timeout: Optional[float] = DEFAULT_REQUEST_TIMEOUT_S,
        idle_timeout: Optional[float] = None,
        label: str = "",
    ) -> Any:
        """Send a request and await its response.

        ``timeout`` bounds the whole call; ``idle_timeout`` bounds the gap
        since the last frame from the agent (any frame, including
        notifications, counts).  One of the two is normally enough: use the
        idle bound for prompts, where a long tool run is legitimate progress.
        """
        loop = asyncio.get_running_loop()
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = loop.create_future()
        self._pending[rid] = fut
        name = label or method
        try:
            await self._send({"jsonrpc": "2.0", "id": rid,
                              "method": method, "params": params})
        except Exception:
            self._pending.pop(rid, None)
            fut.cancel()
            raise

        started = loop.time()
        try:
            while True:
                if fut.done():
                    return fut.result()
                now = loop.time()
                if timeout is not None and now - started > timeout:
                    raise AcpError(
                        CODE_LOCAL_TIMEOUT,
                        f"{name} timed out after {int(timeout)}s")
                if idle_timeout is not None and now - self.last_activity > idle_timeout:
                    raise AcpError(
                        CODE_LOCAL_TIMEOUT,
                        f"{name} timed out: no activity from the agent for "
                        f"{int(idle_timeout)}s")
                await asyncio.wait({fut}, timeout=_POLL_S)
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict) -> None:
        await self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _fail_pending(self, reason: str) -> None:
        for _rid, fut in list(self._pending.items()):
            if not fut.done():
                fut.set_exception(AcpProcessError(reason))
        self._pending.clear()

    # ---------------------------------------------------------------- teardown

    async def wait_exit(self, timeout: float) -> bool:
        proc = self._proc
        if proc is None or proc.returncode is not None:
            return True
        try:
            await asyncio.wait_for(proc.wait(), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False

    async def close(self, timeout: float = 30.0) -> Optional[int]:
        """Close stdin, let the process exit on its own; never force-kill."""
        proc = self._proc
        if proc is None:
            return None
        try:
            if proc.stdin is not None and not proc.stdin.is_closing():
                proc.stdin.close()
        except Exception:
            pass
        if not await self.wait_exit(timeout):
            return None
        return proc.returncode

    async def dispose(self) -> None:
        """Cancel helper tasks.  The caller owns killing the child."""
        for task in (self._reader_task, self._stderr_task):
            if task is not None and not task.done():
                task.cancel()
        for task in (self._reader_task, self._stderr_task):
            if task is None:
                continue
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._fail_pending(self.closed_reason or "ACP transport disposed")

    def error_detail(self) -> str:
        """Best-effort human-readable failure detail for TurnResult.error."""
        parts = []
        if self.closed_reason:
            parts.append(self.closed_reason)
        stderr = self.stderr_text()
        if stderr:
            parts.append(stderr[-800:])
        if self.protocol_noise:
            parts.append("non-protocol stdout: " + self.protocol_noise[0][:200])
        return " | ".join(p for p in parts if p)
