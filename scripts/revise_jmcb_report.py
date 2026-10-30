"""Rebuild the JMCB evidence and publish its wording correction in both packages."""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
import zipfile
from datetime import datetime

import fitz
from PIL import Image, ImageDraw, ImageFont

import rewrite_astra_highrisk_reports as reports

DOI = '10.1093/jmcb/mjac004'
NAME = 'review_10.1093_jmcb_mjac004.pdf'
SOURCE = reports.BASE / 'data/input/scholar/王丹丹/10.1093_jmcb_mjac004'


def read_table(number):
    csv.field_size_limit(10000000)
    with (SOURCE / 'extended data' / f'Supplemental Table S{number}.txt').open() as stream:
        reader = csv.DictReader(stream, delimiter='\t')
        headers = reader.fieldnames
        rows = list(reader)
    retained = [row for row in rows if not any(
        row.get(flag) == '+' for flag in ('Reverse', 'Potential contaminant', 'Only identified by site'))]
    return headers, sum(float(row.get('Intensity') or 0) > 0 for row in retained)


def make_evidence():
    headers, rabbit_count = read_table(10)
    counts = [rabbit_count, read_table(13)[1], read_table(15)[1]]
    assert counts == [419, 492, 554], counts
    names = [header.split('_HFHC_', 1)[1] for header in headers if header.startswith('Intensity ')]
    baseline = [name for name in names if name.startswith('T')]
    two_week = [name for name in names if name.startswith('2W')]
    assert baseline == ['T1-1', 'T1-2', 'T1-3', 'T2-1', 'T2-2', 'T2-3']
    assert len(two_week) == 9
    regular = ImageFont.truetype(str(reports.FONT), 29)
    bold = ImageFont.truetype(str(reports.FONT_BOLD), 34)

    def label(draw, xy, value, *, heading=False, color='#222222'):
        draw.text(xy, value, font=bold if heading else regular, fill=color)

    def crop(doc, page, rect, width):
        pix = doc[page - 1].get_pixmap(matrix=fitz.Matrix(4, 4), clip=fitz.Rect(rect), alpha=False)
        im = Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
        im = im.resize((width, round(im.height * width / im.width)), Image.Resampling.LANCZOS)
        return im

    with fitz.open(SOURCE / 'mjac004.pdf') as doc:
        canvas = Image.new('RGB', (1560, 800), 'white')
        draw = ImageDraw.Draw(canvas)
        label(draw, (30, 15), '样本对应关系：图注原文与附表S10列名', heading=True)
        label(draw, (30, 68), '正文第5页 Fig.3 图注（原文截图）：')
        canvas.paste(crop(doc, 5, (50, 620, 563, 700), 1490), (30, 112))
        label(draw, (30, 368), '附表S10列名摘录（省略共有的仪器、批次前缀）：', heading=True)
        label(draw, (30, 431), '干预前：' + '、'.join(baseline))
        label(draw, (30, 486), '2周：' + '、'.join(two_week[:6]))
        label(draw, (117, 531), '、'.join(two_week[6:]))
        label(draw, (30, 595), '需说明：第三个个体对应哪条记录？2周数据是否参与论文计算？', heading=True)
        label(draw, (30, 658), '列名中的编号、分段或批次，不能直接换算成独立动物数。')
        label(draw, (30, 709), '以上仅确认记录对应不清，不能据此认定少做了一只兔的实验。')
        canvas.save(reports.FIXED_EVIDENCE / reports.PAPERS[DOI]['evidence'][0][0])

        canvas = Image.new('RGB', (1560, 1030), 'white')
        draw = ImageDraw.Draw(canvas)
        label(draw, (30, 15), '蛋白数量：论文原文与附表复算对照', heading=True)
        label(draw, (30, 69), '正文第3页兔实验段落（原文截图）：')
        canvas.paste(crop(doc, 3, (313, 524, 563, 596), 1150), (30, 114))
        label(draw, (30, 515), '复算对照表（复核人员根据公开附表制作）：', heading=True)
        columns = [30, 650, 915, 1190, 1530]
        rows = [
            ['样本及附表', '论文数量', '复算数量', '是否一致'],
            ['兔实验 / S10', '462', str(counts[0]), '少43个，待解释'],
            ['患者 Lipo-HiPL / S13', '492', str(counts[1]), '一致'],
            ['患者 HDL-HiPL / S15', '554', str(counts[2]), '一致'],
        ]
        for row_no, row in enumerate(rows):
            y = 575 + row_no * 65
            for col, value in enumerate(row):
                draw.rectangle((columns[col], y, columns[col+1], y+65),
                               fill='#fff0e6' if row_no == 1 else '#eeeeee' if row_no == 0 else 'white',
                               outline='#aaaaaa')
                label(draw, (columns[col]+12, y+13), value)
        label(draw, (30, 862), '筛选：排除 Reverse、Potential contaminant、Only identified by site 标记为“+”的条目；')
        label(draw, (30, 910), '再保留总信号强度 Intensity > 0 的记录。此处未使用各样本子列代替总强度。')
        label(draw, (30, 963), '需要解释的是兔实验462与419的差异；目前不能认定数量虚报。')
        canvas.save(reports.FIXED_EVIDENCE / reports.PAPERS[DOI]['evidence'][1][0])
    return {'S10': counts[0], 'S13': counts[1], 'S15': counts[2]}


def main():
    backup = reports.ROUND / ('jmcb_wording_backup_' + datetime.now().strftime('%Y%m%d_%H%M%S'))
    backup.mkdir()
    for filename, _ in reports.PAPERS[DOI]['evidence']:
        shutil.copy2(reports.FIXED_EVIDENCE / filename, backup / filename)
    shutil.copy2(reports.OUT / NAME, backup / NAME)
    for folder in (reports.ARCHIVE, reports.GROUPED):
        shutil.copytree(folder, backup / folder.name)
        shutil.copy2(folder.with_suffix('.zip'), backup / (folder.name + '.zip'))
    counts = make_evidence()
    staged = backup / 'regenerated' / NAME
    staged.parent.mkdir()
    reports.make_pdf(DOI, reports.PAPERS[DOI], staged)
    check = {'doi': DOI, **reports.validate_pdf(staged, reports.PAPERS[DOI], backup / NAME,
                                               preserve_screenshots=False),
             'evidence_revision': '重新制作样本列名摘录及蛋白数量对照，去除不成立的动物数与患者数量推断',
             'recomputed_counts': counts}
    for folder in (reports.ARCHIVE, reports.GROUPED):
        manifest_path = folder / 'manifest.json'
        manifest = json.loads(manifest_path.read_text())
        row = next(row for row in manifest if row['doi'] == DOI)
        shutil.copy2(staged, folder / row['pdf'])
        row['sha256'] = hashlib.sha256(staged.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2))
    shutil.copy2(staged, reports.OUT / NAME)
    for filename, _ in reports.PAPERS[DOI]['evidence']:
        shutil.copy2(reports.FIXED_EVIDENCE / filename,
                     reports.ARCHIVE / 'evidence_screenshots' / ('10.1093_jmcb_mjac004_' + filename))
    reports.write_summary(reports.ARCHIVE / 'summary_astra_20261022.md', grouped=False)
    reports.write_summary(reports.GROUPED / '论文复核汇总.md', grouped=True)
    checks_path = reports.GROUPED / '校验记录.json'
    checks = json.loads(checks_path.read_text())
    checks = [check if row['doi'] == DOI else row for row in checks]
    checks_path.write_text(json.dumps(checks, ensure_ascii=False, indent=2))
    for folder in (reports.ARCHIVE, reports.GROUPED):
        shutil.make_archive(str(folder), 'zip', root_dir=folder)
        with zipfile.ZipFile(folder.with_suffix('.zip')) as zipped:
            assert zipped.testzip() is None
            for row in json.loads((folder / 'manifest.json').read_text()):
                assert zipped.read(row['pdf']) == (folder / row['pdf']).read_bytes()
    print(json.dumps({'backup': str(backup), 'check': check}, ensure_ascii=False))


if __name__ == '__main__':
    main()
