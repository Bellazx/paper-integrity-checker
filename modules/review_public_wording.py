"""Display-only language policy shared by API, workflow and report repairs.

Apply after semantic validation and legacy sanitization. Never normalize the
result JSON or database enums: those are inputs to the scientific decision gate.
"""
from bisect import bisect_right
from difflib import SequenceMatcher
from html import escape
from html.parser import HTMLParser
import re

POLICY_VERSION = '2026-10-23-public-wording-v2-inline-evidence'
PUBLIC_WRITING_RULES = '''
正式复核报告用语：结论只显示“有风险”或“未发现风险”，论文分类写“有风险论文”或“未发现风险”。
正文直接写论文、图表、源数据中的事实、核验依据和局限，不得出现“原报告”“初审报告”“初检报告”
“旧报告”“上一轮报告”等追述审核过程的措辞，也不得把待核查线索写成论文自身的主张。
没有发现问题时注明是在本次材料及核验范围内未发现风险，不代表证明论文绝对无问题。
供程序读取的 result/verdict/dimension_verdicts 仍使用规定的高风险/低风险枚举；
这是内部字段兼容要求，最终PDF由统一展示层转换。正文、证据图说明使用上述读者用语。
证据截图要求：每个确认问题优先提供能直接看清问题的论文原图局部或源数据行列摘录，保留图号、
表头、单位及必要对照；不要只附整页缩略图，不得用带错误结论的旧标注图作证据。
证据图里只保留来源内容及简短定位标记，解释写在图外的caption中，使用同一套读者话术。
截图说明必须与正文的数值、分组、结论一致，不能有错别字、被截断的关键数字或图文重叠。
可以在evidence_images中提供本地图片path，或pdf_path、从1开始的page、PDF点坐标clip=[x0,y0,x1,y1]，
并填写dimension、issue_index及caption。每个维度内的确认问题按（1）（2）（3）独立分段，
issue_index为对应问题的整数编号；同一问题可以附多张图，不得按图片顺序猜测对应关系。
excel_evidence_regions也须填写issue_index。每个问题后紧跟对应截图及说明，不能先堆问题再集中放图。
中英文报告遵循相同的问题编号和证据对应关系；英文结论用“Risk identified”或“No risk identified”，
不使用High/Low Risk，也不追述original report、initial screening report等审核历史。
裁剪坐标必须实际查看确认，不得猜测；源数据使用具体工作表及行列范围。
'''

PROCESS_REFERENCE = re.compile(
    r'(?:原(?:始|来)?|旧版?|此前|先前|之前|前次|上次|上一轮|前一轮|上一版|首轮|前序)(?:的)?(?:复核|复审|审核|检测)?报告'
    r'|(?:初审|初检|初筛)(?:的)?(?:检测|复核|复审)?报告'
)


def public_text(text: str) -> str:
    """Normalize labels without changing facts, quantities or negations."""
    text = PROCESS_REFERENCE.sub('待核查线索', text)
    text = re.sub(r'(?:初审|初检|初筛)(?:阶段|环节|结果)?', '线索筛查', text)
    text = text.replace('原复审', '复核').replace('原复核', '复核')
    # Reader-facing reports carry one title whatever stage produced them; the
    # product name stays in footers (user decision 2026-11-06). These
    # replacements are display-only; result/verdict enums and validation logic
    # remain unchanged.
    text = text.replace('Paper Pecker论文预警报告（AI复核版）', '论文复核报告')
    text = text.replace('Paper Pecker论文预警报告', '论文复核报告')
    text = text.replace('Paper Pecker Risk Alert Report — AI Review', 'Paper Review Report')
    text = text.replace('Paper Pecker Risk Alert Report', 'Paper Review Report')
    text = text.replace('本模块复核结论', '本节结论')
    text = text.replace('本报告由Paper Pecker自动生成，所有发现均基于算法分析，仅供参考。',
                        '本报告依据本次提供的论文及补充材料，列示可核查的问题及依据。')
    text = text.replace('本报告由论文复核系统自动生成，所有发现均基于算法分析，仅供参考。',
                        '本报告依据本次提供的论文及补充材料，列示可核查的问题及依据。')
    text = text.replace('该风险结论不等同于已经证明存在学术不端',
                        '该风险结论不涉及对作者主观意图的认定')
    # Narrative nouns must remain grammatical, including negative findings.
    for old, new in (
        ('未发现高风险', '未发现风险'), ('未见高风险', '未见风险'),
        ('无高风险', '无明确风险'), ('不构成建议高风险', '不构成风险'),
        ('不支持建议高风险', '不支持风险判定'),
        ('建议高风险问题', '风险问题'), ('建议高风险结论', '有风险的结论'),
        ('建议低风险结论', '未发现风险的结论'),
        ('建议高风险', '有风险'), ('确认高风险', '有风险'),
        ('建议低风险', '未发现风险'), ('确认低风险', '未发现风险'),
        ('高风险论文', '有风险论文'), ('低风险论文', '未发现风险的论文'),
        ('高风险结论', '有风险的结论'), ('低风险结论', '未发现风险的结论'),
        ('高风险判定', '风险判定'), ('高风险依据', '风险判定依据'),
        ('高风险问题', '风险问题'), ('高风险证据', '风险证据'),
        ('高风险发现', '风险发现'), ('高风险线索', '风险线索'),
        ('不构成高风险', '不构成风险'), ('不支持高风险', '不支持风险判定'),
        ('高风险', '有风险'), ('低风险', '未发现风险'),
    ):
        text = text.replace(old, new)
    return text


class _PublicHTML(HTMLParser):
    """Preserve tags/attributes/images; normalize text across inline markup."""
    BLOCKS = {'p', 'div', 'h1', 'h2', 'h3', 'h4', 'li', 'td', 'th', 'br', 'hr',
              'section', 'body', 'table', 'tr', 'ul', 'ol'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.tokens = []
        self.output = []
        self.ignored = 0

    def flush(self):
        texts = [value for is_text, value in self.tokens if is_text]
        before = ''.join(texts)
        after = public_text(before)
        starts, position = [], 0
        for value in texts:
            starts.append(position)
            position += len(value)
        values = [''] * len(texts)
        if texts:
            for op, i, j, a, b in SequenceMatcher(None, before, after, autojunk=False).get_opcodes():
                if op == 'equal':
                    for n, value in enumerate(texts):
                        left, right = max(i, starts[n]), min(j, starts[n] + len(value))
                        if left < right:
                            values[n] += before[left:right]
                elif op in ('insert', 'replace'):
                    n = min(len(texts)-1, max(0, bisect_right(starts, i)-1))
                    values[n] += after[a:b]
        iterator = iter(values)
        self.output.extend(escape(next(iterator), quote=False) if is_text else value
                           for is_text, value in self.tokens)
        self.tokens = []

    def handle_starttag(self, tag, attrs):
        if tag in self.BLOCKS:
            self.flush()
        self.tokens.append((False, self.get_starttag_text()))
        if tag in {'script', 'style'}:
            self.ignored += 1

    def handle_startendtag(self, tag, attrs):
        if tag in self.BLOCKS:
            self.flush()
        self.tokens.append((False, self.get_starttag_text()))

    def handle_endtag(self, tag):
        self.tokens.append((False, f'</{tag}>'))
        if tag in {'script', 'style'}:
            self.ignored = max(0, self.ignored-1)
        if tag in self.BLOCKS:
            self.flush()

    def handle_data(self, data):
        if data:
            self.tokens.append((not self.ignored, data))

    def handle_comment(self, data):
        self.tokens.append((False, '<!--' + data + '-->'))

    def handle_decl(self, decl):
        self.tokens.append((False, '<!' + decl + '>'))


def public_report_html(html: str) -> str:
    parser = _PublicHTML()
    parser.feed(html)
    parser.close()
    parser.flush()
    return ''.join(parser.output)


def install_renderer_policy(module):
    """Also cover legacy renderers loaded through the project/API wrapper."""
    original = module._sanitize_internal_references
    if getattr(original, '_public_wording_policy', False):
        return

    def sanitize(html):
        return public_report_html(original(html))

    sanitize._public_wording_policy = True
    module._sanitize_internal_references = sanitize

    # A short reviewed paragraph is already concise. Keeping only its first
    # sentence can remove the finding's explanation or its limited impact.
    compact_section = module._compact_section_text
    compact_reason = module._compact_overall_reason

    def section(text, verdict):
        formatted = module._format_section_text(str(text or ''))
        lines = [line.strip() for line in formatted.splitlines() if line.strip()]
        body = [line for line in lines if not line.startswith('本模块复核结论：')]
        if ('低风险' in verdict and body and len(body[0]) <= 650
                and not re.search(r'初检|初审|内部|流程', body[0])):
            # Structured observations must remain complete, as in the renderer.
            if not any(line.startswith(('问题位置：', '问题类型：', '具体情况：',
                                        '需关注原因：', '核查依据：')) for line in body):
                conclusion = [line for line in lines if line.startswith('本模块复核结论：')]
                return '\n'.join(conclusion + [body[0]])
        return compact_section(text, verdict)

    def reason(info, text):
        if text.strip() and len(text) <= 1200:
            return text
        return compact_reason(info, text)

    module._compact_section_text = section
    module._compact_overall_reason = reason
