---
domain: physics
gap_types_dominant: [methodology, descriptive, mechanism]
evidence_base:
  primary: [experimental_measurement, numerical_simulation, theoretical_derivation, large_collaboration_report, rpp_codata_review, textbook_consensus, prl_letter, nature_phys_paper]
  secondary: [benchmark_evaluation, replication_paper, arxiv_preprint]
  not_applicable: [rct, cohort, meta_of_rcts]
term_check_overrides:
  require_rct_or_meta: false
  require_primary_evidence_per_gap: 1
default_search_sources: [semantic_scholar, crossref]
---

# 主题补丁 — 物理学

适用于一切以**物理现象本身**为研究对象的综述：经典力学 / 电动力学 / 热力学与统计物理 / 量子力学 / 量子场论 / 相对论 / 凝聚态 / 原子分子光学 / 等离子体 / 粒子物理 / 天体物理 / 宇宙学 / 计算物理。本节是对 CLAUDE.md 中 **Evidence hierarchy、适用范围、Suggested structure** 等节的 **domain-conditional 覆盖**，冲突时本节优先。

**与 education-psychology 的边界**：研究"物理本身"（如"中子星合并 GW 信号的电磁对应体"、"莫尔超晶格的关联电子相"）用本 patch；研究"如何教物理 / 学生如何学物理"（如 FCI、Peer Instruction、概念转变）走 `patches/education-psychology.md`（PER 部分）。两个 patch 在量表名称（FCI / FMCE / BEMA）上有少量重叠 — 看研究目的决定走哪个。

**与 health 的边界**：医学物理（MRI / 超声 / 放疗剂量学）默认走 health（CLAUDE.md 默认），仅当主题聚焦在物理原理本身（如"压缩感知 MRI 重建的傅立叶采样下界"）时走 physics patch。

**已实证可用** — 暂无 exemplar（首次启用时记录到本表）。

## 主题命名（强制）

主题目录名必须显式包含 **子领域 + 物理标度 + 实验/理论标签**（必要时加体系名）。物理学跨越 30 个量级的能标与长度标度，遗漏标度或实验/理论标签会让文献检索完全失焦。

- ✅ `中子星合并引力波信号的电磁对应体探测（实验）`、`二维莫尔超晶格关联电子相图（理论）`、`量子计算超导比特相干时间退相干机制`、`暗物质直接探测实验的 spin-independent 截面限制`
- ❌ `黑洞`、`量子力学`、`超导`、`暗物质`

子领域速查（中文 → arXiv 主分类）：

| 领域 | arXiv 主分类 | 期刊主阵地 |
|---|---|---|
| 高能粒子理论 / 唯象 | hep-th / hep-ph | JHEP, NPB, PRD, PLB |
| 高能粒子实验 | hep-ex | PRL, PRD, JHEP, EPJC |
| 核物理理论 / 实验 | nucl-th / nucl-ex | PRC, NPA, PLB, PRL |
| 引力 / 宇宙学 | gr-qc, astro-ph.CO | PRD, JCAP, CQG, ApJ |
| 天体物理 | astro-ph.{GA, SR, HE, EP, IM} | ApJ, MNRAS, A&A, Nature Astron |
| 凝聚态理论 / 实验 | cond-mat.{str-el, mes-hall, supr-con, soft, stat-mech, mtrl-sci} | PRL, PRB, PRX, Nature Mater, Nat Phys |
| 量子物理 / AMO / 量子信息 | quant-ph | PRA, PRX Quantum, NJP, Nature Phys |
| 数学物理 | math-ph | CMP, JMP, Lett Math Phys |
| 计算物理 / 数值方法 | physics.comp-ph | Comp Phys Commun, JCP |
| 统计与流体 | physics.flu-dyn, nlin.{CD,PS} | PRF, JFM, Chaos |
| 光学 / 等离子 / 加速器 | physics.{optics, plasm-ph, acc-ph} | Opt Lett, PRApp, PRAB |

## 一级证据扩展

物理学领域 RCT/cohort 范式**不适用**，证据等级按下列调整。下列来源**升为一级证据，与同行评议 PRL / Nature Phys 论文并列**（覆盖 CLAUDE.md §Evidence hierarchy 的默认排序）：

- **顶级综述刊物**：*Reviews of Modern Physics* (RMP)、*Physics Reports* (Phys Rep)、*Annual Review of {Nuclear and Particle Science, Astronomy and Astrophysics, Condensed Matter Physics}*
- **大型合作组官方报告**：LIGO/Virgo/KAGRA 协作组、ATLAS/CMS/LHCb（LHC）、Belle II、Planck、JWST、IceCube、DESI、HERA 等。这些"集体作者论文"虽然单作者 200–3000 人，**仍是该子领域最高等级实证**，不要因作者数多而降权
- **标准参考资料**：Particle Data Group (*Review of Particle Physics*, RPP, 每两年更新)、NIST CODATA 物理常数（每四年更新）、Particle Data Group 的 Cosmology Review、APS / IUPAP 政策白皮书
- **经典教科书**：Landau-Lifshitz 全套（理论物理 1–10）、Goldstein *Classical Mechanics*、Jackson *Classical Electrodynamics*、Sakurai *Modern Quantum Mechanics*、Peskin-Schroeder *QFT*、Weinberg *Cosmology* / *QFT*、Ashcroft-Mermin *Solid State Physics*、Mahan *Many-Particle Physics*、Polchinski *String Theory*、Carroll *Spacetime and Geometry*
- **实验数据分析与统计教科书**：Bevington-Robinson *Data Reduction and Error Analysis for the Physical Sciences*、Taylor *An Introduction to Error Analysis*、Hughes-Hase *Measurements and their Uncertainties*、Cowan *Statistical Data Analysis*、Lyons *Statistics for Nuclear and Particle Physicists*；数理统计经典如 Gauss-Markov 原定理、Aitken 1935 GLS、Greene / Wooldridge / Davidson-MacKinnon 计量教材也归此类
- **物理教学论文期刊**：*American Journal of Physics* (AJP)、*European Journal of Physics* (EJP)、*Physical Review Physics Education Research* (PRPER)、*Physics Education*。检索物理实验教学话题（χ² 拟合优度、误差传播、概念转变实验）时**优先用 venue 限定 + Semantic Scholar**，CrossRef 默认权重对这些刊物的话题命中率低
- **领域里程碑预印本**：诺奖级或公认 milestone 的预印本（如 BCS 原始论文、Higgs / Englert-Brout、AdS/CFT Maldacena 1997 等）即使早期版本与发表版本差异较大，引用时应注明 arXiv 版本

理由：物理研究的"复制"逻辑与生命科学完全不同 — 大型合作组单次发布即代表全球该方向半数主要实验设施的结果，无 second-cohort 概念；RPP 与 CODATA 是事实标准而非"指南建议"。`study_type="other"` 对物理论文几乎是默认（CrossRef 元数据对 PRD / PRB 不做研究类型分类），不要据此排除。

## arXiv 与 CrossRef 的混合处理

物理领域的事实是 **arXiv 预印本先行、期刊发表滞后 6–18 个月**。具体规则：

- **已发表论文**：优先用期刊 DOI（CrossRef 覆盖完整），prose 中可附 arXiv ID 方便读者
- **未发表预印本（仅 arXiv）**：CrossRef 不索引 arXiv，需要手工 verify。arXiv ID 格式 `arXiv:YYMM.NNNNN` 或老格式 `arXiv:hep-th/0501001`，可作为 identifier
- **极新工作（< 3 个月）**：通常只有 arXiv 版本；正文应注明"arXiv 预印本（截至 YYYY-MM-DD 未正式发表）"作为限定
- **重要 milestone 预印本**：发表版本若与预印本差异显著（如增删章节），引用时同时给两个 identifier 并在 prose 里点明使用了哪一版

## "大队列" 不直接套用 — 物理 "sample" 是 events / shots / runs

- **粒子物理实验**：LHC 一次 run 约 10¹⁵ 碰撞 events；单个 ATLAS / CMS 分析的"signal events" 1000–10⁶ 已是常态。"sample size" 用 integrated luminosity（fb⁻¹）而非 n
- **引力波实验**：LIGO/Virgo O3 + O4 累计 GW 事件 ~300 个；单次事件即可发 PRL。这不等于 "n=1 是 underpowered"
- **凝聚态实验**：单晶样品 n=1 + 多温度/磁场扫描已是常规分析单位；"n=10 晶体" 已是较大样本
- **计算物理**：DFT 模拟规模常报 "system size = N atoms" 与 "k-point grid"；MD 报"timesteps × particles"；MC 报"sweeps × spins"
- **天体观测**：单源研究 n=1（如 GRB 221009A）即可顶级期刊；统计样本 n=10²–10⁴（如 BOSS LRG）已是大样本

不要套用医学 / 教育的 n≥1000 标尺。按物理实际单位描述。

## 物理常用缩写中文对照

首次出现仍按 CLAUDE.md §Prose style 给中文（"GW（gravitational wave，引力波）"），本表仅作快速参考：

| 缩写 | 全称 | 中文 |
|---|---|---|
| SM / BSM | Standard Model / Beyond the Standard Model | 标准模型 / 超出标准模型 |
| QCD / QED / EW | Quantum Chromo / Electro Dynamics / Electroweak | 量子色动力学 / 量子电动力学 / 电弱 |
| QM / QFT | Quantum Mechanics / Field Theory | 量子力学 / 量子场论 |
| GR / SR | General / Special Relativity | 广义 / 狭义相对论 |
| EFT | Effective Field Theory | 有效场论 |
| RG / IR / UV | Renormalization Group / Infrared / Ultraviolet | 重整化群 / 红外 / 紫外 |
| CMB / LSS | Cosmic Microwave Background / Large-Scale Structure | 宇宙微波背景 / 大尺度结构 |
| GW / BBH / BNS | Gravitational Wave / Binary Black Hole / Binary Neutron Star | 引力波 / 双黑洞 / 双中子星 |
| DM / DE | Dark Matter / Dark Energy | 暗物质 / 暗能量 |
| SUSY / MSSM | Supersymmetry / Minimal Supersymmetric Standard Model | 超对称 / 最小超对称标准模型 |
| CP / CPT | Charge-Parity / + Time symmetry | 电荷-宇称（+ 时间）对称性 |
| CKM / PMNS | Cabibbo-Kobayashi-Maskawa / Pontecorvo-Maki-Nakagawa-Sakata 矩阵 | 夸克 / 中微子混合矩阵 |
| AdS/CFT | Anti-de Sitter / Conformal Field Theory duality | 反德西特-共形场论对偶 |
| DFT / HF / CC | Density Functional Theory / Hartree-Fock / Coupled Cluster | 密度泛函 / 哈特利-福克 / 耦合簇 |
| MD / MC / TB | Molecular Dynamics / Monte Carlo / Tight Binding | 分子动力学 / 蒙特卡洛 / 紧束缚 |
| BEC / BCS / FFLO | Bose-Einstein Condensate / Bardeen-Cooper-Schrieffer / Fulde-Ferrell-Larkin-Ovchinnikov | 玻色-爱因斯坦凝聚 / BCS 超导 / FFLO 配对 |
| FQH / IQH / QSH | Fractional / Integer / Quantum Spin Hall | 分数 / 整数 / 量子自旋霍尔 |
| TI / TSC / TCI | Topological Insulator / Superconductor / Crystalline Insulator | 拓扑绝缘体 / 拓扑超导体 / 拓扑晶体绝缘体 |
| 2DEG / 2DES | Two-Dimensional Electron Gas / System | 二维电子气 / 系统 |
| ARPES / STM / TEM | Angle-Resolved Photoemission / Scanning Tunneling / Transmission Electron Microscopy | 角分辨光电子能谱 / 扫描隧道显微 / 透射电镜 |
| LHC / LIGO / Virgo / Planck / JWST / IceCube / Belle II / KAGRA / DESI | 各大型实验设施 | 大型强子对撞机 / 激光干涉引力波天文台 / Virgo 干涉仪 / 普朗克卫星 / 詹姆斯韦布望远镜 / 冰立方 / Belle II / KAGRA / 暗能量光谱仪 |
| RPP / CODATA | Review of Particle Physics / Committee on Data | 粒子数据评论 / 国际科技数据委员会 |
| RMP / PRL / PRX / PRD / PRB / NPB / JHEP / ApJ / MNRAS | (顶级期刊) | 现代物理评论 / 物理评论快报 / X / D / B / 核物理 B / 高能物理 / 天体物理 / 皇家天文学会月刊 |

## 已知局限（物理专属）

- **arXiv 预印本无 CrossRef DOI**。工具链默认走 CrossRef，未发表的 arXiv 论文需要手工提供 arXiv URL + 主要元数据（标题 / 作者 / 年份）。已发表论文走期刊 DOI，arXiv ID 作为辅助标识。
- **中文物理教学与普及类刊物覆盖差**：《物理学报》（Acta Physica Sinica）/《Chinese Physics B/Letters》CrossRef 覆盖良好；但《物理》《大学物理》《物理通报》等教学/普及型刊物 DOI 稀缺，需用户手工提供。
- **极新结果（< 3 个月）几乎仅有 arXiv 版本**，引用必须注明"arXiv 预印本，截至 YYYY-MM-DD 未正式发表"。
- **大型合作组论文作者列表 truncate**：ATLAS / CMS / LIGO / Planck 等的作者数从数百到数千；CrossRef 元数据中作者字段可能只保留前 N 个。引用时用 "(LIGO Scientific and Virgo Collaboration, 2023)" 或 "(ATLAS Collaboration, 2024)" 集体作者形式即可。
- **教科书引用**：Landau-Lifshitz / Jackson / Sakurai 等经典教材只有 ISBN 没有 DOI，工具链无法 verify。需要在 references store 里手工添加（按"标准教材知识"处理，参考 CLAUDE.md §Core principles 第 6 条）。
- **理论与实验范式不对称**：理论文章常无 "sample"，证据等级看的是数学严密性 + 与已知极限的一致性 + 是否提供可检验预言。综述里不要硬套实验范式的 effect size 语言。
- **争议性话题（如弦论 vs LQG / 暗物质 vs MOND / 多重宇宙）**：领域内长期分歧不会因综述而解决，prose 里明示双方立场与各自最强证据，不强行裁决。
