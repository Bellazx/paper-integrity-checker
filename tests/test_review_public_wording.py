"""Reader wording must not change scientific verdicts or evidence pixels."""
import copy
import importlib.util
from html.parser import HTMLParser

import fitz
import pytest
from PIL import Image

from api.services.review_validation import renderer_module, renderer_path
from modules.review_public_wording import PUBLIC_WRITING_RULES, public_report_html, public_text


def visible(html):
    class Text(HTMLParser):
        parts = []

        def handle_data(self, data):
            self.parts.append(data)
    parser = Text()
    parser.parts = []
    parser.feed(html)
    return ''.join(parser.parts)


@pytest.fixture(params=['api', 'workflow'])
def renderer(request):
    if request.param == 'api':
        return renderer_module()
    spec = importlib.util.spec_from_file_location('direct_workflow_renderer', renderer_path())
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_inline_markup_and_urls_keep_facts_and_negation():
    html = ('<p>结论：<b>建议高</b>风险。未发现高风险证据，不构成高风险。</p>'
            '<p>原<b>报告</b>提示Fig.3可能重复，核查后未发现重复；'
            '初审报告认为n=3，但源数据有5列。建议低风险。</p>'
            '<a href="http://test/高风险/初审报告.pdf">证据</a>'
            '<img src="data:image/png;base64,abcd"/>')
    result = public_report_html(html)
    text = visible(result)
    assert '结论：有风险' in text
    assert '未发现风险证据，不构成风险' in text
    assert '待核查线索提示Fig.3可能重复，核查后未发现重复' in text
    assert 'n=3，但源数据有5列' in text
    assert '原报告' not in text and '初审报告' not in text
    assert 'http://test/高风险/初审报告.pdf' in result
    assert 'src="data:image/png;base64,abcd"' in result
    assert result == public_report_html(result)


@pytest.mark.parametrize('term', ['原报告', '初审报告', '初检报告', '旧报告', '上一轮报告', '此前复核报告'])
def test_process_terms_do_not_become_claims_by_the_paper(term):
    assert public_text(term + '提示可能重复，但核查未发现重复。') == '待核查线索提示可能重复，但核查未发现重复。'


def test_legacy_heading_disclaimer_and_section_label_are_reader_facing():
    html = ('<h1>Paper Pecker论文预警报告（AI复核版）</h1>'
            '<p>本模块复核结论：建议高风险。</p>'
            '<p>本报告由Paper Pecker自动生成，所有发现均基于算法分析，仅供参考。</p>'
            '<p>该风险结论不等同于已经证明存在学术不端。</p>')
    result = public_report_html(html)
    text = visible(result)
    assert '论文复核报告' in text
    assert '本节结论：有风险' in text
    assert '不涉及对作者主观意图的认定' in text
    assert '所有发现均基于算法分析' not in text
    for term in ('Paper Pecker', '学术不端', '本模块复核结论'):
        assert term not in text
    assert result == public_report_html(result)


@pytest.mark.parametrize('risk', ['高风险', '低风险'])
def test_actual_pdf_all_sections_and_captions_use_public_words(renderer, tmp_path, risk):
    info = {'result': risk, 'verdict': '建议' + risk,
            'dimension_verdicts': dict.fromkeys(['image', 'data', 'ref', 'methodology'], risk),
            'image_review': '核查Fig.1至Fig.3，未发现真实重复。',
            'data_review': '问题位置：Table 1。问题类型：数值不一致。具体情况：值为3和5。'
                           '需关注原因：影响比较。核查依据：逐项复算。' if risk == '高风险'
                           else '核查Table 1，三个组均有5列，图文一致。',
            'ref_review': '核对参考文献[1]—[3]，DOI与题名一致。',
            'methodology_review': '图注与方法均为n=5。', 'reason': '逐项核对Table 1。'}
    before = copy.deepcopy(info)
    image = tmp_path / 'source.png'
    Image.new('RGB', (1200, 800), '#ddeeff').save(image)
    evidence = [{'path': image, 'dimension': 'data',
                 'caption': '原报告与初审报告提示需核查：Table 1，建议' + risk}]
    html = '<body>' + renderer._review_overview_html(None, info)
    html += renderer._analysis_html(info, evidence)
    html += '<p>旧报告；初检报告；原报告；初审报告。复核结论：建议' + risk + '</p></body>'
    pdf = tmp_path / 'review.pdf'
    renderer._render_pdf(html, str(pdf))
    assert info == before
    with fitz.open(pdf) as doc:
        text = ''.join(page.get_text() for page in doc)
        for forbidden in ['高风险', '低风险', '原报告', '旧报告', '初审报告', '初检报告']:
            assert forbidden not in text
        assert ('有风险' if risk == '高风险' else '未发现风险') in text
        assert 'Table 1' in text
        for page in doc:
            for im in page.get_image_info():
                for block in page.get_text('blocks'):
                    if block[6] == 0:
                        assert (fitz.Rect(block[:4]) & fitz.Rect(im['bbox'])).is_empty


def test_focused_crop_keeps_correct_source_region(renderer, tmp_path):
    source = tmp_path / 'source.pdf'
    with fitz.open() as doc:
        p = doc.new_page(width=400, height=500)
        p.draw_rect(fitz.Rect(100, 150, 300, 250), fill=(1, 0, 0))
        doc.save(source)
    info = {'result': '高风险', 'verdict': '建议高风险',
            'dimension_verdicts': {'data': '高风险', 'image': '低风险', 'ref': '低风险', 'methodology': '低风险'},
            'evidence_images': [{'dimension': 'data', 'pdf_path': str(source), 'page': 1,
                                 'clip': [100, 150, 300, 250], 'caption': '第1页Table 1'}]}
    items = renderer._review_evidence_images(info, {}, tmp_path / 'evidence')
    assert len(items) == 1
    with Image.open(items[0]['path']) as im:
        assert im.size == (600, 300)
        assert im.getpixel((300, 150)) == (255, 0, 0)
    info['evidence_images'][0]['clip'] = [-10, 150, 300, 250]
    with pytest.raises(ValueError, match='裁剪范围'):
        renderer._review_evidence_images(info, {}, tmp_path / 'bad')


def test_all_api_prompt_stages_receive_wording_and_screenshot_rules():
    from api.services import review
    info = {'result': '低风险', 'verdict': '建议低风险'}
    prompts = [review._build_prompt('10.1/x', '/tmp/report', '/tmp/input'),
               review._build_lowrisk_verifier_prompt('10.1/x', '/tmp/report', '/tmp/input', info),
               review._build_judge_prompt('10.1/x', '/tmp/report', '/tmp/input', info, info),
               review._build_deterministic_agent_prompt('10.1/x', '/tmp/report', '/tmp/input', {})]
    for prompt in prompts:
        assert PUBLIC_WRITING_RULES in prompt
        assert '不得用带错误结论的旧标注图' in prompt


def test_final_judge_receives_crop_candidates_without_auto_accepting_them():
    from api.services import review
    info = {'result': '高风险', 'verdict': '建议高风险',
            'evidence_images': [{'dimension': 'data', 'pdf_path': '/tmp/source.pdf',
                                 'page': 3, 'clip': [10, 20, 100, 200], 'caption': 'Table 1'}]}
    compact = review._compact_review_result_for_prompt(info)
    assert compact['evidence_images'] == info['evidence_images']
    prompt = review._build_judge_prompt('10.1/x', '/tmp/report', '/tmp/input', info, {})
    assert '/tmp/source.pdf' in prompt and '"clip": [10, 20, 100, 200]' in prompt
    assert '仅为定位候选，不是已确认结论' in prompt
