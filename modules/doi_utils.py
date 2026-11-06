from __future__ import annotations

import re
from pathlib import Path


def dir_name_to_doi(name_or_path) -> str:
    """Canonical DOI parse from a paper directory name (the single source of truth).

    Handles all observed dir-name formats:
      * Format A:  ``10.1038__s41565-025-02082-0``  →  ``10.1038/s41565-025-02082-0``  (double-underscore → /)
      * Format A indexed: ``1000_10.1038__s41536-023-00341-z``  →  ``10.1038/s41536-023-00341-z``  (strip leading index)
      * Format B:  ``10.1088_1361-6560_ab97e6``  →  ``10.1088/1361-6560/ab97e6``  (each underscore → /)
      * OUP/OUP-style 2-part: ``10.1093_aje_kwad223``  →  ``10.1093/aje/kwad223``
      * Pure 2-part: ``10.1001_jama.2021.9883``  →  ``10.1001/jama.2021.9883``  (no extra slash, only one _)
      * 4-part: ``10.6061_clinics_2021_e1713``  →  ``10.6061/clinics/2021/e1713``
      * 2-part with leading index: ``20260527_10.1093_aje_kwad223``  →  ``10.1093/aje/kwad223``  (strip prefix before ``10.``)

    For IOP/PHY PUB-style dirs (e.g. ``10.1088_1361-6560_ab97e6``) the canonical
    DOI is the 3-segment form, so every underscore in the post-prefix part is
    converted to a slash. For dirs that are already 2-segment (e.g.
    ``10.1001_jama.2021.9883``) the canonical DOI is the 2-segment form, so
    only the first underscore becomes a slash.

    Also honours ``doi.txt`` in the directory if present and non-empty.
    """
    # Accept Path or str
    if isinstance(name_or_path, Path):
        d = name_or_path
        doi_file = d / "doi.txt"
        if doi_file.exists():
            try:
                t = doi_file.read_text(encoding="utf-8").strip()
                if t:
                    return t
            except Exception:
                pass
        name = d.name
    else:
        name = str(name_or_path)

    if not name:
        return ""

    # Strip any leading non-DOI prefix: anything before "10." in the name
    m = re.search(r"10\.\d{4,}", name)
    if m and m.start() > 0:
        name = name[m.start():]

    # Format A: 10.NNNN__REST → 10.NNNN/REST (double underscore → single slash)
    if "__" in name:
        prefix, rest = name.split("__", 1)
        return f"{prefix}/{rest}"

    # No underscore at all → treat the whole thing as the DOI (already clean)
    if "_" not in name:
        return name

    parts = name.split("_")
    prefix = parts[0]
    rest = "_".join(parts[1:])  # everything after the first underscore

    if len(parts) == 2:
        # 2-part dir: 10.NNNN_journal_article or 10.NNNN_articleid
        # If the rest contains a '_' (i.e. it has multiple segments hidden by 2-part
        # dir name) we need to detect it. But if we got here len(parts) == 2 means
        # there's no second '_' in the name, so 2-seg is correct.
        # E.g. 10.1093_aje → 10.1093/aje (only 2 segments)
        # E.g. 10.1001_jama.2021.9883 → 10.1001/jama.2021.9883 (legit 2-seg)
        return f"{prefix}/{rest}"

    # 3+ parts: every underscore in rest becomes a slash → N-seg DOI
    # E.g. 10.1088_1361-6560_ab97e6 → 10.1088/1361-6560/ab97e6
    # E.g. 10.1093_aje_kwad223 → 10.1093/aje/kwad223
    # E.g. 10.6061_clinics_2021_e1713 → 10.6061/clinics/2021/e1713
    return f"{prefix}/" + "/".join(parts[1:])


def dir_doi_conflict(name_or_path, doi: str) -> str:
    """The directory's DOI when ``doi`` (as read from the PDF) is not the DOI
    the directory is named after; "" when the name is not a DOI or they agree.
    A PDF whose own DOI differs from its directory is usually a misplaced
    file, so callers hold the result back instead of filing it under either."""
    candidates = [c.lower() for c in (normalize_doi(x) for x in dir_name_to_doi_candidates(name_or_path)) if c]
    if not candidates or not candidates[0].startswith("10."):
        return ""
    given = (normalize_doi(doi) or (doi or "").strip()).lower()
    return "" if not given or given in candidates else candidates[0]


def dir_name_to_doi_candidates(name_or_path) -> list[str]:
    """Return all plausible DOI candidates for a dir name, in priority order.

    For 2-part dir names where the rest might be either a single segment
    (legit 2-seg) or two segments (OUP-style 3-seg), this returns both
    variants so DB lookups can try each. Use this for cross-checks; for
    authoritative writes use ``dir_name_to_doi``.
    """
    canonical = dir_name_to_doi(name_or_path)
    if not canonical:
        return []
    cands = [canonical]
    # If canonical is 2-seg and 2nd part has a '_', also emit 3-seg
    if canonical.count("/") == 1:
        a, b = canonical.split("/")
        if "_" in b:
            jrnl, art = b.split("_", 1)
            three_seg = f"{a}/{jrnl}/{art}"
            if three_seg != canonical:
                cands.append(three_seg)
    return cands


def normalize_doi(value: str) -> str:
    """Normalize common DOI input forms without inventing a DOI.

    Keeps valid DOI strings unchanged, strips doi.org prefixes, and fixes the
    frequent missing-slash typo such as ``10.1038s41467-...``.
    """
    value = (value or "").strip().strip(".")
    if not value:
        return ""
    value = value.replace("doi:", "").replace("DOI:", "").strip()
    value = value.replace("https://doi.org/", "").replace("http://doi.org/", "").strip()
    # Semicolons are valid DOI characters in legacy Wiley identifiers such as
    # ``10.1002/...3.0.co;2-p``. Commas and closing square brackets remain
    # delimiters because they commonly terminate DOI citations in prose.
    found = re.search(r"10\.\d{4,9}/[^\s,\]]+", value)
    if found:
        doi = found.group(0).rstrip(".")
        # Parentheses are valid inside DOIs, especially Elsevier/Lancet IDs
        # like 10.1016/S0140-6736(20)30547-X. Only remove a trailing ")" when
        # it is clearly punctuation outside the DOI.
        while doi.endswith(")") and doi.count(")") > doi.count("("):
            doi = doi[:-1].rstrip(".")
        # A few known folder-name formats encode an additional slash as an
        # underscore. Do not rewrite arbitrary underscores: they are valid DOI
        # characters, as in Springer chapter DOI 10.1007/978-...-2_3.
        doi = re.sub(r"^(10\.1210/clinem)_([A-Za-z0-9][A-Za-z0-9.-]*)$", r"\1/\2", doi)
        doi = re.sub(r"^(10\.1088/\d{4}-\d{4})_([A-Za-z0-9][A-Za-z0-9.-]*)$", r"\1/\2", doi)
        return doi
    missing_slash = re.match(r"^(10\.\d{4,})(?!/)(.+)$", value)
    if missing_slash:
        return f"{missing_slash.group(1)}/{missing_slash.group(2).lstrip('_/')}"
    return ""


def choose_authoritative_doi(current: str, detected: str) -> tuple[str, bool, bool]:
    """Return (doi, changed, conflict), preserving a user-supplied DOI.

    The detection pipeline may extract a DOI after the API layer has already
    applied a user override. That override is authoritative. Detected DOI is
    only used to fill an empty current DOI.
    """
    current_norm = normalize_doi(current)
    detected_norm = normalize_doi(detected)
    if current_norm:
        return current_norm, current_norm != (current or ""), bool(detected_norm and detected_norm != current_norm)
    if detected_norm:
        return detected_norm, True, False
    return "", bool(current), False
