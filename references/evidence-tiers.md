# 证据分级 — Evidence tiers（跨领域）

挑选正文引用时按"证据强度"取 top（每条观点 1–3 条，见 `references/prose-style.md` §一.2）。
本文件给**默认分级**（医学 / 临床），再说明**其它领域如何用 patch 覆盖**它。
**冲突时 patch 优先**：默认分级是领域无关基线，patch 在其上做 species / discipline 覆盖。

---

## 一、默认分级（医学 / 临床 / 营养 / 运动 / 公共卫生）

大致按此顺序优先（强 → 弱）：

1. **系统综述与 meta-analysis**（systematic review / meta-analysis）
2. **大型 RCT**（randomized controlled trial，随机对照试验）
3. **大型前瞻队列**（major prospective cohort）
4. **临床指南 / 共识声明**（clinical guidelines / consensus statements）
5. **机制 / 动物证据**（mechanistic / animal evidence）——**仅当**人体数据缺失、或用来支撑机制时引用

健康域的一级证据细化：下列机构的指南 / 系统综述**升为一级，与 meta / RCT 并列**——
Cochrane、USPSTF（美国预防服务工作组）、WHO 指南、各专科学会指南（ACC/AHA、ADA、ESC、KDIGO、ESPEN/ASPEN 等）、
GRADE 工作组方法学文件。理由：这些机构已对一手 RCT / 队列做过系统整合与分级，引用它们等同引用一篇高质量 meta。

**"大队列" = human-scale（n≥10⁴ 量级）。** 人群 / 营养流行病学里，"major prospective cohort" 默认指
n≥10⁴（NHANES、UK Biobank、Nurses' Health Study、EPIC 动辄 10⁵–10⁶）。
注意分尺子：硬终点 RCT 常需 n≥10³–10⁴ 才有 power，但代谢 / 营养机制性 RCT 的 n=20–100 是常规体量，**不要**按人群队列尺度判其"偏小"。
这把尺**只适用于人体研究**——其它领域走各自 patch 的相对阈值（见 §三）。

---

## 二、`study_type="other"` ≠ 弱证据（所有领域适用）

references store 里每条 entry 的 `study_type` 字段由 verify 工具按 CrossRef / EuPMC 元数据自动填，
用它来加权 synthesis——**但不要把 `other` 等同于弱证据**。

元数据在**老论文、非医学期刊、机构报告、理工科论文**上粒度不够，无法自动分类，常落到 `other`：
- 一篇 N=10,000 的前瞻队列、一项双盲 RCT，可能因期刊老旧而标 `other`；
- 机构权威报告（NRC / AAFCO / OECD / RPP / CODATA / Codex 等）在 CrossRef 里几乎没有规范化研究类型字段，落 `other` 是常态；
- PRD / PRB / Food Chemistry 这类期刊 CrossRef 默认不做研究类型分类。

**做法**：关键论点若落在 `other` 条目上，**在正文 prose 里手动明说研究类型**
（"N=10,000 的前瞻队列"、"双盲 RCT"、"LIGO 合作组官方报告"、"NRC 2006 权威报告"），
不要仅凭 `study_type` 字段挑选或排除。

---

## 三、其它领域用 patch 覆盖

CLAUDE.md / 本文件的默认分级按**人类临床循证**调校。其它领域写综述前**先读对应 `patches/<domain>.md`**
（EBR skill 的 `patches/` 目录下已带 health / animals / education-psychology / physics / food-science 五份）。
每份 patch 的 frontmatter 给机器可读配置（`evidence_base` / `term_check_overrides` / `default_search_sources`），
正文给一级证据扩展、"大队列"相对阈值、缩写中文对照、领域已知局限。

### 各域一级证据扩展（速查）

| 领域 | 一级证据扩展（升到与 meta/RCT 并列）| "大队列"相对阈值 |
|---|---|---|
| **health**（默认）| Cochrane / USPSTF / WHO / 专科学会指南（ACC-AHA, ADA, ESC, KDIGO, ESPEN 等）| 前瞻队列 n≥10⁴ |
| **animals**（伴侣动物 / 兽医）| NRC *Nutrient Requirements*、AAFCO Official Publication、FEDIAF Guidelines、WSAVA Global Nutrition Guidelines & BCS Charts、Hand's *Small Animal Clinical Nutrition*、AAHA 指南 | 猫/狗前瞻队列 n=80–500 已是大队列；RCT n=10–20 常规；meta 纳入 5–20 篇是天花板 |
| **education-psychology**（教育 / 学习科学 / 教育心理）| OECD PISA、IEA TIMSS/PIRLS（含 Video Study）、NCES NAEP、IES What Works Clearinghouse 评级、AERA / APA / Routledge / Cambridge handbooks、Hattie *Visible Learning* meta-synthesis、PER 验证量表（FCI / FMCE / BEMA）| 双峰：国际评估 n≥10⁵ 常态；班级/校干预 n=100–500 常规、n>1000 已大；课堂 video study n=30–100 节即大样本 |
| **physics**（物理学）| *Reviews of Modern Physics* / *Physics Reports* / *Annual Review of …*、大型合作组官方报告（LIGO/Virgo, ATLAS/CMS/LHCb, Planck, JWST, DESI 等）、Particle Data Group RPP / NIST CODATA、经典教科书（Landau-Lifshitz, Jackson, Peskin-Schroeder, Weinberg 等）、误差分析教材（Taylor, Bevington, Cowan）、milestone 预印本 | **无队列概念**；大型合作组单次发布即代表全球半数主要设施结果，无 second-cohort |
| **food-science**（食品科学）| Codex Alimentarius 标准、JECFA / JMPR 评估、EFSA Scientific Opinions、FDA GRAS Notices / Guidance、USDA-ARS / FoodData Central、NASEM Food & Nutrition Board、AOAC *Official Methods*、ISO TC 34 / 22000、中国 GB 2760/2761/2762/2763/4789/5009 系列、EU Reg 178/2002 等、FSANZ、顶级综述刊 *Annual Review of Food Science and Technology* | 按子领域而定（理化 / 微生物 / 感官各异）；"食品对人/动物效应"主题回落 health 默认 |

### `require_rct_or_meta` 闸门：各域是否要求每个 gap 至少一条 RCT/meta

frontmatter 的 `term_check_overrides.require_rct_or_meta` 决定是否对 **decision / comparison 型 gap**
（及无 gap_type 的 legacy gap）强制"至少 1 条 RCT 或 meta"。各域取值：

| 领域 | `require_rct_or_meta` | 原因 |
|---|---|---|
| **health** | `true` | 临床决策默认要 RCT/meta 背书 |
| **education-psychology** | `true` | 教育干预可做（cluster）RCT，决策类命题要求实验证据 |
| **animals** | `false` | 兽医 RCT 稀缺（伦理 + 成本 + 样本数限制），靠机构报告 / 队列 / 病例系列 |
| **physics** | `false` | 物理无 RCT/cohort 范式，靠实验测量 / 数值模拟 / 理论推导 / 大合作组报告 |
| **food-science** | `false` | "食品本身"主题无 RCT 范式，靠标准方法 / 法规意见 / 感官 panel；"食品对人效应"才回落 health |

补充规则（所有域通用）：
- 此闸门**只对 `decision` / `comparison` 型 gap 强制**；`mechanism` / `safety` / `diagnostic` / `descriptive` / `methodology`
  型 gap 天然依赖队列 / 病例系列 / 横断面 / 机制证据，**已豁免**此条（但仍须每个 gap ≥3 条独立 verified 证据）。
- `evidence_base.primary / secondary / not_applicable` 字段当前是 **advisory metadata**，工具不自动校验——
  它告诉你本域"哪些算一级、哪些不适用"，挑引用时据此加权，但不是 lint 强约束。

### 走其它领域（无现成 patch）

涉及社会学 / 经济学 / 政策 / 材料 / 工程教育 / 环境健康等**无现成 patch** 的领域时，
按需新建 `patches/<domain>.md`，在 research_log Round 1 写明加载了哪个 patch。新 patch 五要素模板：
① 主题命名规则（强制必含的维度）② 一级证据扩展（哪些机构报告 / handbook / 量表升一级）
③ "大队列" 相对阈值 ④ 领域常用缩写中文对照 ⑤ 已知局限（数据库覆盖、跨文化外推等）。

---

## 四、critical appraisal 工具（按需，self-review 时用）

挑定 top cited 证据后，对核心引用做方法学评估（手动评，记入 entry metadata）：

| 研究类型 | 工具 | 一句说明 |
|---|---|---|
| RCT | **RoB 2** (Sterne 2019) | 现行标准，替代旧 Cochrane RoB |
| 非随机干预 | **ROBINS-I** (Sterne 2016) | 涵盖 confounding / selection 等 7 域 |
| 诊断准确性 | **QUADAS-2** (Whiting 2011) | sensitivity / specificity 研究 |
| 综述（评二次综述）| **AMSTAR 2** / **ROBIS** | 综述方法学质量评估 |
| 定性研究 | **CASP qualitative** | 招募 / 数据收集 / 分析透明度 |
| 机制 / in vitro | _无标准_，自定 | 看重复性 / 模型相关性 / 测量精度——这正是 physics / food-science 把 `study_type=other` 升一级的原因 |
| 证据 body 整体 | **GRADE** (Guyatt 2008) | 4 级 + 推荐强度；实操建议表每条建议给 GRADE 等级（High / Moderate / Low / Very Low）|

---

## 五、领域通用的已知局限（写进 §限定与争议）

- CrossRef / OpenAlex 对**中文期刊**覆盖差。需要 CNKI / 万方来源时，用户须手工提供 DOI（或等价标识符），工具链无法自动发现。
- **行业资助与利益冲突**不会自动标注，须在 prose 中按需显式声明。
- **灰色文献**（机构白皮书、政策报告、企业内部 monograph）通常无 DOI，工具链无法自动发现；需用户手工提供全文或机构网址。

领域特异的局限（兽医期刊覆盖、教育 CNKI 缺位、物理预印本版本差异、出版偏差等）见对应 `patches/<domain>.md`。
