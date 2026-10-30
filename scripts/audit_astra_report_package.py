"""Record file-level checks and render the ten published review reports."""
import hashlib
import json
from pathlib import Path

import fitz
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
ROUND = ROOT / 'data/yujing_scholar_hugaowei_fanlingzhong_wangdandan_round_1'
OUT = ROUND / 'codex_cli_astra_recheck_20261022/pdf_checks'


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    archive = ROUND / 'all_high_risk_reports_astra_20261022'
    rows = json.loads((archive / 'manifest.json').read_text())
    audit = []
    for row in rows:
        archived = archive / row['pdf']
        current = ROOT / 'data/output/yujing_scholar/review_v2' / archived.name
        record = {'doi': row['doi'], 'scholar': row['scholar'], 'pdf': str(current),
                  'archive_matches_current': hashlib.sha256(current.read_bytes()).digest() == hashlib.sha256(archived.read_bytes()).digest()}
        with fitz.open(current) as doc:
            record['pages'] = [{'page': i+1, 'chars': len(p.get_text()), 'images': len(p.get_image_info())} for i,p in enumerate(doc)]
            record['layout_errors'] = []
            full_text = ''.join(p.get_text() for p in doc)
            for required in ['Paper Pecker', '一、论文基本信息', '二、复核风险结论', '三、复核详情', '四、综合评估', '五、免责声明']:
                if required not in full_text:
                    record['layout_errors'].append('Missing template text: ' + required)
            for i, page in enumerate(doc):
                for span in page.get_texttrace():
                    for char, glyph, origin, bbox in span['chars']:
                        if glyph == 0 and not chr(char).isspace():
                            record['layout_errors'].append(f'Page {i+1}: missing glyph {chr(char)!r}')
                for im in page.get_image_info():
                    rect = fitz.Rect(im['bbox'])
                    for block in page.get_text('blocks'):
                        if block[6] == 0 and not (fitz.Rect(block[:4]) & rect).is_empty:
                            record['layout_errors'].append(f'Page {i+1}: image/text overlap {block[4][:40]}')
                pix = page.get_pixmap(matrix=fitz.Matrix(1.5,1.5),alpha=False)
                pix.save(OUT / f'{current.stem}_p{i+1:02}.png')
            canvas = Image.new('RGB', (420*min(3, len(doc)), 620*((len(doc)+2)//3)), 'white')
            draw = ImageDraw.Draw(canvas)
            for i, page in enumerate(doc):
                pix = page.get_pixmap(matrix=fitz.Matrix(0.68,0.68),alpha=False)
                im = Image.frombytes('RGB', (pix.width,pix.height),pix.samples)
                im.thumbnail((410,590))
                x,y=(i%3)*420,(i//3)*620
                canvas.paste(im,(x,y+22))
                draw.text((x+8,y+4),f'Page {i+1}',fill='black')
            tile = OUT / (current.stem + '.jpg')
            canvas.save(tile, quality=88)
            record['contact_sheet'] = str(tile)
        audit.append(record)
    (OUT/'inventory.json').write_text(json.dumps(audit,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps([{'doi': row['doi'], 'pages': len(row['pages']), 'errors': row['layout_errors']} for row in audit],ensure_ascii=False,indent=2))
    assert all(row['archive_matches_current'] and not row['layout_errors'] for row in audit)


if __name__ == '__main__':
    main()
