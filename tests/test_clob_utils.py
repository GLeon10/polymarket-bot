from modules.clob_utils import has_tag, token_by_outcome


def _market(tags, tokens):
    return {"tags": tags, "tokens": tokens}


def _token(outcome, token_id="abc", price=0.5):
    return {"outcome": outcome, "token_id": token_id, "price": price}


# ── has_tag ──────────────────────────────────────────────────────────────────

def test_has_tag_match():
    assert has_tag(_market(["Geopolitics", "World"], []), {"Geopolitics"})


def test_has_tag_no_match():
    assert not has_tag(_market(["Sports", "NBA"], []), {"Geopolitics"})


def test_has_tag_partial_overlap():
    assert has_tag(_market(["Geopolitics", "Weather"], []), {"Weather", "Daily Temperature"})


def test_has_tag_empty_market_tags():
    assert not has_tag(_market([], []), {"Geopolitics"})


def test_has_tag_none_tags():
    assert not has_tag({"tags": None, "tokens": []}, {"Geopolitics"})


# ── token_by_outcome ─────────────────────────────────────────────────────────

def test_token_by_outcome_yes():
    market = _market([], [_token("Yes", "id1"), _token("No", "id2")])
    t = token_by_outcome(market, "Yes")
    assert t is not None
    assert t["token_id"] == "id1"


def test_token_by_outcome_no():
    market = _market([], [_token("Yes", "id1"), _token("No", "id2")])
    t = token_by_outcome(market, "No")
    assert t["token_id"] == "id2"


def test_token_by_outcome_case_insensitive():
    market = _market([], [_token("YES", "id1")])
    assert token_by_outcome(market, "yes") is not None
    assert token_by_outcome(market, "Yes") is not None


def test_token_by_outcome_missing():
    market = _market([], [_token("Yes", "id1")])
    assert token_by_outcome(market, "No") is None


def test_token_by_outcome_empty_tokens():
    assert token_by_outcome(_market([], []), "Yes") is None
