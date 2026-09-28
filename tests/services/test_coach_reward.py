"""What the Coach may say a listing offers: the listing's own figures and
terms, never a currency code passed off as a value, never an estimate."""

from app.services.ai_coach import _reward_text


def test_an_investment_is_never_quoted_without_its_price():
    text = _reward_text({"amount": 150000000, "currency": "UZS", "equity_percent": 20})
    assert "amount 150000000 UZS" in text
    assert "20% of her business" in text
    loan = _reward_text({"amount": 20000000, "currency": "UZS", "rate_percent": 4})
    assert "at 4% interest" in loan


def test_a_currency_code_is_not_a_reward():
    assert _reward_text({"volume": 4000, "currency": "UZS"}) == (
        "the listing states nothing — say so, do not estimate"
    )


def test_terms_are_quoted_as_recorded():
    assert _reward_text({"price": 0, "sessions": 8}) == "8 sessions; free of charge"
    assert "commission 12%" in _reward_text({"commission_percent": 12})
    assert "first month free" in _reward_text({"first_month_free": True, "commission_percent": 0})
    assert "not repaid" in _reward_text({"amount": 1, "repayable": False})


def test_free_text_is_quoted_not_paraphrased():
    assert _reward_text({"text": "Office space for six months"}) == (
        '"Office space for six months"'
    )
