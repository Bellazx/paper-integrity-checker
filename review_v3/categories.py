"""Issue categories for severity calibration.

The calibrator classifies each confirmed issue into one category; the severity
follows from the category in code. Asking a model to weigh a long list of
exceptions gave inconsistent severities; choosing one category with a stated
reason is a narrower question, and the mapping to "substantive" stays fixed.
"""
from __future__ import annotations

SUBSTANTIVE, MINOR = "substantive", "minor"

# code: (label, severity, when it applies)
CATEGORIES: dict[str, tuple[str, str, str]] = {
    "S1": ("数据真实性", SUBSTANTIVE,
           "不同实验、样品、条件或时间点的测量值或图像被逐值复用、成块复制或拼接；或测量值出现无法由列语义解释的非自然规律"
           "（小数尾串继承、固定比例、CV=0、末位填充）"),
    "S2": ("主要结果的数据载体互斥", SUBSTANTIVE,
           "主要结果（性能值、效应量、核心计数）在表、图、补充表、源数据之间给出互斥数值，且已确认两处是同一个量、同一条件、"
           "同一统计量、同一汇总范围，这一矛盾改变了某个比较、排序、推荐或结论；或者同类互斥在实验结果中独立出现 ≥3 处"
           "（同一个差值在不同角度、面板或复述句中重复出现只算一处）"),
    "S3": ("主张被本文图表否定", SUBSTANTIVE,
           "论文对核心比较或主要结论明确写出的主张（本文方法优于基线、在某范围内全部最佳、改进幅度、某现象被消除或不受影响、"
           "变化方向）被本文印出的图表数值直接否定（方向相反或差异可忽略）；包括用论文自己写明的检测阈值或读取规则读论文自己的"
           "结果图，读数与“某现象被消除、不受影响、改善到某值”这类明确主张相反"),
    "S4": ("系统性错配", SUBSTANTIVE, "论文据以比较或下结论的结果被成行/成块互换、样品标签或工况互换，使结论归属到错误条件"),
    "S5": ("统计量不可能成立", SUBSTANTIVE,
           "报告的关键统计量在数学上不可能由所述数据得到（如所述样本量下达不到的 p 值、GRIM 不可行的均值）"),
    "S6": ("方法按印出公式起相反作用", SUBSTANTIVE,
           "所提或被评价方法的核心计算公式按字面会使方法起相反作用（如罚项变成奖励、优化方向颠倒），同段或邻近文字没有写明"
           "正确方向，且论文没有其他约束或步骤保证其正确"),
    "M1": ("孤立单格或小差异", MINOR,
           "主要结果中孤立的一个派生格、置信区间界、补充表单值，或合计与分项相差很少（相对 <1%），不改变任何比较、排序或结论"),
    "M2": ("正文转述错误", MINOR, "正文的数值、方向、对象或图号与自洽的图表不符，结论方向不变"),
    "M3": ("公式与方法问题", MINOR,
           "公式、推导、量纲（如 dB 与线性值混用）、符号定义、方法设计或判据合理性方面的问题，未达到 S6"),
    "M4": ("统计口径未说明", MINOR,
           "统计量、汇总方式、重复次数、随机种子未说明；图中误差棒/箱线图与表内 SD 的口径未确认相同"),
    "M5": ("图中点数少于样本量", MINOR, "散点图中能数出的点少于声明的样本量（点重叠常见，不能证明样本被删减）"),
    "M6": ("编辑与图注问题", MINOR, "图注、面板字母、图号、交叉引用、术语、单位写法、排版"),
    "M7": ("参考文献著录", MINOR, "参考文献编号、作者、页码、刊名、重复著录、悬空引用"),
    "M8": ("表述超出数据", MINOR, "摘要/结论措辞超出数据支持范围或缺少本文结果支撑，但没有被本文图表数值否定"),
    "M9": ("换口径重算才出现的矛盾", MINOR,
           "只有复核人员改用论文没有使用的口径、单位、定义、外部模型或自选图像指标重新计算时才出现的矛盾；以及论文没有印出、"
           "由复核人员推算出的数值"),
    "M10": ("次要信息", MINOR, "对比表中引自其他文献的数值、描述性统计、可复现性说明等次要信息中的问题"),
}


def severity(code: str) -> str | None:
    entry = CATEGORIES.get(str(code or "").strip().upper())
    return entry[1] if entry else None


def table() -> str:
    """Category table for prompts."""
    rows = ["| 类别 | 名称 | 定级 | 适用情形 |", "|---|---|---|---|"]
    for code, (label, sev, when) in CATEGORIES.items():
        rows.append(f"| {code} | {label} | {'实质问题' if sev == SUBSTANTIVE else '规范问题'} | {when} |")
    return "\n".join(rows)
