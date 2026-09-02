"""Retrieval helpers and the safety pre-filter."""

import pytest

from app.services.rag import build_context, chunk_text, needs_human_escalation


@pytest.mark.parametrize(
    "disclosure",
    [
        # Present tense is how a disclosure is usually written, and it is what
        # a past-tense-only keyword list misses.
        "Erim meni uradi, nima qilay?",
        "Erim meni uryapti",
        "Eram meni kaltakladi, nima qilay?",
        "Meni ishga majburlayapti va tahdid qilyapti",
        "Меня избивает муж",
        "Он мне угрожает",
        "Я не хочу жить",
        "My husband beats me",
        "I want to kill myself",
        "I want to die",
    ],
)
def test_violence_disclosure_is_escalated_before_reaching_the_model(disclosure):
    assert needs_human_escalation(disclosure)


@pytest.mark.parametrize(
    "spelling",
    # The same sentence with each apostrophe glyph Uzbek Latin is typed with,
    # and with none at all.
    [
        "O\u02bbzimni o\u02bbldirgim kelyapti",
        "O\u2018zimni o\u2018ldirgim kelyapti",
        "O'zimni o'ldirgim kelyapti",
        "Ozimni oldirgim kelyapti",
    ],
)
def test_apostrophe_spelling_does_not_defeat_the_filter(spelling):
    assert needs_human_escalation(spelling)


@pytest.mark.parametrize(
    "question",
    [
        "Qaysi kursni tanlashim kerak?",
        "Как открыть ИП?",
        "Buxgalteriya kursiga qanday yozilaman?",
        "Grant olish uchun nima kerak?",
        "How do I get a certificate?",
    ],
)
def test_ordinary_questions_are_not_escalated(question):
    assert not needs_human_escalation(question)


def test_chunking_keeps_pieces_within_the_size_budget():
    text = "\n\n".join(f"Paragraph {i} " + "word " * 60 for i in range(10))
    chunks = chunk_text(text, size=600, overlap=100)
    assert chunks
    assert all(len(chunk) <= 700 for chunk in chunks)


def test_oversized_paragraph_is_split_rather_than_dropped():
    chunks = chunk_text("x" * 5000, size=1000, overlap=200)
    assert len(chunks) > 1
    assert sum(len(c) for c in chunks) >= 5000


def test_empty_context_is_explicit():
    assert "empty" in build_context([])
