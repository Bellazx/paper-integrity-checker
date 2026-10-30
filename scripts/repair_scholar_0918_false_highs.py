#!/usr/bin/env python3
"""Record independent rechecks and repair two unsupported 0918 high verdicts."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path


BASE = Path("/home/sjtu/paper-integrity-checker")
ROUND = BASE / "data" / "yujing_scholar_0918_round_1"

REPAIRS = {
    1601005: {
        "doi": "10.1007/s11802-014-1999-5",
        "image_review": (
            "逐页核对 Fig.1–Fig.7，未发现图像重复、拼接或异常编辑。Fig.5 风场箭头和 Fig.7 "
            "散点属于展示层抽稀及不同空间网格配准结果，不能用可见点数反推原始 40×40 计算子块数量。"
        ),
        "data_review": (
            "材料未包含可审计源数据表。正文所述每景 40×40 个 SAR 子块是计算网格；Fig.5 为抽稀后的"
            "风矢量展示，Fig.7 为与 0.25° QuikSCAT 网格匹配后的对比点，三者统计口径不同，不构成样本量矛盾。"
            "COR 与 RMSE 数值前后一致，未发现可定位的数据完整性问题。"
        ),
        "ref_review": "22 条参考文献完整可读，正文引用主题与文献列表一致；个别英文拼写错误属于文字校对问题。",
        "methodology_review": (
            "CMOD4/CMOD5、RADARSAT2、QuikSCAT 与 ECMWF 的使用和时间线合理。正文写 QuikSCAT 为 "
            "11:00 UTC，而 Fig.6 图注为 05:00 UTC，确认存在 6 小时时间说明不一致；该处更符合单一文字/"
            "图注错误，未发现第二项独立证据支持将其上升为完整性高风险。"
        ),
        "reason": (
            "独立复核后不采纳此前高风险推断：40×40 SAR 计算子块、抽稀显示的风矢量以及与 QuikSCAT "
            "网格匹配后的散点不是同一统计口径，不能直接比较点数。正文 11:00 UTC 与 Fig.6 图注 05:00 UTC "
            "确有不一致，但单一时间说明笔误不足以支持高风险。可用材料中未发现具体、可重复定位的完整性问题，"
            "最终建议低风险。"
        ),
    },
    1601048: {
        "doi": "10.19551/j.cnki.issn1672-9129.2017.06.005",
        "image_review": (
            "逐图核对 Fig.1–Fig.12，未发现图像重复、拼接或异常编辑；Table 1 与 Fig.10 的数值趋势一致。"
        ),
        "data_review": (
            "材料未包含可审计源数据表。PDF 中可读取的 Table 1 与 Fig.10 逐项对应，算法参数和实验描述"
            "未见可定位的数据完整性矛盾。"
        ),
        "ref_review": (
            "上传 PDF 共 5 页，末页在参考文献第 [6] 条中途直接结束，文件本身明显缺页/截断；因此正文引用的 "
            "[7]–[13] 未出现在当前文件中属于材料不完整，不能据此认定原论文参考文献异常。可读的 [1]–[6] "
            "与正文主题相符。"
        ),
        "methodology_review": (
            "PCA、Concave Hull、RANSAC、ROS/Gazebo/MoveIt 与 UR5 的方法链及时间线合理。资助编号排版"
            "存在疑似重复，但属于著录规范问题，不足以支持完整性高风险。"
        ),
        "reason": (
            "独立复核确认当前上传 PDF 在末页参考文献第 [6] 条中途截断，属于材料缺页；不能把因此未显示的 "
            "[7]–[13] 作为论文自身缺失引用的证据。图像、可读数据和方法学亦未发现可定位的完整性问题。"
            "最终建议低风险。"
        ),
    },
}


def load_one(path: Path) -> tuple[object, dict]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    item = raw[0] if isinstance(raw, list) else raw
    return raw, item


def main() -> int:
    audit_dir = ROUND / "independent_rechecks"
    audit_dir.mkdir(parents=True, exist_ok=True)
    for batch_num, replacement in REPAIRS.items():
        path = ROUND / "results_judge" / f"batch_{batch_num:04d}.json"
        raw, item = load_one(path)
        if str(item.get("doi", "")).lower() != replacement["doi"].lower():
            raise RuntimeError(f"DOI mismatch: batch_{batch_num}")
        audit = audit_dir / f"batch_{batch_num:04d}_original_judge.json"
        if not audit.exists():
            audit.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
        item.update(replacement)
        item.update({
            "result": "低风险",
            "verdict": "建议低风险",
            "trigger": "scholar_0918_round1 独立二次确认",
            "dimension_verdicts": {
                "image": "建议低风险", "data": "建议低风险",
                "ref": "建议低风险", "methodology": "建议低风险",
            },
            "independent_recheck_time": datetime.now().isoformat(timespec="seconds"),
        })
        output = [item] if isinstance(raw, list) else item
        path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"repaired batch_{batch_num}: {replacement['doi']} -> 低风险", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
