# Project-owned coverage policy, derived from paper-batch-review coverage_validator.
# Both API review and batch PDF publication import this implementation.
"""Coverage validator — post-review coverage audit.

Every final review must address all four review dimensions and every HIGH finding,
even when one confirmed issue already determines the paper-level verdict. A high-risk
data finding must not short-circuit image or reference review. Low-risk results also
retain the stricter figure-vs-source-data checks below.

Enforced for a 低风险 verdict:
  1. Every deterministic finding (duplicate column pair / cross-sheet reuse /
     decimal-precision mismatch) must be referenced in the review text.
  2. Each main-text figure must have a figure-vs-source-data statement (Step 5G).
  3. The review text must be substantive (not a generic one-liner).
"""
import json
import re
from modules.review_column_roles import is_summary_sample_count_pair


_LOWRISK = ("低风险", "建议低风险")
_FIG_CMP_VERBS = ("一致", "不一致", "对调", "矛盾", "吻合", "相符", "源数据", "图中")
_FIG_RENDER_VERBS = ("渲染", "fitz", "对比", "核对", "曲线形状", "峰位", "柱高",
                     "量级", "趋势", "数值范围", "坐标轴", "数据点",
                     "rendered", "比对", "核查图", "查看图")


def _review_text(result):
    parts = [result.get(k, "") or "" for k in
             ("image_review", "data_review", "ref_review", "methodology_review", "reason")]
    return "\n".join(parts)


def _fig_tokens(fig_name):
    """Extract referenceable tokens for a figure filename, e.g. 'Fig 2', 'Fig.2'."""
    # Publisher asset names commonly use ``gr1``, ``f1`` or ``figure1``
    # instead of the literal ``fig1`` (e.g. Elsevier ``...-gr1_lrg.jpg``).
    # Treat these as the same reader-facing figure token so the mandatory
    # figure-vs-source-data gate can actually be satisfied.
    m = re.search(r'(?:fig(?:ure)?|gr|f)[ _.-]?(\d+)', fig_name, re.IGNORECASE)
    if m:
        n = m.group(1)
        return [f"fig {n}", f"fig.{n}", f"fig. {n}", f"fig{n}", f"figure {n}", f"figure{n}", f"图{n}", f"图 {n}"]
    return []


def _norm_text_token(value):
    return re.sub(r"\s+", " ", str(value or "").strip().lower())


def _usable_token(value):
    token = _norm_text_token(value)
    if not token or token in {"unknown", "#", "col", "value", "values", "data"}:
        return ""
    # Avoid one-character numeric tokens that match unrelated prose.
    if len(token) < 2 and not any("\u4e00" <= ch <= "\u9fff" for ch in token):
        return ""
    return token


def _location_tokens(loc):
    parts = str(loc or "").split("::")
    tokens = []
    if len(parts) >= 2:
        tokens.append(parts[1])
    if len(parts) >= 3:
        tokens.append(parts[2])
    if parts:
        tokens.append(parts[-1])
    return [_usable_token(t) for t in tokens if _usable_token(t)]


def _group_id_tokens(group):
    gid = _usable_token(group.get("group_id"))
    tokens = [gid] if gid else []
    m = re.search(r"(\d+)$", gid or "")
    if m:
        n = m.group(1)
        tokens.extend([f"reuse group {n}", f"复用组{n}", f"复用组 {n}", f"第{n}组", f"第 {n} 组"])
    return tokens


def _figure_label_tokens(label):
    raw = _usable_token(label)
    if not raw:
        return []
    return [raw, f"fig {raw}", f"fig.{raw}", f"fig{raw}", f"图{raw}", f"图 {raw}"]


def _reuse_group_tokens(group):
    tokens = []
    tokens.extend(_group_id_tokens(group))
    header_sig = group.get("header_signature") or ""
    for part in str(header_sig).split("|"):
        tok = _usable_token(part)
        if tok:
            tokens.append(tok)
    for h in group.get("headers_sample", []) or []:
        tok = _usable_token(h)
        if tok:
            tokens.append(tok)
    for label in group.get("figure_labels", []) or []:
        tokens.extend(_figure_label_tokens(label))
    for loc in group.get("locations_sample", []) or []:
        tokens.extend(_location_tokens(loc))
    for rep in group.get("representatives", []) or []:
        for loc in rep.get("locations", []) or []:
            tokens.extend(_location_tokens(loc))
    seen = set()
    out = []
    for token in tokens:
        token = _usable_token(token)
        if token and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def _mentions_reuse_concept(text):
    return any(k in text for k in ("跨表", "跨 sheet", "跨sheet", "复用", "重复", "reuse", "re-use"))


def _reuse_group_covered(group, text):
    tokens = _reuse_group_tokens(group)
    if any(token in text for token in tokens):
        return True

    count = group.get("count")
    if count is not None and str(count) in text and _mentions_reuse_concept(text):
        return True

    # A concise low-risk review may refer to a single grouped class globally
    # (e.g. "全部跨表复用组均为同一源数据导出的合理复用"). Treat that as covered
    # only when the evidence actually has one group; the caller enforces this.
    return False


def validate(result, evidence, enforce=False):
    """Return (final_result, gaps). Mutates a copy of result.

    final_result: dict with _coverage_status/_coverage_gaps annotations.
    If enforce=True, low-risk results with gaps are upgraded to high-risk.
    gaps: list of human-readable coverage gaps (empty if fully covered).
    """
    out = dict(result)
    verdict = (result.get("verdict") or "") + (result.get("result") or "")
    text = _review_text(result).lower()
    gaps = []

    # Paper-level risk does not waive multi-dimension coverage.  High-risk
    # reviews still need explicit conclusions for all dimensions and every HIGH
    # finding; only the figure-vs-source release gate remains low-risk-specific.
    is_lowrisk = any(k in verdict for k in _LOWRISK) and "高风险" not in (result.get("result") or "")

    section_fields = {
        "image": "image_review",
        "data": "data_review",
        "ref": "ref_review",
        "methodology": "methodology_review",
    }
    dimension_verdicts = result.get("dimension_verdicts") or {}
    valid_dimension_verdicts = {"建议高风险", "建议低风险"}
    for dimension, field in section_fields.items():
        if len(str(result.get(field) or "").strip()) < 12:
            gaps.append(f"{dimension}维度缺少实质复核结论")
        if dimension_verdicts.get(dimension) not in valid_dimension_verdicts:
            gaps.append(f"{dimension}维度缺少明确风险结论")

    det = (evidence or {}).get("deterministic_findings", {})

    # 1. deterministic findings must be referenced (by sheet or header token)
    # Legacy evidence can contain identical sample-count columns in paired
    # summary blocks. Equal n is not duplication of measured observations.
    # Keep all raw measurements and independent sample-size mismatch findings.
    duplicate_pairs = [pair for pair in det.get("duplicate_column_pairs", []) or []
                       if not is_summary_sample_count_pair(pair)]
    duplicate_groups = {}
    for dp in duplicate_pairs:
        duplicate_groups.setdefault((dp.get("file") or "", dp.get("sheet") or ""), []).append(dp)

    # A concise report may adjudicate dozens of sparse-matrix column pairs as
    # one class. Accept that grouped coverage when it names the workbook/sheet,
    # explicitly discusses duplicate columns/pairs, and states that the whole
    # class was checked. Requiring every pairwise combination to be repeated in
    # reader-facing prose makes reports unusably long without adding evidence.
    covered_duplicate_groups = set()
    for key, members in duplicate_groups.items():
        file_name, sheet_name = key
        file_token = _norm_text_token(file_name.rsplit("/", 1)[-1].rsplit(".", 1)[0])
        short_file_match = re.search(r"(?:mmc|moesm|table|source)[ _.-]?\d+", file_token, re.I)
        short_file_token = _norm_text_token(short_file_match.group(0)) if short_file_match else ""
        sheet_token = _norm_text_token(sheet_name)
        has_location = bool(
            (sheet_token and sheet_token in text)
            or (file_token and file_token in text)
            or (short_file_token and short_file_token in text)
        )
        has_duplicate_cue = any(cue in text for cue in (
            "重复列", "列对", "dup-col", "dup col", "列值重复", "稀疏矩阵",
        ))
        has_group_verdict = any(cue in text for cue in (
            "全部", "均为", "均已", "整类", "组", "假阳性", "不构成", "异常",
        ))
        if has_location and has_duplicate_cue and has_group_verdict:
            covered_duplicate_groups.add(key)

    for dp in duplicate_pairs:
        if (dp.get("file") or "", dp.get("sheet") or "") in covered_duplicate_groups:
            continue
        sheet = (dp.get("sheet") or "").lower()
        ha = (dp.get("col_a_header") or "").lower()
        hb = (dp.get("col_b_header") or "").lower()
        if dp.get("cross_group"):
            ga = (dp.get("group_a") or "")
            gb = (dp.get("group_b") or "")
            # Extract distinct tokens between the two groups to find the
            # distinguishing words (e.g. "KNN" vs "PZT", "S9 a" vs "S9 b").
            ga_parts = set(ga.lower().replace("|", " ").split())
            gb_parts = set(gb.lower().replace("|", " ").split())
            diff_a = {t for t in ga_parts - gb_parts if len(t) > 2}
            diff_b = {t for t in gb_parts - ga_parts if len(t) > 2}
            if diff_a and diff_b:
                # Groups differ in substance (e.g. KNN vs PZT).
                # Require the sheet name to appear AND both distinguishing
                # tokens to appear near the sheet context — not just anywhere.
                # We check "{sheet}.*{tok}" or "{tok}.*{sheet}" within a
                # 200-char window to avoid cross-paragraph false coverage.
                def _near_sheet(tok, sheet_str, full_text, window=80):
                    """Check if tok appears within window chars of sheet_str."""
                    for m in re.finditer(re.escape(sheet_str), full_text):
                        start = max(0, m.start() - window)
                        end = min(len(full_text), m.end() + window)
                        if tok in full_text[start:end]:
                            return True
                    return False

                sheet_mentioned = sheet and sheet in text
                if sheet_mentioned:
                    a_near = any(_near_sheet(tok, sheet, text) for tok in diff_a)
                    b_near = any(_near_sheet(tok, sheet, text) for tok in diff_b)
                else:
                    a_near = False
                    b_near = False
                if not (sheet_mentioned and a_near and b_near):
                    gaps.append(
                        f"跨组重复列对(可能涉及不同样品/材料): {dp.get('sheet')} "
                        f"[{ga} {dp.get('col_a_header')} <> "
                        f"{gb} {dp.get('col_b_header')}]"
                    )
            else:
                # Groups differ only in trivial tokens (e.g. "a1" vs "a2") —
                # fall back to the standard sheet/header check.
                if sheet and sheet not in text and ha not in text and hb not in text:
                    gaps.append(f"未说明重复列对: {dp.get('sheet')} [{dp.get('col_a_header')}<>{dp.get('col_b_header')}]")
        else:
            if sheet and sheet not in text and ha not in text and hb not in text:
                gaps.append(f"未说明重复列对: {dp.get('sheet')} [{dp.get('col_a_header')}<>{dp.get('col_b_header')}]")
    reuse_groups = det.get("cross_sheet_reuse_groups") or []
    if reuse_groups:
        global_reuse_summary = (
            len(reuse_groups) == 1
            and _mentions_reuse_concept(text)
            and any(k in text for k in ("组", "类", "全部", "均", "代表", "样例"))
        )
        for group in reuse_groups:
            if not global_reuse_summary and not _reuse_group_covered(group, text):
                gid = group.get("group_id") or "?"
                count = group.get("count")
                sample = (group.get("locations_sample") or ["?"])[0]
                gaps.append(f"未说明跨表复用分组: {gid} (共{count}项，样例 {sample})")
    else:
        for ru in det.get("cross_sheet_reuse", []):
            locs = ru.get("locations", [])
            if locs and not any(loc.split("::")[-1].lower() in text for loc in locs):
                gaps.append(f"未说明跨表重复: {locs[0] if locs else '?'}")
    decimal_items = det.get("decimal_precision_mismatch", []) or []
    if decimal_items and not any(cue in text for cue in (
        "小数精度", "小数位", "精度不一致", "精度特征", "decimal_uniformity",
        "decimal precision mismatch", "decimal_precision_mismatch", "decimal-precision mismatch",
    )):
        gaps.append(f"未说明小数精度不一致类别（共{len(decimal_items)}项）")

    trailing_high = [
        item for item in det.get("trailing_digit_uniformity", []) or []
        if str(item.get("severity") or "").lower() == "high"
    ]
    if trailing_high and not any(cue in text for cue in (
        "末位", "尾数", "末4位", "末 4 位", "trailing_digit_uniformity",
    )):
        gaps.append(f"未说明HIGH末位数字异常类别（共{len(trailing_high)}项）")

    passage_high = [
        item for item in det.get("passage_offset_pattern", []) or []
        if str(item.get("severity") or "").lower() == "high"
    ]
    if passage_high and not any(
        cue in text for cue in ("跨行", "伪重复", "整数偏移", "passage", "代次", "跨时间点", "固定比例", "固定倍数", "等比", "fixed_ratio")
    ):
        gaps.append(f"未说明HIGH跨行伪重复类别（共{len(passage_high)}项）")

    # 1b. Every HIGH automated finding must be ADDRESSED (coverage, not fact-determination):
    #     the review must reference each HIGH dimension/test and give it a verdict word
    #     (假阳性/正常/已知模式 OR 异常/问题/重复). Findings are grouped by (dim,test) so a
    #     class of 13 identical image hits needs one addressed conclusion, not 13.
    high = (evidence or {}).get("high_findings", [])
    _verdict_words = ("假阳性", "正常", "已知模式", "可解释", "属", "异常", "问题",
                      "重复", "不一致", "复用", "拼接", "一致")
    groups = {}
    for h in high:
        key = (h.get("dim", ""), h.get("test") or h.get("match_type") or h.get("issue_type") or "")
        groups.setdefault(key, []).append(h)
    # Concept cues per finding TYPE — the review must use vocabulary specific to that
    # finding (not just "图"), proving it actually addressed THAT issue. This is still a
    # coverage check (was it discussed + given a verdict), not a fact-determination.
    _type_cues = {
        "full_duplicate": ("重复", "跨页", "同图", "同一图", "duplicate", "phash", "嵌入版"),
        "partial_region": ("局部", "区域", "拼接", "模板", "sift", "partial", "复用"),
        "cross_group_duplicate": ("重复", "相同", "复用", "两列", "列", "overlap", "一致"),
        "verified_manuscript_content_conflict": ("正文表述不一致", "文稿内容冲突", "正文内容矛盾", "稿件内容一致性",
                                                 "正文不一致", "正文矛盾", "正文冲突", "内容不一致", "内容矛盾", "内容冲突",
                                                 "组别顺序", "图号笔误", "计时表述", "时间线"),
        "linear_dependency": ("线性", "依赖", "相关", "r2", "r²", "斜率"),
        "coefficient_of_variation": ("变异系数", "cv", "标准差"),
        "arithmetic_sequence": ("等差", "数列", "序列"),
        "geometric_sequence": ("等比", "数列", "序列"),
        "benfords_law": ("benford", "本福特", "首位"),
        "decimal_uniformity": ("小数", "精度", "位数"),
        # Decimal-injection findings are explained in reader-facing reports as
        # truncation/rounding or decimal-tail inheritance.  They are distinct
        # from decimal_uniformity but use the same plain-language vocabulary.
        "decimal_injection": ("小数", "精度", "位数", "尾数", "截断", "归一化"),
        "value_recycling": ("复用", "重复值", "唯一值", "计数"),
        "rowwise_value_reuse": ("逐行", "同行", "行内复用", "行内重复", "行内值复用", "行内数值复用",
                                "行内数值重复", "rowwise value reuse", "row-wise value reuse",
                                "事件序列", "跨处理组", "行内容", "重复值"),
        "rectangular_matrix_reuse": ("矩阵", "逐格", "整块", "格完全相同", "跨图/跨表", "跨表/跨图"),
        "cross_sheet_row_duplicate": ("跨表整行", "跨表行重复", "筛选子集", "跨表重复"),
        "cross_sheet_column_duplicate": ("跨表列重复", "跨表整列", "共享对照列", "共享横轴", "逐列核查"),
        "same_sheet_channel_duplicate": ("通道", "channel", "不同测量指标"),
        "same_sheet_exact_series_reuse": ("数列复用", "序列重复", "逐值相同", "整列重复"),
        "fixed_ratio_series": ("固定比例", "固定倍数", "等比", "同比例"),
        "semantic_transform_conflict": ("归一化", "换算", "缩放", "变换", "比例"),
        "near_modified_matrix_reuse": ("矩阵", "修改", "近似重复"),
        "digit_edit_matrix_reuse": ("矩阵", "数字修改", "数位修改"),
        "figure_n_mismatch": ("样本量", "样本数", "重复数", "n值"),
        "short_curve_duplicate": ("短序列", "短曲线", "重复列", "逐值相同"),
        "terminal_digit": ("末位", "尾数", "数字分布"),
        "sd_regularity": ("标准差", "sd", "规律"),
        "trailing_digit_uniformity": ("末位", "尾数", "末 4 位", "末4位", "末几位", "末位填充", "末位数字"),
        # The detector also emits this type for longitudinal measurements that
        # share an exact multiplicative step. Reviews normally describe those
        # in reader-facing Chinese as “跨时间点固定比例”, not “passage/offset”.
        "passage_offset_pattern": ("passage", "passages", "伪重复", "跨行", "整数偏移", "offset",
                                   "半步", "通代", "代次", "timepoint", "跨时间点", "固定比例",
                                   "固定倍数", "等比", "fixed_ratio"),
    }
    for (dim, test), items in groups.items():
        cues = [test] + list(_type_cues.get(test, ()))
        if dim == "image_splicing":
            cues += ["拼接", "泳道", "背景", "曝光"]
        if dim == "reference":
            cues += ["参考文献", "doi", "引用", "著录"]
        # data-finding sheet/location token is a strong specific cue
        for it in items:
            loc = (it.get("location") or "").lower()
            if "/" in loc:
                seg = loc.split("/")[1].strip()
                if seg:
                    cues.append(seg)
        if not cues:
            cues = [dim]
        addressed = any(c and c.lower() in text for c in cues) and any(w in text for w in _verdict_words)
        if not addressed:
            gaps.append(f"未对HIGH发现给出结论: {dim}/{test} (共{len(items)}项)")

    # 2. figure-vs-data check must have been PERFORMED (Step 5G). For low-risk
    #    verdicts, we require BOTH comparison language AND render/verification
    #    evidence, covering a minimum number of figures.
    figs = (evidence or {}).get("main_figures", [])
    has_cmp_language = any(v in text for v in _FIG_CMP_VERBS)
    has_render_evidence = any(v in text for v in _FIG_RENDER_VERBS)
    covered_figs = 0
    for fig_idx, fig in enumerate(figs):
        toks = _fig_tokens(fig)
        # Supplementary assets may have opaque names (MOESM*.tif) without a
        # literal Fig/figure token.  The report generator labels these assets
        # by stable ordinal (图1, 图2, ...), so accept that reader-facing token.
        # Always include the ordinal fallback.  Asset names such as
        # ``sf001.tif`` can accidentally match the generic ``f<digits>``
        # pattern, yet the report quite reasonably refers to them as 图1/图2.
        toks = list(toks) + _fig_tokens(f"figure{fig_idx + 1}")
        if toks and any(t in text for t in toks):
            covered_figs += 1
    if is_lowrisk and figs:
        min_figs = min(2, len(figs))
        if not has_cmp_language or covered_figs < min_figs:
            gaps.append(f"未见图文一致性核查证据: 引用主图 {covered_figs}/{len(figs)}"
                        + ("，缺少图-数据比对结论" if not has_cmp_language else "")
                        + (f"，需至少覆盖 {min_figs} 张主图" if covered_figs < min_figs else ""))
        elif not has_render_evidence:
            gaps.append("图文一致性核查缺少渲染对比证据: 未见渲染/对比/核对等操作痕迹，"
                        "需用 fitz 渲染主图并与源数据数值比对")
    elif is_lowrisk:
        # No extracted figure files — still require figure-vs-data comparison
        # evidence in the review text (reviewer should render from PDF directly).
        if not has_cmp_language or not has_render_evidence:
            missing = []
            if not has_cmp_language:
                missing.append("缺少图-数据比对结论")
            if not has_render_evidence:
                missing.append("未见渲染/对比/核对等操作痕迹")
            gaps.append("未见图文一致性核查证据: 无预提取主图文件，"
                        "需从论文PDF渲染主图并与源数据比对。" + "；".join(missing))

    # 3. substantive text
    if len(_review_text(result)) < 60:
        gaps.append("复核文本过短，缺乏具体证据")

    if gaps:
        out["_coverage_status"] = "warning"
        out["_coverage_gaps"] = gaps
        if not enforce or not is_lowrisk:
            return out, gaps
        out["result"] = "高风险"
        out["verdict"] = "建议高风险"
        note = "（覆盖率校验：低风险结论未覆盖以下必查项，按规则上调为建议高风险）"
        out["reason"] = (result.get("reason", "") + "\n" + note + " " + "；".join(gaps)).strip()
        out["_coverage_downgraded"] = True
    else:
        out["_coverage_status"] = "ok"
        out["_coverage_gaps"] = []
    return out, gaps
