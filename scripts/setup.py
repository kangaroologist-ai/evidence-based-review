#!/usr/bin/env python3
"""EBR (Evidence-Based Research) one-time setup CLI.

EBR is a portable skill installed under ``~/.claude/skills/EBR/``. The runnable
tools live in ``EBR/scripts/`` and their support library in ``EBR/scripts/lib/``.
Unlike the in-repo project (which keeps ``reviews/ state/ tmp/`` next to a
``tools/`` directory), a portable install has nowhere to put per-user state. This
script bootstraps a **workspace** directory to hold ``reviews/ state/ tmp/
patches/`` and tells the user how to point the tools at it.

How the tools find the workspace
--------------------------------
``lib/project.py:project_root()`` resolves the root in this order:

1. ``$HEALTH_REVIEW_ROOT`` (if set) — used verbatim.
2. Walk up from the lib file looking for a dir that has ``tools/`` **and**
   (``reviews/`` or ``CLAUDE.md``).
3. Fallback: ``<lib>/../../..``.

The portable layout uses ``scripts/`` (not ``tools/``), so the marker-walk in
step 2 will never match a freshly created workspace. ``$HEALTH_REVIEW_ROOT`` is
therefore the load-bearing mechanism, and this script prints the exact
``export`` line to source. (We still drop a minimal ``CLAUDE.md`` marker so that
*if* a future layout grows a ``tools/`` dir, the walk could also resolve it.)

API keys are all OPTIONAL. ``lib/apis.py`` reads each as ``env > state/<file> >
None``; with no key at all, EBR runs end-to-end and only ``genealogy`` /
``search`` get throttled. This script can stash optional keys into the
workspace ``state/`` dir but never requires them.

Stdlib only: argparse / pathlib / shutil / os / sys.
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import sys

# --- self-contained import contract -----------------------------------------
# setup.py lives in EBR/scripts/; its support lib is EBR/scripts/lib/. Insert the
# script's own directory so ``from lib import ...`` resolves regardless of cwd.
_SCRIPTS_DIR = pathlib.Path(__file__).resolve(strict=False).parent
if str(_SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_DIR))

from lib import project  # noqa: E402  (after sys.path tweak, by design)

# The skill's patches/ live one level above scripts/: EBR/scripts/ -> EBR/patches/.
_SKILL_ROOT = _SCRIPTS_DIR.parent
_PATCHES_SRC = _SKILL_ROOT / "patches"

# State filenames mirror what lib/apis.py reads (env var > state/<file> > None).
_EMAIL_FILE = "email"  # EBR_EMAIL > state/email > apis.EMAIL fallback
_OPENALEX_FILE = "openalex_api_key"  # OPENALEX_API_KEY > state/openalex_api_key
_SEMANTIC_FILE = "semantic_scholar_api_key"  # SEMANTIC_SCHOLAR_API_KEY > state/...

_CLAUDE_MARKER = """# EBR workspace

This directory is an EBR (Evidence-Based Research) workspace created by
`scripts/setup.py`. It holds `reviews/`, `state/`, `tmp/`, and `patches/`.

The EBR tools locate this workspace via the `HEALTH_REVIEW_ROOT` environment
variable. Make sure it is exported before running any tool:

    export HEALTH_REVIEW_ROOT="{workspace}"

This file also doubles as a `project_root()` marker.
"""


# --- helpers ----------------------------------------------------------------

def _resolve_workspace(raw: str | None) -> pathlib.Path:
    """Workspace dir from --workspace, defaulting to the current cwd."""
    base = raw if raw else os.getcwd()
    return pathlib.Path(base).expanduser().resolve(strict=False)


def _ensure_dirs(workspace: pathlib.Path) -> list[pathlib.Path]:
    """Create reviews/ state/ tmp/ (idempotent). Returns the created/ensured set."""
    subdirs = [workspace / "reviews", workspace / "state", workspace / "tmp"]
    workspace.mkdir(parents=True, exist_ok=True)
    for d in subdirs:
        d.mkdir(parents=True, exist_ok=True)
    return subdirs


def _copy_patches(workspace: pathlib.Path) -> tuple[bool, str]:
    """Copy the skill's patches/ into <workspace>/patches/.

    Returns (ok, message). Missing skill patches/ is a graceful warning, not a
    crash — EBR can still run; only domain frontmatter (term_check / lint /
    search) is degraded.
    """
    dst = workspace / "patches"
    if not _PATCHES_SRC.is_dir():
        return (
            False,
            f"[WARN] skill patches/ not found at {_PATCHES_SRC} — "
            f"skipping copy. Domain patches (health/animals/physics/...) "
            f"will be unavailable; create {dst} manually if needed.",
        )
    dst.mkdir(parents=True, exist_ok=True)
    copied = 0
    for entry in sorted(_PATCHES_SRC.iterdir()):
        if entry.is_file():
            shutil.copy2(entry, dst / entry.name)
            copied += 1
        elif entry.is_dir():
            shutil.copytree(entry, dst / entry.name, dirs_exist_ok=True)
            copied += 1
    return (True, f"copied {copied} patch entr{'y' if copied == 1 else 'ies'} -> {dst}")


def _write_marker(workspace: pathlib.Path) -> pathlib.Path:
    """Write a minimal CLAUDE.md marker (overwrite — idempotent)."""
    marker = workspace / "CLAUDE.md"
    marker.write_text(
        _CLAUDE_MARKER.format(workspace=workspace.as_posix()), encoding="utf-8"
    )
    return marker


def _write_state_file(
    workspace: pathlib.Path, filename: str, value: str, *, secret: bool
) -> pathlib.Path:
    """Write a single state/<filename>. chmod 600 for secrets (keys)."""
    state = workspace / "state"
    state.mkdir(parents=True, exist_ok=True)
    path = state / filename
    path.write_text(value.strip() + "\n", encoding="utf-8")
    if secret:
        os.chmod(path, 0o600)
    return path


def _key_status(workspace: pathlib.Path, env_var: str, filename: str) -> str:
    """Describe how a key/email would resolve: env var, state file, or fallback."""
    env_val = os.getenv(env_var)
    if env_val and env_val.strip():
        return f"set via ${env_var} (env)"
    state_path = workspace / "state" / filename
    if state_path.exists() and state_path.read_text(encoding="utf-8").strip():
        return f"set via {state_path}"
    return "not set (fallback)"


_FALLBACK_NOTE = (
    "No API key is required: EBR runs end-to-end with zero keys. Without them, "
    "OpenAlex genealogy and Semantic Scholar search are rate-limited (shared "
    "pools) but otherwise fully functional. CrossRef always uses a polite-pool "
    "mailto and needs no registration."
)


# --- subcommands ------------------------------------------------------------

def _print_summary(
    workspace: pathlib.Path,
    *,
    patches_msg: str,
    email_set: str | None,
) -> None:
    export_line = f'export HEALTH_REVIEW_ROOT="{workspace.as_posix()}"'
    print("=" * 72)
    print("EBR workspace ready")
    print("=" * 72)
    print(f"workspace : {workspace}")
    print(f"  reviews/  state/  tmp/  patches/  CLAUDE.md")
    print(f"patches   : {patches_msg}")
    if email_set:
        print(f"email     : {email_set}")
    print()
    print("Point the EBR tools at this workspace — add to your shell, then source it:")
    print()
    print(f"  {export_line}")
    print()
    print("API key status (all OPTIONAL):")
    print(f"  email             : {_key_status(workspace, 'EBR_EMAIL', _EMAIL_FILE)}")
    print(
        f"  OpenAlex (genealogy): "
        f"{_key_status(workspace, 'OPENALEX_API_KEY', _OPENALEX_FILE)}"
    )
    print(
        f"  Semantic Scholar    : "
        f"{_key_status(workspace, 'SEMANTIC_SCHOLAR_API_KEY', _SEMANTIC_FILE)}"
    )
    print()
    print(_FALLBACK_NOTE)
    print("=" * 72)


def cmd_init(args: argparse.Namespace) -> int:
    workspace = _resolve_workspace(args.workspace)
    _ensure_dirs(workspace)
    patches_ok, patches_msg = _copy_patches(workspace)
    _write_marker(workspace)

    email_set: str | None = None
    if args.email:
        path = _write_state_file(workspace, _EMAIL_FILE, args.email, secret=False)
        email_set = f"wrote {args.email} -> {path}"
    if args.openalex_key:
        _write_state_file(workspace, _OPENALEX_FILE, args.openalex_key, secret=True)
    if args.semantic_scholar_key:
        _write_state_file(
            workspace, _SEMANTIC_FILE, args.semantic_scholar_key, secret=True
        )

    if not patches_ok:
        print(patches_msg, file=sys.stderr)
    _print_summary(workspace, patches_msg=patches_msg, email_set=email_set)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    # Infer workspace: --workspace > $HEALTH_REVIEW_ROOT > project_root() > cwd.
    if args.workspace:
        workspace = _resolve_workspace(args.workspace)
    elif os.getenv("HEALTH_REVIEW_ROOT"):
        workspace = pathlib.Path(os.environ["HEALTH_REVIEW_ROOT"]).resolve(strict=False)
    else:
        # project.project_root() honors the same env first, then marker-walk.
        workspace = project.project_root()

    print("=" * 72)
    print("EBR workspace status")
    print("=" * 72)
    print(f"workspace : {workspace}")
    exists = workspace.is_dir()
    print(f"  exists  : {'yes' if exists else 'NO (run: setup.py init --workspace ...)'}")
    if exists:
        for name in ("reviews", "state", "tmp", "patches"):
            sub = workspace / name
            print(f"  {name + '/':9}: {'present' if sub.is_dir() else 'missing'}")
        marker = workspace / "CLAUDE.md"
        print(f"  CLAUDE.md: {'present' if marker.exists() else 'missing'}")
    print()
    env_root = os.getenv("HEALTH_REVIEW_ROOT")
    if env_root:
        print(f"$HEALTH_REVIEW_ROOT = {env_root}")
    else:
        print("$HEALTH_REVIEW_ROOT is NOT set — tools may not find this workspace.")
        print(f'  fix: export HEALTH_REVIEW_ROOT="{workspace.as_posix()}"')
    print()
    print("API key status (all OPTIONAL):")
    print(f"  email             : {_key_status(workspace, 'EBR_EMAIL', _EMAIL_FILE)}")
    print(
        f"  OpenAlex (genealogy): "
        f"{_key_status(workspace, 'OPENALEX_API_KEY', _OPENALEX_FILE)}"
    )
    print(
        f"  Semantic Scholar    : "
        f"{_key_status(workspace, 'SEMANTIC_SCHOLAR_API_KEY', _SEMANTIC_FILE)}"
    )
    print()
    print(_FALLBACK_NOTE)
    print("=" * 72)
    return 0


# --- argparse ---------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="setup.py",
        description=(
            "EBR (Evidence-Based Research) one-time setup. Bootstraps a "
            "workspace (reviews/ state/ tmp/ patches/) and reports optional "
            "API-key configuration. Idempotent — safe to re-run."
        ),
    )
    sub = parser.add_subparsers(dest="command")

    p_init = sub.add_parser(
        "init",
        help="Create/refresh a workspace and (optionally) store email + API keys.",
    )
    p_init.add_argument(
        "--workspace",
        metavar="DIR",
        default=None,
        help="Workspace directory (default: current working directory).",
    )
    p_init.add_argument(
        "--email",
        metavar="ADDR",
        default=None,
        help="Email for CrossRef/OpenAlex polite pool -> state/email.",
    )
    p_init.add_argument(
        "--openalex-key",
        metavar="KEY",
        default=None,
        help="Optional OpenAlex Premium key -> state/openalex_api_key (chmod 600).",
    )
    p_init.add_argument(
        "--semantic-scholar-key",
        metavar="KEY",
        default=None,
        help="Optional Semantic Scholar key -> state/semantic_scholar_api_key (chmod 600).",
    )
    p_init.set_defaults(func=cmd_init)

    p_status = sub.add_parser(
        "status",
        help="Show current workspace + which optional keys are set.",
    )
    p_status.add_argument(
        "--workspace",
        metavar="DIR",
        default=None,
        help="Workspace to inspect (default: $HEALTH_REVIEW_ROOT, else inferred).",
    )
    p_status.set_defaults(func=cmd_status)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    # Support both subcommands (init/status) AND the spec's top-level flags:
    #   setup.py --workspace D --email E   (implies init)
    #   setup.py --status                  (implies status)
    # Pre-scan so users aren't forced to type the subcommand.
    raw = list(sys.argv[1:] if argv is None else argv)
    has_subcommand = bool(raw) and raw[0] in {"init", "status"}
    if not has_subcommand:
        if "--status" in raw:
            raw = ["status"] + [a for a in raw if a != "--status"]
        else:
            raw = ["init"] + raw

    args = parser.parse_args(raw)
    if not getattr(args, "func", None):
        parser.print_help()
        return 0
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
