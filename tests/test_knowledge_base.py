"""The portal documentation the assistant quotes: complete in three languages,
and true of the product as it is."""

from app.knowledge_base import KNOWLEDGE, KNOWLEDGE_TRANSLATIONS


def test_every_document_has_russian_and_english_editions():
    for title, source_type, body in KNOWLEDGE:
        assert body.strip(), title
        assert source_type in {"faq", "guideline", "legal", "policy"}, title
        editions = KNOWLEDGE_TRANSLATIONS[title]
        assert set(editions) == {"ru", "en"}, title
        for language, (edition_title, edition_body) in editions.items():
            assert edition_title.strip() and edition_body.strip(), (title, language)


def test_the_documents_describe_the_product_as_it_is():
    everything = "\n".join(
        [body for _, _, body in KNOWLEDGE]
        + [body for editions in KNOWLEDGE_TRANSLATIONS.values() for _, body in editions.values()]
    )
    # Accounts are opened with an address and a password (or Google), not by phone.
    assert "telefon raqami orqali" not in everything
    assert "по номеру\nтелефона" not in everything and "phone number and" not in everything
    # Diagnostic v2: digital skills exists, social activity does not.
    assert "raqamli koʻnikmalar" in everything and "ijtimoiy faollik" not in everything
    # Certificates follow completion; there is no final test in the product.
    assert "yakuniy test" not in everything and "итоговый тест" not in everything


def test_the_assistant_describes_its_own_limits():
    titles = [title for title, _, _ in KNOWLEDGE]
    assert any("AI yordamchi" in title for title in titles)
