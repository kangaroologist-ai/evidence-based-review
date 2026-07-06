"""orchestratord — a per-project resident daemon for the CLI tools (E3 of
docs/orchestrator_implementation_plan.md §2).

Motive (§2.1): each `python tools/verify.py …` pays Python cold start + httpx
client construction + a fresh, process-local throttle window. A round fires
dozens of verify/search/genealogy/fetch in sequence → dozens of cold starts and
dozens of independent throttlers hammering the same host. The daemon keeps the
interpreter, the httpx connection pool, the HostGate buckets and the OpenAlex
key warm; the CLI degrades to a thin "send argv, stream text back" client.

Layout:
- protocol      : length-prefixed JSON frames over a Unix domain socket.
- thin client   : try_connect / ensure_daemon / client_call / cli_entry.
- server        : serve_forever / handle_conn (flock single-instance, accept
                  loop with idle self-exit, one thread per connection).

Opt-in (E3 gray-launch, §10 B8): the CLI only routes through the daemon when
HEALTH_REVIEW_DAEMON=1. Default and any daemon failure fall back to running the
tool's own main() locally — byte-identical to the pre-daemon CLI (dual-track).

Concurrency (§2.7-a): dispatch is fully serialized by _DISPATCH_LOCK — only one
run() executes at a time, so the per-request `--parallel` pool is the only
concurrency and the global os.chdir / sys.stdout swaps can't race. That makes
the §5.2 DAEMON_MAX_WORKERS budget moot for E3; it matters only once E5 relaxes
this serialization to overlap requests.

Env handling. Only OPENALEX_API_KEY / SEMANTIC_SCHOLAR_API_KEY /
HEALTH_REVIEW_CACHE_DIR are injected per-request (apis module-global override,
B12-bis — valid because dispatch is serialized; see apis.py). HEALTH_REVIEW_ROOT
and HEALTH_REVIEW_TESTFLIGHT are forwarded but NOT consumed by apis: the daemon
runs under its own process env for those. ROOT can't cross-contaminate because
the socket path itself derives from project_root() (HEALTH_REVIEW_ROOT-aware) —
a client with a different ROOT connects to a *different* daemon, so each repo
gets its own. TESTFLIGHT therefore reflects the daemon's startup value, not the
per-call one (testflight users run local). ROOT/TESTFLIGHT stay on the
whitelist as reserved-for-E4 fields. Cross-repo cwd resolution is B12/E4's
`project.resolve`.
"""
from __future__ import annotations

import contextlib
import fcntl
import hashlib
import io
import json
import os
import pathlib
import signal
import socket
import struct
import sys
import threading
from collections.abc import Callable
from typing import IO

from . import apis, liveness, project

PROTOCOL_VERSION = 1
_LEN = struct.Struct(">I")  # 4-byte big-endian length prefix
_MAX_FRAME = 64 * 1024 * 1024  # 64 MiB guard against a corrupt length

# Whitelisted env keys forwarded to the daemon. Never forward the whole environ
# (leaks secrets, pollutes daemon state). Only keys that change tool behaviour.
ENV_WHITELIST = (
    "HEALTH_REVIEW_TESTFLIGHT",
    "HEALTH_REVIEW_CACHE_DIR",
    "HEALTH_REVIEW_ROOT",
    "OPENALEX_API_KEY",
    "SEMANTIC_SCHOLAR_API_KEY",
)

DISPATCH_CMDS = ("verify", "search", "genealogy", "fetch", "round")

# Serializes the global os.chdir + sys.stdout/stderr swaps a dispatch performs
# (§2.7-a). Acquired OUTER to cli_runtime._INVOKE_LOCK (which run() takes for
# its own argv/chdir swap); consistent order → no deadlock.
_DISPATCH_LOCK = threading.Lock()


# AF_UNIX sun_path is 104 bytes on macOS, 108 on Linux. A bind() on an over-long
# path raises "AF_UNIX path too long" — which the daemon would hit on a deep
# checkout (or a long repo path), then dual-track would mask the failure forever
# (the daemon silently never starts). Stay well under the lower limit.
_SOCK_PATH_LIMIT = 100


def _safe_sock_path(desired: str) -> str:
    """Return ``desired`` if it fits the AF_UNIX limit, else a short,
    deterministic fallback. The fallback hashes the desired path so it stays
    per-project unique, and client + daemon derive it identically.

    The base is a FIXED ``/tmp`` (POSIX-guaranteed, ~5 bytes) — NOT
    tempfile.gettempdir(), which reads $TMPDIR: a client and daemon with
    different $TMPDIR would otherwise derive different fallback sockets and
    never connect. The uid in the name avoids cross-user collisions, and the
    socket is created 0600 (umask-tight bind in serve_forever)."""
    if len(os.fsencode(desired)) <= _SOCK_PATH_LIMIT:
        return desired
    digest = hashlib.sha1(os.fsencode(desired)).hexdigest()[:12]
    return f"/tmp/healthrev-{os.getuid()}-{digest}.sock"


def _sock_path() -> str:
    return _safe_sock_path(str(project.project_root() / "state" / "orchestratord.sock"))


def _pid_path() -> str:
    # A regular file (flock target) — no AF_UNIX length limit — so it stays
    # under state/ regardless of where the socket lands. Single-instance is the
    # flock on THIS path, keyed to the project root.
    return str(project.project_root() / "state" / "orchestratord.pid")


class DaemonGone(Exception):
    """The daemon closed the connection before sending an exit frame. The
    caller re-runs locally (writes are idempotent — refs upsert + os.replace)."""


# ---- protocol --------------------------------------------------------------


def send_frame(sock: socket.socket, obj: dict) -> None:
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    sock.sendall(_LEN.pack(len(payload)) + payload)


def _recv_exactly(sock: socket.socket, n: int) -> bytes | None:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            return None  # clean EOF (or peer reset) mid-frame
        buf.extend(chunk)
    return bytes(buf)


def recv_frame(sock: socket.socket) -> dict | None:
    """Return the next frame, or None on a clean EOF at a frame boundary."""
    header = _recv_exactly(sock, _LEN.size)
    if header is None:
        return None
    (length,) = _LEN.unpack(header)
    if length > _MAX_FRAME:
        raise ValueError(f"frame too large: {length} bytes")
    body = _recv_exactly(sock, length)
    if body is None:
        return None
    return json.loads(body.decode("utf-8"))


# ---- thin client -----------------------------------------------------------


def whitelist_env() -> dict[str, str]:
    return {k: os.environ[k] for k in ENV_WHITELIST if k in os.environ}


def _resolve_sock(sock_path: str | None) -> str:
    return _safe_sock_path(sock_path) if sock_path is not None else _sock_path()


def read_pid(pid_path: str | None = None) -> int | None:
    """The pid the running daemon wrote into its (flock-held) lockfile, or None."""
    try:
        text = pathlib.Path(pid_path or _pid_path()).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text.split()[0]) if text else None
    except (ValueError, IndexError):
        return None


def is_running(sock_path: str | None = None) -> bool:
    """Ground truth: can we actually connect? (A stale pidfile can lie.)"""
    conn = try_connect(sock_path)
    if conn is None:
        return False
    conn.close()
    return True


def stop_running(pid_path: str | None = None, sock_path: str | None = None) -> bool:
    """SIGTERM the daemon recorded in the lockfile. Returns True if a signal was
    sent. The OS releases the flock on death; the next start unlinks the stale
    socket, so a hard terminate is safe.

    Guards against killing a stale/reused pid: only signals when a daemon is
    actually reachable on the socket (a live daemon has already written its
    current pid after flock, so the lockfile pid is then trustworthy)."""
    if not is_running(sock_path):
        return False
    pid = read_pid(pid_path)
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        return True
    except OSError:
        return False


def try_connect(sock_path: str | None = None) -> socket.socket | None:
    """Connect to a running daemon; None if none is listening."""
    path = _resolve_sock(sock_path)
    conn = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        conn.connect(path)
    except (FileNotFoundError, ConnectionRefusedError):
        conn.close()
        return None
    except OSError:
        conn.close()
        return None
    return conn


def ensure_daemon(
    sock_path: str | None = None,
    pid_path: str | None = None,
    *,
    idle: float = 300.0,
    spawn_timeout: float = 3.0,
) -> socket.socket | None:
    """Return a connection to the daemon, forking one on demand. None if a
    daemon can't be reached within spawn_timeout (caller falls back to local)."""
    path = _resolve_sock(sock_path)
    conn = try_connect(path)
    if conn is not None:
        return conn

    if threading.active_count() > 1:
        # Forking from a multithreaded process is unsafe: the child keeps only
        # the calling thread, so any lock another thread held is frozen-locked
        # forever — _build_dispatch's imports (httpx, logging) would deadlock.
        # The CLI is single-threaded so this never trips there; a future
        # in-thread caller (RoundRunner) must start the daemon out-of-band, not
        # fork from inside a pool. Fall back to local execution.
        return None

    os.makedirs(os.path.dirname(path), exist_ok=True)
    pidp = pid_path or _pid_path()
    # Fork a detached daemon. The child runs serve_forever (warm interpreter,
    # inheriting the client's already-imported modules) and never returns to
    # client code — the explicit _exit below + the finally backstop guarantee
    # it. ⚠ Do NOT add any statement between serve_forever() and _exit: if a
    # flock-loser returns from serve_forever, falling through would re-run the
    # whole CLI in the child (double execution).
    pid = os.fork()
    if pid == 0:  # child
        try:
            os.setsid()
            devnull = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(devnull, fd)
            serve_forever(sock_path=path, pid_path=pidp, idle=idle)
            os._exit(0)
        finally:
            os._exit(0)

    # parent: poll until the child binds (or another racing daemon wins).
    deadline = _monotonic() + spawn_timeout
    while _monotonic() < deadline:
        conn = try_connect(path)
        if conn is not None:
            return conn
        _sleep(0.01)
    with contextlib.suppress(ChildProcessError):
        os.waitpid(pid, os.WNOHANG)
    return None


def client_call(
    conn: socket.socket,
    cmd: str,
    argv: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    out: IO[str] | None = None,
    err: IO[str] | None = None,
) -> int:
    """Send a request and stream the response. Returns the tool's exit code.

    Raises DaemonGone if the connection drops before an exit frame arrives."""
    out = out or sys.stdout
    err = err or sys.stderr
    try:
        send_frame(
            conn,
            {
                "v": PROTOCOL_VERSION,
                "cmd": cmd,
                "argv": list(argv),
                "cwd": cwd if cwd is not None else os.getcwd(),
                "env": env if env is not None else whitelist_env(),
            },
        )
    except (ConnectionError, OSError) as exc:
        # Daemon died before/while we sent — fall back to a local re-run.
        raise DaemonGone(str(exc)) from exc
    while True:
        try:
            frame = recv_frame(conn)
        except (ConnectionError, OSError) as exc:
            raise DaemonGone(str(exc)) from exc
        if frame is None:
            raise DaemonGone("connection closed before exit frame")
        kind = frame.get("t")
        if kind == "stdout":
            out.write(frame.get("data", ""))
            out.flush()
        elif kind == "stderr":
            err.write(frame.get("data", ""))
            err.flush()
        elif kind == "exit":
            code = frame.get("code", 0)
            return code if isinstance(code, int) else 1


def cli_entry(cmd: str, local_main: Callable[[], None]) -> int:
    """Standalone CLI entry with opt-in daemon routing.

    Default (HEALTH_REVIEW_DAEMON unset) and every daemon failure run
    ``local_main()`` directly — byte-identical to the pre-daemon
    ``if __name__ == '__main__': main()`` (local_main raises SystemExit, which
    propagates its exact code). Returns 0 only when local_main returns normally.
    """
    if os.environ.get("HEALTH_REVIEW_DAEMON") == "1":
        conn = ensure_daemon()
        if conn is not None:
            try:
                return client_call(conn, cmd, sys.argv[1:])
            except DaemonGone:
                pass  # fall through to a local re-run (idempotent writes)
            finally:
                conn.close()
    local_main()
    return 0


# ---- server ----------------------------------------------------------------


class _FrameWriter(io.TextIOBase):
    """A text stream that ships each write to the client as a stdout/stderr
    frame. _DISPATCH_LOCK serializes *requests*, but a single request's
    `--parallel` worker threads all print to this process-global sys.stdout
    concurrently — so guard send_frame with a per-connection lock (shared by the
    stdout and stderr writers of one conn) or two workers' frame bytes interleave
    on the wire and corrupt the stream."""

    def __init__(self, conn: socket.socket, stream: str, lock: threading.Lock) -> None:
        self._conn = conn
        self._stream = stream
        self._lock = lock

    def writable(self) -> bool:
        return True

    def write(self, s: str) -> int:
        if s:
            with self._lock:
                send_frame(self._conn, {"t": self._stream, "data": s})
        return len(s)

    def flush(self) -> None:  # frames are sent eagerly; nothing to buffer
        pass


def _build_dispatch() -> dict[str, Callable[[list[str], str, dict], int]]:
    """Import the tools and map cmd → run(). Done in the daemon process only
    (the client never needs this), so importing the tools here can't cycle back
    through a client's `from lib import daemon`."""
    import fetch
    import genealogy
    import round as round_cmd  # tools/round.py (E4 RoundRunner CLI). The `as`
    import search  # alias binds round_cmd, so the builtin round() is NOT shadowed.
    import verify

    return {
        "verify": verify.run,
        "search": search.run,
        "genealogy": genealogy.run,
        "fetch": fetch.run,
        "round": round_cmd.run,
    }


def _conn_alive(conn: socket.socket) -> bool:
    """C12: True while the client peer is still connected. A non-blocking MSG_PEEK
    returns b'' once the peer sent FIN (closed) and raises ECONNRESET on a reset;
    BlockingIOError (no data, still open) = alive. Restores blocking mode so the
    _FrameWriter on the same conn/thread is unaffected."""
    try:
        conn.setblocking(False)
        try:
            return conn.recv(1, socket.MSG_PEEK) != b""
        except (BlockingIOError, InterruptedError):
            return True  # no pending data but the connection is open
        except OSError:
            return False  # ECONNRESET / EPIPE → peer gone
        finally:
            with contextlib.suppress(OSError):
                conn.setblocking(True)
    except OSError:
        return True  # couldn't probe → fail open (never abort a real commit on a probe glitch)


def _dispatch_one(
    fn: Callable[[list[str], str, dict], int],
    argv: list[str],
    cwd: str | None,
    env: dict,
    conn: socket.socket,
) -> int:
    """Run one command: install per-request key/cache ctx, redirect stdout/
    stderr to frames, invoke run(). Fully serialized via _DISPATCH_LOCK."""
    with _DISPATCH_LOCK:
        token = apis.set_request_ctx(env)
        write_lock = threading.Lock()  # shared by both writers of this conn
        saved_out, saved_err = sys.stdout, sys.stderr
        sys.stdout = _FrameWriter(conn, "stdout", write_lock)
        sys.stderr = _FrameWriter(conn, "stderr", write_lock)
        # C12: let refs.save() abort the store commit if the client vanished mid-run.
        liveness.set_probe(lambda: _conn_alive(conn))
        try:
            return fn(argv, cwd, env)
        except liveness.ClientGone as exc:  # C12: client gone before commit → no write
            sys.stderr.write(f"[abort] {exc}\n")
            return 2
        except SystemExit as exc:  # run() shouldn't raise it, but be safe
            code = exc.code
            return code if isinstance(code, int) else (0 if code is None else 1)
        except Exception as exc:  # never let a handler crash take down the conn
            sys.stderr.write(f"[ERROR] {type(exc).__name__}: {exc}\n")
            return 2
        finally:
            liveness.clear_probe()
            sys.stdout, sys.stderr = saved_out, saved_err
            apis.reset_request_ctx(token)


def handle_conn(conn: socket.socket, dispatch: dict) -> None:
    try:
        req = recv_frame(conn)
        if req is None:
            return
        cmd = req.get("cmd")
        fn = dispatch.get(cmd)
        if fn is None:
            send_frame(conn, {"t": "stderr", "data": f"[ERROR] unknown cmd {cmd!r}\n"})
            send_frame(conn, {"t": "exit", "code": 2})
            return
        code = _dispatch_one(
            fn,
            req.get("argv") or [],
            req.get("cwd"),
            req.get("env") or {},
            conn,
        )
        send_frame(conn, {"t": "exit", "code": code})
    except (ConnectionError, BrokenPipeError, OSError):
        pass  # client vanished mid-stream; nothing to report to
    finally:
        with contextlib.suppress(OSError):
            conn.close()


class _Inflight:
    def __init__(self) -> None:
        self._n = 0
        self._lock = threading.Lock()

    def enter(self) -> None:
        with self._lock:
            self._n += 1

    def leave(self) -> None:
        with self._lock:
            self._n -= 1

    @property
    def value(self) -> int:
        with self._lock:
            return self._n


def serve_forever(
    *,
    sock_path: str | None = None,
    pid_path: str | None = None,
    idle: float = 300.0,
    dispatch: dict | None = None,
) -> None:
    """flock single-instance → bind UDS → accept loop (one thread/conn) →
    idle self-exit when no request has arrived for `idle`s and none is in
    flight. A second daemon that can't get the flock simply returns."""
    path = _resolve_sock(sock_path)
    pidp = pid_path or _pid_path()
    # The socket may live in a temp dir (long-path fallback) while the pidlock
    # always lives under state/ — ensure BOTH dirs exist.
    os.makedirs(os.path.dirname(path), exist_ok=True)
    os.makedirs(os.path.dirname(pidp), exist_ok=True)

    lock_fd = os.open(pidp, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return  # another daemon owns this project
        # Record our pid in the (flock-held) lockfile so a control CLI can
        # SIGTERM us — e.g. to force a restart after a config change (the E5
        # conservative-rollback path). The OS releases the flock on death and
        # the next start unlinks the stale socket, so a hard kill is safe.
        with contextlib.suppress(OSError):
            os.ftruncate(lock_fd, 0)
            os.write(lock_fd, f"{os.getpid()}\n".encode())
            os.fsync(lock_fd)
        if dispatch is None:
            dispatch = _build_dispatch()
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            # Tolerate a foreign/squatted file at the path (PermissionError etc),
            # not just a missing one — bind handles the leftover.
            with contextlib.suppress(OSError):
                os.unlink(path)  # clear a stale sock left by a kill -9'd daemon
            # Create the socket 0600 from the start (no world-connectable window
            # between bind and chmod) — matters for the shared /tmp fallback.
            saved_umask = os.umask(0o077)
            try:
                srv.bind(path)
            finally:
                os.umask(saved_umask)
            with contextlib.suppress(OSError):
                os.chmod(path, 0o600)  # belt-and-suspenders
            srv.listen(64)
            srv.settimeout(idle)
            inflight = _Inflight()
            while True:
                try:
                    conn, _ = srv.accept()
                except socket.timeout:
                    if inflight.value == 0:
                        break  # idle → exit; next call re-forks on demand
                    continue
                # Count BEFORE starting the thread so a just-accepted request
                # can't be missed by the next accept-timeout's idle check.
                inflight.enter()
                threading.Thread(
                    target=_serve_conn,
                    args=(conn, dispatch, inflight),
                    daemon=True,
                ).start()
        finally:
            srv.close()
            with contextlib.suppress(FileNotFoundError):
                os.unlink(path)
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _serve_conn(conn: socket.socket, dispatch: dict, inflight: _Inflight) -> None:
    # inflight was incremented by the accept loop before this thread started.
    try:
        handle_conn(conn, dispatch)
    finally:
        inflight.leave()


# Indirection so tests can monkeypatch timing, and so the module has no hard
# dependency on time at import (mirrors apis' monotonic discipline).
def _monotonic() -> float:
    import time

    return time.monotonic()


def _sleep(seconds: float) -> None:
    import time

    time.sleep(seconds)
