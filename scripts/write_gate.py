"""tools/write_gate.py — the 写作/收尾闸 (workflow_spec §1 N6/N11, §4; plan v3.1 G2).

HARD-BLOCK gate at two落点: end of write-loop (N6, before Phase 4) and finalize
(N11, after lint). It refuses to let a draft proceed / be delivered unless the
faithfulness verdicts are clean and lint passes. Unlike round_gate it reads the
written artifacts (faithfulness_report / claim_evidence_map / lint), which only
exist once writing has happened.

    python tools/write_gate.py reviews/<topic> [--json]
    python tools/write_gate.py reviews/<topic> --record-attempt --max-rewrites N
    # exit 0 = ok, 1 = blocked (retry), 2 = bad path, 3 = blocked at rewrite cap
    #   (failure_report.md written — stop and involve the user; spec N6 / G6).

Checks (lib.gatelib pass/fail/pending) — ALL hard-enforced today (R38 doc-sync; the old
'[pending …]' labels were stale, every check below now real-FAILs on real stores):
* lint           — lint_review exit ∈ {0,2}                          [enforced]
* faithfulness   — suspect=0, insufficient=0, needs_review cleared   [enforced, 断言级]
* claim-map      — every declarative clause has ≥1 claim_id          [enforced F1/F3]
* cross-gap      — no unreconciled reverse faithful assertions       [enforced F10]
* high-risk-grounding — high-risk claims grounded ≥ fulltext         [enforced F5;
                   title-only-near-quantitative also covered by lint G4]
* metadata       — erratum/EoC/realtime retraction recheck           [enforced M1-3;
                   retracted-in-body also covered by lint]
* duplicate-cluster, entailment-judged, evidence-uncertain, second-decomposer,
  faithfulness-audit, figure-data — also enforced (see gate()).
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import re
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))

import faithfulness
import refs
from lib import claimids, gatelib, layout

_METADATA_NOTE_RE = re.compile(
    r"预印本|preprint|关注声明|expression of concern|更正|勘误|erratum|corrigendum",
    re.IGNORECASE,
)


def _is_legacy(topic_dir: pathlib.Path) -> bool:
    """`.gates_legacy` grandfathers a pre-gate topic past the hard checks
    (mirrors `.lint_legacy`). The ONLY escape — without it the gate hard-blocks."""
    return (topic_dir / ".gates_legacy").exists()


def _used_keys(review_text: str) -> list[str]:
    return list(dict.fromkeys(faithfulness._CITATION_RE.findall(review_text)))

_HERE = pathlib.Path(__file__).parent
CLAIM_ID_RE = "<!-- claim:"  # F1 sidecar marker (not built yet)


def _run_lint(topic_dir: pathlib.Path) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, str(_HERE / "lint_review.py"), f"reviews/{topic_dir.name}"],
        cwd=str(_HERE.parent),
        capture_output=True,
        text=True,
        env={**os.environ, "HEALTH_REVIEW_DAEMON": "0"},
    )
    tail = (proc.stdout + proc.stderr).strip().splitlines()
    return proc.returncode, "\n".join(tail[-6:])


def _state_path(topic_dir: pathlib.Path) -> pathlib.Path:
    return topic_dir / layout.META_DIRNAME / "write_gate_state.json"


def _read_attempts(topic_dir: pathlib.Path) -> int:
    try:
        return int(json.loads(_state_path(topic_dir).read_text(encoding="utf-8")).get("attempts", 0))
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return 0


def _write_attempts(topic_dir: pathlib.Path, attempts: int) -> None:
    path = _state_path(topic_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"attempts": attempts}), encoding="utf-8")


# ── checks ────────────────────────────────────────────────────────────────────


def _check_lint(topic_dir: pathlib.Path) -> gatelib.CheckResult:
    code, tail = _run_lint(topic_dir)
    if code in (0, 2):
        return gatelib.passed("lint", f"lint_review exit {code}")
    return gatelib.failed("lint", f"lint_review exit {code}:\n{tail}")


def _check_faithfulness_verdicts(
    results: list["faithfulness.ClaimVerdict"], log_text: str
) -> gatelib.CheckResult:
    counts: dict[str, int] = {}
    for verdict in (cv.verdict for cv in results):
        counts[verdict] = counts.get(verdict, 0) + 1
    suspect = counts.get("suspect", 0)
    insufficient = counts.get("insufficient", 0)
    needs_review = counts.get("needs_review", 0)
    if suspect or insufficient:
        return gatelib.failed(
            "faithfulness",
            "faithfulness FAIL — suspect=%d insufficient=%d (spec §0.6 gate: both "
            "must be 0); see meta/faithfulness_report.md" % (suspect, insufficient),
        )
    # R2-F4: per-claim clearing. A needs_review claim passes only if its claim_id is
    # named in a `needs-review-cleared: …` marker (or an explicit `ALL`). A stray
    # blanket phrase no longer clears everything.
    if needs_review:
        cleared = gatelib.needs_review_cleared_ids(log_text)
        if "ALL" not in cleared:
            uncleared = [
                cv.claim_id or cv.key for cv in results
                if cv.verdict == "needs_review" and (cv.claim_id or "") not in cleared
            ]
            if uncleared:
                return gatelib.failed(
                    "faithfulness",
                    "%d needs_review claim(s) not cleared — resolve them or name their "
                    "claim_id in a '<!-- needs-review-cleared: id1 id2 -->' marker (spec §0.6): %s"
                    % (len(uncleared), ", ".join(str(x) for x in sorted(set(uncleared))[:8])),
                )
    return gatelib.passed(
        "faithfulness",
        "%d claims — suspect=0 insufficient=0, needs_review=%d cleared/none"
        % (len(results), needs_review),
    )


def _check_claim_map_coverage(review_text: str, legacy: bool) -> gatelib.CheckResult:
    """F1/F3 HARD: a delivered review that cites sources must carry claim_id
    sidecars (the writer's by-construction mapping). No sidecars + citations +
    not legacy → FAIL. Per-clause coverage itself is enforced by lint F3."""
    name = "claim-map-coverage"
    if claimids.has(review_text):
        return gatelib.passed(name, "claim_id sidecars present (per-clause coverage enforced by lint F3)")
    if "[@" not in review_text:
        return gatelib.passed(name, "no citations to map")
    if legacy:
        return gatelib.pending(name, ".gates_legacy — claim_id coverage grandfathered")
    return gatelib.failed(
        name,
        "review.md cites sources but carries NO claim_id sidecars — writer must emit "
        "<!-- claim:CID --> per factual clause (F1/F3); or add .gates_legacy to grandfather",
    )


def _check_evidence_uncertain(
    topic_dir: pathlib.Path, results: list["faithfulness.ClaimVerdict"], legacy: bool
) -> gatelib.CheckResult:
    """F9 HARD: a numeric field whose span the validator could not confirm is
    marked ``uncertain`` in meta/evidence_table.json; a claim must not rest on it.
    A MISSING or CORRUPT table is itself a FAIL when the review carries high-risk
    claims (the spec N6 evidence_extract step was skipped) — only ``pending`` when
    there are no high-risk claims (no table needed) or the topic is grandfathered."""
    name = "evidence-uncertain"
    path = topic_dir / layout.META_DIRNAME / "evidence_table.json"
    high_risk = [cv.key for cv in results if cv.risk == "high"]
    if not path.exists():
        if legacy or not high_risk:
            return gatelib.pending(
                name,
                "no meta/evidence_table.json"
                + ("" if high_risk else " (no high-risk claims — table not required)"),
            )
        return gatelib.failed(
            name,
            "%d high-risk claim(s) but meta/evidence_table.json MISSING — spec N6 "
            "evidence_extract (--prompt→抽取→--validate) was skipped" % len(high_risk),
        )
    try:
        table = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        if legacy:
            return gatelib.pending(name, "evidence_table.json unreadable (.gates_legacy)")
        return gatelib.failed(name, "meta/evidence_table.json corrupt — re-run evidence_extract --validate")
    bad = [
        key for key, row in table.items()
        if isinstance(row, dict)
        and any(row.get(field) == "uncertain" for field in ("n", "effect", "ci", "p"))
    ]
    if bad and not legacy:
        return gatelib.failed(
            name,
            "evidence_table 有未验证 span 的数值字段 uncertain（F9，断言不得倚靠）: "
            + ", ".join(sorted(bad)[:6]),
        )
    # N6/N7 coverage: a high-risk claim resting on a cited key with NO evidence row
    # means the writer composed it ad hoc, not from the table. Real citation keys
    # only ('(unbound)'/'(inference)' synthetics are flagged elsewhere).
    missing = sorted({
        k for k in high_risk
        if not k.startswith("(") and k not in table
    })
    if missing and not legacy:
        return gatelib.failed(
            name,
            "high-risk 被引 key 在 evidence_table 无行（writer 未据表写、临场合成，spec N6/N7）: "
            + ", ".join(missing[:6]),
        )
    return gatelib.passed(name, f"{len(table)} evidence row(s), no uncertain field / uncovered high-risk key")


_POS_DIR_RE = re.compile(
    # R16: add common positive verbs 提升/增强/促进/加快/增进 (were unbucketed → both their
    # genuine positives and negations were invisible to the cross-gap gate).
    r"增加|升高|提高|提升|增强|促进|加快|增进|改善|有效|获益|正相关|上升|"
    r"increase|improv|effective|benefit|raise|elevat",
    re.IGNORECASE,
)
# M8: cover null-result / no-effect phrasings so a positive-vs-null pair is detected.
# R7: add 'lower' (symmetric to pos 'raise') — a common direction verb that was missing.
_NEG_DIR_RE = re.compile(
    r"减少|降低|下降|恶化|无效(?:果|应)?|无获益|未改善|不改善|非劣效|负相关|无显著|"
    r"无统计学(?:意义|差异)|差异(?:无统计学意义|不显著)|decrease|reduc|ineffective|lower|"
    r"no\s+benefit|no\s+(?:significant\s+)?(?:effect|difference)|null\s*result|not\s+significant|worsen",
    re.IGNORECASE,
)
# R31: a copula / explicit negation sitting DIRECTLY before a negative-direction verb double-
# negates it → positive ('不是无效' = effective; '并非无获益' = has benefit). The glue-bridge
# _is_negated_before_verb misses these: its bridge char here is the copula 是/非 (不是 / 并非),
# which is deliberately NOT in _NEG_GLUE. Checked only in the _NEG_DIR_RE branch of _direction;
# matched against `before` (clause text ending exactly at the verb), so it fires on a trailing
# 不是/并非/绝非/不算 — not on a 不 buried earlier in the clause.
_COPULA_NEG_DIR_RE = re.compile(r"(?:不是|并不是|并非|绝非|远非|不算|不属于)\s*$")
# R21: the cross-gap direction must compare the QUANTITY axis (does the outcome go up vs down),
# NOT the efficacy axis. An explicit quantity verb decides direction with PRECEDENCE over an
# efficacy word, because for a lower-is-better outcome (visceral fat / drowsiness / TEWL) a
# 'beneficial reduction' (减少X有效) is the SAME finding as 'X 降低' — labeling one pos (via 有效)
# and the other neg (via 降低) manufactured a false cross-gap contradiction on real drafts.
# R31: a 减[重脂肥] that forms a fixed weight-loss EXPOSURE/topic NOUN (减重人群 / 减脂训练 /
# 减肥手术) is not a direction verb — it names the population/intervention ('减重人群补充胶原肽改善
# 弹性'). The R21 up-before-down guard only rescued this when an UP verb was present; with an
# efficacy outcome (改善) the topic-noun 减 still won the down-branch → labeled the claim 'neg' →
# false cross-gap HARD-FAIL against a concurring 'pos' claim. The lookahead drops 减[重脂肥] ONLY
# when followed by a topic-forming noun; 减重 as a real reduction VERB ('减重显著有效' / '显著减重')
# and 减少/减小/减轻/减缓/减弱/减低/减内脏脂肪 still match.
_QUANT_DOWN_RE = re.compile(
    r"减(?![重脂肥](?:人群|者|组|患者|手术|术|期|后|训练|饮食|方案|计划|对象|目标|干预))"
    r"(?:少|小|轻|缓|半|弱|低)?|降低|下降|缩小|缩短|削减|reduc|decreas|lower|declin",
    re.IGNORECASE,
)
_QUANT_UP_RE = re.compile(r"增加|升高|提高|提升|上升|增长|增强|增多|上调|increase|raise|elevat|\brise\b", re.IGNORECASE)
# efficacy/benefit verbs — quantity-AMBIGUOUS (改善 means up for BMD, down for a disease marker),
# so they set direction ONLY when no explicit quantity verb is present.
_EFFICACY_POS_RE = re.compile(r"改善|有效|获益|有益|促进|加快|增进|正相关|improv|effective|benefit", re.IGNORECASE)
# null-association ('X 下降无关' = decline is UNRELATED = no decline effect) → not a direction.
_NULL_ASSOC_RE = re.compile(r"无关(?:联|系)?|不相关|无相关|无显著(?:相)?关", re.IGNORECASE)
# M8 negation detection (R15 + R16). A CJK negator (不/未/无/没) flips a positive verb to neg ONLY
# when the chars BRIDGING it to the verb are all "negation glue" — modal/ability/degree morphemes
# (能/足以/达到/会/法/力/太/显著/明显…). This is symmetric:
#   · R15 over-flip closed: a bare 无/不 buried in a FIXED NOUN compound (无机磷升高 = inorganic
#     phosphate ROSE, 无氧阈提高, 无创指标改善, 不饱和脂肪酸升高) is NOT bridged by glue (机/氧/创/饱
#     are noun morphemes) → stays positive.
#   · R16 under-flip closed: a real negation whose negator is separated from the verb by glue
#     (不太能改善, 不足以改善, 未达到改善标准, 无显著改善) IS bridged → neg.
# Glue deliberately EXCLUDES noun morphemes, so it never re-opens the over-flip.
_NEG_GLUE = set("能足以够会可法力太达到有显著明大幅进步统计学上必而得几乎尚仍")
# R17: negation-scope adverbs that may bridge a negator to the verb (不再X / 不复X / 未见X /
# 不予X / 不曾X). Only consulted for the BRIDGE, kept out of _NEG_GLUE; a noun compound carrying
# one of these chars is still safe because its other morpheme is not in the bridge set (无再生
# 提高: 生∉bridge → pos; 无复发提高: 发∉bridge → pos).
_NEG_BRIDGE = _NEG_GLUE | set("再复见予曾")
# Match 难以 / 不一定 as PHRASES, not bare 难/一/定 — else the adjective/noun uses 艰难/困难/灾难
# and 不定期/稳定 would false-negate a positive verb (艰难提高 = increased with difficulty = pos).
_CJK_NEG_RE = re.compile(r"不一定|难以|[不未无没]")
# English negation is clause-scoped (not a fixed char window): 'failed to increase' / 'unable to
# improve' / 'did not raise' have the negator far from the verb. The old 4-char window missed them.
_EN_NEG_RE = re.compile(
    r"\b(?:not|no|fail(?:ed|s|ure)?|unable|cannot|can[''`]?t|without|lack(?:ed|s|ing)?|"
    r"did\s+not|does\s+not|do\s+not|n[''`]t)\b",
    re.IGNORECASE,
)


def _is_negated_before_verb(before: str) -> bool:
    """True if the positive verb at the end of `before` (clause text up to the verb) is negated.
    CJK: the negator NEAREST the verb must be bridged to it only by negation-glue chars (a noun
    morpheme in the bridge → not a verb negation). English: any clause-scoped negator before it."""
    cjk = list(_CJK_NEG_RE.finditer(before))
    if cjk and all(c in _NEG_BRIDGE for c in before[cjk[-1].end():]):
        return True
    return bool(_EN_NEG_RE.search(before))
# NB: "争议" is the section's own name (限定与争议), so it is NOT a reconcile cue —
# only words that signal the conflict is actually being discussed count.
_RECONCILE_RE = re.compile(r"矛盾|不一致|分歧|相互冲突|冲突|conflicting|inconsisten|reconcile", re.IGNORECASE)
_LIMIT_SECTION_RE = re.compile(r"^#+[^\n]*(?:限定|争议|局限)", re.MULTILINE)
# R2-F6: generic measure-fillers must not count as shared content (else two unrelated
# clusters 'share' 水平/数值 and a reconcile note or a contradiction is mis-attributed).
# R6 (structural, replaces R5's adverb enumeration): the cross-gap false-merge had two
# roots — (1) the char-windows in _outcome_tokens slice words mid-character so adverb/word
# FRAGMENTS leak (significantly→'icantly', placebo→'placeb'), and (2) experimental FRAMING/
# comparator words (versus/placebo/group/干预组/对照) leak as shared outcome tokens. Fixed by
# whole-word window snapping + a generic `\b[A-Za-z]+ly\b` adverb strip + framing stop-words.
# R8: statistical reporting abbreviations — NOT outcomes (my R7 _ABBR_RE widening admitted
# them, so two unrelated claims both reporting 'OR'/'CI' false-merged into a contradiction).
# Excluded from BOTH content & outcome so a stat token can neither anchor a cluster nor pad
# the ≥2-shared floor. (Biomarker abbreviations LDL/HDL/CRP/BMI are NOT here — they stay.)
_STAT_ABBR = {"or", "rr", "hr", "ci", "sd", "se", "cv", "md", "smd", "ae", "sae", "nnt",
              "arr", "rd", "irr", "iqr", "auc", "roc", "npv", "ppv", "icc", "rmse"}
# Fillers that are neither PICO nor outcome — excluded from BOTH content & outcome tokens.
_STOP_WORDS = ({"study", "trial", "studies", "with", "that", "this", "from", "have",
                "研究", "试验", "显示", "结果", "提示", "表明", "可能",
                "水平", "数值", "程度", "指标", "level", "value", "values"}
               | _STAT_ABBR)
# Experimental FRAMING / comparator words: legit SHARED PICO (they DO count toward the
# ≥2-shared-content cluster floor) but NOT an outcome — so they are excluded from OUTCOME
# tokens only (R6: putting them in _STOP_WORDS dropped real same-outcome contradictions
# below the floor; leaking them into outcomes false-merged different-outcome claims).
_FRAMING_STOP_WORDS = {
    "placebo", "versus", "control", "controls", "baseline", "group", "groups", "arm", "arms",
    "comparison", "concentration", "intervention", "treatment", "outcome", "outcomes",
    "endpoint", "endpoints", "primary", "secondary", "cohort", "participants", "patients",
    "compared", "relative", "overall",
    # R7: framing prepositions/comparators that leaked as outcome tokens
    "over", "above", "across", "through", "within", "during", "than", "after", "before",
    "干预", "预组", "组中", "治疗", "对照", "基线", "相比", "安慰", "慰剂", "主要", "次要",
    "终点", "队列", "受试", "患者", "整体", "相较",
    # R20: generic CJK qualifiers / abstract-category nouns that are NEVER a measured outcome but
    # were leaking as the load-bearing shared-OUTCOME anchor → false cross-gap contradictions on
    # real drafts ([@a]↑ vs [@b]↓ on just '反应'/'代谢'/'暴露'/'主观'/'方向'). Outcome-stop only, so
    # they still count as shared PICO. A real same-outcome contradiction keeps its true outcome
    # bigram (骨密度反应提高 vs 骨密度未提高 → still share 骨密/密度), so this opens no fail-open.
    "反应", "代谢", "暴露", "主观", "客观", "方向", "程度", "效应", "影响", "水平",
    "作用", "效果", "趋向",  # R20: generic 'effect/tendency' nouns — same false-anchor risk as 效应
    # R21: more abstract meta-nouns that are never a measured clinical outcome but could anchor a
    # false outcome cluster (latent — 0/62 real topics today, but realistic-prose-triggerable). A
    # specific outcome keeps its own bigram (炎症指标升高 → 炎症 survives), so no fail-open.
    "机制", "变化", "差异", "关系", "现象", "情况", "数据", "条件", "指标", "特征",
    # R32: side-effect / safety meta-nouns. '副作用' bigram-splits to 副作 + 作用 (作用 already above);
    # 副作 leaked as the load-bearing shared OUTCOME anchor, false-merging two DIFFERENT-exposure
    # claims (RF microneedling↑ vs isotretinoin↓) whose only overlap was '副作用'. Different
    # interventions' side-effects are never a cross-gap contradiction axis. Outcome-stop only (still
    # PICO), and a real same-outcome pair keeps its true bigram, so no fail-open.
    "副作", "不良", "毒性", "耐受",
    # R35: '证据' (evidence) is a generic epistemic meta-noun, never a measured clinical outcome — it
    # leaked as the SOLE shared-outcome anchor merging '运动→骨密度 的证据' (pos) with '运动→跌倒 的证据'
    # (neg), two same-exposure DIFFERENT-outcome faithful claims that concur → false cross-gap HARD
    # FAIL (topic 成年人骨密度). Same class as 数据/指标/机制 above. A real same-outcome pair keeps its
    # true bigram (骨密/密度), so no fail-open.
    "证据", "结论", "结果",
}
_OUTCOME_STOP_WORDS = _STOP_WORDS | _FRAMING_STOP_WORDS
# R17: framing/group prefixes whose trailing boundary leaks a spurious bigram into an SV-order
# subject window ('治疗组血压'→疗组/组血). Split the subject window on these and keep the last
# segment (the real outcome). R18: the group marker is always 组 (组/组中/亚组) — split on `组…`,
# NOT bare 中/亚, so an outcome that merely STARTS with 中/亚 ('中性粒细胞', '亚临床甲减') is
# preserved. R19: also consume the group LOCUS char after 组 (组中/组间/组内/组外 'in/between/
# within/outside the group') — '两组间血压' was leaking the 间血 bigram → a DIFFERENT-outcome
# false-merge (组间血压↑ vs 组间血糖↓) → false contradiction HARD-FAIL.
_FRAMING_BOUNDARY_RE = re.compile(
    r"组[中间内外]?|剂量[下中]?|人群[中内]?|期间|随访|基线|"
    r"治疗|对照|干预|研究|实验|观察|模型|安慰剂?|"
    # R22: causal verbs ('运动使血压升高') — without splitting at them the SV subject window keeps
    # the exposure+verb prefix and leaks a '使血' bigram. Since blood markers (血压/血糖/血脂/血钙/
    # 血浆肾素…) all start with 血 and 使 is high-frequency, any same-exposure pair on two DIFFERENT
    # blood markers false-merged on the spurious 使血 anchor → false contradiction HARD-FAIL.
    # NB: 致 is only matched in the multi-char causal forms 致使/导致 — bare 致 is an OUTCOME
    # morpheme (致敏/致癌/致病/致死), not framing, so it must not be split. Only the causal verbs
    # actually seen in real review prose are included; the exotic causal-verb tail (造成/诱导/
    # 调节/改变…) is left out ON PURPOSE — it is latent (0/62 real topics) AND several of those
    # verbs overlap real outcome nouns (调节性T细胞=Treg, 诱导型NOS=iNOS), so splitting them would
    # break real outcomes. That tail is the documented asymptotic residual.
    r"使|让|令|致使|导致|促使|引起|引发"
)
# R7: structural CJK particles — an outcome bigram that spans one (组〔的〕/疗〔后〕/线〔时〕)
# is a framing-boundary artifact, not an outcome (the real outcome is a separate bigram).
_CJK_PARTICLES = set("的了着过后时之其地得被把将所而和与及对于在")
# R7: a generic `…ly` strip false-removes -ly NOUNS/adjectives that can be real outcomes
# (family/supply/anomaly), silencing a real contradiction (fail-OPEN). Strip a `…ly` word
# ONLY if it is NOT one of these non-degree-adverb words.
_LY_NOT_ADVERB = {
    "family", "families", "supply", "supplies", "anomaly", "anomalies", "assembly",
    "monopoly", "belly", "jelly", "lily", "rally", "ally", "folly", "bully", "apply",
    "reply", "comply", "imply", "multiply", "early", "likely", "unlikely", "only",
    "holy", "ugly", "silly", "daily", "weekly", "monthly", "yearly", "hourly", "costly",
    "friendly", "elderly", "orderly", "lonely", "lovely", "deadly", "lively", "timely",
    "fully", "duly", "wholly", "solely", "italy",
}
_LY_ADVERB_RE = re.compile(r"\b[A-Za-z]+ly\b", re.IGNORECASE)  # generic English degree adverb
_ABBR_RE = re.compile(r"\b[A-Z]{2,5}[0-9]?\b")  # R7: clinical abbreviations (LDL/HDL/BMI/CRP/VO2)
# R2-F3: a null-significance marker that co-occurs with a positive verb wins the
# *significance* axis → the claim is a (politely-phrased) null result, direction = neg.
_NULL_SIG_RE = re.compile(
    r"无统计学(?:意义|差异)|无显著|不显著|差异不显著|非劣效|null\s*result|not\s+significant|"
    r"no\s+(?:significant\s+)?(?:effect|difference)",
    re.IGNORECASE,
)
_CLAUSE_BOUNDARY_RE = re.compile(r"[，、。；;]")
# R32: a contrastive conjunction (但/然而/不过/可是/却) opens a clause about a DIFFERENT aspect
# (typically side-effects/caveats) than the directional verb's outcome. _CLAUSE_BOUNDARY_RE does
# not list it, so the post-verb outcome window crossed it and pulled the contrastive tail in —
# '减少皮脂腺体积…但副作用让它…' leaked '副作' (副作用 bigram) as the shared OUTCOME anchor, false-
# merging two DIFFERENT-exposure claims (RF microneedling↑ vs isotretinoin↓) into a cross-gap
# contradiction HARD-FAIL. Cut the outcome window at the contrast word so only the verb's own
# clause contributes outcome tokens.
_CONTRAST_BOUNDARY_RE = re.compile(r"但是?|然而|不过|可是|(?<![冷退忘抛])却")
# R3-A5: degree adverbs that follow a direction verb ('升高〔明显〕') are NOT the outcome —
# when the post-verb window holds only these, fall through to the SV-order subject.
_DEGREE_ADVERBS = {"明显", "显著", "略微", "进一步", "大幅", "轻微", "趋势", "稍有",
                   "几乎", "大致", "基本"}  # R20: 几乎 (almost) etc. were anchoring false outcome clusters
# R5: stripped from the sentence BEFORE windowing so an adverb between subject and verb
# ('CRP significantly reduced') can't leave a 'icantly' fragment in the outcome window.


def _content_words(text: str) -> set[str]:
    """Shared-subject (PICO) tokens — degree adverbs stripped (R6: an adverb is not PICO,
    and 'significantly' shared by two claims used to reach the ≥2 floor on its own). CJK
    has no word delimiters, so use 2-char bigrams alongside ≥4-char latin words AND ≥2-char
    uppercase clinical abbreviations (LDL/HDL/BMI/CRP — R7, else 3-letter biomarker outcomes
    were never tokens → contradictions missed). Framing words ARE kept here (shared PICO);
    they are dropped only from OUTCOME tokens."""
    text = _strip_adverbs(text)
    latin = {w.lower() for w in re.findall(r"[A-Za-z]{4,}", text)}
    abbr = {w.lower() for w in _ABBR_RE.findall(text)}
    cjk = re.findall(r"[一-鿿]", text)
    bigrams = {cjk[i] + cjk[i + 1] for i in range(len(cjk) - 1)}
    return (latin | abbr | bigrams) - _STOP_WORDS


# R4: CJK multi-char degree adverbs (进一步) stripped as substrings (Latin adverbs are
# handled generically by _LY_ADVERB_RE — no enumeration to keep current).
_DEGREE_ADVERB_SUB_RE = re.compile("|".join(sorted(_DEGREE_ADVERBS, key=len, reverse=True)))


def _strip_adverbs(text: str) -> str:
    """Remove degree adverbs before tokenizing: generic English `…ly` (significantly /
    dramatically / strongly — no enumeration) EXCEPT -ly nouns/adjectives that can be real
    outcomes (family/supply/anomaly — R7, else stripping them silenced a contradiction) +
    the CJK degree-adverb list (R6)."""
    text = _LY_ADVERB_RE.sub(lambda m: m.group(0) if m.group(0).lower() in _LY_NOT_ADVERB else "", text)
    return _DEGREE_ADVERB_SUB_RE.sub("", text)


def _outcome_words(text: str) -> set[str]:
    """Tokens for an outcome window (already snapped to whole-word boundaries by the
    caller, so no edge fragments). Excludes framing/comparator words (an outcome is the
    thing measured, not the experimental setup) + CJK particle-boundary bigrams (组的/疗后,
    R7) on top of content stops; keeps a single-char CJK outcome (钙/钠, R3-A5-ii)."""
    text = _strip_adverbs(text)
    base = {
        t for t in (_content_words(text) - _FRAMING_STOP_WORDS)
        if not (len(t) == 2 and (t[0] in _CJK_PARTICLES or t[1] in _CJK_PARTICLES))
    }
    cjk = re.findall(r"[一-鿿]", text)
    if len(cjk) == 1 and cjk[0] not in _OUTCOME_STOP_WORDS:
        base = base | {cjk[0]}
    return base


def _clause_of(sentence: str, pos: int) -> str:
    """The comma/semicolon/period clause containing character offset `pos`."""
    starts = [0] + [m.end() for m in _CLAUSE_BOUNDARY_RE.finditer(sentence)]
    ends = [m.start() for m in _CLAUSE_BOUNDARY_RE.finditer(sentence)] + [len(sentence)]
    for s, e in zip(starts, ends):
        if s <= pos <= e:
            return sentence[s:e]
    return sentence


def _clause_and_before(sentence: str, v: int) -> tuple[str, str]:
    """The clause containing offset `v`, and the clause text up to (not including) `v`."""
    starts = [0] + [m.end() for m in _CLAUSE_BOUNDARY_RE.finditer(sentence)]
    ends = [m.start() for m in _CLAUSE_BOUNDARY_RE.finditer(sentence)] + [len(sentence)]
    for s, e in zip(starts, ends):
        if s <= v <= e:
            return sentence[s:e], sentence[s:v]
    return sentence, sentence[:v]


def _direction(sentence: str) -> str | None:
    """M8 (R21): 'pos' / 'neg' / None on the QUANTITY axis (does the outcome go up vs down).
    An explicit quantity verb (升高/降低) decides direction with PRECEDENCE over an efficacy word
    (有效/改善) — a beneficial reduction '减少X有效' is neg (same as 'X 降低'), not pos. A quantity
    verb made null by 无统计学意义 / 无关 in its clause yields None (下降无关 = no decline). Efficacy
    words set direction only when no explicit quantity verb is present; a negated positive
    ('不能改善') counts as neg (clause-scoped, R2-F3 + R3-A7)."""
    # R21: a sentence carrying BOTH an up and a down quantity verb is multi-outcome ('降低 TEWL、
    # 提高水合度'; '增强 GLP-1 …降低血糖') — a single direction label can't be correct for each
    # clustered outcome, and using it false-merges two claims that actually CONCUR on the shared
    # outcome. Don't bucket such a sentence (conservative: a missed multi-directional contradiction
    # is a SECONDARY-net safe-direction loss, never a false HARD-FAIL).
    up_hit, down_hit = _QUANT_UP_RE.search(sentence), _QUANT_DOWN_RE.search(sentence)
    if up_hit and down_hit:
        return None
    # up checked before down so a real outcome-up verb ('提高耐力') wins over a bare 减 that merely
    # sits in an intervention/topic name ('减脂训练'); a genuine reduction has no up verb.
    for rx, sign in ((_QUANT_UP_RE, "pos"), (_QUANT_DOWN_RE, "neg")):
        hit = rx.search(sentence)
        if hit:
            clause, before = _clause_and_before(sentence, hit.start())
            if _NULL_SIG_RE.search(clause) or _NULL_ASSOC_RE.search(clause):
                return None  # 升/降 with no significance / no association → not a direction
            if _is_negated_before_verb(before):
                return "pos" if sign == "neg" else "neg"  # negated quantity change → opposite
            return sign
    eff = _EFFICACY_POS_RE.search(sentence)
    if eff:
        clause, before = _clause_and_before(sentence, eff.start())
        if _is_negated_before_verb(before) or _NULL_SIG_RE.search(clause):
            return "neg"
        return "pos"
    neg = _NEG_DIR_RE.search(sentence)
    if neg:
        _clause, before = _clause_and_before(sentence, neg.start())
        # double negation: a negated negative-direction verb flips positive (不是无效 = effective).
        # _is_negated_before_verb catches glue-bridged cases (不能无效); _COPULA_NEG_DIR_RE catches
        # the copula bridge (不是/并非) that glue deliberately excludes.
        if _is_negated_before_verb(before) or _COPULA_NEG_DIR_RE.search(before):
            return "pos"
        return "neg"
    return None  # neither → not bucketed


def _outcome_tokens(sentence: str) -> set[str]:
    """M9: the *outcome* being raised/lowered. VO order (升高〔血压〕) → tokens after the
    verb, cut at the next clause boundary (R2-F6). SV order (血压〔升高〕, R2-F7) → if the
    post-verb window holds nothing usable OR only a degree adverb (升高〔明显〕, R3-A5-i),
    take the immediate subject before the verb. Single-char outcomes (钙) preserved (A5-ii)."""
    # R6: strip degree adverbs from the whole sentence first, then SNAP every window to
    # whole-word boundaries (the direction regexes match verb STEMS inside inflected words —
    # 'improv' in 'improved' — so a raw char-window starts/ends mid-word and leaks fragments
    # like 'icantly'/'placeb'). Snapping = no fragment can ever become a token.
    sentence = _strip_adverbs(sentence)

    def _is_latin(c: str) -> bool:  # NB: CJK chars are .isalpha()==True — must exclude them
        return c.isascii() and c.isalpha()

    def _snap_fwd(i: int) -> int:   # advance past the rest of the current LATIN word only
        while i < len(sentence) and _is_latin(sentence[i]):
            i += 1
        return i

    def _snap_back(i: int) -> int:  # rewind to the start of the current LATIN word only
        while i > 0 and _is_latin(sentence[i - 1]):
            i -= 1
        return i

    toks: set[str] = set()
    for rx in (_POS_DIR_RE, _NEG_DIR_RE):
        for m in rx.finditer(sentence):
            we = _snap_fwd(m.end())                     # clean start: past the verb's word
            window = _CLAUSE_BOUNDARY_RE.split(sentence[we:_snap_fwd(we + 16)])[0]
            after = _CONTRAST_BOUNDARY_RE.split(window)[0]   # R32: stop at 但/然而/… contrast clause
            post = _outcome_words(after)
            if post:
                toks |= post
            else:  # SV order: the immediate subject before the verb. A SHORT window (the
                # subject sits right before the verb); Latin is rescued to whole words by
                # _snap_back, while CJK stays short so it doesn't reach the framing prefix
                # ('在干预组中〔钙〕升高' → 钙, not the shared 在干/组中 framing). R6.
                ws = _snap_back(m.start())
                before = _CLAUSE_BOUNDARY_RE.split(sentence[_snap_back(max(0, ws - 5)):ws])[-1]
                # R17: keep only the IMMEDIATE subject — drop a leading framing-group prefix
                # ('治疗组'/'干预组中'/'亚组') so a boundary bigram (疗组/组血/中血/究组) can't leak
                # as a shared outcome and false-merge two DIFFERENT-outcome claims into a
                # contradiction. '治疗组血压' → 血压; '研究组中钙' → 钙; a clean '疗效' is untouched.
                before = _FRAMING_BOUNDARY_RE.split(before)[-1]
                toks |= _outcome_words(before)
    return toks


def _limit_section(review_text: str) -> str:
    match = _LIMIT_SECTION_RE.search(review_text)
    return review_text[match.start():] if match else ""


def _pair_reconciled(limit_text: str, shared: set[str], keys: tuple[str, str]) -> bool:
    """B2: a reconcile cue must be CO-LOCATED with THIS conflict — a §限定 sentence that
    signals reconciliation AND either names one of the pair's keys (strong signal) or
    shares ≥2 content tokens with the cluster (R2-F6: ≥2, not ≥1, and fillers excluded —
    a single generic token no longer disarms an unrelated cluster)."""
    for sent in re.split(r"[。！？\n]", limit_text):
        if not _RECONCILE_RE.search(sent):
            continue
        if any(f"@{k}" in sent for k in keys):
            return True
        if len(_content_words(sent) & shared) >= 2:
            return True
    return False


def _check_cross_gap(
    results: list["faithfulness.ClaimVerdict"], review_text: str, store: refs.Store
) -> gatelib.CheckResult:
    """F10: cluster claims by (outcome × PICO/comparison) **across gap boundaries**,
    then flag opposing-direction pairs not reconciled in §限定与争议. A contradiction
    between gap-2 and gap-5 is what 逐条忠实 misses. Cluster proxy (M9): ≥2 shared
    content tokens AND a shared OUTCOME token (adjacent to the direction verb), so
    same-exposure/different-outcome pairs don't false-merge. Reconcile is per-cluster
    (B2): a stray reconcile word elsewhere can't disarm a specific conflict."""
    name = "cross-gap-contradiction"
    claims = [cv for cv in results if cv.verdict in ("faithful", "needs_review")]
    pos = [c for c in claims if _direction(c.sentence) == "pos"]
    neg = [c for c in claims if _direction(c.sentence) == "neg"]
    limit_text = _limit_section(review_text)

    unreconciled: list[str] = []
    for p in pos:
        p_words, p_out = _content_words(p.sentence), _outcome_tokens(p.sentence)
        for n in neg:
            if p.key == n.key:
                continue
            shared = p_words & _content_words(n.sentence)
            shared_outcome = p_out & _outcome_tokens(n.sentence)
            if len(shared) >= 2 and shared_outcome:  # same outcome×PICO cluster
                if not _pair_reconciled(limit_text, shared, (p.key, n.key)):
                    unreconciled.append(f"[@{p.key}]↑ vs [@{n.key}]↓ ({'/'.join(sorted(shared_outcome)[:2])})")
    if unreconciled:
        return gatelib.failed(
            name,
            "跨 gap 反向 faithful 断言未在 §限定与争议 就该簇 reconcile（F10/B2）: "
            + "; ".join(sorted(set(unreconciled))[:5]),
        )
    return gatelib.passed(name, "no unreconciled reverse assertions across outcome clusters")


def _read_cid_map(topic_dir: pathlib.Path, name: str) -> dict[str, str]:
    path = topic_dir / layout.META_DIRNAME / name
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return {str(k): str(v) for k, v in data.items()} if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _load_judgments(topic_dir: pathlib.Path) -> dict[str, str]:
    """meta/judge_verdicts.json: {claim_id: entailed|not_entailed|unclear} from the
    F8 decorrelated entailment judge (written by the write-loop judge agent)."""
    return _read_cid_map(topic_dir, "judge_verdicts.json")


def _fresh_judgments(
    topic_dir: pathlib.Path, results: list["faithfulness.ClaimVerdict"]
) -> dict[str, str]:
    """Judgments with STALE verdicts dropped (spec §0.6.g「改写复用旧 ID」): combine_judges
    stamps meta/judge_targets.json = {claim_id: claim_hash}. A verdict survives only
    if its claim_id's current text hashes to the stamped target; a reused id over a
    rewritten clause mismatches → dropped → that claim is treated as un-judged → the
    entailment-judged check fails it. With no targets sidecar (legacy/manual) accept
    as-is."""
    judgments = _load_judgments(topic_dir)
    targets = _read_cid_map(topic_dir, "judge_targets.json")
    if not targets:
        return judgments
    current = {cv.claim_id: faithfulness.claim_hash(cv) for cv in results if cv.claim_id}
    return {
        cid: verdict
        for cid, verdict in judgments.items()
        if targets.get(cid) is not None and current.get(cid) == targets.get(cid)
    }


def _check_entailment_judged(
    results: list["faithfulness.ClaimVerdict"], judgments: dict[str, str], legacy: bool
) -> gatelib.CheckResult:
    """F8 HARD (spec §0.6.b 正面蕴含「来源沉默=fail」 + §0.6.g 多 judge): the
    deterministic checks (span exists / number-in-span / section / negation) do NOT
    confirm the source POSITIVELY ENTAILS the claim. A high-risk claim that passed
    them but was never LLM-entailment-judged is unconfirmed → FAIL (forces the
    write-loop judge to run). not_entailed / unclear verdicts are already folded
    into faithfulness via apply_judgments."""
    name = "entailment-judged"
    # EVERY factual claim still 'faithful' (high-risk OR low-risk cited factual,
    # spec §4「每事实断言…正面蕴含 verdict」) must carry a verdict of EXACTLY
    # 'entailed' — a missing id, OR a garbage / not_entailed / unclear / STALE value,
    # does NOT confirm positive entailment. (not_entailed/unclear already became
    # suspect/needs_review via apply_judgments; stale verdicts are dropped by
    # _fresh_judgments; so a still-'faithful' factual claim must be 'entailed'.)
    # only claims that carry a claim_id are checked here — a faithful cited claim
    # missing its sidecar is caught by claim-map coverage, not double-flagged here.
    unjudged = [
        cv.key for cv in results
        if faithfulness._needs_judge(cv) and cv.claim_id and judgments.get(cv.claim_id) != "entailed"
    ]
    if unjudged and not legacy:
        return gatelib.failed(
            name,
            "%d factual claim(s) passed确定性核 but were NOT LLM-entailment-judged — "
            "正面蕴含/来源沉默 未确认（spec §0.6.b/g；高风险走 3-judge 集成、低风险单 judge → "
            "meta/judge_verdicts.json）: %s" % (len(unjudged), ", ".join(sorted(set(unjudged))[:6])),
        )
    return gatelib.passed(name, "factual claims entailment-judged (or none)")


def _check_high_risk_grounding(
    results: list["faithfulness.ClaimVerdict"], legacy: bool
) -> gatelib.CheckResult:
    """F5 HARD (spec §0.6 grounding 下限): a high-risk claim (quant / comparison /
    author-conclusion / negation / set) must ground ≥ fulltext/pdf_text. Abstract
    or title-only for a high-risk claim → FAIL (the write-loop fetches fulltext for
    cited entries, C10, to clear this). `.gates_legacy` grandfathers."""
    name = "high-risk-grounding"
    weak = [
        cv.key for cv in results
        if cv.risk == "high" and cv.grounding in ("title_only", "abstract")
    ]
    if not weak:
        return gatelib.passed(name, "all high-risk claims grounded ≥ fulltext/pdf_text")
    if legacy:
        return gatelib.pending(name, f".gates_legacy — {len(weak)} high-risk abstract/title-only grandfathered")
    return gatelib.failed(
        name,
        "%d high-risk claim(s) grounded only on abstract/title-only — spec §0.6 要求 "
        "≥ fulltext；为被引条目抓全文 (C10) 或软化断言: %s"
        % (len(weak), ", ".join(sorted(set(weak))[:6])),
    )


def _annotated_near(review_text: str, key: str) -> bool:
    """The metadata note (_METADATA_NOTE_RE) sits on the SAME LINE as some [@key]
    occurrence — matching the annotation convention (note right by the citation),
    so a stray note in §方法 boilerplate elsewhere can't disarm the gate."""
    marker = f"[@{key}]"
    return any(
        marker in line and _METADATA_NOTE_RE.search(line)
        for line in review_text.splitlines()
    )


def _check_metadata(
    store: refs.Store, used_keys: list[str], review_text: str, legacy: bool
) -> gatelib.CheckResult:
    """M1/M3 HARD (spec §0.6.k): a cited entry under an expression-of-concern, an
    erratum/correction, OR a preprint must be flagged in prose (retracted is a hard
    FAIL upstream). Any such cited entry unannotated → FAIL."""
    name = "metadata"
    flagged: list[str] = []  # (key:reason)
    for key in used_keys:
        doi = refs.resolve_citation_key(store, key)
        entry = store.get("entries", {}).get(doi) if doi else None
        if entry is None:
            continue
        md = refs.metadata_flags(entry)
        labels = [
            label
            for field, label in (
                ("expression_of_concern", "EoC"),
                ("erratum", "erratum"),
                ("preprint", "preprint"),
            )
            if md[field]
        ]
        # Per-NEIGHBOURHOOD: the note must sit near this entry's [@key], not just
        # anywhere in the doc — else a stray 'preprint' in §方法 boilerplate would
        # disarm the gate for every flagged entry (verification finding).
        if labels and not _annotated_near(review_text, key):
            flagged.append(f"{key}({'/'.join(labels)})")
    if flagged and not legacy:
        return gatelib.failed(
            name,
            "被引 EoC/erratum/预印本 文献未在正文标注（spec §0.6.k 元数据闸）: "
            + ", ".join(sorted(flagged)[:6]),
        )
    return gatelib.passed(
        name,
        f"metadata ok ({len(flagged)} cited EoC/erratum/preprint{' annotated' if flagged else ''}; "
        "retracted blocked upstream by lint)",
    )


_DEDUP_NOTE_RE = re.compile(
    r"重复发表|同一(?:研究|试验|数据|队列|样本)|duplicate publication|same (?:study|trial|cohort)",
    re.IGNORECASE,
)


def _check_duplicate_cluster(
    store: refs.Store, used: list[str], review_text: str, legacy: bool
) -> gatelib.CheckResult:
    """Metadata 闸 (spec §0.6.k 重复发表): two cited keys sharing a duplicate_cluster_id
    (normalized-title cluster) are likely the SAME study cited twice — double-counting
    evidence. If the prose carries no dedup note, FAIL (non-legacy)."""
    name = "duplicate-cluster"
    clusters: dict[str, list[str]] = {}
    for key in used:
        doi = refs.resolve_citation_key(store, key)
        entry = store.get("entries", {}).get(doi) if doi else None
        if not entry:
            continue
        cluster_id = refs.metadata_status_fields(entry).get("duplicate_cluster_id")
        if cluster_id:
            clusters.setdefault(str(cluster_id), []).append(key)
    dups = {cid: keys for cid, keys in clusters.items() if len(keys) > 1}
    if not dups:
        return gatelib.passed(name, "no cited key shares a duplicate cluster")
    # M3: the dedup note must be CO-LOCATED with THIS cluster's keys — a stray
    # '重复发表' about an unrelated topic no longer disarms every cluster.
    unnoted = [
        ", ".join(keys) for keys in dups.values()
        if not _cluster_dedup_noted(review_text, keys)
    ]
    if not unnoted:
        return gatelib.passed(name, f"{len(dups)} cluster(s) cited >1× — each carries a co-located dedup note")
    if legacy:
        return gatelib.pending(name, f".gates_legacy — {len(unnoted)} duplicate cluster(s): {'; '.join(unnoted[:3])}")
    return gatelib.failed(
        name,
        "同一研究疑被多次引用（duplicate_cluster_id 相同）却无近邻去重说明，恐重复计票（spec §0.6.k）: "
        + "; ".join(unnoted[:3]),
    )


def _cluster_dedup_noted(review_text: str, keys: list[str]) -> bool:
    """A dedup note that names one of THIS cluster's keys in the same sentence."""
    for sent in re.split(r"[。！？\n]", review_text):
        if _DEDUP_NOTE_RE.search(sent) and any(f"@{k}" in sent for k in keys):
            return True
    return False


def _check_second_decomposer(
    topic_dir: pathlib.Path, review_text: str, legacy: bool
) -> gatelib.CheckResult:
    """§0.6.d 第二分解器 + diff (LLM 步): an independent re-decomposition of the prose
    must run and find no clause the writer left unmapped. The write-loop agent writes
    meta/decomp_diff.json = {undermapped:[...]}; a missing file (infra active) or a
    non-empty undermapped list is a FAIL. Only fires once claim_id infra is active."""
    name = "second-decomposer"
    if not claimids.has(review_text):
        return gatelib.passed(name, "no claim_id sidecars — second-decomposer n/a")
    path = topic_dir / layout.META_DIRNAME / "decomp_diff.json"
    if not path.exists():
        if legacy:
            return gatelib.pending(name, ".gates_legacy — second-decomposer not run")
        return gatelib.failed(
            name,
            "no meta/decomp_diff.json — 第二分解器（spec §0.6.d，另一档模型独立再分解 + diff）未跑",
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        if legacy:
            return gatelib.pending(name, "decomp_diff.json unreadable (.gates_legacy)")
        return gatelib.failed(name, "meta/decomp_diff.json corrupt — re-run second-decomposer")
    # stub-rejection ([6], symmetric to _audit_has_sample): a real run reports how many
    # clauses it independently decomposed. A bare {} (missing keys) = decomposer never
    # actually ran → FAIL (can't certify coverage from an empty artifact).
    total = data.get("clauses_total")
    if not isinstance(data, dict) or "undermapped" not in data or not isinstance(total, int) or total < 1:
        if legacy:
            return gatelib.pending(name, "decomp_diff.json is a stub (.gates_legacy)")
        return gatelib.failed(
            name,
            "meta/decomp_diff.json 是 stub（缺 clauses_total≥1 或 undermapped 键）—第二分解器未真跑（spec §0.6.d）",
        )
    flagged = data.get("undermapped") or []
    if flagged and not legacy:
        return gatelib.failed(
            name,
            "第二分解器发现 %d 处 writer 漏拆/未映射子句（spec §0.6.d）: %s"
            % (len(flagged), "; ".join(str(x)[:50] for x in flagged[:4])),
        )
    return gatelib.passed(name, f"second-decomposer ran ({total} clauses), no unmapped clause")


def _audit_has_sample(meta: pathlib.Path) -> bool:
    """True only if a faithfulness_audit artifact carries a REAL sample — a stub
    (``{}`` / empty / '0 faithful verdict(s)') must not satisfy the gate (adversarial
    finding: existence-only check let an empty artifact pass)."""
    for ext in ("md", "json"):
        path = meta / f"faithfulness_audit.{ext}"
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        # a real .md checklist has '_sample size: N_' with N≥1 or ≥1 table data row
        if re.search(r"sample size:\s*[1-9]", text) or re.search(r"\|\s*\S.*\|\s*(?:high|low)\s*\|", text):
            return True
    return False


def _check_faithfulness_audit(
    topic_dir: pathlib.Path, results: list["faithfulness.ClaimVerdict"], legacy: bool
) -> gatelib.CheckResult:
    """§0.6.i 假阴审计 in the hard gate (symmetric to §0.4 recall audit): the entailment
    judge — the 招牌属性 — must not be the only un-audited LLM step. The judge runs on
    EVERY faithful claim that needs a verdict (high-risk 3-judge OR low-risk single,
    spec §0.6.g/i), so the trigger is "any judged faithful claim", not just high-risk —
    else vague phrasing (risk=low) silently skips the audit. The artifact must carry a
    REAL sample (a stub doesn't certify a recheck was set up)."""
    name = "faithfulness-audit"
    meta = topic_dir / layout.META_DIRNAME
    judged = [cv for cv in results if faithfulness._needs_judge(cv)]  # faithful + (high | cited factual)
    if _audit_has_sample(meta):
        return gatelib.passed(name, "faithfulness_audit sample present")
    if legacy or not judged:
        return gatelib.pending(name, "no faithfulness_audit (legacy / no judged faithful claim)")
    return gatelib.failed(
        name,
        "no non-trivial meta/faithfulness_audit.* but %d judged faithful claim(s) — §0.6.i "
        "假阴审计 未跑（run faithfulness_audit.py，人工抽样复核蕴含 judge）" % len(judged),
    )


# R2-F8: a self-made figure = a LOCAL image (markdown `![](path)` OR html `<img src=path>`),
# any *.svg/*.png/*.jpg path that isn't an http(s) URL — not only `figures/`.
_FIGURE_RE = re.compile(
    r"!\[[^\]]*\]\((?!https?://)([^)]+\.(?:svg|png|jpg|jpeg))\)"
    r"|<img[^>]*\bsrc=[\"']?(?!https?://)([^\"'>\s]+\.(?:svg|png|jpg|jpeg))",
    re.IGNORECASE,
)


def _figure_path(m: "re.Match[str]") -> str:
    return m.group(1) or m.group(2) or "?"


def _check_figure_data(review_text: str, legacy: bool) -> gatelib.CheckResult:
    """§0.6.l 图表数据: a self-made figure (`figures/…`) embeds data whose numbers can
    be visually fabricated. Each such figure must carry a claim_id sidecar on its
    reference line so its data enters claim_evidence_map (mapped to its source [@key] /
    evidence). A self-made figure with no claim_id → FAIL (non-legacy)."""
    name = "figure-data"
    unmapped: list[str] = []
    n_figs = 0
    for line in review_text.splitlines():
        for m in _FIGURE_RE.finditer(line):
            n_figs += 1
            if not claimids.CLAIM_ID_RE.search(line):
                unmapped.append(_figure_path(m))
    if not unmapped:
        return gatelib.passed(name, f"{n_figs} self-made figure(s), each data-mapped" if n_figs else "no self-made figure")
    if legacy:
        return gatelib.pending(name, f".gates_legacy — {len(unmapped)} unmapped figure(s)")
    return gatelib.failed(
        name,
        "自制图数据未进 claim_evidence_map（图引用行缺 claim_id sidecar，spec §0.6.l 防视觉幻觉）: "
        + ", ".join(unmapped[:4]),
    )


def gate(topic_dir: pathlib.Path) -> list[gatelib.CheckResult]:
    store = refs.load(topic_dir)
    review_path = topic_dir / "review.md"
    if store is None:
        return [gatelib.failed("store", f"no references store under {topic_dir}")]
    if not review_path.exists():
        return [gatelib.failed("review", f"review.md not found under {topic_dir}")]
    review_text = review_path.read_text(encoding="utf-8")
    log_text = gatelib.read_text(topic_dir / "research_log.md")
    legacy = _is_legacy(topic_dir)
    used = _used_keys(review_text)

    # Evaluate faithfulness ONCE; fold in the F8 LLM entailment-judge verdicts
    # (meta/judge_verdicts.json: not_entailed→suspect, unclear→needs_review);
    # refresh on-disk artifacts so what we gate on == what reviewer / finalize read.
    fresults = faithfulness.evaluate(store, review_text, topic_dir=topic_dir)
    # _fresh_judgments drops STALE verdicts (claim_id reused over a rewritten clause:
    # the stamped target hash won't match the current claim) so apply + the
    # entailment check both see only verdicts that still bind their current text.
    judgments = _fresh_judgments(topic_dir, fresults)
    faithfulness.apply_judgments(fresults, judgments)
    meta_dir = topic_dir / layout.META_DIRNAME
    faithfulness._write_faithfulness_report(meta_dir, fresults)
    faithfulness._write_claim_evidence_map(meta_dir, fresults)

    results = [
        _check_lint(topic_dir),
        _check_faithfulness_verdicts(fresults, log_text),
        _check_entailment_judged(fresults, judgments, legacy),
        _check_claim_map_coverage(review_text, legacy),
        _check_cross_gap(fresults, review_text, store),
        _check_high_risk_grounding(fresults, legacy),
        _check_evidence_uncertain(topic_dir, fresults, legacy),
        _check_metadata(store, used, review_text, legacy),
        _check_duplicate_cluster(store, used, review_text, legacy),
        _check_second_decomposer(topic_dir, review_text, legacy),
        _check_faithfulness_audit(topic_dir, fresults, legacy),
        _check_figure_data(review_text, legacy),
    ]
    # R2-F5: `.gates_legacy` grandfathers the WHOLE write_gate uniformly — lint /
    # faithfulness / cross-gap also exempt, matching the Stop hook's full-skip (lint
    # additionally has its own `.lint_legacy`). A legacy review is never hard-blocked.
    return gatelib.grandfather(results) if legacy else results


def decide_attempt(blocked: bool, attempts: int, max_rewrites: int) -> tuple[int, int, bool]:
    """N6 rewrite-loop decision (G6). Returns (exit_code, new_attempts, write_report).

    * not blocked            → (0, 0, False)         reset counter, proceed
    * blocked, under cap     → (1, attempts+1, False) retry: rewrite + re-gate
    * blocked, at/over cap   → (3, attempts+1, True)  write failure_report, stop
    """
    if not blocked:
        return 0, 0, False
    new_attempts = attempts + 1
    if new_attempts >= max_rewrites:
        return 3, new_attempts, True
    return 1, new_attempts, False


def _write_failure_report(
    topic_dir: pathlib.Path, results: list[gatelib.CheckResult], attempts: int
) -> pathlib.Path:
    fails = [r for r in results if r.status == "fail"]
    path = layout.reviewers_dir(topic_dir) / "failure_report.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# write_gate failure report",
        "",
        f"write_gate blocked after **{attempts}** rewrite attempt(s) — at the cap "
        "(spec N6 / plan v3.1 G6). Stop and involve the user; do not deliver.",
        "",
        "## Unresolved blocking checks",
    ]
    lines += [f"- **{r.name}**: {r.message}" for r in fails] or ["- (none recorded)"]
    lines += [
        "",
        "## Next",
        "- A human must adjudicate the remaining faithfulness/lint failures, or",
        "- relax the claim (delete the unsupported number / soften the over-claim), then",
        "- reset the counter: delete `meta/write_gate_state.json` and re-run write_gate.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="write_gate — 写作/收尾硬阻断闸 (spec N6/N11 / G2).")
    parser.add_argument("topic_dir")
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--record-attempt",
        action="store_true",
        help="N6 rewrite-loop mode: count failed attempts; at --max-rewrites write "
        "failure_report.md and exit 3 (G6).",
    )
    parser.add_argument("--max-rewrites", type=int, default=3)
    args = parser.parse_args()

    topic_dir = pathlib.Path(args.topic_dir)
    if not topic_dir.is_dir():
        print(f"[ERROR] not a topic dir: {topic_dir}", file=sys.stderr)
        raise SystemExit(2)

    results = gate(topic_dir)
    summary = gatelib.summarize(results)
    blocked = summary["blocked"]

    if args.record_attempt:
        exit_code, attempts, write_report = decide_attempt(
            blocked, _read_attempts(topic_dir), args.max_rewrites
        )
        _write_attempts(topic_dir, attempts)
        summary["attempts"] = attempts
        if write_report:
            report = _write_failure_report(topic_dir, results, attempts)
            summary["failure_report"] = str(report)
    else:
        exit_code = 1 if blocked else 0

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        state = {0: "ok", 1: "BLOCKED", 3: "BLOCKED (rewrite cap → failure_report)"}[exit_code]
        print(f"write_gate: {state}")
        for result in results:
            mark = {"pass": "✓", "fail": "✗", "pending": "…"}[result.status]
            print(f"  {mark} [{result.status}] {result.name}: {result.message}")
        if summary.get("failure_report"):
            print(f"  → {summary['failure_report']}")
    raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
