"""Unlimited, evidence-based adjudication of the two retained 13005 drafts."""
import asyncio
import json
import logging
import os
from pathlib import Path
import sys
import time
import uuid

BASE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BASE))
from api.services import review
from api.services.review_lock import exclusive_review
from scripts.review_phillip_pending import INITIAL, OUTPUT
from scripts.run_scholar_batch import run_reviews, render_and_update_reviews

DOI = '10.1002/jcsm.13005'
SESSION = '24bcd5f2-2ff0-4268-b331-6343dd76aa07'
JOB = OUTPUT / 'phillip_13005_adjudication_1029'
JOB.mkdir(exist_ok=True)
RESULT = JOB / 'results.json'


def save(path, data):
    temp = path.with_suffix('.tmp')
    temp.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    temp.replace(path)


def status(state, **kw):
    save(JOB / 'status.json', dict(state=state, updated_at=time.time(),
                                 phase_timeout_seconds=None, paper_timeout_seconds=None, **kw))


CONFLICTS = '''
## 本篇专项裁决要求（必须逐项解决，不能用“两个草稿都是低风险”代替核验）
先读取完整两轮草稿：{first}、{second}。两者是待验证意见，不是可信事实。
可复用核查脚本位于 .audit_jcsm13005、.rev13005_tmp、.rev13005；先检查算法和配对是否正确再运行。
1. Fig3A / Fig4B / Fig4C 8点与总体9人的关系：区分全文总体n与该具体指标的n声明；核对重叠点、缺失值、说明文字。
2. Fig3A p约0.016 vs P<0.01：核对正文到底是哪一比较、配对t/重复测量ANOVA、双侧/单侧等实际统计口径。
3. 效应量d：从正文明确其分母定义，比较不同合法定义与数字化误差，不要将口径不同直接当论文错误。
4. Fig4A/4B/4C FGR=MPS-FBR：两轮一轮声称闭合一轮声称三人残差0.098/0.298/0.069。
   原脚本按PDF绘图顺序匹配9人与8人，这是未经证明的受试者身份对应，可能因缺失一人造成索引错位。
   必须用配对连线、可确定身份等证据核实；不能仅将PDF路径顺序或最小残差匹配当作真实身份。
   检查FGR公式、单位、干预时长和坐标轴校准，报告可验证事实与无法确定的限制。
5. 第二轮明确说Fig2和Fig6轴标未查完，又在reason声称Fig3–6全部数值闭合；请打开原图补齐并消除矛盾。
6. 核对图像/参考文献/方法学原有告警，每一维度保留具体依据；缺源数据不能写成已执行xlsx级核查。
最终正文不得包含互相矛盾的“全部闭合”与“仍有不闭合”；不能因为没有原始数据就断言图件证明无问题。
无独立实质问题时低风险；有坐实问题时高风险并提供可定位证据；未核查必须如实说明，不能为了结论编造。
结论字段只使用 result=高风险/低风险、verdict=建议高风险/建议低风险；每个维度单独给出简短枚举。
证据截图的路径必须真实存在，dimension与issue_index绑定到对应复核意见的阿拉伯数字编号问题。
核查脚本/数值表存入 {job} 方便独立复算。研究无时间限制，合理利用已有产物完成专项裁决。
'''


def main():
    os.environ['REVIEW_PAPER_TIMEOUT_SECONDS'] = '0'
    item = next(r for r in json.loads(INITIAL.read_text()) if r['doi'] == DOI)
    artifacts = review._review_artifacts_dir(str(OUTPUT / 'review_v2'))
    args = (DOI, item['report_json'], item['input_dir'])
    path = review._review_state_path(*args, artifacts)
    with exclusive_review(path.with_suffix('.lock')):
        state = review._load_review_state(path, *args)
        # Recover the actual final JSON that the legacy transport rejected.
        candidates = []
        for session in (Path.home() / '.claude/projects').glob('*/' + SESSION + '.jsonl'):
            for line in session.read_text().splitlines():
                row = json.loads(line)
                if row.get('type') != 'assistant':
                    continue
                for block in row.get('message', {}).get('content', []):
                    if block.get('type') == 'text':
                        value = review._extract_json_from_text(block.get('text', ''))
                        if isinstance(value, dict) and value.get('doi') == DOI and value.get('data_review'):
                            candidates.append(value)
        if not candidates:
            raise RuntimeError('Retained verifier JSON not found')
        second = candidates[-1]
        save(JOB / 'reviewer2_raw.json', second)
        save(JOB / 'reviewer1.json', state['stages']['reviewer1'])
        save(path.with_name(path.stem + '_' + uuid.uuid4().hex + '.history.json'), state)
        second = review._normalize_review_result(second)
        review._store_stage(path, state, 'reviewer2', second)
    original = review._build_judge_prompt
    def focused(*a, **kw):
        return original(*a, **kw) + CONFLICTS.format(
            first=JOB / 'reviewer1.json', second=JOB / 'reviewer2_raw.json', job=JOB)
    review._build_judge_prompt = focused
    status('running', pid=os.getpid(), started_at=time.time())
    results = asyncio.run(run_reviews([item], OUTPUT, 1, scholar='Phillip',
                                     result_path=RESULT, resume=True))
    final = results[0]
    if (final.get('trigger') == 'review_error' or final.get('_coverage_gaps')
            or final.get('_coverage_error')):
        status('incomplete', error=final.get('_review_error') or final.get('_coverage_gaps'))
        return 2
    # Stage publication separately so the primary reviewer can inspect the
    # adjudicated facts before final database write.
    status('validated_awaiting_publication', result=final.get('result'))
    return 0


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
    with exclusive_review(JOB / 'job.lock'):
        try:
            code = main()
        except Exception as exc:
            status('error', error=str(exc))
            raise
        raise SystemExit(code)
