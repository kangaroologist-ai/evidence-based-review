from __future__ import annotations

import argparse
import pathlib
import re
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import refs

PREPRINT_PREFIXES = ("10.1101/", "10.21203/", "10.31219/", "10.20944/")
PREPRINT_JOURNALS = {"biorxiv", "medrxiv", "research square", "preprints.org", ""}
TITLE_THRESHOLD = 0.85


def jaccard(left: str, right: str) -> float:
    left_tokens = set(re.findall(r"\w+", (left or "").lower()))
    right_tokens = set(re.findall(r"\w+", (right or "").lower()))
    union = left_tokens | right_tokens
    if not union:
        return 1.0
    return len(left_tokens & right_tokens) / len(union)


def is_preprint(entry: refs.Entry) -> bool:
    doi = entry["doi"]
    if any(doi.startswith(prefix) for prefix in PREPRINT_PREFIXES):
        return True
    journal = entry.get("journal", "").strip().lower()
    return journal in PREPRINT_JOURNALS


def first_family(entry: refs.Entry) -> str:
    author = (entry.get("authors") or [""])[0]
    return author.split(",", 1)[0].strip().lower()


def _year(entry: refs.Entry) -> int:
    return entry.get("year", 0)


def _choose_direction(
    left_doi: str,
    right_doi: str,
    left_entry: refs.Entry,
    right_entry: refs.Entry,
) -> tuple[str, str]:
    left_preprint = is_preprint(left_entry)
    right_preprint = is_preprint(right_entry)
    if left_preprint and not right_preprint:
        return left_doi, right_doi
    if right_preprint and not left_preprint:
        return right_doi, left_doi

    left_year = _year(left_entry)
    right_year = _year(right_entry)
    if left_year != right_year:
        return (left_doi, right_doi) if left_year <= right_year else (right_doi, left_doi)
    return (left_doi, right_doi) if left_doi <= right_doi else (right_doi, left_doi)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("topic_dir", help="Path to a topic directory under reviews/")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    store = refs.load(topic_dir)
    if store is None:
        print(f"[ERROR] missing references store: {topic_dir}")
        raise SystemExit(1)

    items = list(store["entries"].items())
    chosen: dict[str, tuple[str, float]] = {}
    for index, (left_doi, left_entry) in enumerate(items):
        if left_entry.get("retracted", False):
            continue
        if isinstance(left_entry.get("superseded_by"), str):
            continue
        left_family = first_family(left_entry)
        if not left_family:
            continue
        for right_doi, right_entry in items[index + 1 :]:
            if right_entry.get("retracted", False):
                continue
            if isinstance(right_entry.get("superseded_by"), str):
                continue
            if first_family(right_entry) != left_family:
                continue
            left_year = _year(left_entry)
            right_year = _year(right_entry)
            if left_year and right_year and abs(left_year - right_year) > 1:
                continue
            similarity = jaccard(left_entry.get("title", ""), right_entry.get("title", ""))
            if similarity <= TITLE_THRESHOLD:
                continue
            old_doi, new_doi = _choose_direction(left_doi, right_doi, left_entry, right_entry)
            current = chosen.get(old_doi)
            if current is None or similarity > current[1]:
                chosen[old_doi] = (new_doi, similarity)

    actions = sorted((old_doi, new_doi) for old_doi, (new_doi, _) in chosen.items())
    for old_doi, new_doi in actions:
        print(f"{old_doi} -> {new_doi}")

    if args.dry_run:
        print(f"[dry-run] {len(actions)} pairs")
        return

    for old_doi, new_doi in actions:
        old_entry = store["entries"][old_doi]
        new_entry = store["entries"][new_doi]
        old_entry["superseded_by"] = new_doi
        supersedes = list(new_entry.get("supersedes", []))
        if old_doi not in supersedes:
            supersedes.append(old_doi)
        new_entry["supersedes"] = sorted(supersedes)

    refs.save(topic_dir, store)
    print(f"applied {len(actions)} dedupe links")


if __name__ == "__main__":
    main()
