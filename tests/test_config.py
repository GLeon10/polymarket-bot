import pytest
from unittest.mock import patch
from config import config


def test_validate_passes_with_valid_config():
    with patch.multiple(
        "config.config",
        PRIVATE_KEY="0xabc",
        WALLET_ADDRESS="0x123",
        MODE="dry_run",
        ALLOC_A=0.90, ALLOC_RSRV=0.10,
        STRATEGY_A_ENABLED=True,
    ):
        config.validate()  # não deve lançar


def test_validate_raises_missing_private_key():
    with patch.multiple("config.config", PRIVATE_KEY="", WALLET_ADDRESS="0x123", MODE="dry_run",
                        ALLOC_A=0.90, ALLOC_RSRV=0.10, STRATEGY_A_ENABLED=True):
        with pytest.raises(EnvironmentError, match="PRIVATE_KEY"):
            config.validate()


def test_validate_raises_missing_wallet():
    with patch.multiple("config.config", PRIVATE_KEY="0xabc", WALLET_ADDRESS="", MODE="dry_run",
                        ALLOC_A=0.90, ALLOC_RSRV=0.10, STRATEGY_A_ENABLED=True):
        with pytest.raises(EnvironmentError, match="WALLET_ADDRESS"):
            config.validate()


def test_validate_raises_invalid_mode():
    with patch.multiple("config.config", PRIVATE_KEY="0xabc", WALLET_ADDRESS="0x123", MODE="production",
                        ALLOC_A=0.90, ALLOC_RSRV=0.10, STRATEGY_A_ENABLED=True):
        with pytest.raises(EnvironmentError, match="MODE"):
            config.validate()


def test_validate_raises_allocation_overflow():
    with patch.multiple("config.config", PRIVATE_KEY="0xabc", WALLET_ADDRESS="0x123", MODE="dry_run",
                        ALLOC_A=0.95, ALLOC_RSRV=0.10, STRATEGY_A_ENABLED=True):
        with pytest.raises(EnvironmentError, match="Aloca"):
            config.validate()


def test_validate_raises_strategy_a_disabled():
    with patch.multiple("config.config", PRIVATE_KEY="0xabc", WALLET_ADDRESS="0x123", MODE="dry_run",
                        ALLOC_A=0.90, ALLOC_RSRV=0.10, STRATEGY_A_ENABLED=False):
        with pytest.raises(EnvironmentError, match="obrigatória"):
            config.validate()


def test_capital_allocation_math():
    assert config.CAPITAL_A    == config.TOTAL_CAPITAL * config.ALLOC_A
    assert config.CAPITAL_RSRV == config.TOTAL_CAPITAL * config.ALLOC_RSRV
    assert config.MAX_TRADE_B_USD == config.TOTAL_CAPITAL * config.MAX_TRADE_B


def test_phase2_defaults():
    assert config.PHASE2_TRIGGER_USD == 45.0
    assert config.BANCA_B_ALLOC_B1 == 0.60
    assert config.BANCA_B_ALLOC_B2 == 0.40
    assert config.B_STOP_LOSS_BANCA == 0.30
    assert config.B1_MIN_RESOLVED_FOR_B2 == 5
