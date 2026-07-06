"""In-process invocation shim for the CLI tools (E2 of the orchestrator plan,
docs/orchestrator_implementation_plan.md §2.5/§2.8).

Goal: let a tool's ``main()`` run in-process (no interpreter cold start) when
called by the daemon (E3) or tests, **without** rewriting each large main()'s
many ``raise SystemExit`` exit points (that deep refactor is higher-risk and
unnecessary — the daemon serializes requests, so wrapping main() with a
save/restore of sys.argv is equivalent and far safer).

``invoke()``:
- sets ``sys.argv = [prog, *argv]`` around the call (restored in finally);
- optionally ``chdir(cwd)`` under a global lock (the daemon serializes
  requests — plan §2.7-a; E4 switches to explicit cwd-passing via
  ``project.resolve``);
- returns the exit code, catching ``SystemExit`` (incl. argparse's exit 2).

``env`` is accepted but currently unused: local mode reads ``os.environ``
directly. Daemon multi-topic key/cache injection is wired in E3 via a
``contextvars.ContextVar`` in ``apis`` (plan §2.3 B12-bis); ``invoke`` will
set it from ``env`` then.
"""
from __future__ import annotations

import os
import sys
import threading
from collections.abc import Callable

# The daemon serializes requests (chdir-lock), but guard sys.argv / chdir
# mutation here too so an accidental concurrent invoke() can't interleave
# this process-global state.
_INVOKE_LOCK = threading.Lock()


def invoke(
    main_fn: Callable[[], None],
    argv: list[str],
    *,
    prog: str | None = None,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
) -> int:
    """Run ``main_fn`` as if invoked with ``argv``; always return an exit code.

    ``main_fn`` is expected to read ``sys.argv`` and raise ``SystemExit`` (the
    existing CLI shape). Returns 0 when it returns normally. ``prog`` sets
    ``sys.argv[0]`` so argparse usage text matches the standalone CLI — without
    it the daemon's own argv[0] would leak into ``--help`` / error output.

    NOTE: ``env`` is accepted but dropped — local mode reads ``os.environ``
    directly. Until E3 routes ``env`` into apis' per-request ContextVar (plan
    §2.3 B12-bis), calling invoke() in-process for two *different topics* reuses
    the import-time-frozen OpenAlex/S2 key + cache dir; harmless in single-
    process CLI mode, MUST be fixed before any in-process multi-topic
    RoundRunner. NOTE: ``_INVOKE_LOCK`` is non-reentrant — invoke() must not be
    called nested (E4's RoundRunner calls run_* sequentially, never one inside
    another, or this self-deadlocks)."""
    del env
    name = prog or (sys.argv[0] if sys.argv else "tool")
    with _INVOKE_LOCK:
        saved_argv = sys.argv
        saved_cwd = os.getcwd() if cwd is not None else None
        sys.argv = [name, *argv]
        try:
            if cwd is not None:
                try:
                    os.chdir(cwd)
                except OSError as exc:
                    # Keep the "invoke always returns an int" contract: a bad
                    # cwd must not escape as a raw OSError to the daemon.
                    print(f"[ERROR] cannot chdir to {cwd}: {exc}", file=sys.stderr)
                    return 1
            try:
                main_fn()
                return 0
            except SystemExit as exc:
                code = exc.code
                if code is None:
                    return 0
                if isinstance(code, int):
                    return code
                # SystemExit("message") / non-int → print to stderr, exit 1.
                print(code, file=sys.stderr)
                return 1
        finally:
            sys.argv = saved_argv
            if saved_cwd is not None:
                os.chdir(saved_cwd)
