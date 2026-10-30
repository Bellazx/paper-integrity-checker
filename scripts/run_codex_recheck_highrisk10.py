#!/usr/bin/env python3
"""Run the current Codex CLI model over every final high-risk paper."""
from __future__ import annotations

import json
import argparse
import os
import subprocess
import time
from pathlib import Path

BASE = Path("/home/sjtu/paper-integrity-checker")
ROUND = BASE / "data/yujing_scholar_hugaowei_fanlingzhong_wangdandan_round_1"
OUT = ROUND / "codex_cli_astra_recheck_20261022"
OUT.mkdir(parents=True, exist_ok=True)


def load_batch_items():
    result = []
    for path in sorted((ROUND / "batches").glob("batch_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        item = data[0] if isinstance(data, list) else data
        result.append((path.stem, item))
    return result


def final_high_dois():
    result = []
    for path in sorted((ROUND / "results_judge").glob("batch_*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        item = data[0] if isinstance(data, list) else data
        if str(item.get("result", "")).strip() == "高风险":
            result.append(str(item.get("doi", "")).strip().lower())
    return set(result)


def make_prompt(batch_id: str, item: dict) -> str:
    doi = item["doi"]
    result_path = ROUND / "results_judge" / f"{batch_id}.json"
    report_name = result_path
    # The public URL filename is also present in the database, but local report
    # generation uses the stable DOI slug.
    slug = doi.replace("/", "_").replace("－", "")
    report_candidates = list((BASE / "data/output/yujing_scholar/review_v2").glob(f"review_{slug}*.pdf"))
    report = report_candidates[0] if report_candidates else (BASE / "data/output/yujing_scholar/review_v2" / f"review_{slug}.pdf")
    return f"""你是独立的论文完整性复核员。请使用 gpt-6-astra 对一篇已经判定为复审高风险的论文重新核验。严禁改动数据库、现有报告、结论和原始材料。仅可在当前工作目录创建临时渲染图及核算脚本。

DOI：{doi}
批次文件：{ROUND / 'batches' / (batch_id + '.json')}
最终复审 JSON：{result_path}
复审 PDF（若存在）：{report}
原始材料目录：{item.get('input_dir')}

请实际读取上述 JSON、复审 PDF、原始论文/补充材料和 JSON 中引用的证据截图。先概括论文的研究问题、实验/模拟设计和该学科中相关计算逻辑，再重新计算或逐项核对高风险的核心数值；不要只复述已有结论，也不要把单纯排版问题自动升级为完整性风险。输出中文，严格包含以下四段：
1. Codex结论：保留高风险 / 建议降级 / 证据不足（三选一）。
2. 核心核验：列出最关键的原文位置、数值、复算值，并说明是否足以支持高风险；如果原结论有误，明确指出错误。
3. PDF格式检查：语言是否简洁通俗、是否直击要害、是否包含与结论直接对应的核心证据截图；列出具体缺陷或写“符合”。
4. 建议：是否需要重写报告或调整数据库结论。

风险提示不等同于已证明存在学术不端。"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('--concurrency', type=int, default=3)
    parser.add_argument('--exclude-batch', action='append', default=[])
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 3:
        parser.error('concurrency must be between 1 and 3')
    high = final_high_dois()
    items = [(bid, item) for bid, item in load_batch_items() if str(item.get("doi", "")).strip().lower() in high]
    if len(items) != 10:
        raise RuntimeError(f"expected 10 high-risk papers, got {len(items)}")
    env = dict(os.environ)
    env["TERM"] = "xterm-256color"
    env["NO_COLOR"] = "1"
    # Keep only a few Astra requests in flight; the provider otherwise starts
    # dropping streams before the model can finish reading the source material.
    pending = [(bid, item) for bid, item in items
               if bid not in args.exclude_batch
               and not ((OUT / f'{bid}.md').exists()
                        and (OUT / f'{bid}.md').stat().st_size > 0)]
    procs = []
    failed = []
    while pending or procs:
        while pending and len(procs) < args.concurrency:
            batch_id, item = pending.pop(0)
            out = OUT / f"{batch_id}.md"
            log = OUT / f"{batch_id}.log"
            prompt = make_prompt(batch_id, item)
            scratch = OUT / batch_id
            scratch.mkdir(exist_ok=True)
            prompt += "\n补充要求：可用项目 venv/bin/python 的绝对路径调用 fitz/openpyxl/scipy。必须渲染并实际查看复审PDF每页（可先做缩略图再查看核心页），不能凭JSON里有截图路径就声称PDF有图。用原论文高分辨率图核对双栏顺序、坐标断轴/基线、归一化和统计方法；色标方向不同不自动等于错误；同一化合物跨分类呈现不自动等于伪重复。凡未实际验证的主张必须标注限制。无网络时不把文献无法访问当风险。不要启动其他agent。最后四段报告须有明确的逐项保留/排除及PDF通过/不通过判断。"
            cmd = [
                "codex", "exec", "--ephemeral", "-m", "gpt-6-astra", "-s", "workspace-write",
                "-C", str(scratch), "-o", str(out), prompt,
            ]
            handle = log.open("w", encoding="utf-8")
            procs.append((batch_id, subprocess.Popen(cmd, cwd=BASE, env=env, stdout=handle, stderr=subprocess.STDOUT), handle))
            print(f"launched {batch_id} {item['doi']}", flush=True)
        remaining = []
        for batch_id, proc, handle in procs:
            rc = proc.poll()
            if rc is None:
                remaining.append((batch_id, proc, handle))
            else:
                handle.close()
                output = OUT / f'{batch_id}.md'
                if rc != 0 or not output.exists() or not output.stat().st_size:
                    failed.append(batch_id)
                print(f"completed {batch_id} rc={rc}", flush=True)
        procs = remaining
        if procs:
            time.sleep(5)
    if failed:
        print(f'failed: {failed}', flush=True)
    return int(bool(failed))


if __name__ == "__main__":
    raise SystemExit(main())
