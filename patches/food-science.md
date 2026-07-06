---
domain: food-science
gap_types_dominant: [comparison, mechanism, safety, decision]
evidence_base:
  primary: [meta, rct, codex_standard, jecfa_evaluation, efsa_opinion, fda_gras_notice, aoac_method, gb_standard, peer_replication, sensory_panel_qda]
  secondary: [case_series, industry_report, patent, trade_publication]
  not_applicable: []
term_check_overrides:
  require_rct_or_meta: false
  require_primary_evidence_per_gap: 1
default_search_sources: [crossref, semantic_scholar]
---

# 主题补丁 — 食品科学

适用于一切以**食品本身**（而不是吃食品的人或动物）为研究对象的综述：食品化学 / 食品微生物 / 食品加工与工程 / 食品安全与法规 / 感官科学 / 食品保藏 / 食品包装 / 食品分析方法。本节是对 CLAUDE.md 中 **Evidence hierarchy、适用范围、Suggested structure** 等节的 **domain-conditional 覆盖**，冲突时本节优先。

**与 health（默认）的边界**：
- 食品对人体的生理 / 临床效应（吃了某食品后血糖 / 心血管 / 体成分变化）走 health 默认。
- 食品本身的成分、结构、加工后化学 / 微生物变化、配方设计、贮藏稳定性、感官属性走 food science。
- 同一主题可能跨两侧：例如"高水分挤出植物蛋白的氨基酸组成与生物利用度"——组成 / 结构走 food science，体内 PDCAAS / DIAAS 喂养实验走 health。综述前先决定**主问题**落在哪一侧，写入 research_log Round 1。

**与 animals 的边界**：
- 宠物食品配方学、AAFCO / FEDIAF 充足性裁定、商业宠粮成分申诉走 `patches/animals.md`。
- 通用动物源食品（牛 / 猪 / 禽 / 水产肉品的化学、微生物、加工、贮藏）走 food science。

**与 agriculture 的边界**：
- 作物育种、采前栽培、农艺学按需新建 `patches/agriculture.md`；目前无 patch 时暂借 food science。
- 采后处理（pre-cooling、washing、curing）、储藏、加工、转化走 food science。

**已实证可用** — 暂无 exemplar（首次启用时记录到本表）。

## 主题命名（强制）

主题目录名必须显式包含 **食品基质 + 加工 / 处理状态 + 目标性质或时间窗**。同一原料在不同加工状态下的化学组成、微生物谱、感官特性可能完全不同，遗漏加工状态或时间窗会让文献检索失焦。

- ✅ `巴氏杀菌全脂牛奶 4℃ 储藏期游离脂肪酸生成与异味形成`、`高水分挤出植物蛋白肉的纤维结构与持水力`、`真空低温烹饪三文鱼的组胺累积与食源性安全`、`冷榨初榨橄榄油储藏期氧化稳定性与多酚衰减`
- ❌ `牛奶变酸`、`植物肉`、`低温烹饪`、`橄榄油氧化`

## 一级证据扩展

食品科学领域 RCT / cohort 范式**仅适用于"食品对人 / 动物效应"主题**（那部分走 health 默认）；"食品本身"主题的证据等级按下列调整。下列来源**升为一级证据，与同行评议 Food Chemistry / JAFC 论文并列**（覆盖 CLAUDE.md §Evidence hierarchy 的默认排序）：

- **国际权威机构报告与意见书**：
  - Codex Alimentarius Commission（FAO/WHO 联合食品法典委员会）标准与指南
  - JECFA（Joint FAO/WHO Expert Committee on Food Additives，食品添加剂联合专家委员会）评估报告
  - JMPR（Joint FAO/WHO Meeting on Pesticide Residues，农药残留联合会议）评估报告
  - EFSA Scientific Opinions（欧洲食品安全局；食品添加剂、新型食品、污染物、酶制剂、营养与健康声称）
  - FDA GRAS Notices / Food Additive Petitions / FDA Guidance for Industry
  - USDA-ARS（美国农业部农业研究服务局）technical bulletins、USDA FoodData Central
  - NASEM Food and Nutrition Board reports（如 *Dietary Reference Intakes* 系列）
- **国际 / 国家技术标准与官方分析方法**：
  - AOAC International *Official Methods of Analysis*
  - ISO TC 34（食品技术）系列、ISO 22000（食品安全管理体系）
  - 中国 GB 2760（食品添加剂使用标准）/ GB 2761（真菌毒素限量）/ GB 2762（污染物限量）/ GB 2763（农药残留限量）/ GB 4789（食品微生物检验）/ GB 5009（食品安全国家标准·理化分析）系列
  - EU Regulations 178/2002（食品安全总则）/ 1333/2008（食品添加剂）/ 1924/2006（健康声称）/ 1169/2011（食品标签）
  - FSANZ Food Standards Code（澳新食品标准）
  - CAC/RCP 1-1969 (HACCP 原则)
- **顶级综述刊物**：
  - *Annual Review of Food Science and Technology*
  - *Critical Reviews in Food Science and Nutrition* (CRFSN)
  - *Trends in Food Science & Technology* (TIFS)
  - *Comprehensive Reviews in Food Science and Food Safety* (CRFSFS, IFT 旗下)
- **手册与教科书**：
  - Belitz / Grosch / Schieberle *Food Chemistry*（食品化学经典）
  - Damodaran / Parkin *Fennema's Food Chemistry*
  - Singh / Heldman *Introduction to Food Engineering*；Toledo *Fundamentals of Food Process Engineering*
  - Fellows *Food Processing Technology*
  - ICMSF *Microorganisms in Foods* 全套（International Commission on Microbiological Specifications for Foods）
  - Jay / Loessner / Golden *Modern Food Microbiology*；Doyle / Buchanan *Food Microbiology: Fundamentals and Frontiers*
  - Stone / Sidel *Sensory Evaluation Practices*；Meilgaard / Civille / Carr *Sensory Evaluation Techniques*；Lawless / Heymann *Sensory Evaluation of Food*
  - Robertson *Food Packaging: Principles and Practice*
- **顶级专业期刊**：
  - 通用化学：*Food Chemistry* (FC)、*Journal of Agricultural and Food Chemistry* (JAFC)、*Journal of Food Science* (JFS)
  - 工程与加工：*Journal of Food Engineering* (JFE)、*Innovative Food Science and Emerging Technologies* (IFSET)、*LWT - Food Science and Technology*
  - 结构与流变：*Food Hydrocolloids*、*Food Research International*
  - 安全与毒理：*Food and Chemical Toxicology*、*International Journal of Food Microbiology*、*Food Control*
  - 品类专刊：*Meat Science*、*International Dairy Journal*、*Postharvest Biology and Technology*、*Cereal Chemistry*、*Journal of Cereal Science*、*Food and Bioproducts Processing*
  - 感官：*Food Quality and Preference*、*Journal of Sensory Studies*
- **专利文献**：USPTO / EPO / CNIPA / WIPO PCT 食品配方与工艺专利（特别是商业化的酶制剂、稳定剂、加工设备）。专利**不进 prose 的主结论引用**，但在 "§工艺现状" 节作为产业现状的支撑。

理由：食品科学是工程 + 化学 + 微生物 + 法规的交叉学科，"规范方法 / 国家标准"本身就是该领域的可复制基线，地位等同其他领域的 meta；JECFA / EFSA 的评估意见综合了几十年厂家未发表实验、跨研究合并、毒理学数据，是事实标准。`study_type="other"` 对食品论文是默认（CrossRef 对 Food Chemistry / JAFC 不做研究类型分类），不要据此排除。

## "大队列" 不直接套用 — 食品科学的 sample 是 batches / replicates / panels

食品研究极少做人群队列。常见 sample 单位与"足够"的阈值：

- **分析化学 / 组分测定**：n=3 重复 + ≥2 独立批次（合计 n≥6）是**入门级**；AOAC / GB 方法验证常要求 n=5 重复 × 3 浓度水平 × 2 操作员 × 2 实验室。报告 mean ± SD 并附 RSD（relative standard deviation，相对标准偏差），分析化学 RSD < 5% 是常见接受线。
- **感官评价**：训练有素的描述性 panel n=8–12 panelists × 2–3 重复（QDA、Spectrum™）已足够；消费者偏好测试 n=80–150 是工业标准下限，n=300+ 可做细分人群分析；三角检验 n=24–36 是 α=0.05 / β=0.20 / p_d=0.30 下的最小 panelist 数（ISO 4120）。
- **微生物挑战 / 储藏实验**：每菌株 n=2–3 strains × 3 独立 batches × 多时点（log CFU/g 报告时给 SD）。预测微生物学（IPMP、ComBase 模型拟合）需 ≥15 个数据点跨多温度。
- **食品工程 / 工艺优化**：单因素试验 n=3 重复；多因素优化用响应面 RSM（central composite design n=20–30 runs、Box-Behnken n=15–17）或正交（L9 / L16 / L25）。
- **储藏期实验**：常温 / 冷藏 n=3 batches × 4–6 时间点（覆盖目标货架期 ≥ 1 倍）；加速储藏（ASLT, Accelerated Shelf Life Testing）n=3 × 短间隔 + Arrhenius 或 Q10 外推。
- **临床 PDCAAS / DIAAS / 糖血响应 / 饱腹感**（落在 health 边界）：按 health 默认套人群 n 标准。

不要套用医学 n≥1000 标尺。食品科学论文中 n=3 batches × 3 replicates 是绝大多数实验的报告单位，不是"underpowered"。

## 食品科学常用缩写中文对照

首次出现仍按 CLAUDE.md §Prose style 给中文（"GRAS（Generally Recognized as Safe，公认安全）"），本表仅作快速参考：

| 缩写 | 全称 | 中文 |
|---|---|---|
| GRAS | Generally Recognized as Safe | 公认安全（FDA 食品成分分类） |
| HACCP | Hazard Analysis and Critical Control Points | 危害分析与关键控制点 |
| GMP / SSOP | Good Manufacturing Practice / Sanitation Standard Operating Procedures | 良好生产规范 / 卫生标准操作程序 |
| ADI / TDI / RfD | Acceptable / Tolerable Daily Intake / Reference Dose | 每日允许 / 耐受摄入量 / 参考剂量 |
| NOAEL / LOAEL / BMDL | No / Lowest Observed Adverse Effect Level / Benchmark Dose Lower bound | 未观察到不良效应剂量 / 最低观察到不良效应剂量 / 基准剂量下限 |
| MRL / EMRL | Maximum (Extraneous) Residue Limit | 最大（外源性）残留限量 |
| $a_w$ | water activity | 水分活度 |
| Brix / TSS | degrees Brix / Total Soluble Solids | 白利度 / 可溶性固形物 |
| TA / pH | Titratable Acidity / pH | 可滴定酸 / 酸碱度 |
| PV / AV / TOTOX / FFA | Peroxide / p-Anisidine / Total Oxidation / Free Fatty Acid Value | 过氧化值 / 茴香胺值 / 总氧化值 / 游离脂肪酸值 |
| TBARS / MDA | Thiobarbituric Acid Reactive Substances / Malondialdehyde | 硫代巴比妥酸反应物 / 丙二醛（脂质氧化指标） |
| WHC / OHC | Water / Oil Holding Capacity | 持水力 / 持油力 |
| TPA | Texture Profile Analysis | 质构剖面分析（硬度 / 弹性 / 黏聚性 / 咀嚼性） |
| DSC / TGA / DMA | Differential Scanning Calorimetry / Thermogravimetric Analysis / Dynamic Mechanical Analysis | 差示扫描量热 / 热重分析 / 动态机械分析 |
| HPLC / UHPLC / GC / GC-MS / LC-MS / LC-MS/MS | (色谱与色谱-质谱联用) | 高效 / 超高效液相 / 气相色谱 / 气质 / 液质 / 液质串联 |
| NMR / FTIR / NIR / Raman | Nuclear Magnetic Resonance / Fourier-Transform Infrared / Near-Infrared / Raman | 核磁共振 / 傅立叶变换红外 / 近红外 / 拉曼光谱 |
| SDS-PAGE / IEF / 2D-PAGE | (蛋白电泳) | SDS 聚丙烯酰胺凝胶电泳 / 等电聚焦 / 双向电泳 |
| TPC / APC / TVC | Total Plate Count / Aerobic Plate Count / Total Viable Count | 菌落总数 / 需氧菌总数 / 总活菌数 |
| D-value / z-value / F-value | (热力致死参数) | 热力致死时间 $D$ / $z$ / $F$ 值 |
| MAP / CAP / VP | Modified / Controlled Atmosphere Packaging / Vacuum Packaging | 气调 / 控气 / 真空包装 |
| HHP / HPP / PEF / UV-C / OH / DBD-CP / PL | High Hydrostatic Pressure / High Pressure Processing / Pulsed Electric Field / UV-C / Ohmic Heating / Dielectric Barrier Discharge Cold Plasma / Pulsed Light | 超高压 / 高压加工 / 脉冲电场 / 紫外 C 杀菌 / 欧姆加热 / 介质阻挡放电冷等离子体 / 脉冲光 |
| QDA / CATA / RATA / JAR | Quantitative Descriptive Analysis / Check-All-That-Apply / Rate-All-That-Apply / Just-About-Right | 定量描述分析 / 全选适用项 / 全评适用项 / 恰好满意度（感官方法） |
| ALOP / FSO / PO / PC | Appropriate Level of Protection / Food Safety Objective / Performance Objective / Performance Criterion | 适宜保护水平 / 食品安全目标 / 性能目标 / 性能准则（Codex 食安管理层级） |
| PDCAAS / DIAAS | Protein Digestibility-Corrected Amino Acid Score / Digestible Indispensable Amino Acid Score | 蛋白质消化率校正氨基酸评分 / 可消化必需氨基酸评分 |
| GI / GL | Glycemic Index / Glycemic Load | 血糖生成指数 / 血糖负荷 |
| RSM / CCD / BBD / CRD / RCBD | Response Surface Methodology / Central Composite / Box-Behnken / Completely Randomized / Randomized Complete Block Design | 响应面方法 / 中心复合 / Box-Behnken / 完全随机 / 随机完全区组设计 |
| ASLT / Q10 | Accelerated Shelf Life Testing / temperature coefficient | 加速货架期试验 / 温度系数 |
| FAO / WHO / Codex / JECFA / JMPR / EFSA / FDA / USDA / FSANZ / FSIS | (国际 / 国家食品安全机构) | 联合国粮农组织 / 世卫组织 / 食品法典委员会 / 食品添加剂联合专家委员会 / 农药残留联合会议 / 欧洲食品安全局 / 美国食品药品监督管理局 / 美国农业部 / 澳新食品标准局 / 美国食品安全与检验局 |
| AOAC / ISO / GB / NMKL / IDF / AOCS | (官方分析方法 / 标准组织) | AOAC 国际 / 国际标准化组织 / 中国国家标准 / 北欧食品分析委员会 / 国际乳品联合会 / 美国油脂化学家学会 |
| IFT | Institute of Food Technologists | 美国食品科技学会 |

## 已知局限（食品科学专属）

- **Codex / GB / EFSA / FDA / JECFA 标准与意见书经常无 DOI**（机构内部 publication ID、docket number、Federal Register citation）。工具链无法自动 verify，需用户手工提供 Codex Standard 编号 / EFSA Journal Article ID / FDA docket / GB 编号，并在 references store 用 institutional reference 形式手工补入。这些往往是核心论点的最硬证据，不要因"DOI 找不到"而放弃引用。
- **中文食品科学期刊覆盖差**：《食品科学》《中国食品学报》《食品工业科技》《食品与发酵工业》《农业工程学报》《现代食品科技》在 CrossRef 中 DOI 覆盖率参差，需用户手工提供 DOI 或 CNKI 链接。Web of Science / EI Compendex 收录的部分中国刊物可走 CrossRef，但命中率低于英文主流刊。
- **行业期刊与 trade publications**（*Food Manufacture*、*Food Engineering*、*Prepared Foods*、*Food Processing*、Mintel / Euromonitor 报告）非学术 OA，CrossRef 索引窄；引用仅限作为产业现状 / 市场结构的支撑，**不进核心论点引用**。
- **专利文献无 DOI**：USPTO / EPO / CNIPA / WIPO PCT 含大量未公开发表的工艺与配方细节，但工具链不自动检索专利。核心论点涉及商业化工艺（如 microparticulated 乳清蛋白、阿拉伯胶 + 卡拉胶稳定体系、转谷氨酰胺酶交联肉糜）时用户需手工补入相关专利号。
- **行业资助偏差严重**：乳制品（IDF、National Dairy Council）、肉类（NPB、NAMI、Beef Checkoff）、糖业（World Sugar Research Organisation、ISA）、植物基（GFI / Good Food Institute）、可可（Mars / Mondelēz）、咖啡（ISIC）等行业机构经常资助"自家产品有益"或"竞品有害"的研究。prose 中**必须显式 declare COI** 并标明资助方。Sugar Research Foundation 1967 年贿赂哈佛公共卫生学院学者改写心血管研究结论的丑闻（Kearns / Schmidt / Glantz 2016, *JAMA Internal Medicine*）是该领域必须知道的反面教材。
- **感官研究的小样本与个体差异**：训练 panel n=8–12 在统计上属于小样本，结论外推到一般消费者需谨慎；偏好测试受文化背景、年龄、性别、采购场景影响极大，跨文化外推常失效（亚洲、欧美、拉美对甜 / 咸 / 鲜 / 苦的接受度系统性不同）。
- **储藏 / 货架期实验的加速外推不确定性**：Arrhenius 与 Q10 外推假设单一反应主导失效路径，对脂肪氧化、酶促褐变、中温区间的微生物增长常常失败（反应机制随温度切换）；引用加速实验数据时应注明"需常温实测验证"。
- **新型食品 / 食品科技初创的灰色资料过多**：精准发酵蛋白、培养肉 / 细胞培养肉、3D 打印食品、个性化营养、合成生物学甜味剂等领域大量信息只见于公司白皮书、IPO 招股书、技术博客、行业大会演讲，缺同行评议研究。引用这类信息时明确归类为"产业信息"而非"科学证据"，并在 prose 里写明"截至 YYYY-MM 未见同行评议数据"。
- **食品微生物的菌株 / 血清型特异性**：同一物种不同菌株毒力 / 耐热性 / 抗酸性可能相差数个数量级（如 *Listeria monocytogenes* serotype 4b vs 1/2a、*E. coli* O157:H7 vs 非致病株、*Salmonella enterica* serovar Typhimurium 不同 phage type）。综述里写"X 菌污染"必须给到血清型 / 菌株号，否则结论可外推性差。
