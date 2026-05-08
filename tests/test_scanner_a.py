"""
Testes do novo Scanner A — Arbitragem Combinatória.

Cobre os três tipos de inconsistência (Tipo 1, 2, 3) e a regra de verificação
de resolução que descarta sinais com fontes divergentes.
"""

from unittest.mock import patch
from modules import scanner_a

# ── Helpers ───────────────────────────────────────────────────────────────────

_RESOLUTION_TEXT_OK = (
    "This market will resolve YES based on Official FIFA results. "
    "Resolution source: official results of the tournament organizer."
)
_RESOLUTION_TEXT_DIFFERENT = (
    "This market will resolve YES based on Reuters wire reports."
)


def _market(question, yes_price, condition_id=None, slug=None, end_date="2026-06-01T00:00:00Z"):
    cid = condition_id or question[:8].lower().replace(" ", "-")
    return {
        "condition_id":  cid,
        "question":      question,
        "market_slug":   slug or cid,
        "taker_base_fee": 0,
        "accepting_orders": True,
        "tags":          ["Sports"],
        "end_date_iso":  end_date,
        "tokens": [
            {"outcome": "Yes", "token_id": f"{cid}-yes", "price": yes_price},
            {"outcome": "No",  "token_id": f"{cid}-no",  "price": round(1 - yes_price, 4)},
        ],
    }


def _run_scan(markets, liq=200.0, resolution_text=_RESOLUTION_TEXT_OK):
    """Roda scanner_a.scan() com mocks completos."""
    with patch("modules.scanner_a.clob_utils.fetch_sampling_markets", return_value=markets), \
         patch("modules.scanner_a.clob_utils.get_orderbook_liquidity", return_value=liq), \
         patch("modules.scanner_a._fetch_resolution_rules", return_value=resolution_text), \
         patch("modules.scanner_a.rule_validator.assess", return_value=("HIGH", "test")):
        return scanner_a.scan()


# ── Tipo 1: Soma de partes ────────────────────────────────────────────────────

def test_type1_detects_undersum():
    # Soma = 0.40 + 0.35 + 0.18 = 0.93 → gap=0.07, retorno real = 0.07/0.93 = 0.0753
    markets = [
        _market("Will Brazil win the World Cup?",    0.40, "br"),
        _market("Will Argentina win the World Cup?", 0.35, "ar"),
        _market("Will Germany win the World Cup?",   0.18, "de"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.signal_type == 1
    assert abs(s.spread - round(0.07 / 0.93, 4)) < 1e-3
    assert s.resolution_ok is True
    assert "UNDERSUM" in s.description


def test_type1_detects_oversum():
    # Soma = 0.45 + 0.40 + 0.25 = 1.10 → gap=0.10, N=3, retorno real = 0.10/(3-1-0.10) = 0.0526
    markets = [
        _market("Will Brazil win the World Cup?",    0.45, "br"),
        _market("Will Argentina win the World Cup?", 0.40, "ar"),
        _market("Will Germany win the World Cup?",   0.25, "de"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.signal_type == 1
    assert abs(s.spread - round(0.10 / 1.90, 4)) < 1e-3
    assert "OVERSUM" in s.description


def test_type1_rejects_sum_within_tolerance():
    # Soma = 0.51 + 0.48 = 0.99 → gap = 0.01 < 0.02 (mínimo) → descartado
    markets = [
        _market("Will Brazil win the World Cup?",    0.51, "br"),
        _market("Will Argentina win the World Cup?", 0.48, "ar"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 0


def test_type1_accepts_spread_just_above_minimum():
    # Soma = 0.50 + 0.47 = 0.97 → gap = 0.03 > 0.02 → UNDERSUM
    markets = [
        _market("Will Brazil win the World Cup?",    0.50, "br"),
        _market("Will Argentina win the World Cup?", 0.47, "ar"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 1
    assert sigs[0].signal_type == 1


def test_type1_rejects_single_market_group():
    # Apenas 1 mercado no grupo → não há inconsistência possível
    markets = [_market("Will Brazil win the World Cup?", 0.40, "br")]
    sigs = _run_scan(markets)
    assert len(sigs) == 0


def test_type1_rejects_insufficient_liquidity():
    markets = [
        _market("Will Brazil win the World Cup?",    0.40, "br"),
        _market("Will Argentina win the World Cup?", 0.35, "ar"),
        _market("Will Germany win the World Cup?",   0.18, "de"),
    ]
    sigs = _run_scan(markets, liq=50.0)  # abaixo de $100
    assert len(sigs) == 0


def test_type1_discards_when_resolution_check_fails():
    # Com fontes diferentes por mercado, o check falha
    markets = [
        _market("Will Brazil win the World Cup?",    0.40, "br"),
        _market("Will Argentina win the World Cup?", 0.35, "ar"),
        _market("Will Germany win the World Cup?",   0.18, "de"),
    ]
    results = []
    call_count = 0

    def alternating_rules(condition_id):
        nonlocal call_count
        call_count += 1
        # Retorna textos diferentes por condition_id
        if condition_id == "br":
            return _RESOLUTION_TEXT_OK
        return _RESOLUTION_TEXT_DIFFERENT

    with patch("modules.scanner_a.clob_utils.fetch_sampling_markets", return_value=markets), \
         patch("modules.scanner_a.clob_utils.get_orderbook_liquidity", return_value=200.0), \
         patch("modules.scanner_a._fetch_resolution_rules", side_effect=alternating_rules):
        results = scanner_a.scan()

    assert len(results) == 0  # descartado por fontes divergentes


def test_type1_discards_when_resolution_unavailable():
    markets = [
        _market("Will Brazil win the World Cup?",    0.40, "br"),
        _market("Will Argentina win the World Cup?", 0.35, "ar"),
    ]
    sigs = _run_scan(markets, resolution_text="")  # sem descrição
    assert len(sigs) == 0


def test_type1_fields_populated():
    markets = [
        _market("Will Brazil win the World Cup?",    0.40, "br", slug="brazil-wc"),
        _market("Will Argentina win the World Cup?", 0.35, "ar", slug="argentina-wc"),
        _market("Will Germany win the World Cup?",   0.18, "de", slug="germany-wc"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.resolution_ok is True
    assert len(s.market_ids) == 3
    assert len(s.questions) == 3
    assert len(s.yes_prices) == 3
    assert s.min_liquidity == 200.0
    assert s.pnl_est > 0


def test_type1_pnl_estimate():
    # retorno real = 0.07/0.93 = 0.0753, pnl = 0.0753 * 50 = 3.77
    markets = [
        _market("Will Brazil win the World Cup?",    0.40, "br"),
        _market("Will Argentina win the World Cup?", 0.35, "ar"),
        _market("Will Germany win the World Cup?",   0.18, "de"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 1
    expected = round(0.07 / 0.93, 4) * 50
    assert abs(sigs[0].pnl_est - expected) < 0.05


def test_type1_rejects_nonzero_fee():
    m = _market("Will Brazil win the World Cup?", 0.40, "br")
    m["taker_base_fee"] = 100
    m2 = _market("Will Argentina win the World Cup?", 0.35, "ar")
    sigs = _run_scan([m, m2])
    assert len(sigs) == 0


def test_type1_rejects_expired_market():
    markets = [
        _market("Will Brazil win the World Cup?",    0.40, "br", end_date="2020-01-01T00:00:00Z"),
        _market("Will Argentina win the World Cup?", 0.35, "ar", end_date="2020-01-01T00:00:00Z"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 0


def test_type1_multiple_events_independent():
    # Dois eventos distintos, ambos com undersum
    markets = [
        _market("Will Brazil win the World Cup?",    0.40, "bwc"),
        _market("Will Argentina win the World Cup?", 0.35, "awc"),
        _market("Will France win the Euros?",        0.42, "feu"),
        _market("Will Spain win the Euros?",         0.38, "seu"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 2
    types = {s.signal_type for s in sigs}
    assert types == {1}


# ── Tipo 2: Subconjunto lógico ────────────────────────────────────────────────

def test_type2_detects_subset_violation():
    # P(Brazil) = 0.45 > P(Brazil or Argentina) = 0.38 → impossível
    markets = [
        _market("Will Brazil or Argentina win the World Cup?", 0.38, "brar"),
        _market("Will Brazil win the World Cup?",              0.45, "br"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.signal_type == 2
    assert abs(s.spread - 0.07) < 1e-4
    assert s.resolution_ok is True


def test_type2_rejects_spread_too_small():
    # P(Brazil) = 0.40, P(Brazil or Argentina) = 0.39 → spread = 0.01 < 0.02
    markets = [
        _market("Will Brazil or Argentina win the World Cup?", 0.39, "brar"),
        _market("Will Brazil win the World Cup?",              0.40, "br"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 0


def test_type2_no_signal_when_subset_correctly_priced():
    # P(Brazil or Argentina) = 0.65 > P(Brazil) = 0.40 → correto, sem sinal
    markets = [
        _market("Will Brazil or Argentina win the World Cup?", 0.65, "brar"),
        _market("Will Brazil win the World Cup?",              0.40, "br"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 0


def test_type2_rejects_insufficient_liquidity():
    markets = [
        _market("Will Brazil or Argentina win the World Cup?", 0.38, "brar"),
        _market("Will Brazil win the World Cup?",              0.45, "br"),
    ]
    sigs = _run_scan(markets, liq=80.0)
    assert len(sigs) == 0


def test_type2_discards_when_resolution_check_fails():
    markets = [
        _market("Will Brazil or Argentina win the World Cup?", 0.38, "brar"),
        _market("Will Brazil win the World Cup?",              0.45, "br"),
    ]

    def diff_rules(cid):
        return _RESOLUTION_TEXT_OK if cid == "brar" else _RESOLUTION_TEXT_DIFFERENT

    with patch("modules.scanner_a.clob_utils.fetch_sampling_markets", return_value=markets), \
         patch("modules.scanner_a.clob_utils.get_orderbook_liquidity", return_value=200.0), \
         patch("modules.scanner_a._fetch_resolution_rules", side_effect=diff_rules):
        sigs = scanner_a.scan()
    assert len(sigs) == 0


def test_type2_both_teams_in_union_checked():
    # "Will Brazil or Argentina..." — ambos são checados individualmente contra a união.
    # Preços individuais somam 1.0 para não disparar Tipo 1 também.
    markets = [
        _market("Will Brazil or Argentina win the World Cup?", 0.65, "brar"),
        _market("Will Brazil win the World Cup?",              0.70, "br"),   # violação: 0.70 > 0.65
        _market("Will Argentina win the World Cup?",           0.30, "ar"),   # OK: 0.30 < 0.65
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 1
    assert "brazil" in sigs[0].description.lower()


# ── Tipo 3: Implicação direta ─────────────────────────────────────────────────

def test_type3_detects_implication_violation():
    # "Win Championship" implica "Qualify for Semifinals"
    # P(win championship) > P(qualify semifinals) → violação
    markets = [
        _market("Will Brazil win the Championship?",          0.45, "br-champ",
                end_date="2026-06-01T00:00:00Z"),
        _market("Will Brazil qualify for the Semifinals?",    0.35, "br-semi",
                end_date="2026-06-01T00:00:00Z"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.signal_type == 3
    assert abs(s.spread - 0.10) < 1e-4


def test_type3_rejects_spread_too_small():
    markets = [
        _market("Will Brazil win the Championship?",        0.36, "br-champ"),
        _market("Will Brazil qualify for the Semifinals?",  0.35, "br-semi"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 0


def test_type3_no_signal_when_correctly_priced():
    # P(win championship) < P(qualify semifinals) → normal, sem sinal
    markets = [
        _market("Will Brazil win the Championship?",        0.30, "br-champ"),
        _market("Will Brazil qualify for the Semifinals?",  0.55, "br-semi"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 0


def test_type3_requires_same_entity():
    # "Brazil win Championship" vs "France qualify Semifinals" → entidades diferentes, sem sinal
    markets = [
        _market("Will Brazil win the Championship?",        0.45, "br-champ"),
        _market("Will France qualify for the Semifinals?",  0.35, "fr-semi"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 0


def test_type3_discards_when_resolution_check_fails():
    markets = [
        _market("Will Brazil win the Championship?",        0.45, "br-champ"),
        _market("Will Brazil qualify for the Semifinals?",  0.35, "br-semi"),
    ]

    def diff_rules(cid):
        return _RESOLUTION_TEXT_OK if cid == "br-champ" else _RESOLUTION_TEXT_DIFFERENT

    with patch("modules.scanner_a.clob_utils.fetch_sampling_markets", return_value=markets), \
         patch("modules.scanner_a.clob_utils.get_orderbook_liquidity", return_value=200.0), \
         patch("modules.scanner_a._fetch_resolution_rules", side_effect=diff_rules):
        sigs = scanner_a.scan()
    assert len(sigs) == 0


def test_type3_no_duplicate_pairs():
    # O mesmo par não deve gerar dois sinais
    markets = [
        _market("Will Brazil win the Championship?",        0.45, "br-champ"),
        _market("Will Brazil qualify for the Semifinals?",  0.35, "br-semi"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 1


# ── Filtros globais ───────────────────────────────────────────────────────────

def test_skips_market_without_tokens():
    m = {
        "condition_id": "x", "question": "Will X win the Cup?",
        "taker_base_fee": 0, "accepting_orders": True,
        "tags": [], "tokens": [], "end_date_iso": "2026-06-01T00:00:00Z",
    }
    sigs = _run_scan([m])
    assert len(sigs) == 0


def test_resolution_check_called_before_signal_emitted():
    """Garante que sinais sem descrição de resolução são sempre descartados."""
    markets = [
        _market("Will Brazil win the World Cup?",    0.40, "br"),
        _market("Will Argentina win the World Cup?", 0.35, "ar"),
        _market("Will Germany win the World Cup?",   0.18, "de"),
    ]
    with patch("modules.scanner_a.clob_utils.fetch_sampling_markets", return_value=markets), \
         patch("modules.scanner_a.clob_utils.get_orderbook_liquidity", return_value=200.0), \
         patch("modules.scanner_a._fetch_resolution_rules", return_value=""):
        sigs = scanner_a.scan()
    assert len(sigs) == 0


# ── Execute (dry-run) ─────────────────────────────────────────────────────────

def test_execute_dry_run_returns_true():
    markets = [
        _market("Will Brazil win the World Cup?",    0.40, "br", slug="brazil-wc"),
        _market("Will Argentina win the World Cup?", 0.35, "ar", slug="argentina-wc"),
        _market("Will Germany win the World Cup?",   0.18, "de", slug="germany-wc"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 1
    with patch("modules.scanner_a.tracker.record_signal"):
        result = scanner_a.execute(sigs[0], client=None)
    assert result is True


def test_execute_records_signal_with_primary_market():
    markets = [
        _market("Will Brazil win the World Cup?",    0.40, "br", slug="brazil-wc"),
        _market("Will Argentina win the World Cup?", 0.35, "ar", slug="argentina-wc"),
        _market("Will Germany win the World Cup?",   0.18, "de", slug="germany-wc"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 1

    recorded = {}

    def capture(*args, **kwargs):
        recorded["args"]   = args
        recorded["kwargs"] = kwargs

    with patch("modules.scanner_a.tracker.record_signal", side_effect=capture):
        scanner_a.execute(sigs[0], client=None)

    assert recorded["args"][0] == "A"                   # módulo
    assert recorded["args"][1] == sigs[0].market_ids[0] # primeiro mercado
    assert "arb-t1" in recorded["args"][3]              # side


# ── Tipo 1b: Ranges exclusivos (_detect_ranges) ───────────────────────────────

def test_ranges_detects_undersum():
    # 4 faixas fechadas (lower + upper): soma=0.85, gap=0.15, retorno = 0.15/0.85 = 0.1765
    markets = [
        _market("Will Nvidia market cap be less than $2T by end of 2026?",        0.25, "r1"),
        _market("Will Nvidia market cap be between $2T and $3T by end of 2026?",  0.22, "r2"),
        _market("Will Nvidia market cap be between $3T and $4T by end of 2026?",  0.18, "r3"),
        _market("Will Nvidia market cap be greater than $4T by end of 2026?",     0.20, "r4"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.signal_type == 1
    assert "RANGE" in s.description
    assert "UNDERSUM" in s.description
    assert abs(s.spread - round(0.15 / 0.85, 4)) < 1e-3


def test_ranges_detects_oversum():
    # 4 faixas fechadas: soma=1.20 → OVERSUM
    markets = [
        _market("Will Nvidia market cap be less than $2T by end of 2026?",        0.32, "r1"),
        _market("Will Nvidia market cap be between $2T and $3T by end of 2026?",  0.30, "r2"),
        _market("Will Nvidia market cap be between $3T and $4T by end of 2026?",  0.28, "r3"),
        _market("Will Nvidia market cap be greater than $4T by end of 2026?",     0.30, "r4"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 1
    assert "OVERSUM" in sigs[0].description


def test_ranges_requires_at_least_3_markets():
    # Apenas 2 faixas → não sinaliza (ranges precisam de 3+ para confirmar exaustividade)
    markets = [
        _market("Will Nvidia market cap be between $2T and $3T by end of 2026?", 0.30, "r1"),
        _market("Will Nvidia market cap be less than $2T by end of 2026?",       0.30, "r2"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 0


def test_ranges_rejects_sum_within_tolerance():
    # Soma = 0.34 + 0.33 + 0.32 = 0.99 → gap = 0.01 < 0.02 → descartado
    markets = [
        _market("Will Nvidia market cap be between $2T and $3T by end of 2026?", 0.34, "r1"),
        _market("Will Nvidia market cap be between $3T and $4T by end of 2026?", 0.33, "r2"),
        _market("Will Nvidia market cap be less than $2T by end of 2026?",       0.32, "r3"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 0


def test_ranges_rejects_insufficient_liquidity():
    markets = [
        _market("Will Nvidia market cap be between $2T and $3T by end of 2026?", 0.30, "r1"),
        _market("Will Nvidia market cap be between $3T and $4T by end of 2026?", 0.25, "r2"),
        _market("Will Nvidia market cap be less than $2T by end of 2026?",       0.30, "r3"),
    ]
    sigs = _run_scan(markets, liq=50.0)
    assert len(sigs) == 0


def test_ranges_rejects_incomplete_set_no_upper():
    # Tem lower (less than) mas não tem upper — conjunto aberto no topo
    markets = [
        _market("Will Nvidia market cap be less than $2T by end of 2026?",        0.25, "r1"),
        _market("Will Nvidia market cap be between $2T and $3T by end of 2026?",  0.22, "r2"),
        _market("Will Nvidia market cap be between $3T and $4T by end of 2026?",  0.18, "r3"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 0


def test_ranges_rejects_incomplete_set_no_lower():
    # Tem upper (greater than) mas não tem lower — conjunto aberto na base
    markets = [
        _market("Will Nvidia market cap be between $2T and $3T by end of 2026?",  0.22, "r1"),
        _market("Will Nvidia market cap be between $3T and $4T by end of 2026?",  0.18, "r2"),
        _market("Will Nvidia market cap be greater than $4T by end of 2026?",     0.20, "r3"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 0


def test_ranges_accepts_above_keyword_as_upper():
    # "above" deve ser reconhecido como extremo superior
    markets = [
        _market("Will Nvidia market cap be less than $2T by end of 2026?",        0.25, "r1"),
        _market("Will Nvidia market cap be between $2T and $3T by end of 2026?",  0.22, "r2"),
        _market("Will Nvidia market cap be between $3T and $4T by end of 2026?",  0.18, "r3"),
        _market("Will Nvidia market cap be above $4T by end of 2026?",            0.20, "r4"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 1
    assert "RANGE" in sigs[0].description


def test_ranges_discards_mismatched_stems():
    # Stems diferentes → não formam grupo
    markets = [
        _market("Will Nvidia market cap be between $2T and $3T by end of 2026?", 0.30, "r1"),
        _market("Will Nvidia revenue be between $50B and $60B by end of 2026?",  0.25, "r2"),
        _market("Will Tesla market cap be less than $1T by end of 2026?",        0.30, "r3"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 0


# ── Tipo 2b: Threshold ordering (_detect_thresholds) ─────────────────────────

def test_thresholds_detects_ordering_violation():
    # P(above 60B) = 0.65 > P(above 50B) = 0.60 → impossível
    markets = [
        _market("Will Nvidia revenue be above 50B in 2026?", 0.60, "th1"),
        _market("Will Nvidia revenue be above 60B in 2026?", 0.65, "th2"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.signal_type == 2
    assert "THRESHOLD" in s.description
    assert abs(s.spread - 0.05) < 1e-4


def test_thresholds_no_signal_correct_ordering():
    # P(above 60B) = 0.40 < P(above 50B) = 0.70 → correto
    markets = [
        _market("Will Nvidia revenue be above 50B in 2026?", 0.70, "th1"),
        _market("Will Nvidia revenue be above 60B in 2026?", 0.40, "th2"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 0


def test_thresholds_rejects_spread_too_small():
    # P(above 60B) = 0.61 → spread = 0.01 < 0.02
    markets = [
        _market("Will Nvidia revenue be above 50B in 2026?", 0.60, "th1"),
        _market("Will Nvidia revenue be above 60B in 2026?", 0.61, "th2"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 0


def test_thresholds_parses_multiplier_suffix():
    # "above 2B" e "above 3B" — multiplier B deve ser parseado corretamente
    markets = [
        _market("Will Nvidia market cap be above 2B in 2026?", 0.80, "th1"),
        _market("Will Nvidia market cap be above 3B in 2026?", 0.90, "th2"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 1
    assert "THRESHOLD" in sigs[0].description


def test_thresholds_no_duplicate_pairs():
    markets = [
        _market("Will Nvidia revenue be above 50B in 2026?", 0.60, "th1"),
        _market("Will Nvidia revenue be above 60B in 2026?", 0.70, "th2"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 1


def test_thresholds_rejects_insufficient_liquidity():
    markets = [
        _market("Will Nvidia revenue be above 50B in 2026?", 0.60, "th1"),
        _market("Will Nvidia revenue be above 60B in 2026?", 0.70, "th2"),
    ]
    sigs = _run_scan(markets, liq=80.0)
    assert len(sigs) == 0


# ── Tipo 2c: Date ordering (_detect_dates) ───────────────────────────────────

def test_dates_detects_ordering_violation():
    # P(by January 2026) = 0.50 > P(by June 2026) = 0.25 → prazo menor não pode custar mais
    markets = [
        _market("Will Nvidia reach $200 by January 2026?", 0.50, "d1"),
        _market("Will Nvidia reach $200 by June 2026?",    0.25, "d2"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 1
    s = sigs[0]
    assert s.signal_type == 2
    assert "DATA" in s.description
    assert abs(s.spread - 0.25) < 1e-4


def test_dates_no_signal_correct_ordering():
    # P(by January 2026) = 0.20 < P(by June 2026) = 0.60 → correto
    markets = [
        _market("Will Nvidia reach $200 by January 2026?", 0.20, "d1"),
        _market("Will Nvidia reach $200 by June 2026?",    0.60, "d2"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 0


def test_dates_rejects_spread_too_small():
    # P(by Jan) = 0.51, P(by Jun) = 0.50 → spread = 0.01 < 0.02
    markets = [
        _market("Will Nvidia reach $200 by January 2026?", 0.51, "d1"),
        _market("Will Nvidia reach $200 by June 2026?",    0.50, "d2"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 0


def test_dates_no_duplicate_pairs():
    markets = [
        _market("Will Nvidia reach $200 by January 2026?", 0.50, "d1"),
        _market("Will Nvidia reach $200 by June 2026?",    0.25, "d2"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 1


def test_dates_rejects_insufficient_liquidity():
    markets = [
        _market("Will Nvidia reach $200 by January 2026?", 0.50, "d1"),
        _market("Will Nvidia reach $200 by June 2026?",    0.25, "d2"),
    ]
    sigs = _run_scan(markets, liq=80.0)
    assert len(sigs) == 0


def test_dates_discards_mismatched_stems():
    # Stems diferentes → não formam grupo
    markets = [
        _market("Will Nvidia reach $200 by January 2026?", 0.50, "d1"),
        _market("Will Tesla reach $200 by June 2026?",     0.25, "d2"),
    ]
    sigs = _run_scan(markets)
    assert len(sigs) == 0


def test_dates_resolution_check_applied():
    markets = [
        _market("Will Nvidia reach $200 by January 2026?", 0.50, "d1"),
        _market("Will Nvidia reach $200 by June 2026?",    0.25, "d2"),
    ]
    sigs = _run_scan(markets, resolution_text="")
    assert len(sigs) == 0


# ── Filtro de qualidade de regras (rule_quality) ──────────────────────────────

def test_scan_discards_low_quality_signal():
    markets = [
        _market("Will Brazil win the World Cup?",    0.40, "br"),
        _market("Will Argentina win the World Cup?", 0.35, "ar"),
        _market("Will Germany win the World Cup?",   0.18, "de"),
    ]
    with patch("modules.scanner_a.clob_utils.fetch_sampling_markets", return_value=markets), \
         patch("modules.scanner_a.clob_utils.get_orderbook_liquidity", return_value=200.0), \
         patch("modules.scanner_a._fetch_resolution_rules", return_value=_RESOLUTION_TEXT_OK), \
         patch("modules.scanner_a.rule_validator.assess", return_value=("LOW", "regras vagas")):
        sigs = scanner_a.scan()
    assert len(sigs) == 0


def test_scan_passes_medium_quality_signal():
    markets = [
        _market("Will Brazil win the World Cup?",    0.40, "br"),
        _market("Will Argentina win the World Cup?", 0.35, "ar"),
        _market("Will Germany win the World Cup?",   0.18, "de"),
    ]
    with patch("modules.scanner_a.clob_utils.fetch_sampling_markets", return_value=markets), \
         patch("modules.scanner_a.clob_utils.get_orderbook_liquidity", return_value=200.0), \
         patch("modules.scanner_a._fetch_resolution_rules", return_value=_RESOLUTION_TEXT_OK), \
         patch("modules.scanner_a.rule_validator.assess", return_value=("MEDIUM", "fonte confiável")):
        sigs = scanner_a.scan()
    assert len(sigs) == 1
    assert sigs[0].rule_quality == "MEDIUM"


def test_scan_rule_quality_stored_on_signal():
    markets = [
        _market("Will Brazil win the World Cup?",    0.40, "br"),
        _market("Will Argentina win the World Cup?", 0.35, "ar"),
        _market("Will Germany win the World Cup?",   0.18, "de"),
    ]
    sigs = _run_scan(markets)  # mocks assess → ("HIGH", "test")
    assert len(sigs) == 1
    assert sigs[0].rule_quality == "HIGH"
