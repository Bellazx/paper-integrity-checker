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
    "claims": "核心方法与核心主张",
}
RULE_FILE = {"image": "image", "image_scan": "image", "stats": "methodology", "data": "data", "consistency": "consistency", "ref": "ref", "methodology": "methodology", "claims": "methodology"}

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
2. 按条目逐项核查，每形成一项判断就用 `pv3 ledger update` 写入台账（被拒绝时按提示修正后重新提交）；需要进一步数字化或复算的，先写初步结论，完成后再覆盖更新。
3. 全部条目写入后运行 `pv3 ledger show --open` 确认没有未处理项，然后回复“完成”。
"""


# Repeated in every continuation: a long session may have been compacted and
# lost the format given in the first prompt.
LEDGER_FORMAT = """写入格式（JSON 列表；evidence 可省略，填写时为截图路径列表）：
pv3 ledger update <<'EOF'
[{"id": "条目id", "status": "issue", "severity": "substantive 或 minor", "location": "页码+图表号",
  "issue_type": "问题类别", "finding": "原值与核查值对照", "why": "影响哪个结果或结论", "basis": "核查依据"},
 {"id": "条目id", "status": "cleared 或 not_applicable", "basis": "具体核查依据（位置、数值或对照）"},
 {"id": "条目id", "status": "unresolved", "note": "已核查到哪一步、还缺什么"}]
EOF
同一条目可以多次写入，后写的覆盖先写的：形成初步判断就先写，之后再更新。"""


def investigator_continue(state: dict, open_ids: list[str]) -> str:
    shown = "、".join(open_ids[:40]) + ("…" if len(open_ids) > 40 else "")
    if state.get("no_progress") or (state.get("chunks", 0) >= 2 and state.get("metrics")
                                    and not any(m.get("closed") for m in state["metrics"])):
        return (f"上一段没有写入任何结论，台账仍有 {len(open_ids)} 项未处理：{shown}。"
                "现在先用 `pv3 ledger update` 写入你已经核查到的结论（可以先写部分条目，"
                "或按类写整组结论；证据不足的写 unresolved 并说明缺什么），然后再继续核查剩余条目。"
                "不要再写新的数字化脚本，除非它是得出某一项结论所缺的最后一步。\n" + LEDGER_FORMAT)
    return (f"继续。台账还有 {len(open_ids)} 项未处理：{shown}。"
            "已写入的条目不要重复核查；逐项完成并及时写入台账。完成后运行 `pv3 ledger show --open` 确认。\n"
            + LEDGER_FORMAT)


def wrapup(open_ids: list[str]) -> str:
    shown = "、".join(open_ids[:40]) + ("…" if len(open_ids) > 40 else "")
    template = ",\n ".join(
        f'{{"id": "{item_id}", "status": "…", "severity": "…", "location": "…", "issue_type": "…", '
        f'"finding": "…", "why": "…", "basis": "…", "note": "…"}}' for item_id in open_ids[:40])
    return ("现在需要收尾（本阶段时间已到或长时间没有写入结论）。不要再开始新的核查或运行新的计算。"
            f"立即用一次 `pv3 ledger update` 为所有未处理条目写入结论（{shown or '无'}）："
            "已发现问题的写 issue（填 severity、location、issue_type、finding、why、basis）；"
            "已排除的写 cleared 并在 basis 写明依据；证据确实不足的写 unresolved，在 note 中写明已核查到哪一步、还缺什么"
            "（unresolved 只需 id、status、note）。不要附 evidence。按下面的模板填写，删掉用不到的字段：\n"
            f"pv3 ledger update <<'EOF'\n[{template}]\nEOF\n"
            "若有记录被拒绝，按提示修正后再提交一次。写完即结束。")


def completion_note() -> str:
    return ("\n\n## 补查说明\n这些条目在此前的核查中因时间不够没有完成（`pv3 item <id>` 的 first 字段有此前的进展）。"
            "本轮只处理这些条目，每条都必须写入结论：先打开条目指向的位置看原值，能判断的立即写 issue/cleared/not_applicable；"
            "同一类告警可以按类写一条结论，但 basis 要列出覆盖的 sheet/列和代表原值。不要展开与这些条目无关的核查。\n")


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
2. 优先核验前一轮的确认问题和高价值排除项，再处理其余条目；每项写入你自己的结论与亲自核查的依据；形成判断就先写入，之后可以覆盖更新。
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


CALIBRATE_FORMAT = """写入格式：
pv3 ledger update <<'EOF'
[{"id": "条目id", "status": "issue", "category": "类别代码（如 S2、M1）", "why": "为什么属于这一类"},
 {"id": "条目id", "status": "cleared", "basis": "核实后问题不成立的原因"}]
EOF
（location、issue_type、finding、basis 不写时沿用原记录。）"""


def evidence(dossier: dict, items: list[dict]) -> str:
    rows = [f"- `{item['id']}`：位置：{str(item.get('location', ''))[:200]}\n  要点：{str(item.get('finding', ''))[:300]}"
            for item in items]
    return f"""你负责为复核报告中的风险问题制作证据截图。每条问题都必须附上能直接看清问题的原文局部截图。

## 论文
{_paper_block(dossier)}

{TOOL_HELP}

## 截图要求
- 截取问题所指的原文局部：表格要包含表头、行名和涉及的单元格；图要包含坐标轴、刻度、图例和涉及的面板；正文要包含涉及的句子。整页截图不算。
- 问题涉及两处（如表与图、正文与补充表）时，分别截两张，每张一个说明。
- 做法：先 `pv3 locate <doc> <页> '<关键文字>'` 找到坐标，再 `pv3 render <doc> <页> --clip x0,y0,x1,y1 --dpi 200` 生成截图，用 Read 打开确认截图里能看到问题涉及的数值和文字；看不全就调整范围重截。源数据表格问题可用 `pv3 view <图片> --box ...` 裁剪已有图片。
- caption 用一句话说明这张截图显示了什么（如“第7页 Table 3 MI 区块：两个模型的系数与区间逐位相同”）。
- 只做截图，不重新核查问题本身。

写入格式（每条问题一条记录）：
pv3 ledger update <<'EOF'
[{{"id": "条目id", "status": "issue", "evidence": [{{"path": "pv3 render 输出的截图路径", "caption": "截图说明"}}]}}]
EOF

## 需要截图的问题（{len(items)} 条）
{chr(10).join(rows)}

全部写入后运行 `pv3 ledger show --open` 确认，然后回复“完成”。
"""


def judge(dossier: dict, items: list[dict], related: list[dict]) -> str:
    from review_v3.categories import table
    rows = [f"- `{item['id']}`（{item.get('category')}，{item.get('dimension')}）：{str(item.get('location', ''))[:160]}"
            f" —— {str(item.get('issue_type', ''))[:80]}" for item in items]
    context = [f"- {str(r.get('location', ''))[:100]}：{str(r.get('issue_type', ''))[:60]}（{r.get('category') or '规范问题'}）"
               for r in related[:30]]
    return f"""你是学术论文诚信复核的终审人员。下列问题已被前序核查定为实质问题，一旦成立，这篇论文会被判为“有风险”。
你的任务是逐条核对论文原文，确认每条问题是否属实、属于哪一类。不要相信前序核查的转述，以原文为准。

## 论文
{_paper_block(dossier)}

{TOOL_HELP}

{rule('calibrate').replace('{table}', table())}

## 终审要求
- 对每条问题：先用 `pv3 item <id>` 读前序记录（字段 first），再打开原文对应页、表、图核实关键数值（`pv3 page`、`pv3 render --clip`、Read 查看图片；必要时自己写脚本复算）。
- 判断：问题本身是否属实（数值有没有读错、有没有把不同的量当成同一个量）；若属实，它属于哪个类别。只有所有条件都满足才定 S 类。
- 问题不成立（读错、误解）→ status=cleared，basis 写明原文实际是什么。属实但属于规范问题 → status=issue，category 填 M 类。确为实质问题 → status=issue，category 填 S 类。
- why 写明你核实到的原文数值和定类理由（一到三句）。

写入格式：
pv3 ledger update <<'EOF'
[{{"id": "条目id", "status": "issue", "category": "S2 或 M4 等", "why": "核实到的原文与定类理由"}},
 {{"id": "条目id", "status": "cleared", "basis": "原文实际是什么，为什么不成立"}}]
EOF

## 待终审的问题（{len(items)} 条）
{chr(10).join(rows)}

## 同一论文中已定为规范问题的其他发现（供参考，不需处理）
{chr(10).join(context) or '（无）'}

全部写入后运行 `pv3 ledger show --open` 确认，然后回复“完成”。
"""


def calibrator_continue(open_ids: list[str], wrapup: bool = False) -> str:
    shown = "、".join(open_ids[:60])
    template = ",\n ".join(f'{{"id": "{i}", "status": "issue", "category": "…", "why": "…"}}' for i in open_ids[:60])
    head = ("现在需要收尾，不要再打开原文或运行计算。" if wrapup else "继续。")
    return (f"{head}还有 {len(open_ids)} 条问题没有写入类别：{shown}。根据 `pv3 item <id>` 中已有的记录直接分类，"
            f"一次写入：\npv3 ledger update <<'EOF'\n[{template}]\nEOF\n"
            "category 填类别代码（S1–S6、M1–M10）；问题不成立的改为 status=cleared 并写 basis。")


def calibrator(dossier: dict, items: list[dict]) -> str:
    from review_v3.categories import table
    rows = [f"- `{item['id']}`（当前：{'实质问题' if item.get('severity') == 'substantive' else '规范问题'}，"
            f"{item.get('dimension')}）：{str(item.get('location', ''))[:120]} —— {str(item.get('issue_type', ''))[:60]}"
            for item in items]
    return f"""你是学术论文诚信复核的定级复核人员。

## 论文
{_paper_block(dossier)}

{TOOL_HELP}

{rule('calibrate').replace('{table}', table())}

{CALIBRATE_FORMAT}

## 待定级的问题（{len(items)} 项，已按当前定级排序）
{chr(10).join(rows)}

全部写入后运行 `pv3 ledger show --open` 确认，然后回复“完成”。
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

## 已核实的事实（按维度；“风险问题”按给定编号逐条写，“需作者更正事项”合并为一段，cleared/not_applicable 一句带过但须提及 cite 中的位置词）
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
