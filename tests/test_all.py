"""
Suite de testes consolidada — todos os módulos em um único arquivo.
Execute com:  pytest tests/test_all.py -q
"""

import re
from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest

# ── Imports ───────────────────────────────────────────────────────────────────
from config import config
from modules.clob_utils import has_tag, token_by_outcome
from modules import oracle_b1, oracle_b2, scanner_a
import modules.oracle_b4 as oracle_b4_mod
from modules.oracle_b1 import (
    _city_from_question, _model_probability,
    _resolution_within_hours as _b1_res_hours,
)
from modules.oracle_b2 import (
    _extract_teams, _detect_game,
    _resolution_within_hours as _b2_res_hours,
)
from modules.oracle_b3 import (
    _detect_candidate_party, _detect_dates as _b3_detect_dates,
    _detect_sum, _filter_politics, _vote_criterion,
    _check_resolution_rules as _b3_check_res,
)
from modules.oracle_b4 import (
    _detect_ml_spread, _filter_sports, _is_live_window,
    _is_blocked_final, _normalize_team, scan as _b4_scan,
)
from modules import rule_validator
from modules.rule_validator import _keyword_quality
from modules.scanner_corr import (
    CorrSignal, _filter_fee, _filter_liquidity,
    _filter_exhaustiveness, _filter_resolution,
    _validate as _corr_validate,
)


# ══════════════════════════════════════════════════════════════════════════════
# clob_utils
# ══════════════════════════════════════════════════════════════════════════════

class TestClobUtils:
    @staticmethod
    def _market(tags, tokens):
        return {"tags": tags, "tokens": tokens}

    @staticmethod
    def _token(outcome, token_id="abc", price=0.5):
        return {"outcome": outcome, "token_id": token_id, "price": price}

    def test_has_tag_match(self):
        assert has_tag(self._market(["Geopolitics", "World"], []), {"Geopolitics"})

    def test_has_tag_no_match(self):
        assert not has_tag(self._market(["Sports", "NBA"], []), {"Geopolitics"})

    def test_has_tag_partial_overlap(self):
        assert has_tag(self._market(["Geopolitics", "Weather"], []), {"Weather", "Daily Temperature"})

    def test_has_tag_empty_market_tags(self):
        assert not has_tag(self._market([], []), {"Geopolitics"})

    def test_has_tag_none_tags(self):
        assert not has_tag({"tags": None, "tokens": []}, {"Geopolitics"})

    def test_token_by_outcome_yes(self):
        market = self._market([], [self._token("Yes", "id1"), self._token("No", "id2")])
        assert token_by_outcome(market, "Yes")["token_id"] == "id1"

    def test_token_by_outcome_no(self):
        market = self._market([], [self._token("Yes", "id1"), self._token("No", "id2")])
        assert token_by_outcome(market, "No")["token_id"] == "id2"

    def test_token_by_outcome_case_insensitive(self):
        market = self._market([], [self._token("YES", "id1")])
        assert token_by_outcome(market, "yes") is not None
        assert token_by_outcome(market, "Yes") is not None

    def test_token_by_outcome_missing(self):
        market = self._market([], [self._token("Yes", "id1")])
        assert token_by_outcome(market, "No") is None

    def test_token_by_outcome_empty_tokens(self):
        assert token_by_outcome(self._market([], []), "Yes") is None


# ══════════════════════════════════════════════════════════════════════════════
# config
# ══════════════════════════════════════════════════════════════════════════════

class TestConfig:
    def test_validate_passes_with_valid_config(self):
        with patch.multiple("config.config", PRIVATE_KEY="0xabc", WALLET_ADDRESS="0x123",
                            MODE="dry_run", ALLOC_A=0.90, ALLOC_RSRV=0.10, STRATEGY_A_ENABLED=True):
            config.validate()

    def test_validate_raises_missing_private_key(self):
        with patch.multiple("config.config", PRIVATE_KEY="", WALLET_ADDRESS="0x123", MODE="dry_run",
                            ALLOC_A=0.90, ALLOC_RSRV=0.10, STRATEGY_A_ENABLED=True):
            with pytest.raises(EnvironmentError, match="PRIVATE_KEY"):
                config.validate()

    def test_validate_raises_missing_wallet(self):
        with patch.multiple("config.config", PRIVATE_KEY="0xabc", WALLET_ADDRESS="", MODE="dry_run",
                            ALLOC_A=0.90, ALLOC_RSRV=0.10, STRATEGY_A_ENABLED=True):
            with pytest.raises(EnvironmentError, match="WALLET_ADDRESS"):
                config.validate()

    def test_validate_raises_invalid_mode(self):
        with patch.multiple("config.config", PRIVATE_KEY="0xabc", WALLET_ADDRESS="0x123",
                            MODE="production", ALLOC_A=0.90, ALLOC_RSRV=0.10, STRATEGY_A_ENABLED=True):
            with pytest.raises(EnvironmentError, match="MODE"):
                config.validate()

    def test_validate_raises_allocation_overflow(self):
        with patch.multiple("config.config", PRIVATE_KEY="0xabc", WALLET_ADDRESS="0x123",
                            MODE="dry_run", ALLOC_A=0.95, ALLOC_RSRV=0.10, STRATEGY_A_ENABLED=True):
            with pytest.raises(EnvironmentError, match="Aloca"):
                config.validate()

    def test_validate_raises_strategy_a_disabled(self):
        with patch.multiple("config.config", PRIVATE_KEY="0xabc", WALLET_ADDRESS="0x123",
                            MODE="dry_run", ALLOC_A=0.90, ALLOC_RSRV=0.10, STRATEGY_A_ENABLED=False):
            with pytest.raises(EnvironmentError, match="obrigatória"):
                config.validate()

    def test_capital_allocation_math(self):
        assert config.CAPITAL_A    == config.TOTAL_CAPITAL * config.ALLOC_A
        assert config.CAPITAL_RSRV == config.TOTAL_CAPITAL * config.ALLOC_RSRV
        assert config.MAX_TRADE_B_USD == config.TOTAL_CAPITAL * config.MAX_TRADE_B

    def test_phase2_defaults(self):
        assert config.PHASE2_TRIGGER_USD == 45.0
        assert config.BANCA_B_ALLOC_B1 == 0.60
        assert config.BANCA_B_ALLOC_B2 == 0.40
        assert config.B_STOP_LOSS_BANCA == 0.30
        assert config.B1_MIN_RESOLVED_FOR_B2 == 5


# ══════════════════════════════════════════════════════════════════════════════
# rule_validator
# ══════════════════════════════════════════════════════════════════════════════

class TestRuleValidator:
    def test_keyword_high_all_three_signals(self):
        text = ("This market resolves via Reuters. "
                "If no data is available, we use the most recent available figure. "
                "Outcomes cover less than 50 or greater than 100.")
        assert _keyword_quality(text) == "HIGH"

    def test_keyword_high_imf_with_fallback(self):
        text = ("Resolution source: IMF Portwatch. "
                "Fallback: if primary source unavailable, use most recent available data.")
        assert _keyword_quality(text) == "HIGH"

    def test_keyword_medium_only_reliable_source(self):
        assert _keyword_quality("This market will resolve according to Reuters wire reports.") == "MEDIUM"

    def test_keyword_medium_only_fallback(self):
        assert _keyword_quality("If no data is available, the market uses the most recent figure.") == "MEDIUM"

    def test_keyword_medium_only_range_coverage(self):
        assert _keyword_quality("Possible outcomes: less than 10 ships or greater than 50 ships.") == "MEDIUM"

    def test_keyword_low_no_signals(self):
        assert _keyword_quality("The market resolves based on general consensus of traders.") == "LOW"

    def test_keyword_low_empty_text(self):
        assert _keyword_quality("") == "LOW"

    def test_assess_high_skips_llm(self):
        rule_validator.clear_cache()
        text = ("Reuters is the resolution source. Fallback if no data available. "
                "Covers less than 10 or greater than 50.")
        with patch("modules.rule_validator._llm_validate") as mock_llm:
            quality, _ = rule_validator.assess(text, "cid-high-test")
            mock_llm.assert_not_called()
        assert quality == "HIGH"

    def test_assess_high_no_api_key_skips_llm(self):
        rule_validator.clear_cache()
        text = "Reuters source. If no data available, fallback. Less than 10 or greater than 50."
        with patch("modules.rule_validator._llm_validate") as mock_llm, \
             patch("modules.rule_validator.config") as mock_cfg:
            mock_cfg.ANTHROPIC_API_KEY = ""
            rule_validator.assess(text, "cid-high-nokey")
            mock_llm.assert_not_called()

    def test_assess_low_calls_llm(self):
        rule_validator.clear_cache()
        text = "The market resolves based on general conditions."
        with patch("modules.rule_validator._llm_validate",
                   return_value={"quality": "MEDIUM", "reason": "LLM upgrade"}) as mock_llm, \
             patch("modules.rule_validator.config") as mock_cfg:
            mock_cfg.ANTHROPIC_API_KEY = "test-key"
            quality, reason = rule_validator.assess(text, "cid-low-test")
            mock_llm.assert_called_once()
        assert quality == "MEDIUM"
        assert "LLM upgrade" in reason

    def test_assess_medium_calls_llm(self):
        rule_validator.clear_cache()
        text = "This market resolves via Reuters."
        with patch("modules.rule_validator._llm_validate",
                   return_value={"quality": "LOW", "reason": "missing fallback"}) as mock_llm, \
             patch("modules.rule_validator.config") as mock_cfg:
            mock_cfg.ANTHROPIC_API_KEY = "test-key"
            quality, _ = rule_validator.assess(text, "cid-med-test")
            mock_llm.assert_called_once()
        assert quality == "LOW"

    def test_assess_no_api_key_uses_keyword_quality(self):
        rule_validator.clear_cache()
        text = "This market resolves via Reuters."
        with patch("modules.rule_validator._llm_validate") as mock_llm, \
             patch("modules.rule_validator.config") as mock_cfg:
            mock_cfg.ANTHROPIC_API_KEY = ""
            quality, _ = rule_validator.assess(text, "cid-nokey-medium")
            mock_llm.assert_not_called()
        assert quality == "MEDIUM"

    def test_assess_no_api_key_low_stays_low(self):
        rule_validator.clear_cache()
        text = "General conditions determine resolution."
        with patch("modules.rule_validator._llm_validate") as mock_llm, \
             patch("modules.rule_validator.config") as mock_cfg:
            mock_cfg.ANTHROPIC_API_KEY = ""
            quality, _ = rule_validator.assess(text, "cid-nokey-low")
            mock_llm.assert_not_called()
        assert quality == "LOW"

    def test_assess_caches_result_per_condition_id(self):
        rule_validator.clear_cache()
        text = "General market conditions."
        with patch("modules.rule_validator._llm_validate",
                   return_value={"quality": "MEDIUM", "reason": "ok"}) as mock_llm, \
             patch("modules.rule_validator.config") as mock_cfg:
            mock_cfg.ANTHROPIC_API_KEY = "test-key"
            rule_validator.assess(text, "cid-cache-1")
            rule_validator.assess(text, "cid-cache-1")
            rule_validator.assess(text, "cid-cache-2")
        assert mock_llm.call_count == 2

    def test_clear_cache_allows_re_evaluation(self):
        rule_validator.clear_cache()
        text = "General market conditions."
        with patch("modules.rule_validator._llm_validate",
                   return_value={"quality": "MEDIUM", "reason": "ok"}) as mock_llm, \
             patch("modules.rule_validator.config") as mock_cfg:
            mock_cfg.ANTHROPIC_API_KEY = "test-key"
            rule_validator.assess(text, "cid-clear")
            rule_validator.clear_cache()
            rule_validator.assess(text, "cid-clear")
        assert mock_llm.call_count == 2

    def test_llm_validate_parses_clean_json(self):
        rule_validator.clear_cache()
        fake_response = {"content": [{"text": '{"exhaustive": true, "has_fallback": true, '
                                               '"edge_cases_handled": false, "source_reliable": true, '
                                               '"quality": "HIGH", "reason": "good rules"}'}]}
        with patch("modules.rule_validator.requests.post") as mock_post:
            mock_post.return_value.raise_for_status = lambda: None
            mock_post.return_value.json = lambda: fake_response
            result = rule_validator._llm_validate("some rules", "cid-llm")
        assert result["quality"] == "HIGH"
        assert result["reason"] == "good rules"

    def test_llm_validate_handles_json_wrapped_in_text(self):
        rule_validator.clear_cache()
        json_block = ('{"exhaustive": false, "has_fallback": false, "edge_cases_handled": false, '
                      '"source_reliable": false, "quality": "LOW", "reason": "vague"}')
        fake_response = {"content": [{"text": f"Here is my evaluation:\n{json_block}\nEnd."}]}
        with patch("modules.rule_validator.requests.post") as mock_post:
            mock_post.return_value.raise_for_status = lambda: None
            mock_post.return_value.json = lambda: fake_response
            result = rule_validator._llm_validate("some rules", "cid-wrapped")
        assert result["quality"] == "LOW"

    def test_llm_validate_returns_low_on_request_error(self):
        rule_validator.clear_cache()
        import requests as req
        with patch("modules.rule_validator.requests.post", side_effect=req.RequestException("timeout")):
            result = rule_validator._llm_validate("some rules", "cid-error")
        assert result["quality"] == "LOW"
        assert "falha" in result["reason"].lower()


# ══════════════════════════════════════════════════════════════════════════════
# scanner_a
# ══════════════════════════════════════════════════════════════════════════════

_RES_OK   = ("This market will resolve YES based on Official FIFA results. "
             "Resolution source: official results of the tournament organizer.")
_RES_DIFF = "This market will resolve YES based on Reuters wire reports."


class TestScannerA:
    @staticmethod
    def _market(question, yes_price, condition_id=None, slug=None,
                end_date="2026-06-01T00:00:00Z"):
        cid = condition_id or question[:8].lower().replace(" ", "-")
        return {
            "condition_id": cid, "question": question,
            "market_slug": slug or cid, "taker_base_fee": 0,
            "accepting_orders": True, "tags": ["Sports"],
            "end_date_iso": end_date,
            "tokens": [
                {"outcome": "Yes", "token_id": f"{cid}-yes", "price": yes_price},
                {"outcome": "No",  "token_id": f"{cid}-no",  "price": round(1 - yes_price, 4)},
            ],
        }

    @staticmethod
    def _run(markets, liq=200.0, res_text=_RES_OK):
        with patch("modules.scanner_a.clob_utils.fetch_sampling_markets", return_value=markets), \
             patch("modules.scanner_a.clob_utils.get_orderbook_liquidity", return_value=liq), \
             patch("modules.scanner_a._fetch_resolution_rules", return_value=res_text), \
             patch("modules.scanner_a.rule_validator.assess", return_value=("HIGH", "test")):
            return scanner_a.scan()

    def _m(self, *a, **kw): return self._market(*a, **kw)

    # Tipo 1 ──────────────────────────────────────────────────────────────────

    def test_type1_detects_undersum(self):
        sigs = self._run([self._m("Will Brazil win the World Cup?", 0.40, "br"),
                          self._m("Will Argentina win the World Cup?", 0.35, "ar"),
                          self._m("Will Germany win the World Cup?", 0.18, "de")])
        assert len(sigs) == 1
        assert sigs[0].signal_type == 1
        assert abs(sigs[0].spread - round(0.07 / 0.93, 4)) < 1e-3
        assert "UNDERSUM" in sigs[0].description

    def test_type1_detects_oversum(self):
        sigs = self._run([self._m("Will Brazil win the World Cup?", 0.45, "br"),
                          self._m("Will Argentina win the World Cup?", 0.40, "ar"),
                          self._m("Will Germany win the World Cup?", 0.25, "de")])
        assert len(sigs) == 1
        assert abs(sigs[0].spread - round(0.10 / 1.90, 4)) < 1e-3
        assert "OVERSUM" in sigs[0].description

    def test_type1_rejects_sum_within_tolerance(self):
        sigs = self._run([self._m("Will Brazil win the World Cup?", 0.51, "br"),
                          self._m("Will Argentina win the World Cup?", 0.48, "ar")])
        assert len(sigs) == 0

    def test_type1_accepts_spread_just_above_minimum(self):
        sigs = self._run([self._m("Will Brazil win the World Cup?", 0.50, "br"),
                          self._m("Will Argentina win the World Cup?", 0.47, "ar")])
        assert len(sigs) == 1

    def test_type1_rejects_single_market_group(self):
        assert self._run([self._m("Will Brazil win the World Cup?", 0.40, "br")]) == []

    def test_type1_rejects_insufficient_liquidity(self):
        sigs = self._run([self._m("Will Brazil win the World Cup?", 0.40, "br"),
                          self._m("Will Argentina win the World Cup?", 0.35, "ar"),
                          self._m("Will Germany win the World Cup?", 0.18, "de")], liq=50.0)
        assert len(sigs) == 0

    def test_type1_discards_when_resolution_check_fails(self):
        markets = [self._m("Will Brazil win the World Cup?", 0.40, "br"),
                   self._m("Will Argentina win the World Cup?", 0.35, "ar"),
                   self._m("Will Germany win the World Cup?", 0.18, "de")]

        def alt(cid):
            return _RES_OK if cid == "br" else _RES_DIFF

        with patch("modules.scanner_a.clob_utils.fetch_sampling_markets", return_value=markets), \
             patch("modules.scanner_a.clob_utils.get_orderbook_liquidity", return_value=200.0), \
             patch("modules.scanner_a._fetch_resolution_rules", side_effect=alt):
            assert scanner_a.scan() == []

    def test_type1_discards_when_resolution_unavailable(self):
        sigs = self._run([self._m("Will Brazil win the World Cup?", 0.40, "br"),
                          self._m("Will Argentina win the World Cup?", 0.35, "ar")], res_text="")
        assert len(sigs) == 0

    def test_type1_fields_populated(self):
        sigs = self._run([self._m("Will Brazil win the World Cup?", 0.40, "br", slug="brazil-wc"),
                          self._m("Will Argentina win the World Cup?", 0.35, "ar", slug="argentina-wc"),
                          self._m("Will Germany win the World Cup?", 0.18, "de", slug="germany-wc")])
        assert len(sigs) == 1
        s = sigs[0]
        assert s.resolution_ok
        assert len(s.market_ids) == 3
        assert s.min_liquidity == 200.0
        assert s.pnl_est > 0

    def test_type1_pnl_estimate(self):
        sigs = self._run([self._m("Will Brazil win the World Cup?", 0.40, "br"),
                          self._m("Will Argentina win the World Cup?", 0.35, "ar"),
                          self._m("Will Germany win the World Cup?", 0.18, "de")])
        assert abs(sigs[0].pnl_est - round(0.07 / 0.93, 4) * 50) < 0.05

    def test_type1_rejects_fee_above_max(self):
        m = self._m("Will Brazil win the World Cup?", 0.40, "br")
        m["taker_base_fee"] = 0.02
        sigs = self._run([m, self._m("Will Argentina win the World Cup?", 0.35, "ar")])
        assert len(sigs) == 0

    def test_type1_accepts_low_fee_market(self):
        markets = [self._m("Will Brazil win the World Cup?", 0.40, "br"),
                   self._m("Will Argentina win the World Cup?", 0.35, "ar"),
                   self._m("Will Germany win the World Cup?", 0.18, "de")]
        for m in markets:
            m["taker_base_fee"] = 0.005
        sigs = self._run(markets)
        assert len(sigs) == 1
        assert sigs[0].spread < round(0.07 / 0.93, 4)

    def test_type1_fee_eliminates_thin_spread(self):
        markets = [self._m("Will Brazil win the World Cup?", 0.50, "br"),
                   self._m("Will Argentina win the World Cup?", 0.47, "ar")]
        for m in markets:
            m["taker_base_fee"] = 0.01
        assert self._run(markets) == []

    def test_type1_rejects_expired_market(self):
        sigs = self._run([self._m("Will Brazil win the World Cup?", 0.40, "br", end_date="2020-01-01T00:00:00Z"),
                          self._m("Will Argentina win the World Cup?", 0.35, "ar", end_date="2020-01-01T00:00:00Z")])
        assert len(sigs) == 0

    def test_type1_multiple_events_independent(self):
        sigs = self._run([self._m("Will Brazil win the World Cup?", 0.40, "bwc"),
                          self._m("Will Argentina win the World Cup?", 0.35, "awc"),
                          self._m("Will France win the Euros?", 0.42, "feu"),
                          self._m("Will Spain win the Euros?", 0.38, "seu")])
        assert len(sigs) == 2

    # Tipo 2 ──────────────────────────────────────────────────────────────────

    def test_type2_detects_subset_violation(self):
        sigs = self._run([self._m("Will Brazil or Argentina win the World Cup?", 0.38, "brar"),
                          self._m("Will Brazil win the World Cup?", 0.45, "br")])
        assert len(sigs) == 1
        assert sigs[0].signal_type == 2
        assert abs(sigs[0].spread - 0.07) < 1e-4

    def test_type2_rejects_spread_too_small(self):
        sigs = self._run([self._m("Will Brazil or Argentina win the World Cup?", 0.39, "brar"),
                          self._m("Will Brazil win the World Cup?", 0.40, "br")])
        assert len(sigs) == 0

    def test_type2_no_signal_when_subset_correctly_priced(self):
        sigs = self._run([self._m("Will Brazil or Argentina win the World Cup?", 0.65, "brar"),
                          self._m("Will Brazil win the World Cup?", 0.40, "br")])
        assert len(sigs) == 0

    def test_type2_rejects_insufficient_liquidity(self):
        sigs = self._run([self._m("Will Brazil or Argentina win the World Cup?", 0.38, "brar"),
                          self._m("Will Brazil win the World Cup?", 0.45, "br")], liq=80.0)
        assert len(sigs) == 0

    def test_type2_discards_when_resolution_check_fails(self):
        markets = [self._m("Will Brazil or Argentina win the World Cup?", 0.38, "brar"),
                   self._m("Will Brazil win the World Cup?", 0.45, "br")]
        with patch("modules.scanner_a.clob_utils.fetch_sampling_markets", return_value=markets), \
             patch("modules.scanner_a.clob_utils.get_orderbook_liquidity", return_value=200.0), \
             patch("modules.scanner_a._fetch_resolution_rules",
                   side_effect=lambda cid: _RES_OK if cid == "brar" else _RES_DIFF):
            assert scanner_a.scan() == []

    def test_type2_both_teams_in_union_checked(self):
        sigs = self._run([self._m("Will Brazil or Argentina win the World Cup?", 0.65, "brar"),
                          self._m("Will Brazil win the World Cup?", 0.70, "br"),
                          self._m("Will Argentina win the World Cup?", 0.30, "ar")])
        assert len(sigs) == 1
        assert "brazil" in sigs[0].description.lower()

    # Tipo 3 ──────────────────────────────────────────────────────────────────

    def test_type3_detects_implication_violation(self):
        sigs = self._run([self._m("Will Brazil win the Championship?", 0.45, "br-champ"),
                          self._m("Will Brazil qualify for the Semifinals?", 0.35, "br-semi")])
        assert len(sigs) == 1
        assert sigs[0].signal_type == 3
        assert abs(sigs[0].spread - 0.10) < 1e-4

    def test_type3_rejects_spread_too_small(self):
        sigs = self._run([self._m("Will Brazil win the Championship?", 0.36, "br-champ"),
                          self._m("Will Brazil qualify for the Semifinals?", 0.35, "br-semi")])
        assert len(sigs) == 0

    def test_type3_no_signal_when_correctly_priced(self):
        sigs = self._run([self._m("Will Brazil win the Championship?", 0.30, "br-champ"),
                          self._m("Will Brazil qualify for the Semifinals?", 0.55, "br-semi")])
        assert len(sigs) == 0

    def test_type3_requires_same_entity(self):
        sigs = self._run([self._m("Will Brazil win the Championship?", 0.45, "br-champ"),
                          self._m("Will France qualify for the Semifinals?", 0.35, "fr-semi")])
        assert len(sigs) == 0

    def test_type3_discards_when_resolution_check_fails(self):
        markets = [self._m("Will Brazil win the Championship?", 0.45, "br-champ"),
                   self._m("Will Brazil qualify for the Semifinals?", 0.35, "br-semi")]
        with patch("modules.scanner_a.clob_utils.fetch_sampling_markets", return_value=markets), \
             patch("modules.scanner_a.clob_utils.get_orderbook_liquidity", return_value=200.0), \
             patch("modules.scanner_a._fetch_resolution_rules",
                   side_effect=lambda cid: _RES_OK if cid == "br-champ" else _RES_DIFF):
            assert scanner_a.scan() == []

    def test_type3_no_duplicate_pairs(self):
        sigs = self._run([self._m("Will Brazil win the Championship?", 0.45, "br-champ"),
                          self._m("Will Brazil qualify for the Semifinals?", 0.35, "br-semi")])
        assert len(sigs) == 1

    # Filtros globais ──────────────────────────────────────────────────────────

    def test_skips_market_without_tokens(self):
        m = {"condition_id": "x", "question": "Will X win the Cup?",
             "taker_base_fee": 0, "accepting_orders": True,
             "tags": [], "tokens": [], "end_date_iso": "2026-06-01T00:00:00Z"}
        assert self._run([m]) == []

    def test_resolution_check_called_before_signal_emitted(self):
        markets = [self._m("Will Brazil win the World Cup?", 0.40, "br"),
                   self._m("Will Argentina win the World Cup?", 0.35, "ar"),
                   self._m("Will Germany win the World Cup?", 0.18, "de")]
        with patch("modules.scanner_a.clob_utils.fetch_sampling_markets", return_value=markets), \
             patch("modules.scanner_a.clob_utils.get_orderbook_liquidity", return_value=200.0), \
             patch("modules.scanner_a._fetch_resolution_rules", return_value=""):
            assert scanner_a.scan() == []

    def test_execute_dry_run_returns_true(self):
        sigs = self._run([self._m("Will Brazil win the World Cup?", 0.40, "br", slug="brazil-wc"),
                          self._m("Will Argentina win the World Cup?", 0.35, "ar", slug="argentina-wc"),
                          self._m("Will Germany win the World Cup?", 0.18, "de", slug="germany-wc")])
        with patch("modules.scanner_a.tracker.record_signal"):
            assert scanner_a.execute(sigs[0], client=None) is True

    def test_execute_records_signal_with_primary_market(self):
        sigs = self._run([self._m("Will Brazil win the World Cup?", 0.40, "br", slug="brazil-wc"),
                          self._m("Will Argentina win the World Cup?", 0.35, "ar", slug="argentina-wc"),
                          self._m("Will Germany win the World Cup?", 0.18, "de", slug="germany-wc")])
        recorded = {}
        with patch("modules.scanner_a.tracker.record_signal",
                   side_effect=lambda *a, **kw: recorded.update({"args": a, "kwargs": kw})):
            scanner_a.execute(sigs[0], client=None)
        assert recorded["args"][0] == "A"
        assert recorded["args"][1] == sigs[0].market_ids[0]
        assert "arb-t1" in recorded["args"][3]

    # Tipo 1b: Ranges ──────────────────────────────────────────────────────────

    def test_ranges_detects_undersum(self):
        sigs = self._run([
            self._m("Will Nvidia market cap be less than $2T by end of 2026?",        0.25, "r1"),
            self._m("Will Nvidia market cap be between $2T and $3T by end of 2026?",  0.22, "r2"),
            self._m("Will Nvidia market cap be between $3T and $4T by end of 2026?",  0.18, "r3"),
            self._m("Will Nvidia market cap be greater than $4T by end of 2026?",     0.20, "r4"),
        ])
        assert len(sigs) == 1
        assert "RANGE" in sigs[0].description
        assert "UNDERSUM" in sigs[0].description
        assert abs(sigs[0].spread - round(0.15 / 0.85, 4)) < 1e-3

    def test_ranges_detects_oversum(self):
        sigs = self._run([
            self._m("Will Nvidia market cap be less than $2T by end of 2026?",        0.32, "r1"),
            self._m("Will Nvidia market cap be between $2T and $3T by end of 2026?",  0.30, "r2"),
            self._m("Will Nvidia market cap be between $3T and $4T by end of 2026?",  0.28, "r3"),
            self._m("Will Nvidia market cap be greater than $4T by end of 2026?",     0.30, "r4"),
        ])
        assert len(sigs) == 1
        assert "OVERSUM" in sigs[0].description

    def test_ranges_requires_at_least_3_markets(self):
        sigs = self._run([
            self._m("Will Nvidia market cap be between $2T and $3T by end of 2026?", 0.30, "r1"),
            self._m("Will Nvidia market cap be less than $2T by end of 2026?",       0.30, "r2"),
        ])
        assert len(sigs) == 0

    def test_ranges_rejects_sum_within_tolerance(self):
        sigs = self._run([
            self._m("Will Nvidia market cap be between $2T and $3T by end of 2026?", 0.34, "r1"),
            self._m("Will Nvidia market cap be between $3T and $4T by end of 2026?", 0.33, "r2"),
            self._m("Will Nvidia market cap be less than $2T by end of 2026?",       0.32, "r3"),
        ])
        assert len(sigs) == 0

    def test_ranges_rejects_insufficient_liquidity(self):
        sigs = self._run([
            self._m("Will Nvidia market cap be between $2T and $3T by end of 2026?", 0.30, "r1"),
            self._m("Will Nvidia market cap be between $3T and $4T by end of 2026?", 0.25, "r2"),
            self._m("Will Nvidia market cap be less than $2T by end of 2026?",       0.30, "r3"),
        ], liq=50.0)
        assert len(sigs) == 0

    def test_ranges_rejects_incomplete_set_no_upper(self):
        sigs = self._run([
            self._m("Will Nvidia market cap be less than $2T by end of 2026?",        0.25, "r1"),
            self._m("Will Nvidia market cap be between $2T and $3T by end of 2026?",  0.22, "r2"),
            self._m("Will Nvidia market cap be between $3T and $4T by end of 2026?",  0.18, "r3"),
        ])
        assert len(sigs) == 0

    def test_ranges_rejects_incomplete_set_no_lower(self):
        sigs = self._run([
            self._m("Will Nvidia market cap be between $2T and $3T by end of 2026?",  0.22, "r1"),
            self._m("Will Nvidia market cap be between $3T and $4T by end of 2026?",  0.18, "r2"),
            self._m("Will Nvidia market cap be greater than $4T by end of 2026?",     0.20, "r3"),
        ])
        assert len(sigs) == 0

    def test_ranges_accepts_above_keyword_as_upper(self):
        sigs = self._run([
            self._m("Will Nvidia market cap be less than $2T by end of 2026?",        0.25, "r1"),
            self._m("Will Nvidia market cap be between $2T and $3T by end of 2026?",  0.22, "r2"),
            self._m("Will Nvidia market cap be between $3T and $4T by end of 2026?",  0.18, "r3"),
            self._m("Will Nvidia market cap be above $4T by end of 2026?",            0.20, "r4"),
        ])
        assert len(sigs) == 1
        assert "RANGE" in sigs[0].description

    def test_ranges_discards_mismatched_stems(self):
        sigs = self._run([
            self._m("Will Nvidia market cap be between $2T and $3T by end of 2026?", 0.30, "r1"),
            self._m("Will Nvidia revenue be between $50B and $60B by end of 2026?",  0.25, "r2"),
            self._m("Will Tesla market cap be less than $1T by end of 2026?",        0.30, "r3"),
        ])
        assert len(sigs) == 0

    # Tipo 2b: Thresholds ──────────────────────────────────────────────────────

    def test_thresholds_detects_ordering_violation(self):
        sigs = self._run([self._m("Will Nvidia revenue be above 50B in 2026?", 0.60, "th1"),
                          self._m("Will Nvidia revenue be above 60B in 2026?", 0.65, "th2")])
        assert len(sigs) == 1
        assert sigs[0].signal_type == 2
        assert "THRESHOLD" in sigs[0].description
        assert abs(sigs[0].spread - 0.05) < 1e-4

    def test_thresholds_no_signal_correct_ordering(self):
        sigs = self._run([self._m("Will Nvidia revenue be above 50B in 2026?", 0.70, "th1"),
                          self._m("Will Nvidia revenue be above 60B in 2026?", 0.40, "th2")])
        assert len(sigs) == 0

    def test_thresholds_rejects_spread_too_small(self):
        sigs = self._run([self._m("Will Nvidia revenue be above 50B in 2026?", 0.60, "th1"),
                          self._m("Will Nvidia revenue be above 60B in 2026?", 0.61, "th2")])
        assert len(sigs) == 0

    def test_thresholds_parses_multiplier_suffix(self):
        sigs = self._run([self._m("Will Nvidia market cap be above 2B in 2026?", 0.80, "th1"),
                          self._m("Will Nvidia market cap be above 3B in 2026?", 0.90, "th2")])
        assert len(sigs) == 1
        assert "THRESHOLD" in sigs[0].description

    def test_thresholds_no_duplicate_pairs(self):
        sigs = self._run([self._m("Will Nvidia revenue be above 50B in 2026?", 0.60, "th1"),
                          self._m("Will Nvidia revenue be above 60B in 2026?", 0.70, "th2")])
        assert len(sigs) == 1

    def test_thresholds_rejects_insufficient_liquidity(self):
        sigs = self._run([self._m("Will Nvidia revenue be above 50B in 2026?", 0.60, "th1"),
                          self._m("Will Nvidia revenue be above 60B in 2026?", 0.70, "th2")], liq=80.0)
        assert len(sigs) == 0

    # Tipo 2c: Dates ───────────────────────────────────────────────────────────

    def test_dates_detects_ordering_violation(self):
        sigs = self._run([self._m("Will Nvidia reach $200 by January 2026?", 0.50, "d1"),
                          self._m("Will Nvidia reach $200 by June 2026?",    0.25, "d2")])
        assert len(sigs) == 1
        assert sigs[0].signal_type == 2
        assert "DATA" in sigs[0].description
        assert abs(sigs[0].spread - 0.25) < 1e-4

    def test_dates_no_signal_correct_ordering(self):
        sigs = self._run([self._m("Will Nvidia reach $200 by January 2026?", 0.20, "d1"),
                          self._m("Will Nvidia reach $200 by June 2026?",    0.60, "d2")])
        assert len(sigs) == 0

    def test_dates_rejects_spread_too_small(self):
        sigs = self._run([self._m("Will Nvidia reach $200 by January 2026?", 0.51, "d1"),
                          self._m("Will Nvidia reach $200 by June 2026?",    0.50, "d2")])
        assert len(sigs) == 0

    def test_dates_no_duplicate_pairs(self):
        sigs = self._run([self._m("Will Nvidia reach $200 by January 2026?", 0.50, "d1"),
                          self._m("Will Nvidia reach $200 by June 2026?",    0.25, "d2")])
        assert len(sigs) == 1

    def test_dates_rejects_insufficient_liquidity(self):
        sigs = self._run([self._m("Will Nvidia reach $200 by January 2026?", 0.50, "d1"),
                          self._m("Will Nvidia reach $200 by June 2026?",    0.25, "d2")], liq=80.0)
        assert len(sigs) == 0

    def test_dates_discards_mismatched_stems(self):
        sigs = self._run([self._m("Will Nvidia reach $200 by January 2026?", 0.50, "d1"),
                          self._m("Will Tesla reach $200 by June 2026?",     0.25, "d2")])
        assert len(sigs) == 0

    def test_dates_resolution_check_applied(self):
        sigs = self._run([self._m("Will Nvidia reach $200 by January 2026?", 0.50, "d1"),
                          self._m("Will Nvidia reach $200 by June 2026?",    0.25, "d2")], res_text="")
        assert len(sigs) == 0

    # Filtro de qualidade ──────────────────────────────────────────────────────

    def test_scan_discards_low_quality_signal(self):
        markets = [self._m("Will Brazil win the World Cup?", 0.40, "br"),
                   self._m("Will Argentina win the World Cup?", 0.35, "ar"),
                   self._m("Will Germany win the World Cup?", 0.18, "de")]
        with patch("modules.scanner_a.clob_utils.fetch_sampling_markets", return_value=markets), \
             patch("modules.scanner_a.clob_utils.get_orderbook_liquidity", return_value=200.0), \
             patch("modules.scanner_a._fetch_resolution_rules", return_value=_RES_OK), \
             patch("modules.scanner_a.rule_validator.assess", return_value=("LOW", "regras vagas")):
            assert scanner_a.scan() == []

    def test_scan_passes_medium_quality_signal(self):
        markets = [self._m("Will Brazil win the World Cup?", 0.40, "br"),
                   self._m("Will Argentina win the World Cup?", 0.35, "ar"),
                   self._m("Will Germany win the World Cup?", 0.18, "de")]
        with patch("modules.scanner_a.clob_utils.fetch_sampling_markets", return_value=markets), \
             patch("modules.scanner_a.clob_utils.get_orderbook_liquidity", return_value=200.0), \
             patch("modules.scanner_a._fetch_resolution_rules", return_value=_RES_OK), \
             patch("modules.scanner_a.rule_validator.assess", return_value=("MEDIUM", "fonte confiável")):
            sigs = scanner_a.scan()
        assert len(sigs) == 1
        assert sigs[0].rule_quality == "MEDIUM"

    def test_scan_rule_quality_stored_on_signal(self):
        sigs = self._run([self._m("Will Brazil win the World Cup?", 0.40, "br"),
                          self._m("Will Argentina win the World Cup?", 0.35, "ar"),
                          self._m("Will Germany win the World Cup?", 0.18, "de")])
        assert len(sigs) == 1
        assert sigs[0].rule_quality == "HIGH"


# ══════════════════════════════════════════════════════════════════════════════
# scanner_corr
# ══════════════════════════════════════════════════════════════════════════════

class TestScannerCorr:
    @staticmethod
    def _market(cid="0xABC", question="Will A win X?", price=0.40, fee=0.0, tags=None):
        return {
            "condition_id": cid, "question": question,
            "taker_base_fee": fee, "tags": tags or [],
            "tokens": [
                {"outcome": "Yes", "token_id": f"yes-{cid}", "price": str(price)},
                {"outcome": "No",  "token_id": f"no-{cid}",  "price": str(1 - price)},
            ],
        }

    @staticmethod
    def _sig(pattern="undersum", prices=None, spread=0.10, market_ids=None, questions=None):
        prices     = prices     or [0.45, 0.45]
        market_ids = market_ids or ["0xA1", "0xA2"]
        questions  = questions  or ["Will A win X?", "Will B win X?"]
        return CorrSignal(pattern=pattern, description="test signal",
                          questions=questions, prices=prices,
                          spread=spread, pnl_est=spread * 50, market_ids=market_ids)

    def _mk(self, *a, **kw): return self._market(*a, **kw)
    def _sg(self, *a, **kw): return self._sig(*a, **kw)

    # F1 — Fee ─────────────────────────────────────────────────────────────────

    def test_f1_passes_zero_fee(self):
        ok, _, fee = _filter_fee(self._sg(), [self._mk("0xA1"), self._mk("0xA2")])
        assert ok and fee == 0.0

    def test_f1_passes_low_fee(self):
        ok, _, fee = _filter_fee(self._sg(), [self._mk("0xA1", fee=0.01), self._mk("0xA2", fee=0.01)])
        assert ok and fee < 0.05

    def test_f1_fails_high_fee(self):
        # weighted_fee=0.02, limit=0.03*0.5=0.015 → fail
        ok, reason, _ = _filter_fee(self._sg(spread=0.03),
                                    [self._mk("0xA1", fee=0.02), self._mk("0xA2", fee=0.02)])
        assert not ok and "fee=" in reason

    def test_f1_geo_tags_force_zero_fee(self):
        ok, _, fee = _filter_fee(self._sg(), [
            self._mk("0xA1", fee=0.02, tags=["Geopolitics"]),
            self._mk("0xA2", fee=0.02, tags=["World"]),
        ])
        assert ok and fee == 0.0

    def test_f1_oversum_uses_no_side_prices(self):
        ok, _, fee = _filter_fee(self._sg(pattern="oversum", prices=[0.40, 0.70]),
                                 [self._mk("0xA1"), self._mk("0xA2")])
        assert ok and fee == 0.0

    # F2 — Liquidez ────────────────────────────────────────────────────────────

    def test_f2_passes_sufficient_liquidity(self):
        with patch("modules.scanner_corr.clob_utils.get_orderbook_liquidity", return_value=200.0):
            ok, _ = _filter_liquidity(self._sg(), [self._mk("0xA1"), self._mk("0xA2")])
        assert ok

    def test_f2_fails_insufficient_liquidity(self):
        with patch("modules.scanner_corr.clob_utils.get_orderbook_liquidity", return_value=50.0):
            ok, reason = _filter_liquidity(self._sg(), [self._mk("0xA1"), self._mk("0xA2")])
        assert not ok and "liq=$50" in reason

    def test_f2_oversum_checks_no_side(self):
        calls = []
        with patch("modules.scanner_corr.clob_utils.get_orderbook_liquidity",
                   side_effect=lambda tid: calls.append(tid) or 500.0):
            _filter_liquidity(self._sg(pattern="oversum"), [self._mk("0xA1"), self._mk("0xA2")])
        assert all("no-" in t for t in calls)

    def test_f2_subset_checks_yes_then_no(self):
        calls = []
        with patch("modules.scanner_corr.clob_utils.get_orderbook_liquidity",
                   side_effect=lambda tid: calls.append(tid) or 500.0):
            _filter_liquidity(self._sg(pattern="subset", market_ids=["0xAB", "0xA"]),
                              [self._mk("0xAB"), self._mk("0xA")])
        assert "yes-0xAB" in calls and "no-0xA" in calls

    # F3 — Exaustividade ───────────────────────────────────────────────────────

    def test_f3_undersum_passes_when_sum_above_50pct(self):
        ok, _ = _filter_exhaustiveness(self._sg(prices=[0.45, 0.45]))
        assert ok

    def test_f3_undersum_fails_when_sum_below_50pct(self):
        ok, reason = _filter_exhaustiveness(self._sg(prices=[0.12, 0.15], spread=0.73))
        assert not ok and "50%" in reason

    def test_f3_oversum_passes_with_few_candidates(self):
        ok, _ = _filter_exhaustiveness(
            self._sg(pattern="oversum", market_ids=[f"0x{i}" for i in range(5)],
                     questions=["Will A win X?"] * 5, prices=[0.25] * 5))
        assert ok

    def test_f3_oversum_fails_with_too_many_candidates(self):
        ok, reason = _filter_exhaustiveness(
            self._sg(pattern="oversum", market_ids=[f"0x{i}" for i in range(11)],
                     questions=["Will A win X?"] * 11, prices=[0.10] * 11))
        assert not ok and "11 candidatos" in reason

    def test_f3_oversum_fails_on_cumulative_keyword(self):
        ok, reason = _filter_exhaustiveness(
            self._sg(pattern="oversum", questions=["Will at least 500 seats change?", "other"]))
        assert not ok and "cumulativo" in reason

    def test_f3_subset_always_passes(self):
        ok, _ = _filter_exhaustiveness(self._sg(pattern="subset", prices=[0.30, 0.45]))
        assert ok

    # F4 — Resolução ───────────────────────────────────────────────────────────

    def test_f4_passes_same_source(self):
        with patch("modules.scanner_corr._fetch_rules", return_value="resolves via reuters results"):
            ok, reason = _filter_resolution(self._sg())
        assert ok and "reuters" in reason

    def test_f4_fails_divergent_sources(self):
        with patch("modules.scanner_corr._fetch_rules",
                   side_effect=["resolves via reuters results", "resolves via associated press data"]):
            ok, reason = _filter_resolution(self._sg())
        assert not ok and "divergentes" in reason

    def test_f4_fails_when_rules_unavailable(self):
        with patch("modules.scanner_corr._fetch_rules", return_value=""):
            ok, reason = _filter_resolution(self._sg(market_ids=["0xA1"]))
        assert not ok and "não disponíveis" in reason

    def test_f4_fails_when_source_unidentified(self):
        with patch("modules.scanner_corr._fetch_rules",
                   return_value="The question closes when the event ends."):
            ok, reason = _filter_resolution(self._sg(market_ids=["0xA1"]))
        assert not ok and "não identificada" in reason

    # _validate pipeline ───────────────────────────────────────────────────────

    def _market_map(self, ids, fee=0.0):
        return {mid: self._mk(mid, fee=fee) for mid in ids}

    def test_validate_all_pass(self):
        sig = self._sg(market_ids=["0xV1", "0xV2"])
        with patch("modules.scanner_corr.clob_utils.get_orderbook_liquidity", return_value=200.0), \
             patch("modules.scanner_corr._fetch_rules", return_value="resolves via official results"):
            result = _corr_validate([sig], self._market_map(["0xV1", "0xV2"]))
        assert len(result) == 1

    def test_validate_discards_on_f1(self):
        sig = self._sg(spread=0.03, market_ids=["0xX1", "0xX2"])
        with patch("modules.scanner_corr.clob_utils.get_orderbook_liquidity", return_value=200.0):
            result = _corr_validate([sig], self._market_map(["0xX1", "0xX2"], fee=0.02))
        assert result == []

    def test_validate_discards_on_f2(self):
        sig = self._sg(market_ids=["0xY1", "0xY2"])
        with patch("modules.scanner_corr.clob_utils.get_orderbook_liquidity", return_value=50.0), \
             patch("modules.scanner_corr._fetch_rules", return_value="resolves via official results"):
            result = _corr_validate([sig], self._market_map(["0xY1", "0xY2"]))
        assert result == []

    def test_validate_discards_on_f3_low_sum(self):
        sig = self._sg(prices=[0.10, 0.15], spread=0.75, market_ids=["0xZ1", "0xZ2"])
        with patch("modules.scanner_corr.clob_utils.get_orderbook_liquidity", return_value=200.0), \
             patch("modules.scanner_corr._fetch_rules", return_value="resolves via official results"):
            result = _corr_validate([sig], self._market_map(["0xZ1", "0xZ2"]))
        assert result == []

    def test_validate_net_spread_updated(self):
        sig = self._sg(market_ids=["0xN1", "0xN2"])
        market_map = {"0xN1": self._mk("0xN1", fee=0.01), "0xN2": self._mk("0xN2", fee=0.01)}
        with patch("modules.scanner_corr.clob_utils.get_orderbook_liquidity", return_value=200.0), \
             patch("modules.scanner_corr._fetch_rules", return_value="resolves via official results"):
            result = _corr_validate([sig], market_map)
        assert len(result) == 1
        assert result[0].spread < 0.10
        assert result[0].pnl_est == pytest.approx(result[0].spread * 50.0, abs=0.01)


# ══════════════════════════════════════════════════════════════════════════════
# oracle_b1
# ══════════════════════════════════════════════════════════════════════════════

class TestOracleB1:
    @staticmethod
    def _market(question, hours_to_end, yes_price, tags=None):
        slug = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")
        end  = (datetime.now(timezone.utc) + timedelta(hours=hours_to_end)).isoformat()
        return {"condition_id": "cid-b1-test", "question": question, "market_slug": slug,
                "end_date_iso": end, "tags": tags or ["Weather", "Daily Temperature"],
                "tokens": [{"outcome": "Yes", "token_id": "yes_tok", "price": yes_price},
                            {"outcome": "No",  "token_id": "no_tok",  "price": 1 - yes_price}]}

    @staticmethod
    def _forecast(temp_max):
        return {"daily": {"temperature_2m_max": [temp_max]}}

    @staticmethod
    def _ending_in(hours):
        end = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
        return {"end_date_iso": end}

    def test_city_detected_new_york(self):
        assert _city_from_question("Will New York max temp exceed 30C?") == "New York"

    def test_city_detected_london(self):
        assert _city_from_question("London high temperature above 25 degrees") == "London"

    def test_city_detected_case_insensitive(self):
        assert _city_from_question("Will SEOUL temperature exceed 35?") == "Seoul"

    def test_city_not_detected(self):
        assert _city_from_question("Will it rain in Buenos Aires?") is None

    def test_city_detected_hong_kong(self):
        assert _city_from_question("Hong Kong high above 33C on May 8?") == "Hong Kong"

    def test_exceed_clearly_above(self):
        assert _model_probability(self._forecast(35), "Will temp exceed 30?") == 0.85

    def test_exceed_slightly_above(self):
        assert _model_probability(self._forecast(31), "Will temp exceed 30?") == 0.60

    def test_exceed_slightly_below(self):
        assert _model_probability(self._forecast(29), "Will temp exceed 30?") == 0.40

    def test_exceed_clearly_below(self):
        assert _model_probability(self._forecast(25), "Will temp exceed 30?") == 0.15

    def test_below_clearly_below(self):
        assert _model_probability(self._forecast(5),  "Will temp be below 10?") == 0.85

    def test_below_slightly_below(self):
        assert _model_probability(self._forecast(9),  "Will temp be below 10?") == 0.60

    def test_below_slightly_above(self):
        assert _model_probability(self._forecast(11), "Will temp be below 10?") == 0.40

    def test_below_clearly_above(self):
        assert _model_probability(self._forecast(20), "Will temp be below 10?") == 0.15

    def test_higher_than_keyword(self):
        assert _model_probability(self._forecast(35), "Will temp be higher than 30?") == 0.85

    def test_lower_than_keyword(self):
        assert _model_probability(self._forecast(5),  "Will temp be lower than 10?") == 0.85

    def test_no_threshold_in_question(self):
        assert _model_probability(self._forecast(30), "Will it be hot today?") is None

    def test_missing_forecast_data(self):
        assert _model_probability({"daily": {"temperature_2m_max": [None]}}, "exceed 30") is None

    def test_resolution_within_window(self):
        assert _b1_res_hours(self._ending_in(12), 24) is True

    def test_resolution_outside_window(self):
        assert _b1_res_hours(self._ending_in(30), 24) is False

    def test_resolution_already_expired(self):
        assert _b1_res_hours(self._ending_in(-1), 24) is False

    def test_resolution_missing_date(self):
        assert _b1_res_hours({"end_date_iso": None}, 24) is False

    def test_scan_generates_signal_on_divergence(self):
        market   = self._market("Will London temp exceed 20C tomorrow?", 10, 0.10)
        forecast = {"daily": {"temperature_2m_max": [25.0]}}
        with patch("modules.oracle_b1.clob_utils.fetch_sampling_markets", return_value=[market]), \
             patch("modules.oracle_b1._fetch_forecast", return_value=forecast), \
             patch("modules.oracle_b1.clob_utils.get_orderbook_liquidity", return_value=200.0):
            sigs = oracle_b1.scan()
        assert len(sigs) == 1
        assert sigs[0].city == "London"
        assert sigs[0].model_prob == 0.85
        assert abs(sigs[0].divergence - 0.75) < 1e-9

    def test_scan_no_signal_low_divergence(self):
        market   = self._market("Will London temp exceed 20C tomorrow?", 10, 0.55)
        forecast = {"daily": {"temperature_2m_max": [21.0]}}
        with patch("modules.oracle_b1.clob_utils.fetch_sampling_markets", return_value=[market]), \
             patch("modules.oracle_b1._fetch_forecast", return_value=forecast), \
             patch("modules.oracle_b1.clob_utils.get_orderbook_liquidity", return_value=200.0):
            assert oracle_b1.scan() == []

    def test_scan_no_signal_price_above_max(self):
        market   = self._market("Will London temp exceed 20C tomorrow?", 10, 0.50)
        forecast = {"daily": {"temperature_2m_max": [26.0]}}
        with patch("modules.oracle_b1.clob_utils.fetch_sampling_markets", return_value=[market]), \
             patch("modules.oracle_b1._fetch_forecast", return_value=forecast), \
             patch("modules.oracle_b1.clob_utils.get_orderbook_liquidity", return_value=200.0):
            assert oracle_b1.scan() == []

    def test_scan_no_signal_low_liquidity(self):
        market   = self._market("Will London temp exceed 20C tomorrow?", 10, 0.10)
        forecast = {"daily": {"temperature_2m_max": [25.0]}}
        with patch("modules.oracle_b1.clob_utils.fetch_sampling_markets", return_value=[market]), \
             patch("modules.oracle_b1._fetch_forecast", return_value=forecast), \
             patch("modules.oracle_b1.clob_utils.get_orderbook_liquidity", return_value=50.0):
            assert oracle_b1.scan() == []

    def test_execute_dry_run(self):
        market   = self._market("Will London temp exceed 20C tomorrow?", 10, 0.10)
        forecast = {"daily": {"temperature_2m_max": [25.0]}}
        with patch("modules.oracle_b1.clob_utils.fetch_sampling_markets", return_value=[market]), \
             patch("modules.oracle_b1._fetch_forecast", return_value=forecast), \
             patch("modules.oracle_b1.clob_utils.get_orderbook_liquidity", return_value=200.0):
            sigs = oracle_b1.scan()
        with patch("modules.oracle_b1.tracker.record_signal"):
            assert oracle_b1.execute(sigs[0], client=None) is True


# ══════════════════════════════════════════════════════════════════════════════
# oracle_b2
# ══════════════════════════════════════════════════════════════════════════════

class TestOracleB2:
    @staticmethod
    def _market(question, hours_to_end, yes_price, game="lol"):
        tags = {"lol": ["league of legends"], "dota2": ["Dota 2"]}[game]
        slug = re.sub(r"[^a-z0-9]+", "-", question.lower()).strip("-")
        end  = (datetime.now(timezone.utc) + timedelta(hours=hours_to_end)).isoformat()
        return {"condition_id": "cid-b2-test", "question": question, "market_slug": slug,
                "end_date_iso": end, "tags": tags,
                "tokens": [{"outcome": "Yes", "token_id": "yes_tok", "price": yes_price},
                            {"outcome": "No",  "token_id": "no_tok",  "price": 1 - yes_price}]}

    @staticmethod
    def _ending_in(hours):
        end = (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()
        return {"end_date_iso": end}

    def test_extract_vs_format(self):
        assert _extract_teams("T1 vs Gen.G Esports?") == ("T1", "Gen.G Esports")

    def test_extract_will_beat_format(self):
        assert _extract_teams("Will T1 beat Gen.G Esports?") == ("T1", "Gen.G Esports")

    def test_extract_will_defeat_format(self):
        assert _extract_teams("Will Team Liquid defeat Cloud9?") == ("Team Liquid", "Cloud9")

    def test_extract_win_against_format(self):
        assert _extract_teams("Will NaVi win against Astralis?") == ("NaVi", "Astralis")

    def test_extract_strips_trailing_punctuation(self):
        result = _extract_teams("OG vs Team Secret.")
        assert result is not None and result[1] == "Team Secret"

    def test_extract_no_match(self):
        assert _extract_teams("Will it rain tomorrow?") is None

    def test_extract_empty_string(self):
        assert _extract_teams("") is None

    def test_detect_lol(self):
        assert _detect_game({"tags": ["league of legends", "LCK"]}) == "lol"

    def test_detect_lol_short_tag(self):
        assert _detect_game({"tags": ["lol", "Esports"]}) == "lol"

    def test_detect_dota2(self):
        assert _detect_game({"tags": ["Dota 2", "Esports"]}) == "dota2"

    def test_detect_no_match(self):
        assert _detect_game({"tags": ["CS2", "Esports"]}) is None

    def test_detect_empty_tags(self):
        assert _detect_game({"tags": []}) is None

    def test_detect_none_tags(self):
        assert _detect_game({"tags": None}) is None

    def test_resolution_in_window(self):
        assert _b2_res_hours(self._ending_in(3), min_h=2, max_h=4) is True

    def test_resolution_too_soon(self):
        assert _b2_res_hours(self._ending_in(1), min_h=2, max_h=4) is False

    def test_resolution_too_far(self):
        assert _b2_res_hours(self._ending_in(6), min_h=2, max_h=4) is False

    def test_resolution_expired(self):
        assert _b2_res_hours(self._ending_in(-1), min_h=2, max_h=4) is False

    def test_model_prob_both_teams(self):
        wr_a, wr_b = 0.6, 0.4
        assert abs(wr_a / (wr_a + wr_b) - 0.60) < 1e-9

    def test_model_prob_only_team_a(self):
        assert 0.70 == 0.70

    def test_model_prob_only_team_b(self):
        assert abs(1.0 - 0.30 - 0.70) < 1e-9

    def test_model_prob_equal_winrates(self):
        wr_a, wr_b = 0.5, 0.5
        assert abs(wr_a / (wr_a + wr_b) - 0.50) < 1e-9

    def test_scan_generates_lol_signal(self):
        market = self._market("T1 vs G2 Esports?", 3, 0.30)
        with patch("modules.oracle_b2.clob_utils.fetch_sampling_markets", return_value=[market]), \
             patch("modules.oracle_b2._lol_winrate", side_effect=lambda t: 0.80 if "T1" in t else 0.40), \
             patch("modules.oracle_b2.clob_utils.get_orderbook_liquidity", return_value=150.0):
            sigs = oracle_b2.scan()
        assert len(sigs) == 1
        assert sigs[0].team_a == "T1" and sigs[0].game == "lol" and sigs[0].side == "Yes"

    def test_scan_no_signal_low_divergence(self):
        market = self._market("T1 vs G2 Esports?", 3, 0.45)
        with patch("modules.oracle_b2.clob_utils.fetch_sampling_markets", return_value=[market]), \
             patch("modules.oracle_b2._lol_winrate", return_value=0.50), \
             patch("modules.oracle_b2.clob_utils.get_orderbook_liquidity", return_value=150.0):
            assert oracle_b2.scan() == []

    def test_scan_no_signal_outside_window(self):
        market = self._market("T1 vs G2 Esports?", 6, 0.20)
        with patch("modules.oracle_b2.clob_utils.fetch_sampling_markets", return_value=[market]), \
             patch("modules.oracle_b2._lol_winrate", return_value=0.80):
            assert oracle_b2.scan() == []

    def test_scan_no_signal_when_no_winrate(self):
        market = self._market("T1 vs G2 Esports?", 3, 0.20)
        with patch("modules.oracle_b2.clob_utils.fetch_sampling_markets", return_value=[market]), \
             patch("modules.oracle_b2._lol_winrate", return_value=None):
            assert oracle_b2.scan() == []

    def test_execute_dry_run(self):
        market = self._market("T1 vs G2 Esports?", 3, 0.30)
        with patch("modules.oracle_b2.clob_utils.fetch_sampling_markets", return_value=[market]), \
             patch("modules.oracle_b2._lol_winrate", side_effect=lambda t: 0.80 if "T1" in t else 0.40), \
             patch("modules.oracle_b2.clob_utils.get_orderbook_liquidity", return_value=150.0):
            sigs = oracle_b2.scan()
        with patch("modules.oracle_b2.tracker.record_signal"):
            assert oracle_b2.execute(sigs[0], client=None) is True


# ══════════════════════════════════════════════════════════════════════════════
# oracle_b3
# ══════════════════════════════════════════════════════════════════════════════

class TestOracleB3:
    @staticmethod
    def _market(question, price=0.30, condition_id=None, tags=None, fee=0.0, end_days=30):
        end_dt = datetime.now(timezone.utc) + timedelta(days=end_days)
        cid    = condition_id or f"0x{abs(hash(question)):016x}"
        return {
            "condition_id": cid, "question": question, "taker_base_fee": fee,
            "tags": tags or ["Politics", "Elections"], "end_date_iso": end_dt.isoformat(),
            "market_slug": "test-slug",
            "tokens": [{"outcome": "Yes", "token_id": f"yes-{cid}", "price": str(price)},
                       {"outcome": "No",  "token_id": f"no-{cid}",  "price": str(1 - price)}],
        }

    @staticmethod
    def _run_detect(fn, markets, rules="official results"):
        with patch("modules.oracle_b3.clob_utils.get_orderbook_liquidity", return_value=500.0), \
             patch("modules.oracle_b3._fetch_resolution_rules", return_value=f"resolves via {rules}"), \
             patch("modules.oracle_b3._extract_resolution_source", return_value=rules):
            return fn(markets)

    def _m(self, *a, **kw): return self._market(*a, **kw)

    def test_filter_keeps_politics_tag(self):
        m = self._m("Will Biden win the 2024 election?", tags=["Politics"])
        with patch("modules.oracle_b3.clob_utils.token_by_outcome",
                   side_effect=lambda m, o: {"token_id": "x", "price": "0.4"}):
            assert len(_filter_politics([m])) == 1

    def test_filter_removes_wrong_tag(self):
        m = self._m("Will Biden win the 2024 election?", tags=["Sports"])
        with patch("modules.oracle_b3.clob_utils.token_by_outcome",
                   side_effect=lambda m, o: {"token_id": "x"}):
            assert _filter_politics([m]) == []

    def test_filter_removes_high_fee(self):
        m = self._m("Will Biden win the election?", fee=0.02)
        with patch("modules.oracle_b3.clob_utils.token_by_outcome",
                   side_effect=lambda m, o: {"token_id": "x"}):
            assert _filter_politics([m]) == []

    def test_filter_removes_expired(self):
        m = self._m("Will Biden win the election?", end_days=100)
        with patch("modules.oracle_b3.clob_utils.token_by_outcome",
                   side_effect=lambda m, o: {"token_id": "x"}):
            assert _filter_politics([m]) == []

    def test_vote_criterion_popular(self):
        assert _vote_criterion("resolves based on popular vote results") == "popular_vote"

    def test_vote_criterion_electoral(self):
        assert _vote_criterion("winner determined by electoral college") == "electoral_college"

    def test_vote_criterion_unknown(self):
        assert _vote_criterion("resolves via official AP results") == "unknown"

    def test_detect_sum_undersum(self):
        markets = [self._m("Will Alice win the 2026 senate race?", 0.30, "0xA1"),
                   self._m("Will Bob win the 2026 senate race?",   0.25, "0xA2"),
                   self._m("Will Carol win the 2026 senate race?", 0.20, "0xA3")]
        sigs = self._run_detect(_detect_sum, markets)
        assert len(sigs) == 1 and "UNDERSUM" in sigs[0].description and sigs[0].signal_type == 4

    def test_detect_sum_oversum(self):
        markets = [self._m("Will Alice win the 2026 senate race?", 0.55, "0xB1"),
                   self._m("Will Bob win the 2026 senate race?",   0.50, "0xB2")]
        sigs = self._run_detect(_detect_sum, markets)
        assert len(sigs) == 1 and "OVERSUM" in sigs[0].description

    def test_detect_sum_no_signal_when_spread_too_low(self):
        markets = [self._m("Will Alice win the 2026 senate race?", 0.490, "0xC1"),
                   self._m("Will Bob win the 2026 senate race?",   0.495, "0xC2")]
        assert self._run_detect(_detect_sum, markets) == []

    def test_detect_sum_rejects_divergent_vote_criterion(self):
        markets = [self._m("Will Alice win the 2026 presidential race?", 0.30, "0xD1"),
                   self._m("Will Bob win the 2026 presidential race?",   0.20, "0xD2"),
                   self._m("Will Carol win the 2026 presidential race?", 0.10, "0xD3")]
        with patch("modules.oracle_b3.clob_utils.get_orderbook_liquidity", return_value=500.0), \
             patch("modules.oracle_b3._fetch_resolution_rules",
                   side_effect=["resolves via popular vote", "resolves via electoral college",
                                 "resolves via popular vote"]):
            sigs = _detect_sum(markets)
        assert all(not s.resolution_ok for s in sigs)

    def test_detect_dates_violation(self):
        markets = [self._m("Will Trump be elected by January 2026?",  0.60, "0xE1"),
                   self._m("Will Trump be elected by December 2026?", 0.40, "0xE2")]
        sigs = self._run_detect(_b3_detect_dates, markets)
        assert len(sigs) == 1 and "DATA" in sigs[0].description

    def test_detect_dates_no_violation(self):
        markets = [self._m("Will Trump be elected by January 2026?",  0.30, "0xF1"),
                   self._m("Will Trump be elected by December 2026?", 0.50, "0xF2")]
        assert self._run_detect(_b3_detect_dates, markets) == []

    def test_detect_candidate_party_violation(self):
        markets = [self._m("Will John Smith win the 2026 senate election?",           0.55, "0xG1"),
                   self._m("Will the Republican Party win the 2026 senate election?", 0.40, "0xG2",
                            tags=["Politics"])]
        sigs = self._run_detect(_detect_candidate_party, markets)
        assert len(sigs) == 1 and "CAND>PARTIDO" in sigs[0].description

    def test_detect_candidate_party_no_signal_when_ok(self):
        markets = [self._m("Will John Smith win the 2026 senate election?",           0.35, "0xH1"),
                   self._m("Will the Republican Party win the 2026 senate election?", 0.50, "0xH2",
                            tags=["Politics"])]
        assert self._run_detect(_detect_candidate_party, markets) == []


# ══════════════════════════════════════════════════════════════════════════════
# oracle_b4
# ══════════════════════════════════════════════════════════════════════════════

class TestOracleB4:
    @staticmethod
    def _market(question, price=0.50, condition_id=None, tags=None, fee=0.0, hours_to_end=2.0):
        end_dt = datetime.now(timezone.utc) + timedelta(hours=hours_to_end)
        cid    = condition_id or f"0x{abs(hash(question)):016x}"
        return {
            "condition_id": cid, "question": question, "taker_base_fee": fee,
            "tags": tags or ["NBA", "Basketball"], "end_date_iso": end_dt.isoformat(),
            "market_slug": "test-slug",
            "tokens": [{"outcome": "Yes", "token_id": f"yes-{cid}", "price": str(price)},
                       {"outcome": "No",  "token_id": f"no-{cid}",  "price": str(1 - price)}],
        }

    @staticmethod
    def _detect(markets, liquidity=600.0):
        with patch("modules.oracle_b4.clob_utils.get_orderbook_liquidity", return_value=liquidity):
            return _detect_ml_spread(markets)

    def _m(self, *a, **kw): return self._market(*a, **kw)

    def test_normalize_strips_whitespace(self):
        assert _normalize_team("  Lakers  ") == "lakers"

    def test_normalize_collapses_spaces(self):
        assert _normalize_team("Los  Angeles  Lakers") == "los angeles lakers"

    def test_live_window_within_range(self):
        assert _is_live_window(self._m("test", hours_to_end=2.0)) is True

    def test_live_window_too_far(self):
        assert _is_live_window(self._m("test", hours_to_end=5.0)) is False

    def test_live_window_blocked_final(self):
        assert _is_live_window(self._m("test", hours_to_end=0.05)) is False

    def test_blocked_final_true(self):
        assert _is_blocked_final(self._m("test", hours_to_end=0.04)) is True

    def test_blocked_final_false(self):
        assert _is_blocked_final(self._m("test", hours_to_end=1.0)) is False

    def test_filter_keeps_nba_tag(self):
        m = self._m("Will the Lakers beat the Celtics?", tags=["NBA"])
        with patch("modules.oracle_b4.clob_utils.token_by_outcome",
                   side_effect=lambda m, o: {"token_id": "x", "price": "0.5"}):
            assert len(_filter_sports([m])) == 1

    def test_filter_removes_wrong_tag(self):
        m = self._m("Will the Lakers beat the Celtics?", tags=["Soccer"])
        with patch("modules.oracle_b4.clob_utils.token_by_outcome",
                   side_effect=lambda m, o: {"token_id": "x"}):
            assert _filter_sports([m]) == []

    def test_filter_removes_high_fee(self):
        m = self._m("Will the Lakers beat the Celtics?", fee=0.02)
        with patch("modules.oracle_b4.clob_utils.token_by_outcome",
                   side_effect=lambda m, o: {"token_id": "x"}):
            assert _filter_sports([m]) == []

    def test_detects_violation(self):
        ml     = self._m("Will the Lakers beat the Celtics?",             0.55, "0xML1")
        spread = self._m("Will the Lakers beat the Celtics by 5+ points?", 0.65, "0xSP1")
        sigs = self._detect([ml, spread])
        assert len(sigs) == 1
        assert sigs[0].spread_margin == pytest.approx(0.10, abs=1e-4)
        assert sigs[0].side == "Yes ML + No spread"

    def test_no_signal_when_spread_below_ml(self):
        ml     = self._m("Will the Lakers beat the Celtics?",             0.65, "0xML2")
        spread = self._m("Will the Lakers beat the Celtics by 5+ points?", 0.55, "0xSP2")
        assert self._detect([ml, spread]) == []

    def test_no_signal_when_violation_too_small(self):
        ml     = self._m("Will the Lakers beat the Celtics?",             0.60, "0xML3")
        spread = self._m("Will the Lakers beat the Celtics by 5+ points?", 0.61, "0xSP3")
        assert self._detect([ml, spread]) == []

    def test_no_signal_when_liquidity_too_low(self):
        ml     = self._m("Will the Lakers beat the Celtics?",             0.50, "0xML4")
        spread = self._m("Will the Lakers beat the Celtics by 5+ points?", 0.70, "0xSP4")
        assert self._detect([ml, spread], liquidity=100.0) == []

    def test_blocked_when_game_ending(self):
        ml     = self._m("Will the Lakers beat the Celtics?",             0.50, "0xML5", hours_to_end=0.04)
        spread = self._m("Will the Lakers beat the Celtics by 5+ points?", 0.70, "0xSP5", hours_to_end=0.04)
        assert self._detect([ml, spread]) == []

    def test_no_signal_when_game_not_live(self):
        ml     = self._m("Will the Lakers beat the Celtics?",             0.50, "0xML6", hours_to_end=8.0)
        spread = self._m("Will the Lakers beat the Celtics by 5+ points?", 0.70, "0xSP6", hours_to_end=8.0)
        assert self._detect([ml, spread]) == []

    def test_deduplication_same_pair(self):
        ml  = self._m("Will the Lakers beat the Celtics?",              0.50, "0xML7")
        sp1 = self._m("Will the Lakers beat the Celtics by 5+ points?", 0.70, "0xSP7a")
        sp2 = self._m("Will the Lakers beat the Celtics by 8+ points?", 0.75, "0xSP7b")
        assert len(self._detect([ml, sp1, sp2])) == 2

    def test_scan_standby_when_no_live_games(self):
        far = self._m("Will the Lakers beat the Celtics?", hours_to_end=10.0)
        with patch("modules.oracle_b4.clob_utils.fetch_sampling_markets", return_value=[far]), \
             patch("modules.oracle_b4.clob_utils.token_by_outcome",
                   side_effect=lambda m, o: {"token_id": "x", "price": "0.5"}):
            sigs = _b4_scan()
        assert sigs == [] and oracle_b4_mod.is_active() is False

    def test_scan_active_when_live_games_present(self):
        live_ml = self._m("Will the Lakers beat the Celtics?",             0.50, "0xLM1", hours_to_end=2.0)
        live_sp = self._m("Will the Lakers beat the Celtics by 5+ points?", 0.70, "0xLS1", hours_to_end=2.0)
        with patch("modules.oracle_b4.clob_utils.fetch_sampling_markets", return_value=[live_ml, live_sp]), \
             patch("modules.oracle_b4.clob_utils.token_by_outcome",
                   side_effect=lambda m, o: {"token_id": f"{o}-tok", "price": "0.5"}), \
             patch("modules.oracle_b4.clob_utils.get_orderbook_liquidity", return_value=600.0):
            _b4_scan()
        assert oracle_b4_mod.is_active() is True
