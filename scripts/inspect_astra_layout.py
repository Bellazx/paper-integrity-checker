"""Render evidence sheets and final report pages for manual layout inspection."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent))
from rewrite_astra_highrisk_reports import PAPERS, EVIDENCE, ROUND
from PIL import Image, ImageDraw

def main():
    target = ROUND / 'layout_checks'
    target.mkdir(exist_ok=True)
    for n, (doi, item) in enumerate(PAPERS.items()):
        canvas = Image.new('RGB', (1600, 620 * ((len(item['evidence'])+1)//2)), 'white')
        draw = ImageDraw.Draw(canvas)
        for i, (filename, _) in enumerate(item['evidence']):
            im = Image.open(EVIDENCE / filename).convert('RGB')
            im.thumbnail((790, 580))
            x, y = i%2*800, i//2*620
            canvas.paste(im,(x,y+30))
            draw.text((x+10,y+5),f'{n+1} / evidence {i+1}',fill='black')
        canvas.save(target / f'evidence_{n+1:02}.jpg', quality=92)
    print(target)

if __name__ == '__main__':
    main()
