---
domain: education-psychology
gap_types_dominant: [descriptive, decision, mechanism]
evidence_base:
  primary: [meta, rct, cluster_rct, ies_wwc_rating, pisa_timss_report, naep_report, hattie_meta_synthesis, aera_handbook, apa_handbook]
  secondary: [observational, case_study, qualitative_synthesis, classroom_video_study]
  not_applicable: []
term_check_overrides:
  require_rct_or_meta: true
  require_primary_evidence_per_gap: 1
default_search_sources: [crossref, semantic_scholar]
protocol_defaults:
  inclusion: "学习者/课堂/教师研究：meta、(cluster) RCT/准实验、大型纵向队列、PER 与教育心理实证、WWC/AERA/APA handbook；中英文（CNKI 需手供标识）"
  exclusion: "纯思辨/政策评论无实证、单一轶事课堂报告、无效度的自编量表"
  outcomes: "以学习/发展结局为准（成绩、迁移、动机、概念转变；效应量 d/g/Hedges）"
---

# 主题补丁 — 教育 / 学习科学 / 发展与教育心理学

适用于一切以学习者、课堂、教师、教学法、认知发展、教育政策为研究对象的综述。本节是对 CLAUDE.md 中 **Evidence hierarchy、适用范围、Suggested structure** 等节的 **domain-conditional 覆盖**，冲突时本节优先。

**已实证可用** — 参见：
- `reviews/东亚初二学生心理特点与物理课堂教学策略/`

## 主题命名（强制）

主题目录名必须显式包含 **学段 + 学科/领域 + 文化或样本背景**（必要时加年龄段）。学段与文化差异会让认知发育、动机模式、课堂结构、教学方法的结论完全反向，遗漏任一维度会让综述不可外推。

- ✅ `东亚初二学生心理特点与物理课堂教学策略`、`小学高年级英语阅读理解阅读策略干预`、`美国高中 AP 物理 inquiry vs direct instruction`
- ❌ `学生心理`、`物理教学`、`怎么学英语`

学段对照（中文 → 大致年龄 → 国际惯用名）：

| 中文 | 年龄 | 国际名 |
|---|---|---|
| 学前 / 幼儿 | 3–6 | preschool / early childhood |
| 小学低 / 中 / 高年级 | 6–9 / 9–11 / 11–12 | primary / elementary G1–G6 |
| 初中（初一/初二/初三 ≈ G7/G8/G9） | 12–15 | middle / lower-secondary |
| 高中（高一/高二/高三 ≈ G10/G11/G12） | 15–18 | high / upper-secondary |
| 大学 / 研究生 | 18+ | undergraduate / graduate |

## 一级证据扩展

教育与教育心理学领域，下列国际组织报告、领域 handbook 与高被引 meta-synthesis **升为一级证据，与同行评议 meta-analysis 并列**（覆盖 CLAUDE.md §Evidence hierarchy 的默认排序）：

- **OECD PISA** 报告（每三年一波，含 background questionnaire 国际比较）
- **IEA TIMSS / PIRLS** 报告（含 Video Study 系列，是跨国课堂结构定量比较的事实基准）
- **NCES NAEP**（美国全国教育进展评估）报告
- **IES What Works Clearinghouse** 系统评级（对教育干预的 RCT / QED 做证据等级裁定）
- **AERA Handbook** 系列（*Handbook of Research on Teaching*、*Handbook of Educational Psychology*、*Handbook of the Learning Sciences*）
- **Hattie 系列 meta-synthesis**（*Visible Learning* 2009 + 后续更新；effect size d 框架在教育领域是 lingua franca）
- **Routledge / Cambridge** Handbooks of Learning Sciences / Educational Psychology
- **APA Handbook of Educational Psychology**
- 物理教育研究：**PER (Physics Education Research)** 与 **AAPT** 的 *Common Core Tests*（FCI / FMCE / BEMA）属于经过严格 IRT 验证的领域基准量表

理由：教育领域同行评议 RCT 多为单校 / 单地区 / 单干预（generalizability 有限），上述机构报告与 handbook 综合数万样本、跨国跨年代复制结果、专家共识，对宏观命题（如"东亚课堂 vs 西方课堂"、"探究式 vs 直接教学"）比单条 RCT 更可信。`study_type="other"` 标签同样适用 —— OECD / NCES / 章节书在 CrossRef 元数据里通常无规范化研究类型字段，不要据此排除。

## "大队列" 是 domain-relative

教育研究的样本量分布是双峰的：

- **国际/全国大型评估**：PISA 单波约 60 万学生跨 80+ 国、TIMSS 单波约 60 万学生、NAEP 单年约 30 万学生。涉及这些数据集做二次分析时，**n ≥ 10⁵ 是常态而非例外**。
- **班级/学校干预研究**：n=100–500 学生（5–20 班）就是教学法 RCT / QED 的常规体量；n>1000 已是大样本。
- **课堂观察 / video study**：n=30–100 节课就是大样本（TIMSS 1999 Video Study 七国约 638 节）。
- **神经/认知实验**：青少年 fMRI 样本 n=20–60 是常规，n>100 已罕见。

不要套用"医学队列 n≥10⁴"或"工业心理 n≥1000"的尺子在 prose 里写"样本偏小"。按相对体量评价。

教育领域 RCT 的另一特殊性：**cluster RCT 是主流**（学校或班级层面随机化），效应量计算需要 ICC（intra-class correlation）校正；实际"独立 n"是学校/班级数而非学生数。看到"N=2,000 学生" 但只有 8 个班时要在 prose 里说清楚。

## 教育/心理学常用缩写中文对照

首次出现仍按 CLAUDE.md §Prose style 给中文（"CLT（Cognitive Load Theory，认知负荷理论）"），本表仅作快速参考：

| 缩写 | 全称 | 中文 |
|---|---|---|
| PISA / TIMSS / PIRLS / NAEP | (见上 §一级证据扩展) | 国际学生评估 / 国际数学与科学趋势 / 国际阅读素养 / 全国教育进展评估 |
| CLT / WM | Cognitive Load Theory / Working Memory | 认知负荷理论 / 工作记忆 |
| PER | Physics Education Research | 物理教育研究 |
| FCI / FMCE / BEMA | Force Concept Inventory / Force and Motion Conceptual Evaluation / Brief Electricity and Magnetism Assessment | 力概念量表 / 力与运动概念评估 / 电磁简评 |
| PI / IE / MBL | Peer Instruction / Interactive Engagement / Microcomputer-Based Laboratory | 同伴互讲 / 互动参与 / 微机辅助实验 |
| PBL / IBL / DL | Problem-Based / Inquiry-Based / Direct Learning | 问题导向 / 探究式 / 直接教学 |
| PCK / TPACK | Pedagogical / Technology-Pedagogy-and-Content Knowledge | 学科教学知识 / 技术-学科-教学知识 |
| ZPD | Zone of Proximal Development (Vygotsky) | 最近发展区 |
| SDT | Self-Determination Theory | 自我决定理论 |
| SES | Socioeconomic Status | 社会经济地位 |
| ICC / HLM / SEM / IRT / LTA | Intra-class Correlation / Hierarchical Linear Model / Structural Equation Model / Item Response Theory / Latent Transition Analysis | 类内相关 / 多层线性模型 / 结构方程模型 / 项目反应理论 / 潜在转移分析 |
| DLPFC / mPFC / NAcc / OFC / VS | Dorsolateral / Medial PFC / Nucleus Accumbens / Orbitofrontal Cortex / Ventral Striatum | 背外侧 / 内侧前额叶 / 伏隔核 / 眶额皮层 / 腹侧纹状体 |
| RCT / QED / cluster RCT | Randomized Controlled Trial / Quasi-Experimental Design | 随机对照试验 / 准实验设计 / 群组随机试验 |
| AERA / APA / NCES / IES / IEA / OECD | (各国际/国家教育研究机构) | 美国教育研究会 / 美国心理学会 / 国家教育统计中心 / 教育科学研究所 / 国际教育成就评价协会 / 经济合作与发展组织 |

## 已知局限（教育/心理专属）

- **CNKI（中国知网）与万方对 CrossRef / Semantic Scholar 几乎不可见**。中国大陆教育学、教学论、心理学的本土研究主流刊物（《课程·教材·教法》《心理学报》《教育研究》《物理教学》等）大多无国际 DOI，工具链无法自动发现。需要用户手工提供 DOI / 全文路径，或换用英文期刊（如 *Educational Studies* / *Asia Pacific Education Review* / *International Journal of Science Education*）的中国子样本研究。
- **日韩本土教育研究**也部分存在类似问题（《教育心理学研究》《日本数学教育学会誌》等），但比中文略好（J-Stage 收录部分 DOI）。
- **教育出版偏差严重**：正向干预效应被夸大 2–3 倍是常态；优先看预注册研究、独立复制、Hattie 系列再综合（已剔除部分膨胀）。What Works Clearinghouse 的 "meets standards without reservations" 评级是较硬的过滤。
- **跨文化外推风险大**：欧美样本（多为 WEIRD：Western, Educated, Industrialized, Rich, Democratic）的研究结论搬到东亚需在 prose 里明示边界。"东亚"本身也非同质块（中/日/韩/港/台/新加坡之间差异可大可小）。
- **同伴效应 / 课堂动力的生态效度局限**：BART、yLG、模拟驾驶等实验范式与真实课堂差距大；少有研究在真实物理/数学课堂直接测同伴效应方向，理论框架（Pekrun control-value / Ames goal structure）需要谨慎外推。
- **TIMSS Video Study 主要是数学课**：1995 三国与 1999 七国的官方 Video Study 数据集都是数学课堂，物理/科学课堂的可比定量证据相对薄弱（TIMSS-R Science Video Study 数据集 Roth 2006 NCES 报告无 CrossRef DOI）。
- **历史价值观漂移**：Hamamura 2011 等跨年代研究表明东亚个人/集体主义价值观正在向个人主义漂移。把 1990s 经典文献的"东亚课堂"图像搬到 2020s 学生要审慎。
