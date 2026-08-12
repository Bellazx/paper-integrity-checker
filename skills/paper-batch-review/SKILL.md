---
name: paper-batch-review
description: >
  Batch AI review of high-risk papers from the database. Queries yujing_quanliang for papers with
  risk_level='高风险' and status=0, verifies image duplicates and data anomalies via visual
  inspection of annotation images and domain knowledge, generates PDF review reports, and updates
  the DB. Use this skill when the user asks to batch review, verify flagged papers, or mentions
  "批量复核", "复核高风险", "检测结果复核", "batch review high-risk papers".
---

# Paper Batch Review (AI复核) v3

Batch-review all high-risk papers by performing **independent discovery** of data integrity issues AND verification of automated detection findings. This v3 skill treats the automated report.json as a supplement — agents must independently read the paper PDF and audit source data to find issues the automated system cannot detect.

## Why This Skill Exists

The v2 review only validated existing report.json findings, leading agents to rubber-stamp "all false positives" without checking. Case in point: 10.1126/scitranslmed.adu0814 — the agent checked 1 of 156 sheets, found n=6 in that one sheet, and concluded "sample sizes match" — missing that Fig.4A had n=5, Fig.4B had n=3, and Fig.5A/6A shared identical data across figures. This v3 review requires agents to independently audit ALL source data before looking at automated findings.

## Completion Checklist (ALL steps must be done)

Before reporting completion to user, verify ALL boxes are checked:

- [ ] Agent results collected for all papers
- [ ] Results JSON written with detailed Chinese-language review text
- [ ] `generate_review_report.py` executed successfully (PDFs generated)
- [ ] DB updated via the script (review_result + review_report_url + review_time + status=2)
- [ ] Summary reported to user

**If any step is missing, the review is NOT complete.**

## Input

`$ARGUMENTS` — optional. If empty, queries DB for all papers with `risk_level='高风险' AND status=0`. Otherwise, accepts a comma-separated list of DOIs.

## Fixed Report Runtime

所有涉及 PDF 渲染或最终报告写回的命令必须使用 Python 3.12.3、PyMuPDF 1.27.2.3 和固定的 Noto Sans CJK 字体。两台服务器的报告环境已统一；不要使用裸 `python3` 生成报告。

```bash
if [ -x /home/sjtu/paper-integrity-checker/venv/bin/python ]; then
  REPORT_PYTHON=/home/sjtu/paper-integrity-checker/venv/bin/python
else
  REPORT_PYTHON=/usr/bin/python3.12
fi
```

报告渲染器会在写 PDF 前校验解释器、PyMuPDF 和字体哈希；环境不匹配时会直接失败，不会生成版式不一致的报告。

## Workflow

### 1. Query Papers to Review

```bash
python3 -c "
import os
import pymssql
conn = pymssql.connect(
    server=os.environ.get('PAPER_DB_SERVER', '10.119.5.44'),
    user=os.environ.get('PAPER_DB_USER', 'yujing'),
    password=os.environ['PAPER_DB_PASSWORD'],
    database=os.environ.get('PAPER_DB_NAME', 'lunwenyujing'),
)
cursor = conn.cursor()
cursor.execute('''SELECT doi FROM yujing_quanliang WHERE risk_level = N'高风险' AND status = 0 ORDER BY doi''')
for row in cursor.fetchall():
    print(row[0])
conn.close()
"
```

### 2. Load Report Data AND Locate Source Files

For each DOI, find the report.json AND the source data directory:

```bash
# Find report.json (directory slug: / → _)
DOI_SLUG=$(echo "$DOI" | tr '/' '_')
REPORT="data/output/${DOI_SLUG}/report.json"

# Find input directory with source files
INPUT_DIR=$(find data/input -maxdepth 2 -type d -name "${DOI_SLUG}" 2>/dev/null | head -1)
# Also check double-underscore slug for Nature papers
DOI_SLUG2=$(echo "$DOI" | tr '/' '__')
INPUT_DIR2=$(find data/input -maxdepth 2 -type d -name "${DOI_SLUG2}" 2>/dev/null | head -1)
```

**IMPORTANT:** For each paper, identify ALL available source files:
- `*.xlsx`, `*.xls` — data spreadsheets (CHECK THESE)
- `*.docx` — data sheets/supplementary tables (CHECK THESE)
- `*.tif`, `*.tiff` — original images (NOTE IF PRESENT)
- `*.csv` — data files
- `source_data/`, `extended_data/` subdirectories

### 2.5 Build the Deterministic Evidence Bundle (RUN FIRST)

Before any agent judgement, generate the machine-built evidence bundle. It recursively
inventories source data (sheets → columns, each pre-categorized as 自变量/统计输出/标准差/
测量值), runs deterministic duplicate-column / cross-sheet-reuse / decimal-precision checks,
lists every main figure that must be visually checked, and emits a **coverage_manifest** of
items the review MUST address.

```bash
python3 /home/library/.claude/skills/paper-batch-review/scripts/review_evidence.py --doi "$DOI"
# -> writes data/output/review_v2/<DOI_SLUG>_evidence.json
```

Read this bundle first. Its `coverage_manifest.must_address` is the checklist your review
must cover. A 建议低风险 conclusion that leaves any HIGH automated finding, any deterministic
finding (duplicate column / cross-sheet reuse / decimal mismatch) unaddressed, or that skips
the figure-vs-source-data check on main figures, is **automatically upgraded to 建议高风险**.

**This is enforced deterministically, not optionally.** The coverage validator
(`scripts/coverage_validator.py`) runs INSIDE `generate_review_report.py` (Step 9) for EVERY
result, no matter whether the review was produced by the API pipeline or by you in this
conversation — the two paths share that one script as their only PDF+DB writer, so the gate is
identical. If the evidence bundle cannot be built, a 建议低风险 result is fail-closed to
建议高风险. Do not rely on the net — cover every must_address item proactively.


### 3. Image Duplicate Review

For each paper with image duplicate findings, perform visual verification:

1. **Check for PHash distance=0 across pages** — If the same image (PHash=0) appears on multiple different pages, this is STRONG evidence of image reuse → **confirm high risk**.

   **IMPORTANT: Distinguish blank pages from real duplicates.** PHash=0 can also occur between blank/nearly-blank PDF pages. Check the image dimensions and content — if both images are very small (<50px in any dimension) or appear to be white/empty, note this as a PDF artifact, NOT a duplicate.

2. **Check template verification scores** — If `template_score >= 0.9`, confirmed duplicate → **confirm high risk**.

3. **Read annotation images** — Use the Read tool on `annotation_path` to view the side-by-side comparison. Determine if the two matched images are:
   - Same type, same content → genuine duplicate (high risk)
   - Different types (e.g., bar chart vs Western blot, heatmap vs network diagram) → false positive
   - Same type but clearly different data → false positive (template match)

4. **Check for standalone TIF images** — If the paper input directory contains .tif/.tiff files that were NOT included in the initial image analysis (check report.json `total_images` count), note this as a gap. If >5 TIF files exist, flag for manual review.

5. **常见图像匹配模式参考（需结合学科背景判断，不可机械套用）**

   以下模式在特定学科场景下可能是假阳性，但**必须通过查看标注图确认图像内容确实不同**后才能判定。不可仅凭匹配到某个模式名称就自动排除。

   - **同一绘图软件模板（GraphPad/Prism/Origin）** — 多个图表共享坐标轴格式和配色。确认方式：轴标签和实际数据值是否不同。若数据值也高度相似，则不可排除
   - **ROC曲线标准结构** — 共享0-1坐标轴和对角参考线。确认方式：AUC值和曲线形状是否不同
   - **单细胞tSNE/UMAP嵌入复用** — 同一降维结果不同特征着色。确认方式：确认论文为单细胞组学研究，且匹配图像仅颜色标注不同
   - **化学结构 vs 其他图表** — 分子环状结构的几何特征匹配到其他图表的线条。确认方式：确认一方是分子结构图而另一方是完全不同类型的图表
   - **fMRI标准脑模板** — 同一解剖背景上的不同激活模式。确认方式：确认论文为fMRI研究且激活模式不同
   - **文献计量/综述图表** — 引用趋势图、VOSviewer网络图等共享结构元素。确认方式：确认为文献计量类图表
   - **有限元参数化研究** — 相同模型不同载荷条件下的应力分布图。确认方式：数值标签和色标是否不同
   - **PDF提取伪影（微小缩略图）** — PDF嵌入的书签或摘要缩略图。确认方式：检查region_b尺寸，宽或高<10像素即为伪影
   - **Sanger测序色谱图 vs Western blot** — 测序峰形与条带结构的几何巧合。确认方式：确认一方是测序色谱而另一方是凝胶/印迹

   **重要提醒：即使匹配了上述某个模式，如果两张图声称展示不同实验的结果，仍需仔细比对图像内容。模式名称是参考信息，不是免检通行证。**

6. **Bioinformatics pipeline repetition** — When the same analysis pipeline (e.g., volcano plot, GO enrichment, KEGG pathway, survival curve) is applied to multiple RNA types (mRNA, lncRNA, circRNA, miRNA, pseudogene) or multiple datasets, it naturally produces visually similar but distinct figures. If a paper has >50 image matches AND all matches are between figures of the same chart type applied to different data subsets, this is a **false positive** — the similarity comes from the pipeline, not from image reuse. Check whether the axes, data points, and labels differ between matched images. Only flag as genuine if the actual data content (not just the visual template) is identical.

7. **图像拼接（PS痕迹）视觉确认** — 初审会对疑似拼接的 Western blot/凝胶类图像做算法粗筛，结果在 report.json 的 `image_splicing` 字段（一律标为 medium，附带标注图路径 `annotation_path` 与疑似分界列 `seam_columns`）。对每条 `image_splicing` 标记：
   - 用 Read 工具查看 `annotation_path` 标注图（红线标出疑似拼接边界）。
   - 对照 `references/forensic_methods.md`「一、图像拼接视觉确认」清单逐条核对：泳道间不自然分界线、背景灰度断层、相邻泳道曝光突变、不同分辨率/压缩质量区域；以及内参（β-actin/GAPDH）跨条件复用、翻转/裁剪/旋转复用。
   - **保守判定**：只有视觉确认背景/曝光/分辨率确实在分界处不连续时才记为完整性问题；若为正常多联图、版式留白或粗筛假阳性，明确写"经查看为正常图像，非拼接"。
   - 确认结论写入 `image_review`（归报告（一）图像复核小节）。

### 3.5 Read Paper PDF for Experimental Claims (MANDATORY — BEFORE data review)

Before examining source data, the review MUST read the paper PDF to extract experimental claims. Use the Read tool on the PDF file in the paper's input directory.

**Extract:**

1. **Sample sizes**: Find ALL "n=X" statements in Methods, figure legends, and text
   - Search for: "n = ", "n=", "N = ", "biological replicates", "independent experiments", "repeated X times"
   - Record: which figure, which experiment type, what n value claimed

2. **Figure descriptions**: For each main-text figure, note:
   - What cell lines, drugs, conditions are tested
   - Whether experiments are described as "independent" or sharing controls
   - What type of measurement (proliferation, viability, tumor volume, etc.)

3. **Output a structured reference list** to use in Step 5:
   ```
   Fig.1D: Super-TDU dose response, n=3
   Fig.4A: NCI-H226 cell viability, Cobimetinib vs PD0325901, n=6
   Fig.4B: HeLa-LATS1/2 DKO cell viability, n=6
   Fig.5A: Huh7 proliferation (DMSO/Cobi/PD), stated independent experiment, n=6
   Fig.6A: Huh7 combination (DMSO/Sora/Cobi/Cobi+Sora), stated independent experiment, n=6
   ```

This list is REQUIRED input for Step 5 sample size verification.

4. **研究设计与时间线线索**（阅读 PDF 时一并记录，供 Step 5F 核查）：
   - 关键试剂/抗体/试剂盒/细胞系的厂商与货号（catalog number）。
   - 伦理审批声明与编号（人体/动物实验）。
   - 所用关键仪器/试剂/软件版本及其面世时间线索。
   - Methods 声明的样本量与各图注/表格 n 是否前后一致。

### 4. Data Anomaly Review — Report.json Level

Review the data anomalies listed in report.json:

1. Read `references/image_false_positive_patterns.md` (includes data-specific patterns at bottom)
2. For each HIGH-severity finding, categorize:
   - **常见数据模式参考（需结合学科背景判断，不可机械套用）**

     以下模式在某些学科中可能是正常的，但在另一些学科中可能是严重异常。复核时必须结合论文的研究领域、实验设计和数据类型来判断，不可因为匹配某个模式就自动排除。

     - **聚类标签列（所有值相同）** — 单细胞测序按cluster分sheet存储时，cluster ID列全为同一值属正常。但若出现在临床试验数据或实验测量数据中，则异常
     - **p值截断（Holm/FDR ≥ 1.0）** — 多重检验校正后非显著通路的p值被截断到1.0属正常。但若大面积出现，需关注统计方法是否适当
     - **2x2列联表结构（a+c=常数）** — 药物警戒等列联表中的数学恒等关系属正常。但在其他类型数据中出现完美线性依赖需警惕
     - **KEGG/GO背景大小（常数列）** — 通路富集分析中的基因库总数或检测基因总数不变属正常
     - **ANOVA等标准误（均衡设计）** — 完全均衡设计的标准误相同属正常。但若不均衡设计也出现完全一致的标准误，需关注
     - **归一化对照（全部=1.0）** — 相对定量实验中对照组归一化到1.0属正常
     - **问卷/量表数据** — Likert量表（1-5分）等有限取值范围的数据，组间出现大量相同值属正常。但需注意区分量表数据和连续测量数据
     - **基因组学分类列** — 染色体编号（1-22, X, Y）、基因生物型、链方向等分类注释列自然重复。但若连续测量列也出现高重复率则异常
     - **生信富集结果表** — GO/KEGG结果表中同一p值或基因计数出现在多行属正常（去归一化存储格式）
     - **共享对照组** — 同一实验的多个处理组共用对照数据是正常的。但需核实：(1) 对照组确实来自同一批实验 (2) 仅对照组相同而处理组不同 — 若处理组数据也相同则异常
   - **Suspicious patterns** (do NOT dismiss):
     - Cross-group duplicates with >80% overlap between UNRELATED experimental groups
     - Identity dependency (R²>0.99999, slope≈1.0, intercept≈0) between measurements that should be independent
     - Coefficient of variation = 0 in measurement data (not cluster IDs or p-values)
     - Arithmetic/geometric sequences in experimental measurement data

### 5. Independent Source Data Audit (MANDATORY — the most important step)

**This step discovers issues that the automated system CANNOT detect.** It must be done for EVERY paper with xlsx/xls source data, BEFORE reviewing report.json automated findings. The independent audit takes priority over automated detection.

#### 5A. Sample Size Verification (EVERY sheet, EVERY group — DO NOT CHERRY-PICK)

For EVERY sheet in the xlsx file, systematically count actual data points per experimental group and compare against paper-stated n values from Step 3.5.

**Run this script to audit ALL sheets at once:**

```python
python3 -c "
import pandas as pd
from pathlib import Path

f = Path('\$EXCEL_PATH')
raw = pd.read_excel(f, sheet_name=None, engine='openpyxl', header=None)
for sheet, df in raw.items():
    # Find group headers (usually in first 2 rows)
    groups = []
    for c in range(df.shape[1]):
        for r in range(min(2, len(df))):
            v = df.iloc[r, c]
            if pd.notna(v) and isinstance(v, str) and len(v) > 1:
                groups.append((c, str(v)[:30]))
                break
    # Count data rows (non-header, non-NaN)
    data_start = 2
    n_data_rows = 0
    for r in range(data_start, len(df)):
        if df.iloc[r, 1:].notna().any():
            n_data_rows += 1
    # Count columns per group boundary
    if groups:
        group_sizes = []
        for i, (col, name) in enumerate(groups):
            next_col = groups[i+1][0] if i+1 < len(groups) else df.shape[1]
            n_cols = next_col - col
            if n_cols > 0 and name not in ['NaN']:
                group_sizes.append((name, n_cols))
        if group_sizes:
            sizes_str = ', '.join(f'{n}:{c}cols' for n, c in group_sizes)
            print(f'{sheet}: {df.shape[0]-2} rows, groups=[{sizes_str}]')
"
```

**Then manually verify key figures** (Fig.1 through Fig.6 at minimum):
- Open the sheet, identify group headers, count data columns per group
- Compare each group's n against the paper-stated value
- **ANY mismatch in main-text figures (Fig.1-6) = HIGH RISK**
- Mismatches in supplementary figures: note and report, still contributes to verdict

**FAILURE MODE TO AVOID**: The failed adu0814 review checked only Fig.6H (which had n=6) and concluded all sample sizes matched. Fig.4A had n=5, Fig.4B had n=3. CHECK ALL MAIN-TEXT FIGURES.

#### 5B. Cross-Figure Data Reuse Detection (CRITICAL NEW CHECK)

This catches the most damaging issue: identical experimental data appearing in multiple figures that claim to be independent experiments.

**Run this detection script:**

```python
python3 -c "
import pandas as pd
from pathlib import Path

f = Path('\$EXCEL_PATH')
raw = pd.read_excel(f, sheet_name=None, engine='openpyxl', header=None)

# Index all data columns by their values
col_index = {}  # tuple of values → list of (sheet, col_idx)
for sheet, df in raw.items():
    for c in range(1, df.shape[1]):
        vals = pd.to_numeric(df.iloc[2:, c], errors='coerce').dropna()
        if len(vals) >= 3:
            key = tuple(round(v, 8) for v in vals.values)
            if key not in col_index:
                col_index[key] = []
            col_index[key].append(f'{sheet}/col{c}')

# Report columns with identical data across DIFFERENT sheets
for key, locations in col_index.items():
    sheets = set(loc.split('/')[0] for loc in locations)
    if len(sheets) > 1:  # same data in different sheets
        print(f'CROSS-FIGURE REUSE ({len(key)} identical values): {locations}')
"
```

If the script finds cross-figure reuse:
1. Check whether the two figures claim to be independent experiments (from Step 3.5)
2. If yes (different experimental designs, different figure numbers) → **HIGH RISK — data reuse**
3. If no (explicitly shared controls, same experiment different views) → note but not necessarily high risk

**Example of real fraud caught by this check**: adu0814 Fig.5A DMSO (6 replicates × 6 timepoints) was 100% identical to Fig.6A DMSO, and Fig.5A Cobimetinib was identical to Fig.6A Cobimetinib — 72 values matched at 6 decimal places across figures claiming different experimental designs.

5B 子模式补充 — 跨行/跨 passage 伪重复(within sub-table):
在 passage × replicate 布局的 sheet 中(如 Supplementary Fig. S11),两个不同 passage 行的前 N 列(生物学重复)完全相同、第 N+1 列相差整数常量(如 +1.5、+2.0),或两行 N+1 列完全相同。这是合成"伪重复"模式 — 真生物重复有随机变异,前 N 列完全相同 + 第 N+1 列整数偏移是人为编辑特征。由自动检测 `passage_offset_pattern` 规则发现(运行: `python3 -c "import sys; sys.path.insert(0,'/home/sjtu/paper-integrity-checker'); from modules.data_checker import _analyze_passage_offset_pattern; ..."`)。**真实案例**: scitranslmed.aba3571 Fig.S11 Section B Passage 5↔17 精确相同 / Passage 8↔14 前两列相同+第 3 列+1.5 / Passage 10↔11 前两列相同+第 3 列+2.0。

#### 5C. Column Pair Scanning

Run scan_columns.py for within-sheet duplicate detection:

```bash
python3 /home/library/.claude/skills/paper-batch-review/scripts/scan_columns.py \
  --excel-dir "$INPUT_DIR"
```

#### 5D. Cross-Sheet Decimal Precision Comparison

This check catches a known fraud pattern where fabricated data has unnaturally uniform or mismatched decimal precision:

Run the decimal-tail scan as part of this step:

```bash
python3 /home/library/.claude/skills/paper-batch-review/scripts/forensic_checks.py scan-decimal-tails \
  --excel-dir "$INPUT_DIR"
```

1. For each xlsx file with multiple sheets, compare the decimal precision patterns:
   - If Sheet A has all values at 2 decimal places and Sheet B has all values at 9 decimal places for the SAME TYPE of measurement (e.g., both are qPCR fold-change values) → **STRONG FRAUD INDICATOR → confirm high risk**
   - If all sheets have identical precision (e.g., all 6 decimal places) for data that should have natural variation → suspicious
   - If precision varies naturally (mix of 2-4 decimals across sheets) → normal

2. For each xlsx file, check for these red flags:
   - **Precision mismatch**: Same measurement type has wildly different decimal places across sheets
   - **Perfect uniformity**: All values in a column have exactly the same number of decimal places (>4 decimals)
   - **Round number dominance**: Experimental data that is suspiciously clean (all values ending in 0 or 5)
   - **Copy-paste patterns**: Multiple cells with identical long decimal values
   - **Uniform trailing digits (末位数字完全相同)**: 同一列内 ≥60% 的值在小数末 4 位完全相同(例: 15 个抗生素处理值中 10 个都末 4 位为 "1246"),或 ≥80% 末 3 位完全相同 — 这是合成数据"末位填充"的典型痕迹,4 位数字 60% 命中概率 ≈ 10⁻³²,真随机数据几乎不可能产生。**真 `decimal_uniformity` 只检查小数位数 COUNT,本规则检查末 N 位数字的 CONTENT,两者互补**。由自动检测 `trailing_digit_uniformity` 规则发现(utils/stats.py → modules/data_checker.py → `_analyze_column_group`)。**真实案例**: scitranslmed.aba3571 Fig.S9 5 个抗生素列(LVX/IMI/CEF/AMP/AMI)各有 60-67% 的值末 4 位为 "1246"。
   - **Paired decimal-tail inheritance / insertion（对应组小数尾串继承/插入）**: 对同一图、同一 sheet、相邻子表或对应组逐行比较小数部分。若右侧/后续测量值大量保留、包含、插入或改写左侧/前序测量值的小数尾串（例如 0.645 -> 0.7645、0.7546 -> 0.67546、0.533 -> 0.4533），且这些列代表不同测量类型或应独立生成的实验值，这是数据规范性异常，不能当作普通小数位数差异排除。经验阈值：同一对应组中 >=50% 行出现 3 位以上小数尾串被包含/继承，或 >=80% 行末 2-3 位一致，并且不能由样本编号、分组编码、时间轴、浓度梯度或统一格式化解释时，确认高风险。**真实案例**: s41467-022-28492-5 Fig.1A 中 plasma MPO-DNA 与 serum MPO-DNA 对应组出现 0.645 -> 0.7645、0.7546 -> 0.67546 等系统性尾串继承/插入。

#### docx Data Sheet Inspection

**IMPORTANT: docx merged cell handling** — When reading docx tables, merged cells produce duplicate values across the merged range. For example, a cell merged across columns 3-6 will appear as the same value in columns 3, 4, 5, and 6. This is a document formatting artifact, NOT data duplication. Before flagging any "identical columns" found in docx data:
1. Check whether the source is a docx file (not xlsx)
2. If docx, verify whether the table uses merged cells (common in clinical/demographic tables)
3. If merged cells are present, the "duplicate columns" are an artifact of the reading method — dismiss as false positive

For each .docx file:
```python
python3 -c "
from docx import Document
doc = Document('$FILE_PATH')
for i, table in enumerate(doc.tables):
    print(f'=== Table {i} ({len(table.rows)} rows x {len(table.columns)} cols) ===')
    for row in table.rows[:5]:
        print([cell.text[:30] for cell in row.cells])
    print()
"
```

Check the same decimal precision patterns as xlsx files.

#### 5E. Statistical Self-Consistency Check (统计自洽性核查)

This catches fabricated summary statistics that the column-level checks cannot.

1. **Terminal-digit / SD-regularity (automatic cross-check)** — run the digit scan on the source data and compare with the automated `terminal_digit` / `sd_regularity` findings in report.json:
   ```bash
   python3 /home/library/.claude/skills/paper-batch-review/scripts/forensic_checks.py scan-digits \
     --excel-dir "$INPUT_DIR"
   ```
   Confirm any flagged column by eye: a measurement/count column whose last digits cluster on a few values (e.g. all end in 0/5), or an SD/SE column that is all-integer / all same-precision, is a data-integrity concern.

2. **GRIM / GRIMMER / p-value recomputation (from reported statistics)** — from the paper text and tables, extract reported summary statistics and test statistics, write them to a triples JSON under `data/output/review_v2/<DOI_SLUG>_triples.json`, then verify:
   ```bash
   python3 /home/library/.claude/skills/paper-batch-review/scripts/forensic_checks.py verify-stats \
     --input data/output/review_v2/<DOI_SLUG>_triples.json
   ```
   Triples JSON format:
   ```json
   {
     "descriptive": [
       {"id": "Table1 Age Ctrl", "mean": 5.19, "sd": 1.12, "n": 28,
        "mean_precision": 2, "sd_precision": 2, "integer_items": true}
     ],
     "test_stats": [
       {"id": "Fig4 t-test", "stat_type": "t", "stat_value": 1.0, "df1": 10,
        "reported_p": "<0.05", "tail": "two"}
     ]
   }
   ```
   - `integer_items: true` ONLY for means of integer-valued items (counts, Likert, scores); GRIM/GRIMMER are skipped otherwise.
   - `stat_type` ∈ t / F / chi2 / r; `df2` is the denominator df for F.
   - A GRIM/GRIMMER inconsistency = the reported mean/SD is mathematically impossible for the stated n. A p-value "decision_error" = the reported significance contradicts the statistic it came from.

3. Record confirmed concerns in `data_review` (归报告（二）数据复核小节), in **plain Chinese without metric names** (e.g. "论文所报均值在所述例数下数学上无法成立" rather than "GRIM 不通过").

#### 5F. Research Design & Timeline Check (研究设计与时间线核查)

Using the leads recorded in Step 3.5, check (see `references/forensic_methods.md`「二」):
1. **试剂/设备货号合理性** — key catalog numbers present and plausibly formatted.
2. **伦理审批号** — present for human/animal studies.
3. **时间线穿越** — no reagent/instrument/software used before its market release vs the study/submission date.
4. **内部 n 矛盾** — Methods-stated n vs actual data points in figures/tables.

Record findings in the **`methodology_review`** field (归报告新增（四）研究设计与统计核查小节). This section uses plain language to explain the experimental or study design, sample size, statistical method, ethics applicability, timeline, and key reagent/instrument descriptions. Timeline anachronisms, statistical self-consistency checks, and internal n contradictions are data-integrity concerns (may support high risk with clear evidence); missing catalog/ethics numbers are normative issues. Leave `methodology_review` empty when nothing is found — the report supplies a concise explanation of what was checked.

#### 5G. Figure-vs-Source-Data Visual Cross-Check (图文一致性核查 — MANDATORY, do NOT skip)

**This is the most important active-audit step and the one most often missed. The review MUST render each main-text figure to an image and compare it AGAINST its own source data — do not merely validate report.json.** Two real failure modes this catches (both confirmed in NCOMMS-25-73188B):

1. **图与数据数值不一致**: a bar/curve in the figure shows values that contradict (or are swapped from) the numbers in its source-data sheet. Example caught: Fig.2f bars showed Mn/CN-PMS strong-adhesion ≈87% but source data said 13.27 — the strong/weak categories were swapped between figure and data.
2. **同一条数据用于两图却标注矛盾**: the identical data column appears under two different figures with contradictory sample/condition labels. Example caught: an EPR trace was labeled "Mn2/CN+PMS" in Fig.3j but "Mn/CN" in Fig.S25 — same numbers, contradictory attribution.

**How to do it (you have the Read tool — USE IT, never write "无法读取像素/需人工核对"):**
```python
# Render each figure PDF (or main-text PDF page) to PNG, then Read the PNG
import fitz
doc = fitz.open("<paper>/supplementary information/Figure 2.pdf")  # or the main PDF
for i, p in enumerate(doc):
    p.get_pixmap(dpi=160).save(f"data/tmp/fig_render_{i}.png")
```
Then use the **Read tool on each rendered PNG** to see the actual bars/curves/labels, and for every quantitative panel (bar charts, %, kinetic curves, fitted peaks) compare the visually-read values against the corresponding source-data columns. Clean up the temp PNGs afterward (data/tmp only, never /tmp).

For EVERY main-text quantitative figure, state in `data_review` or `image_review`: "Fig.X 图中显示 …, 源数据为 …, 一致/不一致". A figure-data mismatch or contradictory cross-figure label is a **data-integrity problem → contributes to high risk** (see Step 7). Confirmed figure-data inconsistencies MUST also appear in the 综合评估/`reason` field, not only in the sub-section.

### 6. Reference Issue Review

For papers with reference issues in report.json:

1. **DOI not found (high severity)**: Check the count. If >5 DOIs not found AND the DOIs look fabricated (not matching any known publisher pattern), flag as high risk.
2. **Title mismatch**: If >3 references have title similarity <0.3 (i.e., DOI resolves to a completely different paper), this suggests DOI fabrication → **flag as high risk**.
3. **Garbled references**: If report shows many references with garbled text, this is a PDF extraction artifact, NOT evidence of fraud — dismiss.

### 7. Determine Verdict

Apply these decision rules for each paper:

```
CONFIRM HIGH RISK (→ DB writes "高风险") if ANY of:
  - Sample size mismatch in main-text figures (paper claims n=X, data has fewer)
  - Cross-figure data reuse (identical values across figures claiming independent experiments)
  - Figure-vs-source-data mismatch (Step 5G): a figure's plotted values contradict/are swapped from its source data, OR the same data column carries contradictory labels across two figures
  - PHash distance=0 across multiple pages (≥2 different pages) with non-blank images
  - Template verification score >= 0.9 between images on different pages
  - SIFT matches confirmed as SAME CONTENT via visual inspection
  - Cross-sheet decimal precision mismatch (2 vs 9 decimals for same measurement)
  - Perfect decimal uniformity (>6 decimal places, all identical) in experimental data
  - Paired decimal-tail inheritance / insertion between corresponding groups or adjacent measurements (e.g., many values preserve or insert the previous column's decimal tail)
  - Identity dependency (R²>0.99999, slope≈1, intercept≈0) between independent measurements
  - >5 fabricated DOIs in references
  - Copy-paste data patterns across sheets
  - Multiple HIGH-severity data anomalies that are NOT explained by known false positive patterns

NEEDS MANUAL CHECK (→ DB writes "高风险") if ANY of:
  - High inlier count (>300) in bioinformatics paper where meaning is unclear
  - Measurement data with identical values but column semantics unclear
  - TIF original images present but not analyzed by initial screening
  - >3 data anomalies that MIGHT be false positives but evidence is inconclusive
  - Source data files (xlsx/docx) exist but could not be read/analyzed
  - Suspicious decimal precision patterns that don't clearly match fraud or normal patterns

SUGGEST DOWNGRADE (→ DB writes "低风险") ONLY if ALL of:
  - ALL image matches are confirmed false positives — 每组匹配已通过查看标注图确认为假阳性（不可仅凭模式名称判定，必须描述看到了什么）
  - ALL HIGH-severity data anomalies已结合论文的学科领域和实验设计确认为已知模式（需说明该论文属于什么学科、为什么该学科中此模式属正常）
  - No concerning findings in source data deep inspection
  - No reference fabrication patterns
  - Agent has actually inspected the source data files (not just report.json)
  - 复核理由中必须说明：(1) 论文的研究领域 (2) 为什么该领域的背景支持将每个发现解释为假阳性 (3) 排除了哪些替代解释
```

**IMPORTANT: Default to "建议高风险" when uncertain.** 结论仅"建议高风险"/"建议低风险"两类，不存在"需人工复查"或"中风险"。The v1 review was too lenient. Only downgrade (建议低风险) when you have STRONG evidence that ALL findings are false positives AND you have inspected source data.

### 8. 分层复核与综合判定模式 (MANDATORY for ALL reviews)

每篇论文使用分层复核与综合判定流程：先产生相互独立的复核结论，再在需要时进行综合判定。综合判定环节可以看到前序复核的具体分析内容和分歧点，必须基于证据做出有依据的判断。

#### 执行方式

将论文分批（每批 4-6 篇），对每批执行两个阶段：

**阶段 1：独立复核（并行）**
```
复核通道一：papers 1-5，独立复核
复核通道二：papers 1-5，独立复核
```
两个复核通道独立执行，互不可见对方结果。

**阶段 2：综合判定**
```
综合判定：针对每篇论文，审查前序复核分析结果，独立验证关键发现，做出最终判定
```

综合判定环节的职责：

**FIRST**: Read `/home/library/.claude/skills/paper-batch-review/RUNTIME_RULES.md` for verdict rules and report-writing standards.

1. 审查两份分析的逻辑和证据是否充分
2. 对关键发现进行独立验证（读取标注图、源数据文件）
3. 关注前序复核是否都遗漏了问题，或过于轻易将发现标记为假阳性
4. 如果前序复核对同一问题的解释不同，必须独立验证后判断
5. **独立完成图文一致性核查（Step 5G）**：即使前序复核都没做，综合判定环节也必须亲自渲染主图、用 Read 工具看图、与源数据逐一比对。常见失败是把"图 vs 数据不一致""同数据跨图标注矛盾"当成"共享横轴"而漏判——要主动以"逐图 vs 其源数据"的姿态复查，而不是只验证 report.json 的自动发现。
6. **撰写 `reason`（综合评估）时，必须把所有维度确认的问题逐条汇总进去**（含图文不一致），不能只写在分节里；对假阳性的解释保持精简（见 JSON content requirements）。
7. 做出最终判断，以综合判定结果为准

#### 报告内容要求

- PDF 报告使用最终复核文本
- **禁止在报告中出现任何内部流程用语**：不可出现内部流程名称、角色称谓、流程协调信息或多通道裁决痕迹
- verdict 字段仅填"建议高风险"或"建议低风险"，不得包含流程信息或"需人工复查"/"中风险"字样
- 正式 PDF 默认采用简明标准：先结论，再列确认问题、核查依据和 1–4 张最关键的具体截图。每个确认问题仍须包含“问题位置/问题类型/具体情况/需关注原因/核查依据”，但只保留足以复现结论的代表性数值。已排除项仅写类别、数量和一句理由；无实质问题的维度最多一段。综合评估不重复各节的长篇技术过程，并明确风险提示不等同于对主观意图的认定。常规报告目标约 3–6 页。

#### 超时和重试规则

- 每个复核通道独立超时：`REVIEW_TIMEOUT_SECONDS`（20 分钟）
- 复核通道超时或出错 → 自动重试 1 次
- 重试仍失败 → 该通道结果默认为"高风险"
- 综合判定环节超时 → 若两个复核结论一致则直接采用，不一致则默认高风险
- **禁止将超时/错误信息写入 PDF 报告**，必须重试或使用降级策略

#### 每个复核通道的复核步骤（独立执行，不可看到另一个通道的结果）

**FIRST**: Read the runtime review rules file before starting any review:
```
Read /home/library/.claude/skills/paper-batch-review/RUNTIME_RULES.md
```
This file contains the definitive review rules, verdict criteria, report-writing standards, and anti-hallucination constraints. Every review agent MUST read it at the start and follow it throughout.

1. **Read the paper PDF** to extract experimental claims (stated n values, figure descriptions, experimental design) AND methodology/timeline leads (Step 3.5)
2. **Run independent source data audit** on ALL xlsx/xls files:
   a. Sample size verification for EVERY group in EVERY main-text figure sheet (Fig.1-6 mandatory, supplementary spot-check)
   b. Cross-figure data reuse detection (run the script to find identical data across different sheets)
   c. Run scan_columns.py for within-sheet column pair duplicates
   d. Cross-sheet decimal precision comparison
   d2. Paired decimal-tail inheritance / insertion check for corresponding groups and adjacent sub-tables (`forensic_checks.py scan-decimal-tails`)
   e. Statistical self-consistency (Step 5E): run `forensic_checks.py scan-digits`; extract reported mean/sd/n and test statistics from the PDF → `forensic_checks.py verify-stats`
   f. Methodology & timeline check (Step 5F): catalog numbers, ethics approval, timeline anachronism, internal n contradictions
3. **Read the report.json** for automated detection findings (including `image_splicing` suspects → confirm per Step 3 item 7)
4. **Validate automated findings** (image duplicates, image splicing, data anomalies, reference issues) against source data
5. **Combine** independent findings (Steps 1-2) WITH validated automated findings (Steps 3-4) → apply verdict rules
6. Return a structured assessment per paper with review text that meets the **Review Text Rubric** below (image splicing → `image_review`; digit/stat checks → `data_review`; methodology/timeline → `methodology_review`)

**CRITICAL**: Steps 1-2 MUST be done BEFORE Step 3. The review must form its own independent assessment before being influenced by automated findings. Automated detection is a supplement, not the primary analysis.

#### Anti-Hallucination Rules (MANDATORY — READ BEFORE STARTING)

1. **Never conclude "all false positives" without checking EVERY main-text figure sheet.**
   The failed review of adu0814 checked only 1 of 156 sheets before concluding "sample sizes match." It missed Fig.4A (n=5), Fig.4B (n=3), and Fig.5A/6A data reuse. This is the failure mode we are preventing.

2. **Sample size check must cover ALL main-text figures (Fig.1 through Fig.6+).**
   Supplementary figures can be spot-checked, but every numbered main-text figure MUST be verified.

3. **Cross-figure comparison is mandatory when a paper has multiple sheets with similar data types.**
   If Vehicle/DMSO/Control groups appear in multiple sheets, compare their data systematically.

4. **When in doubt, report the observation — don't dismiss it.**
   If you can't determine whether a finding is a false positive, describe what you see and let the verdict default to high risk.

5. **Cite specific evidence for EVERY claim.**
   - REJECTED: "未发现样本量异常"（which sheets did you check?）
   - ACCEPTED: "核查了Fig.4A(n=5/5)、Fig.4B(n=3/3)、Fig.5A(n=6/6/6)、Fig.6A(n=6/6/6/6)、Fig.6H(n=6×5组)，其中Fig.4A和Fig.4B的实际样本量低于论文声明的n=6"

#### Review Text Rubric (MANDATORY — minimum specificity requirements)

Agent-generated review text goes directly into the PDF report that users read. Generic one-liners like "未发现异常" are UNACCEPTABLE. Every field must meet these minimum requirements:

**Reader-facing structure (MANDATORY).** The PDF is read by non-specialist audit staff. For every confirmed problem, write in this order before giving technical formulas:
1. **问题位置**：which figure/table/file/sheet/row/column has the problem.
2. **发现了什么**：plain-language description of the visible issue.
3. **为什么重要**：why this suggests a data-integrity concern or why it is only a false positive.
4. **核查依据**：numeric examples, formulas, scripts, or statistical self-consistency checks.

Do NOT open a paragraph with a formula or jargon-only statement such as "F=(Beta/SE)^2 成立". Every confirmed issue must use the same five reader-facing labels in this order: `问题位置：`、`问题类型：`、`具体情况：`、`需关注原因：`、`核查依据：`. For example: "问题位置：Table S2 反向分析中 Ovarian cyst 第202-207行与 POF 第214-219行。问题类型：不同表型使用相同统计数据。具体情况：两块使用完全相同的6个SNP及统计量。需关注原因：这会使两个表型的因果分析实际上建立在同一组输入数据上。核查依据：逐行比对SNP、Beta和SE，并用F=(Beta/SE)^2复算后全部一致。"

For English reports use the same order and labels: `Location:`、`Issue type:`、`What was found:`、`Why it matters:`、`Verification basis:`. Explain any domain term the first time it appears. If a calculation supports the finding, show the formula, substituted values, intermediate result, paper-reported value, and difference.

For the `reason` field (PDF「五、综合评估」), separate different issue types with line breaks: data dimension, image dimension, references, methodology/statistical checks, remaining false positives, and final verdict should be separate paragraphs.

Use reader-facing Chinese labels for automated rules. Do not put raw internal field names in the main prose. Examples: write "跨表整行重复告警" instead of `cross_sheet_row_duplicate`, "小数位一致告警" instead of `decimal_uniformity`, "首位数字分布告警" instead of `Benford`, and "数值重复使用告警" instead of `value_recycling`.

**`image_review` must include:**
- Total number of matched pairs detected
- For each pair: page numbers (page_X vs page_Y) and verdict (false positive / genuine duplicate)
- Specific reason for each false positive (e.g., "投稿原图与正文同一图片的PDF嵌入版匹配", "页眉页脚模板匹配", "不同类型图像：柱状图 vs Western blot")
- If no matches detected: state how many images were analyzed and how many pages covered
- **图像拼接确认**：若 report.json 有 `image_splicing` 标记，逐项给出查看标注图后的结论（确认为拼接迹象 / 正常图像），并说明依据（背景/曝光/分辨率是否在分界处不连续）

**`data_review` must include:**
- **Independent findings (from Step 5)**: sample size audit results for each main-text figure, cross-figure reuse detection results, column pair scanning results
- For each HIGH finding (both independent AND automated): exact location (filename → sheet name → column name / row range)
- Specific numeric examples (≥3 representative values, with decimal precision noted)
- Verdict reasoning: cite the specific false positive pattern name (e.g., "cluster_label整数ID", "Holm校正p值截断到1.0") OR explain why it is a genuine concern
- Sample size audit summary: for EACH main-text figure, state "Fig.X: 论文声明n=Y, 实际数据n=Z"
- Cross-figure reuse results: which sheets compared, any identical data found
- **统计自洽性结论（Step 5E）**：末位数字/SD规律性核查结果；若做了 GRIM/GRIMMER/p值核算，给出具体不一致项（用平实中文，不写指标名），例如"表1对照组所报均值在所述例数下数学上无法成立"

**`ref_review` must include:**
- Number of HIGH-level reference issues
- 1-2 specific examples (quote ref_text fragment + corresponding DOI + verdict)
- Explicitly distinguish PDF parsing errors (garbled/fragmented text) from genuine citation anomalies (complete readable text but DOI mismatch)

**`methodology_review`（可选；有发现才填，无则留空字符串）must include:**
- 具体问题与位置：如"Methods 声明每组 n=6，但图 4A 实际仅 5 个数据点"
- 时间线/货号/伦理号问题的具体描述与依据
- 平实中性措辞，不使用造假/篡改等词；无问题时此字段留空，报告将不显示（四）小节

### 9. Generate PDF Reports AND Update Database (MANDATORY — DO NOT SKIP)

**CRITICAL: This step is MANDATORY. Never update the DB manually without generating PDF reports first. The generate_review_report.py script handles BOTH PDF generation AND DB update atomically. Skipping this step will result in broken "复审报告" links on the frontend.**

After all review passes complete:

1. Compile results into a JSON file with detailed Chinese-language review text for each paper
2. Run the script — it generates PDFs AND updates DB in one pass

```bash
"$REPORT_PYTHON" /home/library/.claude/skills/paper-batch-review/scripts/generate_review_report.py \
  --results /tmp/batch_review_results.json \
  --output data/output/review_reports/
```

The `--results` JSON file format:

```json
[
  {
    "doi": "10.3389/fendo.2021.720784",
    "result": "高风险",
    "trigger": "pic>=100",
    "image_review": "具体的图像复核描述（中文，含图像拼接确认结论）...",
    "data_review": "具体的数据复核描述（中文，含统计自洽性结论）...",
    "ref_review": "参考文献复核描述（中文，可选）...",
    "methodology_review": "研究设计与统计核查描述（中文，可选；无问题留空字符串）...",
    "evidence_images": [{"dimension": "image|data|ref|methodology", "path": "本地证据图路径", "caption": "证据截图说明"}],
    "verdict": "建议高风险",
    "reason": "具体理由（中文）..."
  }
]
```

**JSON content requirements:**
- `image_review` and `data_review` must contain **specific, detailed Chinese-language descriptions** of the review findings, not generic placeholders
- For 高风险 papers: describe the exact evidence (which table, which columns, what values)
- For 低风险 papers: explain which false positive patterns apply and why
- These texts go directly into the PDF report that users read

**`reason`（→ PDF「五、综合评估」）MUST aggregate EVERY confirmed finding across ALL dimensions.** This is the single most important content rule: 综合评估 is what users read first, so any genuine problem confirmed in a sub-section (image_review / data_review / ref_review / methodology_review) MUST also be enumerated here. Do NOT leave a confirmed finding buried in a sub-section only. Concretely, for a 高风险 verdict the `reason` should list each confirmed integrity problem in one clause apiece (e.g. "①Fig.2f 图值与源数据强/弱吸附对调；②Fig.3j 与 Fig.S25 同一条 EPR 谱却样品标注矛盾；③Fig.S26/S28/S48 降解曲线及误差棒逐位复用；④O 1s XPS 去卷积跨样品复用"), then state the verdict. A reader checking 综合评估 should see the complete list of real problems without reading the sub-sections.

**`evidence_images`（可选）只填写最终确认的高风险证据图。** 每项必须给出所属维度、可读取的本地图片路径和不含内部路径的中文说明。低风险维度、已排除告警和纯假阳性不得加入；每篇优先保留 1-4 张最关键截图。

**Conciseness rule for false-positive explanations (MANDATORY).** When dismissing automated findings as false positives, be brief: state the category, the count, and the one-line reason — do NOT enumerate every fragment, every looked-up DOI, or every verification step. Over-detailed false-positive write-ups read as defensive and expose the automated screen's weaknesses unnecessarily.
- BAD (too verbose): "初审参考文献维度满分(20条:19条low_match_score+1条not_found),经核查全部为正文PDF由作者Word文档转换时参考文献断行所产生的解析伪影…证据:送检文本多为被换行切断的片段(如'211-221 (2025).'、'Technol. 59, 1060-1079 (2025).'、单独的'59'等)…凡含可校验标识的条目,DOI经联网核实均真实且吻合(如Zhu等Nat. Water,10.1038/s44221-024-00382-8;Deng等Angew,10.1002/ange.202509493;Chen等Water Res. 268,10.1016/j.watres.2024.122622),唯一not_found项亦为…"
- GOOD (concise): "参考文献20条问题经核查均为正文格式转换导致的著录断行所致,抽样核实文献真实且DOI吻合,不构成完整性问题。"
- Spend the detail budget on CONFIRMED problems (exact sheet/column/values), not on explaining away false positives.

**BAD examples (UNACCEPTABLE — will be rejected):**
```
image_review: "未发现图像重复异常。"
data_review: "数据异常标记经核查均为假阳性，无数据完整性问题。"
ref_review: "该论文无自动检测报告可供复核。"
```
These contain zero specific information. No page numbers, no file names, no values, no reasoning.

**GOOD examples (minimum acceptable standard):**
```
image_review: "共检出8组图像匹配。其中7组为论文正文页面(page_3/4/6/8)与投稿原始图片文件的正常匹配（同一图像的PDF嵌入版与原始文件），属假阳性。第8组(page_5 vs page_12)经视觉核查为不同类型图像（柱状图 vs 散点图），属模板匹配假阳性。未发现真实的跨页图像重复。"

data_review: "共4条HIGH级别数据异常。(1) Source Data Fig.2.xlsx / Sheet 'Fig2b': cross_group_duplicate标记, Col E(siCtrl) vs Col H(siRNA-1)重叠率92%，经核查两列分别为25和28个数据点，5个共同值为常见归一化值(0.00, 1.00, 0.50)，实际独立数据不同，属假阳性。(2-3) decimal_uniformity出现在CPS评分列（整数1/2/3，0位小数属正常）。(4) Benford偏离来自小样本(n=32)分类变量，不适用Benford定律。全部为假阳性。"

data_review (高风险): "关键发现：Figure 5I源数据（CHX chase蛋白降解实验，声明n=3生物学重复）中，Ctrl-sh组的第1和第2列数据完全一致（6个时间点全部相同，精确到6位小数：1.000000, 1.079898, 0.370102, 0.163102, 0.188794, 0.061887）。TRIM47-sh1组的第1和第2列也有4/6个时间点数值完全一致。生物学重复实验产生完全相同的数值在统计学上极不可能，该数据存在重复或异常。"

ref_review: "共3条HIGH级别参考文献问题。(1) ref #12: ref_text为乱码碎片'J. Med. Chem. 2019, 62, 5'（双栏拼接），属PDF解析错误，假阳性。(2-3) ref #28, #35: DOI不存在于CrossRef，ref_text完整可读但对应DOI查无此文，属真正引用异常。"

image_review (含拼接确认): "共检出5组图像匹配，均为同一图表模板的正常复用，属假阳性。另对第6页1张Western blot图做了拼接核查：查看标注图后确认该图中部某竖直分界两侧背景灰度与曝光连续一致，未见拼接迹象，判为正常图像。"

image_review (拼接问题): "对第4页一张凝胶图做拼接核查：标注图显示某泳道间存在清晰竖直分界线，分界两侧背景灰度明显不同、曝光水平突变，提示该图可能由不同来源拼接而成，属图像完整性问题。"

data_review (含统计自洽性): "样本量核查：Fig.2A 论文声明n=6实际n=6，Fig.3B n=3实际n=3，一致。末位数字核查：Source Data Fig.4.xlsx / 'Fig4c' 的肿瘤体积列(n=120)末位数字高度集中于0与5，与自然测量数据预期分布明显不符，属数据完整性问题。统计量核算：表2处理组所报均值在所述例数下数学上无法成立，存在不一致。"

methodology_review (有问题时): "研究设计部分声明每组生物学重复n=6，但图4A对应源数据仅含5个数据点，正文与数据不一致。其余试剂货号、伦理审批号均完整。"
```

**Language requirements (MANDATORY):**
- **NEVER** include code/script filenames in review text (e.g., scan_columns.py, generate_report.py). Use neutral descriptions instead: "独立列对扫描", "跨图数据比对检测", "样本量核查脚本"
- **NEVER** use absolute/accusatory language: 造假、篡改、伪造、编造、不端、欺诈、操纵
- **USE** neutral descriptive alternatives: 异常、完整性问题、规范问题、非自然生成、重复或异常
- Example replacements:
  - ~~数据造假~~ → 数据完整性问题
  - ~~数据篡改~~ → 数据异常
  - ~~人工编造~~ → 非自然生成
  - ~~学术不端~~ → 学术规范问题
  - ~~引用造假~~ → 引用异常
  - ~~图像操纵~~ → 图像异常
- Reports describe **observations and patterns**, not conclusions about intent

The script automatically:
- Generates PDF at `data/output/review_reports/review_{DOI_SLUG}.pdf`
- Updates `yujing_quanliang` with:
  - `review_result` = "高风险" or "低风险"
  - `review_report_url` = `http://10.119.9.99/review_reports/review_<DOI_SLUG>.pdf`
  - `review_time` = current timestamp
  - `status` = 2

The script automatically copies generated PDFs to the nginx-served directory (`data/output/review_v2/`), so no manual copy step is needed.

**DO NOT manually run SQL UPDATE for review_result/status without running this script first.**

### 10. Report to User

Show a summary table:
- Total papers reviewed
- Confirmed high risk count
- Needs manual check count (grouped with high risk in DB)
- Suggested downgrade count
- False positive rate

## Important Notes

- **DB constraint**: Only UPDATE `yujing_quanliang`. Never INSERT/UPDATE `yujing` table.
- **Binary results**: DB only stores "高风险" or "低风险". 结论措辞统一为"建议高风险"/"建议低风险"，不使用"需人工复查"或"中风险"。
- **PDF location**: `data/output/review_v2/` — nginx maps `/review_reports/` to this directory (configured in `/etc/nginx/sites-available/knowledge-base`)
- **Concurrent safety**: Each review process works on independent papers, no shared state.
- **Default to caution**: When uncertain, keep as high risk. It's better to have a few false positives than to miss real fraud.
- **Source data inspection is mandatory**: Do NOT downgrade a paper without inspecting its source data files.
