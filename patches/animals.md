---
domain: animals
gap_types_dominant: [decision, descriptive, mechanism, safety]
evidence_base:
  primary: [meta, rct, cohort, guideline, nrc_handbook, aafco_standard, fediaf_guideline, wsava_guideline]
  secondary: [case_series, retrospective, expert_consensus, industry_white_paper]
  not_applicable: []
term_check_overrides:
  require_rct_or_meta: false
  require_primary_evidence_per_gap: 1
default_search_sources: [crossref, semantic_scholar]
protocol_defaults:
  inclusion: "家养/同伴动物（猫/犬/兔等）同行评议研究 + 主要学会指南（NRC/AAFCO/FEDIAF/WSAVA/AAHA/AVDC）；英文为主；物种相对'大队列'阈值（猫犬 n=80–500 即大样本）"
  exclusion: "纯生产/实验诱导模型且无临床外推、其它物种专属且无目标物种数据、无 DOI 厂商白皮书（除非用户手供）"
  outcomes: "以临床/营养结局为准（疾病分级、患病率、能量/营养需求、围术期并发症）；study_type=other ≠ 弱证据，按 abstract 手判设计"
---

# 主题补丁 — 伴侣动物 / 小动物兽医营养

适用于一切以非人物种（猫 / 狗 / 兔 / 反刍动物 / 啮齿等）为研究对象的综述。本节是对 CLAUDE.md 中 **Evidence hierarchy、适用范围、Suggested structure** 等节的 **species-conditional 覆盖**，冲突时本节优先。

**已实证可用** — 参见：
- `reviews/成年绝育猫体重评估与每日能量需求/`
- `reviews/菊酯类驱蚊产品的人猫安全性与缓释器械有效性/`
- `reviews/成年绝育室内猫晨间爆发活动成因与缓解/`

## 主题命名（强制）

主题目录名必须显式包含 **物种 + 生命阶段 + 性别 / 绝育状态**。同一物种在生命阶段（幼 / 成年 / 老龄）与性激素水平（完整 / 绝育 / 怀孕 / 哺乳）下的能量、营养、疾病谱差异巨大，遗漏任一维度会让文献检索与综述结论失效。

- ✅ `成年绝育猫体重评估与每日能量需求`、`8 周龄断奶仔猫蛋白质需求`、`未绝育成年公犬肛门腺疾病`
- ❌ `猫的能量需求`、`狗减肥`、`兔子掉毛`

## 一级证据扩展

兽医营养与小动物医学领域，下列机构权威报告与教材**升为一级证据，与 meta / RCT 并列**（覆盖 CLAUDE.md §Evidence hierarchy 的默认排序）：

- **NRC** *Nutrient Requirements of Dogs and Cats* (2006, National Academies Press)
- **AAFCO** Official Publication（年度更新；犬猫食品营养充足性裁定标准）
- **FEDIAF** Nutritional Guidelines for Complete and Complementary Pet Food for Cats and Dogs
- **WSAVA** Global Nutrition Guidelines & Body Condition Score Charts
- **Hand's** *Small Animal Clinical Nutrition* (5th ed., 2010, Mark Morris Institute)
- **AAHA** Nutritional Assessment Guidelines / Weight Management Guidelines

理由：兽医领域 RCT 稀缺（伦理 + 成本 + 样本数限制），这些机构报告综合了内部未发表实验、跨研究 meta、专家共识，是**该领域的"教科书共识"**。猫狗营养主题中直接引用 NRC 2006 与引用一篇 meta 同级，不要因"看起来像 guideline"而下调权重。`study_type="other"` 标签同样适用 —— NRC / AAFCO / FEDIAF 在 CrossRef 元数据里通常没有规范化研究类型字段，落到 `other` 是正常现象，不要据此排除。

## "大队列" 是 species-relative

"Major prospective cohort" 的体量阈值要按物种调整。**猫 / 狗主题下，n=80–500 的前瞻队列已是大队列**（参考已收录：Thes 2015 n=80 client-owned cats、Bermingham 2010 meta 115 处理组、Scarlett 1998 n=1,457、Lund 2005 n=8,159 已属顶级稀有样本）。不要套用人队列 n≥10⁴ 的尺子在 prose 里写"样本偏小"——除非确实是 n<30 的临床报告，否则按相对体量评价。

同理：

- 猫 / 狗 RCT n=10–20 是常规体量（Appleton 2001 n=16 实验性增重 RCT 是该领域经典）；不要按"人 RCT n=16 即 underpowered"标准否定
- 兽医 meta 纳入研究常 5–20 篇（vs 人 meta 经常 50+）；这是领域天花板，不是质量缺陷

## 兽医常用缩写中文对照

首次出现仍按 CLAUDE.md §Prose style 给中文（"BCS（Body Condition Score，体况评分）"），本表仅作快速参考：

| 缩写 | 全称 | 中文 |
|---|---|---|
| BCS / MCS | Body / Muscle Condition Score | 体况 / 肌肉状况评分 |
| RER / MER / DER | Resting / Maintenance / Daily Energy Requirement | 静息 / 维持 / 每日能量需求 |
| ME | Metabolizable Energy | 代谢能 |
| DLW / IC | Doubly Labeled Water / Indirect Calorimetry | 双标水法 / 间接量热法 |
| FLUTD / FIC | Feline Lower Urinary Tract Disease / Idiopathic Cystitis | 猫下泌尿道疾病 / 特发性膀胱炎 |
| FHL | Feline Hepatic Lipidosis | 猫肝脂质沉积症 |
| CKD / DM | Chronic Kidney Disease / Diabetes Mellitus | 慢性肾病 / 糖尿病 |
| OHE | Ovariohysterectomy | 卵巢子宫切除（母猫绝育） |
| AAFCO / FEDIAF / WSAVA / NRC | 见上 §一级证据扩展 | 美国饲料管理协会 / 欧洲宠物食品工业联合会 / 世界小动物兽医协会 / 美国国家研究委员会 |

## 已知局限（伴侣动物专属）

- 兽医专科 newsletter（NAVC Clinician's Brief、Compendium）、宠物食品厂商技术白皮书（Royal Canin / Hill's / Purina 内部 monograph）、AAFCO / FEDIAF 内部技术报告通常无 DOI，工具链无法自动发现；用户需手工提供文献全文或机构网址。
- 主流兽医期刊（JFMS / JVIM / AJVR / BJN / J Anim Physiol Anim Nutr）由 CrossRef + Semantic Scholar 覆盖完整，无需额外配置 PubMed source。
