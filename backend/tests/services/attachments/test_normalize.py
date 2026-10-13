from ai_classification.services.attachments.normalize import normalize, normalize_page


def test_separator_and_case_collapse():
    assert normalize("TX-778201") == "TX778201"
    assert normalize("tx 778201") == "TX778201"
    assert normalize("TX_778_201") == "TX778201"


def test_arabic_indic_digits_become_ascii():
    # ٧٧٨٢٠١ = 778201 in Arabic-Indic digits
    assert normalize("TX-٧٧٨٢٠١") == "TX778201"


def test_empty_and_whitespace():
    assert normalize("") == ""
    assert normalize("  -  _ ") == ""


# ── normalize_page: Arabic spelling unification for page matching ──


def test_page_strips_diacritics_and_tatweel():
    assert normalize_page("المُحْفَظَة") == normalize_page("المحفظة")
    assert normalize_page("اــلمحفظة") == normalize_page("المحفظة")


def test_page_folds_alef_forms():
    assert normalize_page("أحمد") == normalize_page("احمد")
    assert normalize_page("إتمام") == normalize_page("اتمام")
    assert normalize_page("آخر") == normalize_page("اخر")


def test_page_ta_marbuta_to_ha():
    assert normalize_page("المحفظة") == normalize_page("المحفظه")


def test_page_alef_maqsura_to_ya():
    assert normalize_page("مستشفى") == normalize_page("مستشفي")


def test_page_drops_leading_al():
    assert normalize_page("المحفظة") == normalize_page("محفظة")


def test_page_strips_spaces_and_dashes_too():
    assert normalize_page("المحفظة-الرئيسية") == normalize_page("المحفظة الرئيسية")
