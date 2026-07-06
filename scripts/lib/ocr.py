"""MinerU OCR rung (plan v3 §3.4 C19).

The 4th rung of the grounding ladder: when born-digital PDF text extraction
(PyMuPDF via lib/pdfx.py) comes back empty — i.e. the PDF is scanned / image
only, exactly the old papers most likely to lack an abstract — fall back to
OCR. MinerU is NOT importable from the toolchain python (it lives in a
separate env, `import mineru` fails here), so it is invoked as a subprocess
CLI and degrades gracefully when absent: OCR is a best-effort top rung, never
a hard dependency. Every failure mode (binary missing / nonzero exit /
timeout / bad path) returns None so the caller simply stays at title_only.
"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import tempfile

# The MinerU CLI binary. Configurable via env because it lives in a separate
# environment; we also probe the common names. (magic-pdf is MinerU's older CLI.)
_OCR_CMD_ENV = "HEALTH_REVIEW_OCR_CMD"
_DEFAULT_CMDS = ("mineru", "magic-pdf")
_DEFAULT_TIMEOUT = 300


def _resolve_cmd(cmd: str | None) -> str | None:
    """Resolve the OCR command to an invocable path, or None if unavailable."""
    for candidate in (cmd, os.environ.get(_OCR_CMD_ENV)):
        if candidate:
            if shutil.which(candidate) or pathlib.Path(candidate).exists():
                return candidate
            return None  # explicitly requested but not found → unavailable
    for candidate in _DEFAULT_CMDS:
        if shutil.which(candidate):
            return candidate
    return None


def ocr_available(cmd: str | None = None) -> bool:
    """True iff a MinerU-class OCR CLI is invocable in this environment."""
    return _resolve_cmd(cmd) is not None


def _collect_markdown(out_dir: pathlib.Path) -> str:
    """Join all markdown MinerU wrote under out_dir (its text output)."""
    parts: list[str] = []
    for md in sorted(out_dir.rglob("*.md")):
        try:
            parts.append(md.read_text(encoding="utf-8"))
        except OSError:
            continue
    return "\n\n".join(p.strip() for p in parts if p.strip())


def ocr_pdf(
    pdf_path: str | pathlib.Path,
    *,
    cmd: str | None = None,
    timeout: int = _DEFAULT_TIMEOUT,
) -> str | None:
    """OCR a (scanned) PDF to text via MinerU. Returns the extracted text, or
    None if OCR is unavailable / the binary errors / it times out / the path is
    missing. NEVER raises — callers fall back to title_only on None."""
    resolved = _resolve_cmd(cmd)
    if resolved is None:
        return None
    pdf_path = pathlib.Path(pdf_path)
    if not pdf_path.exists():
        return None
    try:
        with tempfile.TemporaryDirectory() as out_dir:
            proc = subprocess.run(
                [resolved, "-p", str(pdf_path), "-o", out_dir],
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if proc.returncode != 0:
                return None
            text = _collect_markdown(pathlib.Path(out_dir))
            return text or None
    except (subprocess.TimeoutExpired, OSError):
        return None
