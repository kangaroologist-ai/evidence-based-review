"""Phase 7 reviewer harness — 3-reviewer 3/3 approve gate with ≤3 revision rounds.

Workflow (see docs/methodology_playbook.md §3 phase 7):

  1. After Phase 6 writes review.md, run:
        python tools/reviewer.py prompt reviews/<topic> --round 1
     Emits a shared prompt template to `reviewers/prompt_round_1.md`.

  2. Main thread spawns 3 INDEPENDENT Opus subagents (model: opus, same
     prompt, no cross-talk). Each subagent writes its verdict + findings
     to `reviewers/round_1_{1,2,3}.md`.

  3. Run:
        python tools/reviewer.py tally reviews/<topic> --round 1
     Parses the 3 reviewer files and prints PASS (3/3 approve), REVISE
     (any request_changes — main thread fixes + bumps round), CONFIRM
     (at the round cap but every remaining FAIL is [mechanical] — run one
     0-new-problem confirmation round instead of failing), or FAIL
     (substantive disagreement at the round cap — write failure_report.md).

  4. Severity-aware failure rule (docs §D, problem 10): reviewers tag each
     FAIL `[mechanical]` (lint-fixable: missing gloss, unresolved [@key],
     missing §) or `[substantive]` (wrong evidence, omitted contradiction,
     prose distortion). Only SUBSTANTIVE FAILs at the round cap are a real
     failure; a mechanical-only round at the cap yields CONFIRM, and the main
     thread may run exactly one confirmation round at MAX+1 via `prompt
     --allow-confirmation-round`. When reviewers DON'T tag (old-style output),
     tally falls back to the original "round == max and not 3/3 → FAIL" rule,
     so legacy behavior and existing tests are unchanged.

  5. Hard limit: after revision round 3 fails to reach 3/3 approve, the
     `prompt` subcommand refuses to start round 4 (unless explicitly authorized
     as the single confirmation round). Main thread runs `reviewer.py
     failure-report` and stops — the user decides whether to re-spawn or
     rewrite a section.

This tool does NOT spawn subagents itself — it has no Claude API access.
It produces prompts and tallies votes; spawning is the main thread's job.
"""
from __future__ import annotations

import argparse
import collections
import pathlib
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, str(pathlib.Path(__file__).parent))

from lib import layout, testflight

MAX_REVISION_ROUNDS = 3
REVIEWER_COUNT = 3
# Tolerant of common LLM formatting on the verdict line: leading >/#/* markup,
# full-width colon, **bold** around the value, trailing punctuation. Still
# line-anchored (re.M) so a "verdict: approve" mention inside prose discussion
# isn't mistaken for the reviewer's actual verdict.
VERDICT_RE = re.compile(
    r"^\s*[>#*\s]*verdict[:：]\s*\**\s*(approve|request_changes)\b",
    re.M | re.I,
)
# A checklist [FAIL] row flips approve → request_changes. Tolerate case and
# inner spacing ([fail], [ FAIL ]) plus the ❌ cross-mark some reviewers use,
# so a self-contradicting "approve" isn't passed on a formatting technicality.
_FAIL_RE = re.compile(r"\[\s*fail\s*\]|❌", re.I)

# A reviewer FAIL row may carry a severity tag so the harness can tell a
# lint-mechanizable miss (missing Chinese gloss, unresolved [@key], a missing
# §section) from a substantive defect (wrong evidence, an unaddressed
# contradiction, prose that distorts the source). Tolerate spacing/case:
# [mechanical] / [ mechanical ] / 【机械】 style and [substantive] / 【实质】.
# Both English and a couple of common Chinese synonyms are accepted because
# reviewers write in Chinese.
_MECHANICAL_TAG_RE = re.compile(r"\[\s*mechanical\s*\]|【\s*机械\s*】", re.I)
_SUBSTANTIVE_TAG_RE = re.compile(r"\[\s*substantive\s*\]|【\s*实质\s*】", re.I)


def _classify_fail_lines(text: str) -> dict[str, int]:
    """Count FAIL rows in one reviewer file by severity tag.

    Returns ``{"mechanical": m, "substantive": s, "untagged": u}`` where each
    bucket counts checklist rows that contain a FAIL marker (``[FAIL]`` or
    ``❌``):

    - ``mechanical``: row also carries a ``[mechanical]`` tag,
    - ``substantive``: row also carries a ``[substantive]`` tag,
    - ``untagged``: row carries neither (old-style reviewer output, or a
      reviewer that forgot to tag).

    A row tagged BOTH ways is counted as ``substantive`` (the stricter
    bucket) — a defect flagged substantive should never be waved through as
    mechanical. The presence of any ``untagged`` row is the signal callers
    use to fall back to the pre-tagging failure logic, so old reviewer files
    (and the existing test fixtures) keep their original behavior.
    """
    counts = {"mechanical": 0, "substantive": 0, "untagged": 0}
    for line in text.splitlines():
        if not _FAIL_RE.search(line):
            continue
        if _SUBSTANTIVE_TAG_RE.search(line):
            counts["substantive"] += 1
        elif _MECHANICAL_TAG_RE.search(line):
            counts["mechanical"] += 1
        else:
            counts["untagged"] += 1
    return counts


def _round_fail_severity(
    topic_dir: pathlib.Path, round_num: int
) -> dict[str, int]:
    """Aggregate FAIL-row severities across all 3 reviewer files of a round."""
    totals = {"mechanical": 0, "substantive": 0, "untagged": 0}
    for reviewer_num in range(1, REVIEWER_COUNT + 1):
        path = _reviewer_file(topic_dir, round_num, reviewer_num)
        if not path.exists():
            continue
        per_file = _classify_fail_lines(path.read_text(encoding="utf-8"))
        for key, val in per_file.items():
            totals[key] += val
    return totals


REVIEWER_PROMPT_TEMPLATE = """# Reviewer subagent prompt — {topic} · Round {round}

你是 3 个独立的 Phase 7 reviewer 之一。同一 prompt 同时发给另外 2 个 reviewer，
你们互不通气。每个 reviewer 独立打分。最终 **3/3 approve** 才发布；任何
request_changes 都触发修订重审（最多 {max_rounds} 轮）。

## 输入

- 综述正文: `reviews/{topic}/review.md`
- 已 verified entry list: `reviews/{topic}/references_store.json` + 单条 entry 在 `reviews/{topic}/references/*.json`
- 已抓取的 abstract: 每条 entry 的 `paths.abstract` 字段指向 `tmp/{topic}/abstracts/<safe_doi>.md`
- **必要时**调 `python tools/fetch.py reviews/{topic} --include fulltext_xml --doi <DOI>` 拉关键引用的全文，核对 prose 是否扭曲原文
- **忠实度制品（spec §0.6 / W3）**：`reviews/{topic}/meta/faithfulness_report.md`（逐断言 verdict）+ `reviews/{topic}/meta/claim_evidence_map.md`（claim→span 溯源）。**全量忠实度由 faithfulness 工具保障，你是二次哨兵**——抽样高风险断言复核 + 审 report 内部一致性，**不逐条全核**。

## 检查 8 项（playbook §10 self-review checklist）

> **分工（哨兵，W3）**：reviewer **1** 额外审 `faithfulness_report` 内部一致性（verdict 与 claim_evidence_map / abstract 是否自洽）+ 抽样高风险断言；reviewer **2/3** 走标准 8 项 + 各自抽样。第 7 项是**抽样**核（非全量；全量在 faithfulness 工具，reviewer 不背「零」）。

对 review.md 全文逐项判断，每项给一行 `[PASS]` / `[FAIL]` + 简短说明：

1. **章节标题是命题不是学科分类**——"§3 系统检索 = 多库 + snowballing + 图书馆员" PASS；"§3 文献检索" FAIL
2. **每个数字带'这意味着什么'解释**——"recall ≈98%" 紧跟 "单库 MEDLINE 不足 80%" PASS；裸数据 / 没解释 FAIL
3. **每观点 ≤5 cite（典型 1–3；只有真正不同类型/方向的强证据才到 5），且每个 [@key] 解析到 verified entry**——多于 5 条 cite 单观点 → 列出位置
4. **矛盾呈现**——subagent 摘要识别的矛盾必须进 §限定与争议 或正文 prose 明示
5. **限定节真写"这条阻止什么结论"**——不是"本综述局限在于..."礼貌收尾
6. **cited verified ratio ≥ 50%**——若不达标主线程必须补 cite 或 prune entries
7. **关键引用的 prose 描述不扭曲原文**——抽 **3-5 条核心 cite**（你自己选）调全文核对
8. **章节必备**：§实操建议表（decision-heavy review）+ §限定与争议 + §方法（含 PRISMA-flow + 检索表）+ §References

## 输出格式

写到指定文件 `reviews/{topic}/reviewers/round_{round}_<你的编号>.md`，**你的编号**由主线程在 spawn 时分配（1 / 2 / 3）。

**第一行必须是**（裸文本一行，**不要**加粗 `**`、不要加 `#` 标题、不要加引用 `>`、行尾不要句号）：
```
Verdict: approve
```
或
```
Verdict: request_changes
```

接下来按 8 项逐项打勾或描述问题，每项一行（标记用恰好 `[PASS]` / `[FAIL]`，大写、带方括号）。

**两条硬要求（违反视为审查不合格）：**

1. **任一项 FAIL 必须列出该类的全部实例，而非第一个。** 第 11 条 / 缩写类
   FAIL → 列全文每个首现无中文的缩写 + 行号；引用类 FAIL → 扫所有 `[@key]`
   列出每一处不贴合 / 无法解析的；缺节类 FAIL → 列出全部缺失的 §。一次性穷举，
   不要"先报一个，等下一轮再报下一个"——那会把一篇综述的问题摊成多轮空跑。
2. **每个 FAIL 必须紧跟一个严重度标签** `[mechanical]` 或 `[substantive]`：
   - `[mechanical]`：lint 可机械判定的——缺中文译名、`[@key]` 能否解析、
     §章节是否齐全、PRISMA flow / References 节是否存在、cite 数是否 ≤5。
   - `[substantive]`：需要人类判断的——证据用错 / 数字与原文不符、矛盾漏呈现、
     prose 扭曲原文、限定节没说清"这条阻止什么结论"。
   标签写在 `[FAIL]` 之后、说明之前。机械类问题会在确认轮快速清零、不计入
   实质性修订预算；实质类问题才消耗修订轮次。

```
1. [PASS] 章节标题均为命题（§1–§8）
2. [FAIL] [mechanical] §3 第 2 段 "recall 87.1%" 无解释；§5 第 3 段 GRADE 等级数字裸出（共 2 处，已全列）
3. [PASS] 每观点 ≤5 cite
4. [FAIL] [substantive] §10 §限定 未呈现 howard2022 vs kyriakoulis2016 的矛盾（subagent 摘要里有）
5. [FAIL] [substantive] §11 §限定 仍是"本综述局限在于..."礼貌收尾
6. [PASS] cited 118/198 = 59.6% ≥ 50%
7. [PASS] 抽 sterne2019 / page20213qco / sukhera2022 三条核对原文均一致
8. [FAIL] [mechanical] §方法 缺 PRISMA flow 数字漏斗；缺 §实操建议表（共 2 处）
```

**只要任意一项 FAIL，Verdict 必须为 `request_changes`**。不允许"通过但有小问题"——
要么 approve（全 PASS）要么 request_changes。

## 限制

- **你是只读审稿人，工具调用要省。** 8 项检查靠**读** `review.md` + `references/*.json`
  + 已抓好的 abstract（`paths.abstract`）完成——这些足够判定引用能否解析、cite 数、
  数字与原文是否一致、PRISMA flow / 各 § 是否存在。不要为"确认产物存在"去重新生成它。
- **绝不运行任何会改动文件或 references store 的工具**——尤其**不要跑
  `render_refs.py` / `lint_review.py` / `term_check.py` / `verify.py` / `exclude.py` /
  `notes.py`**。这些是主线程的活；你跑了既是无用功、又可能改动交付产物或留下脏 diff。
  审稿不靠重新渲染/重新 lint，靠读。
- 唯一允许的"写侧"动作：**仅当**某条核心引用的 prose 忠实度确有疑义、且 abstract 不足以判断时，
  才 `fetch.py --include fulltext_xml --doi <DOI>` 拉那**一两条**的全文核对；不要批量拉、不要预拉。
- 抽查核对原文：选 3–5 条**最吃重**的引用用 Read 看 abstract 即可，不要逐条全量过一遍。
- 不要写"延伸阅读"或"未来研究"——那是综述本身的工作
- 不读 research_log.md / self_review.md（author 视角，不该影响 reviewer 判断）
- 不修改 review.md（让主线程改）
- ≤ 1500 字硬上限
"""


FAILURE_REPORT_TEMPLATE = """# Reviewer 修订循环失败报告 — {topic}

_生成时间: {timestamp}_

## 状态

{max_rounds} 轮修订均未达到 3/3 reviewer approve 标准（playbook §3 phase 7）。
主线程已 stop，等用户介入决定。

## 每轮投票统计

{round_summary}

## 各 reviewer 在最后一轮的 FAIL 项目（去重前原文）

{last_round_issues}

## 建议处理路径

先按严重度标签分流上面的 FAIL 项（`[mechanical]` vs `[substantive]`）：

- **`[mechanical]` 项**（缺中文 / `[@key]` 不解析 / 缺 § / 缺 PRISMA flow）：本不该走到 failure。
  先 `python tools/lint_review.py reviews/{topic}` 把整类机械问题清零，再用
  `python tools/reviewer.py prompt reviews/{topic} --round 4 --allow-confirmation-round`
  跑一轮 0-新问题确认轮即可放行——机械类不计入实质修订预算。若 tally 仍把它们当
  failure，多半是 reviewer 漏标 `[substantive]/[mechanical]`（→ 回退到旧判定逻辑）。
- **`[substantive]` 项且某 reviewer 反复揪同一项**：可能要求过严或 reviewer 误判。看 prose 是否真的违反 playbook 标准；如系误判，主线程在下一轮 prompt 加 1 句 clarification 再启动新一轮。
- **`[substantive]` 项且各 reviewer 揪不同项目**：综述真有质量问题，需大改。考虑 (a) prose-style 整篇过一遍；(b) 补充某些 § 的证据；(c) 重新 spawn analyst subagent 看是否有 cite_recommend 被漏掉。

修订完毕用户可以让主线程重新跑 reviewer（reset round counter — 删除 `reviewers/revision_log.md` 与 `reviewers/round_*.md` 后重新从 round 1 开始）。
"""


def _reviewer_file(topic_dir: pathlib.Path, round_num: int, reviewer_num: int) -> pathlib.Path:
    return layout.reviewer_round_path(topic_dir, round_num, reviewer_num)


def _read_reviewer_verdict(
    path: pathlib.Path,
) -> tuple[str | None, str | None, bool]:
    """Return ``(raw_verdict, effective_verdict, contradicted)``.

    Single source of truth for parsing a reviewer markdown file. Both
    ``cmd_tally`` and ``cmd_failure_report`` go through this so the
    ``approve + [FAIL]`` downgrade rule is applied consistently across
    both subcommands (Codex P2 round 5).

    - ``raw_verdict``: ``approve`` / ``request_changes`` / ``None``
      (file missing, or no ``Verdict:`` line).
    - ``effective_verdict``: same as ``raw``, except ``approve`` files
      whose body contains ``[FAIL]`` are downgraded to ``request_changes``
      per the reviewer prompt rule "any [FAIL] → Verdict must be
      request_changes".
    - ``contradicted``: ``True`` iff the raw verdict said ``approve``
      but the body listed at least one ``[FAIL]`` row.
    """
    if not path.exists():
        return None, None, False
    text = path.read_text(encoding="utf-8")
    match = VERDICT_RE.search(text)
    if not match:
        return None, None, False
    raw = match.group(1).lower()
    contradicted = raw == "approve" and bool(_FAIL_RE.search(text))
    effective = "request_changes" if contradicted else raw
    return raw, effective, contradicted


def _prompt_file(topic_dir: pathlib.Path, round_num: int) -> pathlib.Path:
    return layout.reviewer_prompt_path(topic_dir, round_num)


def _failure_report_file(topic_dir: pathlib.Path) -> pathlib.Path:
    return layout.failure_report_path(topic_dir)


def _revision_log_file(topic_dir: pathlib.Path) -> pathlib.Path:
    return layout.revision_log_path(topic_dir)


def cmd_prompt(
    topic_dir: pathlib.Path,
    round_num: int,
    allow_confirmation_round: bool = False,
) -> int:
    # Round MAX+1 is permitted ONLY as an explicit, opt-in confirmation round
    # (docs §D problem 10): when `tally` reports [CONFIRM] because the round at
    # the cap had mechanical-only FAILs, the main thread fixes them and runs one
    # 0-new-problem confirmation round. Without the flag the hard cap stands, so
    # the default path still refuses round 4.
    confirmation_round = MAX_REVISION_ROUNDS + 1
    if allow_confirmation_round and round_num == confirmation_round:
        pass  # explicitly authorized single confirmation round
    elif round_num > MAX_REVISION_ROUNDS:
        extra = (
            ""
            if round_num != confirmation_round
            else " (a single confirmation round is allowed only with "
            "--allow-confirmation-round, and only when `tally` reported [CONFIRM])"
        )
        print(
            f"[ERROR] revision round {round_num} > max {MAX_REVISION_ROUNDS}; "
            f"run `python tools/reviewer.py failure-report {topic_dir}` and stop"
            f"{extra}"
        )
        return 1
    if not topic_dir.exists():
        print(f"[ERROR] topic dir does not exist: {topic_dir}")
        return 1
    review_path = topic_dir / "review.md"
    if not review_path.exists():
        print(f"[ERROR] review.md not found under {topic_dir}; write Phase 6 first")
        return 1

    prompt_path = _prompt_file(topic_dir, round_num)
    content = REVIEWER_PROMPT_TEMPLATE.format(
        topic=topic_dir.name,
        round=round_num,
        max_rounds=MAX_REVISION_ROUNDS,
    )
    layout.ensure_subdirs(topic_dir)
    prompt_path.write_text(content, encoding="utf-8")
    print(f"[OK] wrote {prompt_path}")
    print(
        f"next: main thread spawns {REVIEWER_COUNT} Opus subagents (model: opus) "
        f"in parallel, each with this prompt + a distinct reviewer number; each "
        f"writes its Verdict + 8-item checklist to "
        f"`reviewers/round_{round_num}_{{1..{REVIEWER_COUNT}}}.md`; then run "
        f"`python tools/reviewer.py tally {topic_dir} --round {round_num}`"
    )
    return 0


def cmd_tally(topic_dir: pathlib.Path, round_num: int) -> int:
    verdicts: list[tuple[int, str | None, pathlib.Path]] = []
    contradictions: list[int] = []
    for reviewer_num in range(1, REVIEWER_COUNT + 1):
        path = _reviewer_file(topic_dir, round_num, reviewer_num)
        # Shared parser applies the approve + [FAIL] downgrade so tally
        # and failure-report stay in lock-step (Codex round 5 P2 fix).
        _raw, effective, contradicted = _read_reviewer_verdict(path)
        if contradicted:
            contradictions.append(reviewer_num)
        verdicts.append((reviewer_num, effective, path))

    missing = [i for i, v, _ in verdicts if v is None]
    if missing:
        print(
            f"[ERROR] reviewer files missing or no Verdict line: reviewer "
            f"{missing} for round {round_num}"
        )
        for i, _, path in verdicts:
            if i in missing:
                print(f"  expected: {path}")
        return 1

    counts = collections.Counter(v for _, v, _ in verdicts)
    approves = counts.get("approve", 0)
    request_changes = counts.get("request_changes", 0)

    print(
        f"Round {round_num}: {approves}/{REVIEWER_COUNT} approve / "
        f"{request_changes} request_changes"
    )
    for i, v, path in verdicts:
        downgrade_tag = " (downgraded: approve+[FAIL])" if i in contradictions else ""
        print(f"  reviewer {i}: {v}{downgrade_tag} ({path.name})")
    if contradictions:
        print(
            f"[WARN] {len(contradictions)} reviewer file(s) wrote "
            f"'Verdict: approve' but listed [FAIL] items: {contradictions}. "
            "Downgraded to request_changes per prompt rule."
        )

    # Classify this round's FAIL rows by severity so the failure decision can
    # tell a mechanical-only round (lint-fixable, should not burn the revision
    # budget) from a substantive disagreement. When reviewers don't tag their
    # FAILs (old-style output / the existing test fixtures), `untagged > 0`
    # and we fall back to the pre-tagging failure logic below.
    severity = _round_fail_severity(topic_dir, round_num)

    # Append to revision log for traceability (severity breakdown included so
    # the rounds-with-substantive-fail count is auditable after the fact).
    log_path = _revision_log_file(topic_dir)
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as log_handle:
        log_handle.write(
            f"\n## Round {round_num} ({timestamp})\n\n"
            f"- {approves}/{REVIEWER_COUNT} approve, "
            f"{request_changes} request_changes\n"
            f"- FAIL severity: {severity['substantive']} substantive, "
            f"{severity['mechanical']} mechanical, "
            f"{severity['untagged']} untagged\n"
        )

    if approves == REVIEWER_COUNT:
        print(f"[PASS] {approves}/{REVIEWER_COUNT} approve — review.md ready to publish")
        return 0

    if round_num >= MAX_REVISION_ROUNDS:
        # New (opt-in) failure rule: only rounds carrying a SUBSTANTIVE FAIL
        # count toward the revision budget. A round whose every FAIL is tagged
        # `[mechanical]` is lint-fixable; per docs §D (problem 10) the main
        # thread fixes those and runs one 0-new-problem confirmation round
        # rather than declaring failure. This branch activates only when the
        # round's FAILs are *cleanly tagged* (no untagged rows) AND none are
        # substantive — otherwise we keep the original behavior so old reviewer
        # output (and the existing test fixtures) are unaffected.
        mechanical_only = (
            severity["untagged"] == 0
            and severity["substantive"] == 0
            and severity["mechanical"] > 0
        )
        if mechanical_only:
            confirm_round = round_num + 1
            print(
                f"[CONFIRM] round {round_num} hit max {MAX_REVISION_ROUNDS} but every "
                f"FAIL is [mechanical] ({severity['mechanical']} item(s)) — not a "
                "substantive deadlock. Fix the mechanical items (ideally clear the "
                "whole class via lint_review.py BEFORE spawning), then run ONE "
                "0-new-problem confirmation round: `python tools/reviewer.py prompt "
                f"{topic_dir} --round {confirm_round} --allow-confirmation-round` and "
                "spawn 3 fresh Opus reviewers. Mechanical rounds do NOT count toward "
                f"the {MAX_REVISION_ROUNDS}-substantive-round failure budget."
            )
            return 3
        # Substantive disagreement (or untagged → fall back to original rule):
        # the review has burned its revision budget at the round cap.
        sub_note = (
            f" ({severity['substantive']} substantive FAIL item(s) remain)"
            if severity["substantive"]
            else ""
        )
        print(
            f"[FAIL] round {round_num} == max {MAX_REVISION_ROUNDS} but not 3/3 "
            f"approve{sub_note}; run `python tools/reviewer.py failure-report "
            f"{topic_dir}` and stop for user input"
        )
        return 2

    print(
        f"[REVISE] start round {round_num + 1}: main thread fixes the FAIL items "
        f"listed in reviewers/round_{round_num}_*.md, then re-runs "
        f"`reviewer.py prompt --round {round_num + 1}` and spawns 3 fresh "
        "Opus subagents (DO NOT reuse round-N reviewers — new round, new spawn)"
    )
    return 3


def cmd_failure_report(topic_dir: pathlib.Path) -> int:
    timestamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
    round_summaries: list[str] = []
    last_round_issues: list[str] = []
    for round_num in range(1, MAX_REVISION_ROUNDS + 1):
        verdicts: list[tuple[int, str]] = []
        issues: list[str] = []
        for reviewer_num in range(1, REVIEWER_COUNT + 1):
            path = _reviewer_file(topic_dir, round_num, reviewer_num)
            # Shared parser applies the approve + [FAIL] downgrade so
            # failure-report's round summary matches what `tally` would
            # have shown (Codex round 5 P2: the report previously
            # showed raw "approve" while tally counted 0/3 — confusing
            # the post-mortem reader).
            _raw, effective, contradicted = _read_reviewer_verdict(path)
            if effective is None:
                # File missing or no Verdict line — still recorded for
                # transparency, but as "missing" to match old behavior.
                if path.exists():
                    verdicts.append((reviewer_num, "missing"))
                continue
            display = effective
            if contradicted:
                display = f"{effective} (downgraded from approve+[FAIL])"
            verdicts.append((reviewer_num, display))
            if round_num == MAX_REVISION_ROUNDS:
                text = path.read_text(encoding="utf-8")
                for line in text.splitlines():
                    if "[FAIL]" in line:
                        issues.append(f"  - reviewer {reviewer_num}: {line.strip()}")
        if verdicts:
            summary = ", ".join(f"R{i}: {v}" for i, v in verdicts)
            round_summaries.append(f"- Round {round_num}: {summary}")
        if round_num == MAX_REVISION_ROUNDS:
            last_round_issues.extend(issues)

    content = FAILURE_REPORT_TEMPLATE.format(
        topic=topic_dir.name,
        timestamp=timestamp,
        max_rounds=MAX_REVISION_ROUNDS,
        round_summary="\n".join(round_summaries) or "_(无数据)_",
        last_round_issues="\n".join(last_round_issues) or "_(无)_",
    )
    report_path = _failure_report_file(topic_dir)
    layout.ensure_subdirs(topic_dir)
    report_path.write_text(content, encoding="utf-8")
    print(f"[OK] wrote {report_path}")
    print("main thread should stop and surface this report to the user")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Reviewer harness — emits the 3-reviewer prompt template and tallies "
            "votes. Main thread spawns the 3 Opus subagents in parallel."
        )
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_prompt = sub.add_parser("prompt", help="Generate the reviewer prompt template.")
    p_prompt.add_argument("topic_dir")
    p_prompt.add_argument("--round", dest="round_num", type=int, required=True)
    p_prompt.add_argument(
        "--allow-confirmation-round",
        action="store_true",
        help=(
            "Permit exactly one round at MAX+1 as a 0-new-problem confirmation "
            "round after a [CONFIRM] (mechanical-only) tally. Off by default — "
            "the hard cap otherwise stands."
        ),
    )

    p_tally = sub.add_parser("tally", help="Tally 3 reviewer votes for a given round.")
    p_tally.add_argument("topic_dir")
    p_tally.add_argument("--round", dest="round_num", type=int, required=True)

    p_fail = sub.add_parser(
        "failure-report", help="Write failure_report.md after revision rounds exhausted."
    )
    p_fail.add_argument("topic_dir")

    args = parser.parse_args()
    topic_dir = pathlib.Path(args.topic_dir)

    with testflight.timer("reviewer", args.cmd, topic_dir=topic_dir):
        if args.cmd == "prompt":
            sys.exit(
                cmd_prompt(
                    topic_dir,
                    args.round_num,
                    allow_confirmation_round=args.allow_confirmation_round,
                )
            )
        elif args.cmd == "tally":
            sys.exit(cmd_tally(topic_dir, args.round_num))
        elif args.cmd == "failure-report":
            sys.exit(cmd_failure_report(topic_dir))
        else:  # pragma: no cover
            parser.error(f"unknown cmd: {args.cmd}")


if __name__ == "__main__":
    main()
