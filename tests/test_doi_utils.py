from modules.doi_utils import dir_name_to_doi, normalize_doi


def test_normalize_doi_preserves_valid_underscore() -> None:
    assert normalize_doi("10.1007/978-3-031-43424-2_3") == "10.1007/978-3-031-43424-2_3"


def test_normalize_doi_decodes_known_extra_slash_formats() -> None:
    assert normalize_doi("10.1210/clinem_dgac577") == "10.1210/clinem/dgac577"
    assert normalize_doi("10.1088/1361-6560_ab9f60") == "10.1088/1361-6560/ab9f60"


def test_normalize_doi_preserves_legacy_wiley_semicolon() -> None:
    doi = "10.1002/1521-4095(20020705)14:13/14<963::aid-adma963>3.0.co;2-p"
    assert normalize_doi(doi) == doi


def test_dir_name_to_doi_elife_slug() -> None:
    assert dir_name_to_doi("10.7554_eLife.97621") == "10.7554/eLife.97621"
