"""Single source of truth for the per-gap analyst subagent schema.

Each round the main thread spawns one Opus analyst per gap; this module is
the contract for what that subagent returns. The schema used to be copy-pasted
across CLAUDE.md, WORKFLOW.md, methodology_playbook.md, notes.py,
bootstrap_topic.py and reviewer.py, and had already drifted (WORKFLOW.md
dropped the 6th item and the ≤500-word cap).

Split of authority:
- CLAUDE.md §Literature workflow Phase 2 keeps the authoritative *prose* copy
  (the main thread is guaranteed to read CLAUDE.md).
- This module is the authoritative *code* copy that notes.py emits into each
  round's gap index. WORKFLOW.md / playbook / bootstrap point here instead of
  re-stating the schema, so there is exactly one prose copy and one code copy.
"""
from __future__ import annotations

# The 7-section return schema, as markdown lines (no trailing newline).
SCHEMA_LINES: list[str] = [
    "1) 本 gap 最强证据 5–8 条：`[@citation_key]` 关键数字 + 一句论点；",
    "2) study_type 分布（meta / RCT / cohort / review / other）；",
    "3) 矛盾呈现：「X 与 Y 在 ... 上不一致，原因可能是 ...」；",
    "4) 新 gap candidate（→ 下一 round 是否需要新 gap？Yes 给描述，No 写 No）；",
    "5) **四类清单**（按 entry 给一行理由）：",
    "   - `cite_recommend`：建议正文引用的核心 entry",
    "   - `exclude_recommend`：建议 exclude（噪声 / 跨域 / 重复）",
    "   - `keep_uncited`：保留 store 不引用（应用案例 / 弱证据）",
    "   - `uncertain`：需要主线程或 user 二次裁决",
    "6) **重叠 / 失焦判断**：本 gap 与 store 已有哪些 gap 实质重叠？描述是否太宽（>200 字）？",
    "7) **逐篇保留文献判读**（写到 `notes/round-N/gap-X.annotated.md`）：对本",
    "   gap **每一条非 exclude_recommend 的保留条目**各给一行，建表：",
    "   `[@key] | study_type | 关键数字一句 | 判定(cite/keep/uncertain) + 一句理由`。",
    "   粒度只覆盖「保留集」——已建议 exclude 的不必逐写（在第 5 项批量给理由即可），",
    "   控成本。这一节让「这篇为什么留在库里」逐篇可审计、便于 reviewer 抽查；",
    "   ≤500 字硬上限只约束 1)–6) 的正文回复，第 7 项的逐篇表落到 annotated.md，不计入。",
]


def index_block(round_number: int | None = None) -> list[str]:
    """The 'Round 收口用法' block notes.py writes into each round's index.

    Returned as a list of markdown lines (callers wrap with surrounding
    blank lines as needed). Keep this in lock-step with CLAUDE.md §Phase 2.

    ``round_number`` is optional and purely cosmetic: when supplied, a concrete
    "落盘" reminder line is appended that interpolates the real round number into
    the ``notes/round-N/<gap-id>.annotated.md`` path (section 7 of the schema).
    Omitting it (the default) reproduces the historical output verbatim, so
    existing/external callers are unaffected.
    """
    block = [
        "## Round 收口用法（playbook §3 phase 2–4）",
        "",
        "对每个 gap 文件并行 spawn 一个 Opus general-purpose Agent。**model: opus**",
        "显式指定。每个 subagent 返回 ≤500 字结构化分析，schema:",
        "",
        *SCHEMA_LINES,
        "",
        "主线程收到所有 subagent 报告后：① 按 exclude_recommend 跑 `exclude.py`；",
        "② 把 cite_recommend 累积进下一 phase 的 outline；③ 把矛盾 + 限定写进 research_log",
        "对应 round 段；④ 若有新 gap candidate 则 `verify.py --declare-gap` 开新 gap。",
        "",
        "详细 prompt 模板见 `docs/methodology_playbook.md` §3 phase 2 与 CLAUDE.md",
        "§Literature workflow 对应章节。",
    ]
    if round_number is not None:
        block.extend(
            [
                "",
                f"> 第 7 项逐篇判读落盘到 `notes/round-{round_number}/<gap-id>.annotated.md`"
                f"（如 `gap-1.annotated.md`），与上方 gap 文件同目录；只覆盖该 gap 的保留集。",
            ]
        )
    return block
