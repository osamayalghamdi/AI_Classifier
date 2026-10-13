from ai_classification.services.attachments.normalize import normalize


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
