from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import project, testflight
import refs


def _score_entry(keywords: list[str], entry: refs.Entry) -> float:
    title = entry.get("title", "").lower()
    journal = entry.get("journal", "").lower()
    title_score = sum(title.count(keyword) for keyword in keywords)
    journal_score = sum(journal.count(keyword) for keyword in keywords) * 0.5
    return float(title_score) + float(journal_score)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("query", nargs="+")
    parser.add_argument("--top", type=int, default=30)
    parser.add_argument("--min-score", type=float, default=1.0)
    parser.add_argument("--reviews-dir")
    args = parser.parse_args()

    reviews_root = pathlib.Path(args.reviews_dir) if args.reviews_dir else project.project_root() / "reviews"
    keywords = [keyword.lower() for keyword in args.query]

    with testflight.timer("search_existing", "main", topics_root=str(reviews_root)) as detail:
        hits: list[tuple[float, str, str, str, str]] = []
        topic_count = 0
        for topic_dir in sorted(path for path in reviews_root.iterdir() if path.is_dir()):
            store = refs.load(topic_dir)
            if store is None:
                continue
            topic_count += 1
            topic = topic_dir.name
            for doi, entry in store["entries"].items():
                if entry.get("retracted", False):
                    continue
                score = _score_entry(keywords, entry)
                if score < args.min_score:
                    continue
                hits.append(
                    (
                        score,
                        topic,
                        doi,
                        entry.get("title", ""),
                        entry.get("citation_key", ""),
                    )
                )

        hits.sort(key=lambda item: (-item[0], item[1], item[2]))
        for score, topic, doi, title, citation_key in hits[: args.top]:
            print(f"{score:5.1f} | {topic:20s} | {doi:35s} | {title[:80]} ({citation_key})")
        print(f"\n{len(hits)} matches across {topic_count} topics")
        detail.update({"topics": topic_count, "hits": len(hits)})


if __name__ == "__main__":
    main()
