#!/usr/bin/env python3
"""Fake ``claude -p`` used by review_v3 tests. No model, no network.

Behaviour per role comes from the JSON file in FAKE_CLAUDE_SCENARIO:
  {"investigator": "clear_all" | "one_per_call" | "noop" | {"issue": {"id": "substantive"}},
   "challenger": ..., "arbiter": ..., "transport_fail_first": true}
Every call is appended to FAKE_CLAUDE_LOG for assertions.
"""
import json
import os
import re
import subprocess
import sys
from pathlib import Path


def arg(name, default=None):
    if name in sys.argv:
        return sys.argv[sys.argv.index(name) + 1]
    return default


def emit(result, subtype="success", turns=3, session=None):
    print(json.dumps({"type": "result", "subtype": subtype, "is_error": subtype != "success",
                      "num_turns": turns, "result": result, "session_id": session,
                      "duration_api_ms": 10, "total_cost_usd": 0.0,
                      "usage": {"input_tokens": 100, "output_tokens": 20}}))


def pv3(*args, stdin=None):
    bin_path = Path(os.environ["PATH"].split(os.pathsep)[0]) / "pv3"
    return subprocess.run([str(bin_path), *args], input=stdin, capture_output=True, text=True)


def write_report(prompt):
    facts = json.loads(re.search(r"```json\n(.*?)\n```", prompt, re.S).group(1))
    result = re.search(r"result：(\S+)", prompt).group(1)
    verdict = re.search(r"verdict：(\S+)", prompt).group(1)
    fields = {}
    for dim, field in (("image", "image_review"), ("data", "data_review"), ("ref", "ref_review"),
                       ("methodology", "methodology_review")):
        parts = []
        for issue in facts[dim]["风险问题"]:
            parts.append(f"（{issue['编号']}）问题位置：{issue['location']}\n问题类型：{issue['issue_type']}\n"
                         f"发现：{issue['finding']}\n为什么重要：{issue['why']}\n核查依据：{issue['basis']}")
        if facts[dim]["需作者更正事项"]:
            parts.append("另需作者更正：\n" + "\n".join(f"{'①②③④⑤'[i % 5]} 位置：{c['位置']}；问题：{c['问题']}；请核对。"
                                                       for i, c in enumerate(facts[dim]["需作者更正事项"])))
        cleared = [f"{c['条目']}（{c['cite']}）属假阳性，可解释：{c['依据']}" for c in facts[dim]["cleared"]]
        cleared += [f"{c['条目']}：{c['依据']}" for c in facts[dim]["not_applicable"]]
        if cleared:
            parts.append("已核对：" + "；".join(cleared))
        if dim == "data":
            parts.append("图文一致性：渲染 Fig. 1 并与源数据比对，数值一致。")
        fields[field] = "\n\n".join(parts) or "核查了该维度全部内容，未发现问题，属正常。"
    final = "最终判定：" + verdict
    reason = "数据维度：见上。\n\n图像维度：见上。\n\n" + final
    print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
                      "result": json.dumps({"doi": "x", "result": result, "verdict": verdict, "trigger": "初筛线索",
                                            **fields, "reason": reason}, ensure_ascii=False)}))


def main():
    prompt = sys.stdin.read()  # sessions pass the prompt on stdin
    scenario = json.loads(Path(os.environ["FAKE_CLAUDE_SCENARIO"]).read_text())
    actor = os.environ.get("PV3_ACTOR", "writer")
    role = actor.split(":")[0]
    session = arg("--session-id") or arg("--resume")
    with open(os.environ["FAKE_CLAUDE_LOG"], "a") as log:
        log.write(json.dumps({"actor": actor, "resume": "--resume" in sys.argv, "session": session,
                              "turns": arg("--max-turns"), "model": arg("--model"),
                              "settings": arg("--settings"), "tools": arg("--tools"),
                              "prompt_chars": len(prompt)}) + "\n")
    marker = Path(os.environ["FAKE_CLAUDE_SCENARIO"] + ".failed_once")
    if scenario.get("transport_fail_first") and not marker.exists():
        marker.write_text("1")
        sys.stderr.write("API Error: 529 overloaded\n")
        sys.exit(1)
    if scenario.get("session_in_use_first") and not marker.exists() and "--session-id" in sys.argv:
        marker.write_text("1")
        sys.stderr.write(f"Error: Session ID {session} is already in use.\n")
        sys.exit(1)
    if scenario.get("delay"):
        import time
        time.sleep(float(scenario["delay"]))
    if role == "writer":
        if scenario.get("writer") == "broken":
            print(json.dumps({"type": "result", "subtype": "success", "is_error": False, "num_turns": 1,
                              "result": "我先调用工具看一下数据。"}))
            return
        write_report(prompt)
        return
    behaviour = scenario.get(actor, scenario.get(role, "uphold" if role == "judge" else "clear_all"))
    if behaviour == "uphold":  # judge default: confirm what calibration decided
        data = json.loads(Path(os.environ["PV3_LEDGER"]).read_text())
        behaviour = {"issue": {i["id"]: (i.get("first") or {}).get("severity", "substantive")
                               for i in data["items"].values()}}
    if "收尾" in prompt and scenario.get("wrapup"):
        behaviour = scenario["wrapup"]
    data = json.loads(Path(os.environ["PV3_LEDGER"]).read_text())
    open_items = [i for i in data["items"].values() if i.get("status", "open") == "open"]
    if behaviour == "noop" or not open_items:
        emit("思考中", subtype="error_max_turns", session=session)
        return
    if behaviour == "one_per_call":
        open_items = open_items[:1]
    entries = []
    issues = behaviour.get("issue", {}) if isinstance(behaviour, dict) else {}
    for item in open_items:
        if item["id"] in issues and data.get("require_category"):
            # Calibration answers with a category; severity follows from it.
            entries.append({"id": item["id"], "status": "issue",
                            "category": "S2" if issues[item["id"]] == "substantive" else "M1",
                            "why": "按类别定级"})
        elif item["id"] in issues:
            entries.append({"id": item["id"], "status": "issue", "severity": issues[item["id"]],
                            "location": "data.xlsx / sheet Sheet1 / B2:B9，Fig. 1",
                            "issue_type": "不同实验使用了相同数据", "finding": "两组 8 个测量值逐值相同",
                            "why": "影响 Fig. 1 组间比较", "basis": "打开源数据逐值比对"})
        else:
            entries.append({"id": item["id"], "status": "cleared",
                            "basis": "打开源数据 Sheet1 查看列头与数值，确认为共享横轴列，属正常"})
    done = pv3("ledger", "update", stdin=json.dumps(entries, ensure_ascii=False))
    if done.returncode not in (0,):
        sys.stderr.write(done.stdout + done.stderr)
    remaining = [i for i in json.loads(Path(os.environ["PV3_LEDGER"]).read_text())["items"].values()
                 if i.get("status", "open") == "open"]
    emit("完成" if not remaining else "未完成", subtype="success" if not remaining else "error_max_turns",
         session=session)


if __name__ == "__main__":
    main()
