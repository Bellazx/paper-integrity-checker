import json
import logging

import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from utils.llm_client import chat

log = logging.getLogger(__name__)

SYSTEM_PROMPT = """You are an expert in academic integrity and research misconduct detection.
You analyze structured results from an automated paper analysis system and produce a professional,
objective assessment report. Be specific about findings, cite exact locations (pages, columns, values),
and assign clear severity levels. Write in English. Use markdown formatting."""

REPORT_TEMPLATE = """Based on the following automated analysis of an academic paper, generate a comprehensive
integrity assessment report.

## Paper Information
- Title: {title}
- Author: {author}
- Journal: {journal}
- DOI: {doi}
- Total pages: {total_pages}
- Total images extracted: {total_images}
- Total references: {total_references}

## Analysis Results

### Image Duplicate Detection
{image_section}

### Data Anomaly Detection
{data_section}

### Reference Verification
{reference_section}

---

Please generate a structured report with the following sections:
1. **Executive Summary** — Overall risk assessment (High/Medium/Low) with key findings in 2-3 sentences
2. **Image Analysis** — Detail each suspicious image pair, what type of duplication was found, and its significance
3. **Data Integrity** — Detail each tabular statistical anomaly or PDB structure-file anomaly, explain why it's suspicious, and assess likelihood of fabrication
4. **Reference Verification** — Summarize reference issues, distinguish between serious (DOI not found) and minor (low match) issues
5. **Overall Assessment** — Synthesize all findings into a final integrity risk rating with justification
6. **Recommendations** — Specific next steps for further investigation

Be objective and evidence-based. Avoid definitive accusations — use language like "suggests", "is consistent with", "warrants further investigation"."""


def _format_image_findings(findings: list[dict]) -> str:
    if not findings:
        return "No suspicious image duplicates detected."

    lines = [f"Found {len(findings)} suspicious image pair(s):\n"]
    for i, f in enumerate(findings):
        lines.append(f"**Pair {i+1}:** Page {f['page_a']} ↔ Page {f['page_b']}")
        lines.append(f"  - Type: {f['match_type']}")
        lines.append(f"  - Similarity: {f['similarity_score']:.3f}")
        lines.append(f"  - Severity: {f['severity']}")
        lines.append(f"  - Details: {f['details']}")
        if f.get("region_a"):
            lines.append(f"  - Region A: x={f['region_a'][0]}, y={f['region_a'][1]}, "
                         f"w={f['region_a'][2]}, h={f['region_a'][3]}")
        if f.get("region_b"):
            lines.append(f"  - Region B: x={f['region_b'][0]}, y={f['region_b'][1]}, "
                         f"w={f['region_b'][2]}, h={f['region_b'][3]}")
        lines.append("")
    return "\n".join(lines)


def _format_data_findings(findings: list[dict]) -> str:
    if not findings:
        return "No data anomalies detected."

    lines = [f"Found {len(findings)} data anomaly(ies):\n"]
    for i, f in enumerate(findings):
        lines.append(f"**Anomaly {i+1}:** [{f['severity'].upper()}] {f['test']}")
        lines.append(f"  - Location: {f['location']}")
        lines.append(f"  - Description: {f['description']}")
        key_details = {k: v for k, v in f['details'].items()
                       if k not in ('testable', 'reason') and not isinstance(v, (list, dict))}
        if key_details:
            safe_details = {k: (bool(v) if hasattr(v, '__bool__') and type(v).__module__ == 'numpy' else v)
                            for k, v in key_details.items()}
            lines.append(f"  - Key metrics: {json.dumps(safe_details, ensure_ascii=False, default=str)}")
        lines.append("")
    return "\n".join(lines)


def _format_reference_findings(findings: list[dict]) -> str:
    if not findings:
        return "All references verified successfully."

    lines = [f"Found {len(findings)} reference issue(s):\n"]
    for i, f in enumerate(findings):
        lines.append(f"**Issue {i+1}:** [{f['severity'].upper()}] {f['issue_type']}")
        lines.append(f"  - Reference #{f['ref_number']}: {f['ref_text'][:150]}...")
        lines.append(f"  - Description: {f['description']}")
        lines.append("")
    return "\n".join(lines)


def generate_report(findings: dict) -> str:
    """Generate a comprehensive integrity report using the LLM."""
    paper = findings.get("paper", {})

    prompt = REPORT_TEMPLATE.format(
        title=paper.get("title", "unknown"),
        author=paper.get("author", "unknown"),
        journal=paper.get("journal", "unknown"),
        doi=paper.get("doi", "unknown"),
        total_pages=paper.get("total_pages", 0),
        total_images=paper.get("total_images", 0),
        total_references=paper.get("total_references", 0),
        image_section=_format_image_findings(findings.get("image_duplicates", [])),
        data_section=_format_data_findings(findings.get("data_anomalies", [])),
        reference_section=_format_reference_findings(findings.get("reference_issues", [])),
    )

    prompt_len = len(prompt)
    if prompt_len > 15000:
        log.warning("Prompt is very long (%d chars), truncating low-severity findings", prompt_len)
        findings_copy = findings.copy()
        for key in ("image_duplicates", "data_anomalies", "reference_issues"):
            items = findings_copy.get(key, [])
            high_medium = [x for x in items if x.get("severity") in ("high", "medium")]
            if len(high_medium) < len(items):
                low_count = len(items) - len(high_medium)
                findings_copy[key] = high_medium
                log.info("Truncated %d low-severity items from %s", low_count, key)

        prompt = REPORT_TEMPLATE.format(
            title=paper.get("title", "unknown"),
            author=paper.get("author", "unknown"),
            journal=paper.get("journal", "unknown"),
            doi=paper.get("doi", "unknown"),
            total_pages=paper.get("total_pages", 0),
            total_images=paper.get("total_images", 0),
            total_references=paper.get("total_references", 0),
            image_section=_format_image_findings(findings_copy.get("image_duplicates", [])),
            data_section=_format_data_findings(findings_copy.get("data_anomalies", [])),
            reference_section=_format_reference_findings(findings_copy.get("reference_issues", [])),
        )

    log.info("Generating LLM report (prompt length: %d chars)", len(prompt))
    report = chat(prompt, system=SYSTEM_PROMPT, temperature=0.3)
    log.info("LLM report generated (%d chars)", len(report))
    return report
