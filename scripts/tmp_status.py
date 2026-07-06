from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import apis, project


def dir_size(path: pathlib.Path) -> int:
    if not path.exists():
        return 0

    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_symlink():
                continue
            if item.is_file():
                total += item.stat().st_size
        except (FileNotFoundError, OSError):
            continue
    return total


def format_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TB"


def _is_archive_candidate(topic_name: str, reviews_root: pathlib.Path) -> bool:
    review_path = reviews_root / topic_name / "review.md"
    if not review_path.exists():
        return False
    text = review_path.read_text(encoding="utf-8")
    return "## References" in text or "<!-- refs:end -->" in text


def report(threshold_gb: float = 5.0) -> int:
    root = project.project_root()
    tmp_root = root / "tmp"
    reviews_root = root / "reviews"
    archive_root = tmp_root / "_archive"
    total_size = 0
    archive_candidates: list[tuple[str, int]] = []

    print("=== tmp usage ===")
    if tmp_root.exists():
        for item in sorted(tmp_root.iterdir(), key=lambda path: path.name):
            if item.name.startswith("_"):
                continue
            if not item.is_dir():
                continue
            size = dir_size(item)
            total_size += size
            candidate = _is_archive_candidate(item.name, reviews_root)
            label = "candidate" if candidate else "active"
            print(f"  {item.name:30s} {format_bytes(size):>10s}  {label}")
            if candidate:
                archive_candidates.append((item.name, size))

    archive_size = dir_size(archive_root)
    total_size += archive_size
    print(f"  {'_archive':30s} {format_bytes(archive_size):>10s}  archive")

    cache_size = dir_size(apis.CACHE_DIR)
    total_size += cache_size
    print("\n=== shared cache ===")
    print(f"  {apis.CACHE_DIR} {format_bytes(cache_size)}")
    print(f"\nTotal: {format_bytes(total_size)}")

    threshold_bytes = threshold_gb * 1024**3
    if total_size > threshold_bytes and archive_candidates:
        print(f"\nOver {threshold_gb:.1f} GB. Archive candidates:")
        for topic_name, size in sorted(archive_candidates, key=lambda item: item[1], reverse=True)[:5]:
            print(f"  tmp/{topic_name} ({format_bytes(size)})")

    return total_size


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--threshold-gb", type=float, default=5.0)
    args = parser.parse_args()
    report(args.threshold_gb)


if __name__ == "__main__":
    main()
