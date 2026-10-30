"""Prompt builders. Rules come from review_v3/rules/*.md (version controlled)."""
from __future__ import annotations

import json
from functools import lru_cache

from review_v3 import config

SHARD_LABEL = {
    "image": "图像重复与拼接告警",
    "image_scan": "图像逐页浏览",
    "stats": "统计与数值复算",
    "data": "源数据与数值规律",
    "consistency": "图文一致性（图注、正文与源数据）",
    "ref": "参考文献",
    "methodology": "研究设计与统计",
}
RULE_FILE = {"image": "image", "image_scan": "image", "stats": "methodology", "data": "data", "consistency": "consistency", "ref": "ref", "methodology": "methodology"}

TOOL_HELP = """## 资料包工具 pv3（已预先抽取，优先使用，不要自己重抽 PDF）
- `pv3 info`：论文、文档、图注、数据文件、告警概况
- `pv3 page <doc> <页> [止页]`：逐页正文（doc 为 main 或补充文档 id）；`pv3 find '<正则>' [--doc main] [--context 1]`：全文检索带页码
- `pv3 captions [--kind figure|table]`：图注/表题及所在页；`pv3 refs [编号…]`：参考文献段或指定条目
- `pv3 files`：数据文件与各 sheet 列（含列角色：independent_variable/measurement 等）
- `pv3 sheet <文件> [sheet] [--rows 1:40] [--cols A:F] [--find 值]`：流式读表（带行号和列字母）
- `pv3 colcmp <文件> <sheet> <列A> <列B> [--file-b F --sheet-b S] [--start 2]`：两列逐值相同比例、差值、比值、r、斜率
- `pv3 render <doc> <页> [--clip x0,y0,x1,y1] [--dpi 150]`：渲染整页或裁剪区域为缩放后的 PNG（坐标单位 pt），再用 Read 查看
- `pv3 locate <doc> <页> '<文字>'`：文字在页面上的坐标，便于裁剪
- `pv3 view <图片路径> [--box x0,y0,x1,y1]`：缩放/裁剪已有图片（如初检标注图）后再 Read
- `pv3 alerts image|ref`：图像告警、参考文献提示
- `pv3 forensic scan-digits|scan-decimal-tails`：末位数字/SD 规律、小数尾串扫描（结果有缓存）
- `pv3 refcheck [--all]`：参考文献逐条 CrossRef 核验结果（后台预先完成，只列出需关注的条目）
- `pv3 crossref --doi <DOI>` / `pv3 crossref --query '<题名 作者 期刊>'`：权威著录查询（单条复查用）
- `pv3 mem '<关键词>' [--dim image|data|consistency|ref|methodology]`：检索既往复核经验
- `pv3 item <id>`：查看台账条目的确定性细节；`pv3 ledger show [--open]`；`pv3 ledger update <<'EOF' [...] EOF`：写入结论
"""


@lru_cache(maxsize=None)
def rule(name: str) -> str:
    return (config.RULES_DIR / f"{name}.md").read_text(encoding="utf-8")


def _paper_block(dossier: dict) -> str:
    paper = dossier.get("paper") or {}
    lines = [f"- DOI：{dossier.get('doi')}", f"- 题名：{paper.get('title') or '（见 pv3 info）'}"]
    if paper.get("journal"):
        lines.append(f"- 期刊：{paper['journal']}")
    docs = "；".join(f"{d['id']}={d['name']}（{d.get('pages')}页）" for d in dossier.get("docs") or [])
    lines.append(f"- 文档：{docs or '未找到 PDF'}")
    lines.append(f"- 数据文件：{len(dossier.get('data_files') or [])} 个；原始材料目录：{dossier.get('input_dir')}")
    if dossier.get("warnings"):
        lines.append("- 资料包提示：" + "；".join(dossier["warnings"]))
    return "\n".join(lines)


def _item_lines(items: list[dict], limit: int = 360) -> str:
    rows = []
    for item in items:
        row = f"- `{item['id']}`：{item.get('title', '')}"
        hint = str(item.get("hint") or "")
        if hint:
            row += f"\n  细节：{hint[:limit]}{'…（pv3 item 查看全部）' if len(hint) > limit else ''}"
        rows.append(row)
    return "\n".join(rows)


def investigator(shard: dict, dossier: dict, memory_hints: str) -> str:
    kind = shard["kind"]
    extra = ""
    if kind == "consistency":
        extra = "\n本部分发现的问题默认属于 data 维度；属于研究设计/统计的写 \"dimension\": \"methodology\"，属于图像的写 \"image\"。\n"
    return f"""你是学术论文诚信复核人员，负责本论文「{SHARD_LABEL[kind]}」部分（分片 {shard['id']}）的核查。其他部分由同事并行负责，你只完成本台账中的条目，但发现台账外的问题也要登记。

## 论文
{_paper_block(dossier)}

{TOOL_HELP}
{rule('common')}

{rule('grading')}

{rule(RULE_FILE[kind])}
{extra}
## 相关的既往复核经验（按需参考，用 pv3 mem 可检索更多）
{memory_hints or '（无）'}

## 本台账条目（{len(shard['items'])} 项，每项都必须写入结论）
{_item_lines(shard['items'])}

## 工作方式
1. 先运行 `pv3 info` 与 `pv3 ledger show` 了解概况。
2. 按条目逐项核查，每完成一项或一小批就用 `pv3 ledger update` 写入台账（被拒绝时按提示修正后重新提交）。
3. 全部条目写入后运行 `pv3 ledger show --open` 确认没有未处理项，然后回复“完成”。
"""


def investigator_continue(state: dict, open_ids: list[str]) -> str:
    shown = "、".join(open_ids[:40]) + ("…" if len(open_ids) > 40 else "")
    if state.get("no_progress") or (state.get("chunks", 0) >= 2 and state.get("metrics")
                                    and not any(m.get("closed") for m in state["metrics"])):
        return (f"上一段没有写入任何结论，台账仍有 {len(open_ids)} 项未处理：{shown}。"
                "现在先用 `pv3 ledger update` 写入你已经核查到的结论（可以先写部分条目，"
                "或按类写整组结论；证据不足的写 unresolved 并说明缺什么），然后再继续核查剩余条目。")
    return (f"继续。台账还有 {len(open_ids)} 项未处理：{shown}。"
            "已写入的条目不要重复核查；逐项完成并及时写入台账。完成后运行 `pv3 ledger show --open` 确认。")


def wrapup(open_ids: list[str]) -> str:
    shown = "、".join(open_ids[:40]) + ("…" if len(open_ids) > 40 else "")
    return ("现在需要收尾（本阶段时间已到或长时间没有写入结论）。不要再开始新的核查或运行新的计算。"
            f"立即用 1–3 次 `pv3 ledger update` 为所有未处理条目写入结论（{shown or '无'}）："
            "已有足够证据的写 issue/cleared/not_applicable 并给出依据；证据确实不足的写 unresolved，"
            "并在 note 中写明已核查到哪一步、还缺什么。写完即结束。")


def restart_note() -> str:
    return ("\n\n## 续接说明\n上一段会话已中断。台账中已有的结论有效，不要重复核查；"
            "先运行 `pv3 ledger show --open`，只处理未处理的条目。\n")


def _compact_record(record: dict) -> dict:
    keep = ("status", "severity", "dimension", "location", "issue_type", "finding", "why", "basis", "note")
    out = {k: record.get(k) for k in keep if record.get(k)}
    if record.get("evidence"):
        out["evidence"] = record["evidence"]
    return out


def challenger(shard: dict, dossier: dict, memory_hints: str, first: dict, selected: set[str]) -> str:
    kind = shard["kind"]
    summary = []
    for item in shard["items"]:
        if item["id"] not in selected:
            continue
        rec = (first.get("items") or {}).get(item["id"], {})
        summary.append(f"- `{item['id']}`（前一轮：{rec.get('status', 'open')}"
                       f"{'/' + rec['severity'] if rec.get('severity') else ''}）：{item.get('title', '')[:160]}")
    added = [r for r in (first.get("items") or {}).values()
             if str(r.get("id", "")).startswith("new:") and r["id"] in selected]
    for rec in added:
        summary.append(f"- `{rec['id']}`（前一轮新增：{rec.get('status')}/{rec.get('severity')}）：{rec.get('title', '')[:160]}")
    return f"""你是学术论文诚信复核的独立核验人员，负责本论文「{SHARD_LABEL[kind]}」部分（分片 {shard['id']}）的反证核验。

## 论文
{_paper_block(dossier)}

{TOOL_HELP}
{rule('common')}

{rule('grading')}

{rule(RULE_FILE[kind])}

{rule('challenge')}

## 相关的既往复核经验
{memory_hints or '（无）'}

## 需要你独立给出结论的条目（{len(summary)} 项；`pv3 item <id>` 查看前一轮的完整结论与依据，字段 first）
{chr(10).join(summary)}

## 工作方式
1. 运行 `pv3 ledger show` 查看你的核验台账。
2. 优先核验前一轮的确认问题和高价值排除项，再处理其余条目；每项写入你自己的结论与亲自核查的依据。
3. 全部写入后运行 `pv3 ledger show --open` 确认，然后回复“完成”。
"""


def arbiter(dimension: str, dossier: dict, items: list[dict]) -> str:
    rows = [f"- `{item['id']}`：{item.get('title', '')[:200]}" for item in items]
    rule_name = {"image": "image", "data": "data", "ref": "ref", "methodology": "methodology"}.get(dimension, "data")
    return f"""你是学术论文诚信复核的仲裁人员。以下条目两轮独立核查的结论不一致或未能完成，请逐项亲自核查后给出最终结论。

## 论文
{_paper_block(dossier)}

{TOOL_HELP}
{rule('common')}

{rule('grading')}

{rule(rule_name)}
{rule('consistency') if dimension == 'data' else ''}

{rule('arbitrate')}

## 待仲裁条目（{len(items)} 项）
{chr(10).join(rows)}

完成后运行 `pv3 ledger show --open` 确认，然后回复“完成”。
"""


def calibrator(dossier: dict, items: list[dict]) -> str:
    rows = [f"- `{item['id']}`（{item.get('severity')}，{item.get('dimension')}）：{str(item.get('location', ''))[:120]}"
            f" —— {str(item.get('issue_type', ''))[:60]}" for item in items]
    return f"""你是学术论文诚信复核的定级复核人员。

## 论文
{_paper_block(dossier)}

{TOOL_HELP}
{rule('common')}

{rule('grading')}

{rule('calibrate')}

## 待定级的问题（{len(items)} 项）
{chr(10).join(rows)}

完成后运行 `pv3 ledger show --open` 确认，然后回复“完成”。
"""


def writer(doi: str, dossier: dict, facts: dict, verdicts: dict, gaps: list[str] | None = None,
           draft: dict | None = None) -> str:
    paper = dossier.get("paper") or {}
    body = f"""{rule('writing')}

## 论文
DOI：{doi}
题名：{paper.get('title') or ''}

## 已确定的结论（原样填写）
result：{verdicts['result']}
verdict：{verdicts['verdict']}
dimension_verdicts：{json.dumps(verdicts['dimension_verdicts'], ensure_ascii=False)}

## 已核实的事实（按维度；issues 按给定编号写，cleared/not_applicable 按类简写但须提及 cite 中的位置词）
```json
{json.dumps(facts, ensure_ascii=False, indent=1)}
```
"""
    if gaps:
        body += ("\n## 上一稿未通过发布校验，需要修正的具体问题\n- " + "\n- ".join(gaps)
                 + "\n只修正这些问题涉及的表述（补写遗漏的位置词、标签、编号或结论表述），不得新增未核实的事实。"
                 "本阶段没有任何工具可用，不要尝试调用工具或查找程序，直接输出修正后的完整 JSON。\n")
    if draft:
        body += "\n## 上一稿\n```json\n" + json.dumps(draft, ensure_ascii=False) + "\n```\n"
    body += "\n现在只输出 JSON 对象。"
    return body
