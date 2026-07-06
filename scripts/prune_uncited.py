"""Round-end pruning that PRESERVES recall (plan v3 §2 C6 / A1).

Unlike ``prune_keep.py`` (which excludes every verified entry NOT in a keep-set
— the aggressive prune that kills broad recall to satisfy the cited-ratio), this
tool acts only on the analyst's explicit four-bucket verdicts:

  - ``--exclude``      : cross-domain noise → ``excluded_reason`` (dropped from
                         body + ratio, same as exclude.py).
  - ``--keep-uncited`` : on-topic real evidence kept in the store but not the
                         strongest support for any proposition → ``keep_uncited``
                         flag. Stays verified + citable, but is excluded from the
                         cited-ratio denominator so reading broadly never forces
                         deleting read literature.

Entries the analyst put under cite_recommend / uncertain are left untouched.
Targets may be given as citation keys ([@key] without the @) or DOIs.

Usage:
    python tools/prune_uncited.py reviews/<topic> --keep-uncited smith2020 jones2019
    python tools/prune_uncited.py reviews/<topic> --exclude "off2021:cross-domain"
    python tools/prune_uncited.py reviews/<topic> --clear-keep smith2020
"""
from __future__ import annotations

import argparse
import pathlib
import sys
from datetime import datetime

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import testflight
import refs


def _resolve(store: refs.Store, target: str) -> str | None:
    """Resolve a target (DOI or citation key) to a stored DOI, or None."""
    doi = target.lower()
    if doi in store["entries"]:
        return doi
    return refs.resolve_citation_key(store, target)


def _append_log(topic_dir: pathlib.Path, lines: list[str]) -> None:
    if not lines:
        return
    log_path = topic_dir / "research_log.md"
    timestamp = datetime.now().isoformat(timespec="seconds")
    block = "\n".join(f"- [{timestamp}] prune_uncited {line}" for line in lines)
    if log_path.exists():
        existing = log_path.read_text(encoding="utf-8").rstrip()
        log_path.write_text(existing + "\n" + block + "\n", encoding="utf-8")
        return
    log_path.write_text(block + "\n", encoding="utf-8")


def _split_reason(spec: str) -> tuple[str, str]:
    """``"key:reason text"`` → ("key", "reason text"); default reason if absent."""
    target, _, reason = spec.partition(":")
    return target.strip(), (reason.strip() or "analyst exclude_recommend")


_CITE_RE = __import__("re").compile(r"\[@([\w:.-]+)\]")


def cited_keys(review_text: str) -> set[str]:
    """Citation keys referenced in the review body."""
    return set(_CITE_RE.findall(review_text))


def mark_uncited_rest(store: refs.Store, cited: set[str]) -> int:
    """A1/C6: mark every verified, non-excluded, non-retracted entry whose
    citation_key is NOT in ``cited`` as keep_uncited. Returns count marked."""
    n = 0
    for doi, entry in store["entries"].items():
        if (
            entry.get("verification_status") == "verified"
            and not entry.get("retracted")
            and not entry.get("excluded_reason")
            and entry.get("citation_key") not in cited
            and not entry.get("keep_uncited")
        ):
            refs.set_keep_uncited(store, doi, True)
            n += 1
    return n


def run(
    store: refs.Store,
    *,
    keep_uncited: list[str],
    exclude: list[str],
    clear_keep: list[str],
    clear_exclude: list[str] | None = None,
) -> tuple[list[str], list[str]]:
    """Apply the verdicts in place. Returns (log_lines, unresolved_targets)."""
    log_lines: list[str] = []
    unresolved: list[str] = []

    for target in clear_exclude or []:
        doi = _resolve(store, target)
        if doi is None:
            unresolved.append(target)
            continue
        refs.include_entry(store, doi)
        log_lines.append(f"clear-exclude `{doi}`")

    for target in keep_uncited:
        doi = _resolve(store, target)
        if doi is None:
            unresolved.append(target)
            continue
        refs.set_keep_uncited(store, doi, True)
        log_lines.append(f"keep-uncited `{doi}`")

    for target in clear_keep:
        doi = _resolve(store, target)
        if doi is None:
            unresolved.append(target)
            continue
        refs.set_keep_uncited(store, doi, False)
        log_lines.append(f"clear-keep `{doi}`")

    for spec in exclude:
        target, reason = _split_reason(spec)
        doi = _resolve(store, target)
        if doi is None:
            unresolved.append(target)
            continue
        # C18 (m6): a duplicate --exclude on an already-excluded entry is a no-op smell
        # (likely a re-run of a stale command) — WARN so it isn't mistaken for new pruning.
        entry = store.get("entries", {}).get(doi)
        if entry is not None and entry.get("excluded_reason"):
            print(
                f"[WARN] `{doi}` already excluded ({entry.get('excluded_reason')!r}); "
                f"--exclude re-flag is a no-op (C18)",
                file=sys.stderr,
            )
        refs.exclude_entry(store, doi, reason)
        log_lines.append(f"exclude `{doi}` — {reason}")

    return log_lines, unresolved


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("topic_dir")
    parser.add_argument(
        "--keep-uncited", nargs="*", default=[], metavar="KEY_OR_DOI",
        help="Mark on-topic-but-uncited entries (excluded from cited-ratio, kept in store).",
    )
    parser.add_argument(
        "--exclude", nargs="*", default=[], metavar="KEY[:reason]",
        help="Exclude cross-domain noise (sets excluded_reason).",
    )
    parser.add_argument(
        "--clear-keep", nargs="*", default=[], metavar="KEY_OR_DOI",
        help="Reverse a prior keep_uncited mark.",
    )
    parser.add_argument(
        "--clear-exclude", nargs="*", default=[], metavar="KEY_OR_DOI",
        help="Recall-safe un-exclude: clear excluded_reason (refs.include_entry). "
        "round_gate forbids permanently excluding abstract/title-only entries "
        "(spec N4); use this to reverse such an exclude, then --keep-uncited them "
        "to keep them off the cited-ratio denominator.",
    )
    parser.add_argument(
        "--keep-uncited-rest", action="store_true",
        help="A1/C6 bulk: mark EVERY verified non-excluded entry whose citation_key "
        "is not cited in <topic>/review.md as keep_uncited — recall-first genealogy "
        "breadth stays in the store but off the cited-ratio denominator, so reading "
        "wide doesn't force deleting read literature. Run after the draft is written.",
    )
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    with testflight.timer("prune_uncited", "main", topic_dir=topic_dir):
        store = refs.load(topic_dir)
        if store is None:
            print(f"[ERROR] missing references store: {topic_dir}")
            raise SystemExit(1)

        log_lines, unresolved = run(
            store,
            keep_uncited=args.keep_uncited,
            exclude=args.exclude,
            clear_keep=args.clear_keep,
            clear_exclude=args.clear_exclude,
        )
        if args.keep_uncited_rest:
            review_path = topic_dir / "review.md"
            cited = cited_keys(review_path.read_text(encoding="utf-8")) if review_path.exists() else set()
            n = mark_uncited_rest(store, cited)
            log_lines.append(f"keep-uncited-rest: marked {n} uncited verified entries (cited={len(cited)})")
        refs.save(topic_dir, store)
        _append_log(topic_dir, log_lines)

    for line in log_lines:
        print(f"[OK] {line}")
    if unresolved:
        print(f"[WARN] unresolved targets (not in store): {', '.join(unresolved)}")
    if not log_lines and not unresolved:
        print("[INFO] nothing to do (no --keep-uncited / --exclude / --clear-keep)")


if __name__ == "__main__":
    main()
