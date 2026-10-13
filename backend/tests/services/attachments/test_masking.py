from ai_classification.services.attachments.masking import mask_personal_data


def test_masks_email():
    assert "test@example.com" not in mask_personal_data("تواصل عبر test@example.com")


def test_masks_saudi_mobile():
    assert "0551234567" not in mask_personal_data("اتصل على 0551234567")


def test_masks_international_phone():
    assert "+966 55 123 4567" not in mask_personal_data("هاتف +966 55 123 4567")


def test_masks_national_id_by_label():
    out = mask_personal_data("رقم الهوية: 1234567890")
    assert "1234567890" not in out
    assert "رقم الهوية" in out  # the label is kept, the value is masked


def test_masks_passport_by_label():
    assert "A1234567" not in mask_personal_data("جواز السفر: A1234567")


def test_masks_name_by_label():
    out = mask_personal_data("الاسم: محمد أحمد")
    assert "محمد" not in out and "أحمد" not in out


def test_masks_multi_word_name_stopping_at_next_label():
    out = mask_personal_data("الاسم: محمد أحمد رقم الهوية: 1234567890")
    assert "محمد" not in out and "أحمد" not in out
    assert "1234567890" not in out  # the doc number is still masked separately


def test_never_masks_error_codes_or_technical_ids():
    text = ("رقم الطلب TX-778201 والرمز E-REF-9 و Invoice INV-55821 و "
            "Error 10036015 و الرقم المرجعي 1234567890")
    assert mask_personal_data(text) == text


def test_does_not_mask_bare_digits_without_a_personal_label():
    # "by context": a bare 10-digit number is NOT masked (could be a technical ID)
    assert "1234567890" in mask_personal_data("مرجع 1234567890")
