---
domain: health
gap_types_dominant: [decision, comparison, mechanism, safety]
evidence_base:
  primary: [meta, rct, large_cohort, guideline]
  secondary: [small_cohort, case_control, case_series]
  not_applicable: []
term_check_overrides:
  require_rct_or_meta: true
  require_primary_evidence_per_gap: 1
default_search_sources: [crossref, semantic_scholar]
---

# 主题补丁 — 人体健康 / 医学 / 营养 / 运动

适用于一切以**人**为研究对象的循证综述：生理 / 生化、临床、营养学、运动科学、公共卫生。本补丁是项目的**默认基线**——CLAUDE.md 的通用工作流就是按人类临床循证调校的，本文件把那套默认的「证据分级 / 大队列阈值 / 缩写习惯 / 已知局限」**显式化并集中到一处**，让健康与其它领域在补丁表里对称。其它领域（animals / physics / …）的补丁是对本基线的 species/discipline 覆盖。

> **frontmatter 说明**：本文件的 frontmatter 与 `tools/lib/patches.py` 的 `DEFAULT_PATCH` 逐字等价——它就是代码里那份默认配置的人类可读副本。`term_check` 读到 `require_rct_or_meta: true`（健康域保留「每个 decision/comparison 型 gap ≥1 条 RCT/meta」闸门）。改这里要同步改 `DEFAULT_PATCH`，反之亦然。

**已实证可用** — 参见：

- `reviews/超重男性减重方案/`
- `reviews/钠每日摄入推荐与超量补救/`
- `reviews/减脂训练循证决策/`
- `reviews/咖啡因戒断反应的神经机制与缓解策略/`

## 主题命名（强制）

主题目录名必须把**人群限定维度**写进去——能量、营养、风险阈值、推荐量在不同人群间差异巨大，遗漏维度会让检索与结论失效。按主题相关性显式包含下列之一或多个：**生命阶段（婴幼 / 儿童 / 青少年 / 成年 / 老年 / 孕产）、性别、基线状态（超重 / 正常 / 临床诊断）、训练 / 暴露状态**。

- ✅ `超重成年男性减重期的能量赤字与蛋白质摄入`、`抗阻训练者减重期蛋白质需求`、`孕中期铁补充的剂量与上限`
- ❌ `减肥`、`蛋白质吃多少`、`补铁`

## 一级证据（默认分级 + 临床细化）

证据排序沿用 CLAUDE.md §Evidence hierarchy 默认：**系统综述 / meta > 大 RCT > 大前瞻队列 > 临床指南 / 共识 > 机制 / 动物**。健康域的细化：

- **下列机构的指南 / 系统综述升为一级证据，与 meta / RCT 并列**：Cochrane Systematic Reviews、USPSTF（美国预防服务工作组）、WHO 指南、各专科学会指南（ACC/AHA、ADA、ESC、KDIGO、ESPEN/ASPEN 等）、GRADE 工作组方法学文件。理由：这些机构已对一手 RCT/队列做过系统整合与分级，引用它们等同引用一篇高质量 meta。
- **`require_rct_or_meta: true`**：每个 `decision` / `comparison` 型 gap（及无 gap_type 的 legacy gap）至少 1 条 RCT 或 meta；`mechanism` / `safety` / `diagnostic` / `descriptive` 型 gap 天然依赖队列 / 机制 / 病例系列，已豁免此条（仍须 ≥3 verified）。
- **`study_type="other"` ≠ 弱证据**：老论文、非医学期刊在 CrossRef / EuPMC 元数据里粒度不够，常落 `other`。关键论点落在 `other` 条目时，正文 prose 手动明说研究类型（"N=10,000 的前瞻队列"、"双盲 RCT"）。

## "大队列" 是 human-scale（n≥10⁴ 量级）

人群流行病学 / 营养流行病学里，**"Major prospective cohort" 默认指 n≥10⁴ 量级**（NHANES、UK Biobank、Nurses' Health Study、EPIC 等动辄 10⁵–10⁶）。在 prose 里评价样本量时用这把尺：

- 前瞻队列 n<10³ 在人群研究里偏小，结论需谨慎外推；n≥10⁴ 才算"大队列"。
- 临床 RCT 的"大"按结局而定：硬终点（死亡 / 心血管事件）试验常需 n≥10³–10⁴ 才有 power；代谢 / 营养干预的机制性 RCT n=20–100 是常规体量，不要按人群队列尺度判其"偏小"。
- **这把尺只适用于人体研究**——伴侣动物（n=80–500 已是大队列）、physics（无队列概念）走各自补丁。

## 临床常用缩写中文对照

首次出现仍按 CLAUDE.md §Prose style 第 11 条给中文（"hsCRP（高敏 C 反应蛋白）"），本表仅作快速参考：

| 缩写 | 全称 | 中文 |
|---|---|---|
| RCT / SR / MA | Randomized Controlled Trial / Systematic Review / Meta-Analysis | 随机对照试验 / 系统综述 / 荟萃分析 |
| RR / OR / HR | Relative Risk / Odds Ratio / Hazard Ratio | 相对风险 / 比值比 / 风险比 |
| CI / NNT / NNH | Confidence Interval / Number Needed to Treat / to Harm | 置信区间 / 需治疗人数 / 需伤害人数 |
| ITT / PP | Intention-To-Treat / Per-Protocol | 意向性治疗 / 符合方案 |
| BMI / WC / WHR | Body Mass Index / Waist Circumference / Waist-Hip Ratio | 体质指数 / 腰围 / 腰臀比 |
| TDEE / BMR / RMR | Total Daily Energy Expenditure / Basal / Resting Metabolic Rate | 每日总能量消耗 / 基础 / 静息代谢率 |
| hsCRP / HbA1c | high-sensitivity C-Reactive Protein / Glycated Hemoglobin | 高敏 C 反应蛋白 / 糖化血红蛋白 |
| iAUC / GSRS | incremental Area Under Curve / Gastrointestinal Symptom Rating Scale | 增量曲线下面积 / 胃肠道症状评分量表 |
| eGFR / BP | estimated Glomerular Filtration Rate / Blood Pressure | 估算肾小球滤过率 / 血压 |

## 已知局限（健康域）

- **中文期刊覆盖差**：CrossRef / OpenAlex / Semantic Scholar 对 CNKI / 万方收录的中文临床与营养文献覆盖不全。需要这些来源时，用户必须手工提供 DOI 或等价标识符，工具链无法自动发现。
- **行业资助与利益冲突不自动标注**：营养 / 补剂 / 食品 / 制药领域的厂商资助研究常见，工具链不会自动提示 COI，须在 prose 中按需显式声明（"该 RCT 由某乳企资助"）。
- **灰色文献无 DOI**：膳食指南 PDF、政府营养报告、机构白皮书通常无 DOI，无法自动发现；用户需手工提供全文或机构网址。
- **个体化外推边界**：综述结论是人群层面证据；落到具体个体的 kcal / 蛋白质克数只能作为"实操建议"小节的应用示例，且必须由综述证据支撑（见 CLAUDE.md §适用范围）。
