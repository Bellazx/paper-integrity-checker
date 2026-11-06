"""Small real-model smoke test in a new isolated directory; no DB or business inputs."""
import asyncio
import json
import os
from pathlib import Path
import time

from review_v3 import engine, ledger, prompts, session, visual_progress


async def main():
    import fitz
    root = Path.cwd() / "experiments/page-resume-smoke"
    root.mkdir(parents=True, exist_ok=False)
    source = root / "input"; source.mkdir()
    pdf = source / "main.pdf"
    with fitz.open() as doc:
        for n in range(1, 4):
            page = doc.new_page()
            page.insert_text((60, 60), f"Figure {n}: Synthetic smoke-test panel. No research finding.")
            page.draw_rect(fitz.Rect(60, 100, 250, 230), color=(0, 0, 0), fill=(n / 4, .2, .3))
        doc.save(pdf)
    dossier = {"doi": "smoke", "input_dir": str(source), "paper": {"title": "Synthetic test"},
               "docs": [{"id": "main", "name": "main.pdf", "path": str(pdf), "pages": 3}],
               "captions": [{"doc": "main", "page": n, "kind": "figure", "label": f"Fig. {n}"} for n in range(1, 4)],
               "data_files": []}
    (root / "dossier").mkdir()
    (root / "dossier/index.json").write_text(json.dumps(dossier))
    visual_progress.initialize(root, dossier)
    review = engine.Review("smoke", "", str(source), root, time.monotonic() + 360)
    review.dossier = dossier
    review.read_dirs = [str(source)]
    results = []
    try:
        for name, item, instruction in [
            ("first", "stage1", "本轮只查看 main 第 1 页并保存页面进度，不要看第 2、3 页。完成后把 stage1 写为 cleared。"),
            ("second", "image::visual_pass", "先读取 pv3 visual pending，仅浏览剩余第 2、3 页，不要重看第 1 页。全部完成后关闭 image::visual_pass。")]:
            path = review.ledger_path("inv", name)
            ledger.create(path, {"role": "investigator", "allow_new": True}, [{"id": item, "title": "测试页面进度接续"}])
            prompt = ("这是合成页面工具测试，无需查网络、记忆或源数据。严格按以下要求做完就结束。\n"
                      + instruction + "\n" + prompts.TOOL_HELP + prompts.visual_resume() + prompts.LEDGER_FORMAT)
            spec = review._spec(name, prompt, path, 2, "investigator:" + name, "complete")
            spec.deadline = time.monotonic() + 180
            started = time.monotonic()
            outcome = await asyncio.wait_for(session.run(spec), 185)
            state = visual_progress.summary(root)
            results.append({"stage": name, "outcome": outcome.status, "wall_seconds": time.monotonic() - started,
                            "progress": state, "ledger": ledger.load(path)})
            (root / "result.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
            print(name, outcome.status, "reviewed", state["reviewed"], flush=True)
            assert state["reviewed"] == (1 if name == "first" else 3), results[-1]
        pages = visual_progress.load(root)["pages"]
        assert len(pages["main:1"]["observations"]) == 1
        assert pages["main:1"]["observations"][0]["actor"] == "investigator:first"
        assert all(pages[f"main:{n}"]["observations"][0]["actor"] == "investigator:second" for n in (2, 3))
        print("PASS: actual Read hooks recorded; second session resumed remaining pages", flush=True)
    finally:
        await review.close()


if __name__ == "__main__":
    asyncio.run(main())
