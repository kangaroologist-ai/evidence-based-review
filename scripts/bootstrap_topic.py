"""Bootstrap a new review topic.

Creates ``reviews/<topic>/`` with:
- ``review.md``     skeleton
- ``research_log.md``  template with protocol + scope + PRISMA flow stubs
- ``references_store.json``  empty store with domain tag

The optional ``--domain`` flag binds the topic to a patches/<domain>.md
frontmatter (every domain including health now has a patch file; a
KNOWN_DOMAINS entry without one falls back to DEFAULT_PATCH). The domain
choice is persisted to the store so lint_review / term_check / search
can load the patch overrides without re-asking the user.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import patches, project, testflight
import refs

REVIEW_TEMPLATE = """# {topic}

## 摘要

_3 段 prose：① 总裁决；② 按目标分流；③ 实操底线。决策性结论，prose-style 见 playbook §9。_

<!-- 命题式章节按需展开：## §1 … / ## §2 … / 每节首句裁决句 -->

## §限定与争议

_把 subagent analyst 摘要识别的矛盾搬到这里。每条写"这条阻止什么结论"，不是礼貌收尾。_

## §方法

- **领域 (domain)**: 见 `research_log.md` 顶部 protocol 段
- **检索源**: 默认 CrossRef + Semantic Scholar 双源（见 `tools/search.py`）
- **雪球法**: OpenAlex genealogy（forward + backward citation chasing）
- **报告规范**: 项目精简 PRISMA-S（见 `docs/methodology_playbook.md` §7）
- **工具**: bootstrap_topic / verify / search / genealogy / fetch / notes / triage (via notes subagent) / reviewer / lint_review / render_refs

<!-- prisma-flow:start -->
<!-- prisma-flow:end -->

<!-- refs:start -->
<!-- refs:end -->
"""

LOG_TEMPLATE = """# {topic} — Research Log

## 范围与领域（protocol）

- **领域 (domain)**: `{domain}`{patch_hint}
- **目标读者 / 应用场景**: _user 填_——把这条综述会被谁拿去做什么决定写一句
- **核心问题（一句话）**: _user 填_——这次综述要回答的根问题
- **纳入标准 (inclusion criteria)**: _user 填_——研究类型 / 人群 / 时段 / 语言 / 出版形式
- **排除标准 (exclusion criteria)**: _user 填_——明确不收什么（避免事后调整）
- **核心 outcomes / 度量**: _user 填_——本综述据此判定有效或差异的指标
- **检索源声明**: 默认 `{default_sources}`；如手工补 CNKI / arXiv / 灰文献，此处声明
- **一级证据扩展**: 见 `patches/{domain}.md` frontmatter（{patch_note}）
- **综述类型定位**: narrative + decision + mechanism 复合型，按需含 methodology 切面（项目默认）

## Round 1

### 识别的空白（每条是一句完整问题）

每条 gap 由 `verify.py --declare-gap` 声明并填入下方。最少要包含 `--gap-type`
与对应子字段（PICO / Mechanism / SPIDER / 等）；缺字段 lint 会 warn。

- gap-1: _待 declare_
- gap-2: _待 declare_

### 每条 gap 的定向扩展

- gap-1 → seed: ; genealogy: ; added:
- gap-2 → seed: ; genealogy: ; added:

### Round 1 各 gap subagent 摘要 + 四类清单

由主线程 spawn analyst subagent（每 gap 一个 Opus）后粘贴。Schema：
强证据 5–8 条 / study_type 分布 / 矛盾 / 是否新 gap candidate /
**四类清单**（cite_recommend / exclude_recommend / keep_uncited / uncertain）/
**重叠或失焦判断**（与已有 gap 重叠？description 是否太宽？）。

### 轮后取舍（Round 1）

由主线程依据 subagent 四类清单做 exclude 决定后填。

## 终止条件工具输出

运行 `python scripts/term_check.py reviews/{topic}` 并把输出粘贴到此处。

## PRISMA flow（4 阶段数字漏斗）

由 `tools/render_refs.py` 在最终 render 时自动 append。初始为空——一次综述
跑完成后才有完整数字（identified / screened / eligible / included）。

## Exclusion audit log（工具自动写入）

`exclude.py` 调用时在此处追加。
"""


def _write_if_needed(path: pathlib.Path, content: str, force: bool) -> None:
    if path.exists() and not force:
        return
    path.write_text(content, encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("topic")
    parser.add_argument(
        "--domain",
        choices=patches.KNOWN_DOMAINS,
        default="health",
        help=(
            "Topic domain — selects patches/<domain>.md overrides for "
            "term_check / lint / search. Default 'health' = CLAUDE.md baseline."
        ),
    )
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    topic_root = project.topic_dir(args.topic)
    tmp_root = project.topic_tmp(args.topic)
    review_path = project.review_path(topic_root)
    log_path = topic_root / "research_log.md"

    if topic_root.exists() and not args.force:
        print(f"[ERROR] {topic_root} already exists; use --force")
        raise SystemExit(2)

    patch = patches.load_patch(args.domain)
    default_sources = ", ".join(patch.get("default_search_sources", []) or ["crossref"])
    patch_path = patches.patch_path(args.domain)
    if patch_path is None:
        patch_hint = "（无 patch 文件，fallback 到代码内 DEFAULT_PATCH）"
        patch_note = "domain 无单独 patch 文件；用 lib/patches.py DEFAULT_PATCH 默认"
    else:
        try:
            _rel = patch_path.relative_to(project.project_root())
        except ValueError:
            _rel = patch_path
        patch_hint = f"（loaded from `{_rel}`）"
        patch_note = "primary / secondary / not_applicable 在 frontmatter 中"

    log_content = LOG_TEMPLATE.format(
        topic=args.topic,
        domain=args.domain,
        patch_hint=patch_hint,
        default_sources=default_sources,
        patch_note=patch_note,
    )

    with testflight.timer("bootstrap_topic", "main", topic_dir=topic_root, topic=args.topic):
        topic_root.mkdir(parents=True, exist_ok=True)
        (topic_root / "figures").mkdir(exist_ok=True)

        for relative_path in (
            "abstracts",
            "assets/figures",
            "assets/tables",
            "assets/pdfs",
            "assets/pdfs_text",
        ):
            (tmp_root / relative_path).mkdir(parents=True, exist_ok=True)

        _write_if_needed(review_path, REVIEW_TEMPLATE.format(topic=args.topic), args.force)
        _write_if_needed(log_path, log_content, args.force)

        existing = refs.load(topic_root)
        if existing is None:
            refs.save(topic_root, refs.new_store(args.topic, domain=args.domain))
        elif existing.get("domain") != args.domain:
            # User passed --force on a re-bootstrap with a different domain.
            existing["domain"] = args.domain
            refs.save(topic_root, existing)

        print(f"bootstrapped: {topic_root}")
        print(f"domain: {args.domain}")
        print(f"cache: {tmp_root}")
        print(
            "next: "
            f"python scripts/verify.py {topic_root} --declare-gap gap-1 \"<desc>\" "
            "--gap-type decision --population <P> --intervention <I> "
            "--comparator <C> --outcome <O>"
        )


if __name__ == "__main__":
    main()
