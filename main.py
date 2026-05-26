"""
Orquestrador principal — roda os 3 módulos em paralelo com seus intervalos.
"""

import logging
import sys
import threading
import time
from pathlib import Path

from config import config
from modules import scanner_a, oracle_b1, oracle_b2, oracle_b3, oracle_b4, oracle_b5, oracle_b5_pro, scanner_corr, tracker, phase_manager

# ── Logging ──────────────────────────────────────────────────────────────────

def _setup_logging():
    log_dir = Path(__file__).parent / "data" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    from datetime import datetime
    log_file = log_dir / f"{datetime.utcnow().strftime('%Y-%m-%d')}.log"

    fmt = "%(asctime)s [%(levelname)s] %(name)s — %(message)s"
    logging.basicConfig(
        level=logging.INFO,
        format=fmt,
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )

    # Logs individuais por módulo (escrita paralela ao log unificado)
    module_fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    for module_name in ("scanner_a", "scanner_corr", "oracle_b1", "oracle_b2", "oracle_b3", "oracle_b4", "oracle_b5", "oracle_b5_pro"):
        fh = logging.FileHandler(log_dir / f"{module_name}.log", encoding="utf-8")
        fh.setFormatter(module_fmt)
        logging.getLogger(module_name).addHandler(fh)

logger = logging.getLogger("main")

# ── Loop de cada módulo ───────────────────────────────────────────────────────

def _run_loop(name: str, interval: int, scan_fn, execute_fn, client):
    """Roda scan_fn a cada `interval` segundos em thread própria."""
    logger.info("Módulo %s iniciado (intervalo=%ds mode=%s)", name, interval, config.MODE)
    while True:
        try:

            signals = scan_fn()
            for signal in signals:
                execute_fn(signal, client)
        except Exception as e:
            logger.exception("Erro inesperado no módulo %s: %s", name, e)
        time.sleep(interval)


def _run_b4_loop():
    """Loop B4 com intervalo dinâmico: standby=30min, ativo=60s."""
    logger.info("Módulo B4 iniciado (standby=%ds, ativo=%ds, mode=%s)",
                config.B4_STANDBY_INTERVAL, config.B4_ACTIVE_INTERVAL, config.MODE)
    while True:
        try:
            signals = oracle_b4.scan()
            for signal in signals:
                oracle_b4.execute(signal, None)
        except Exception as e:
            logger.exception("Erro inesperado no módulo B4: %s", e)
        interval = config.B4_ACTIVE_INTERVAL if oracle_b4.is_active() else config.B4_STANDBY_INTERVAL
        time.sleep(interval)


def _run_b5_loop(client):
    """Loop B5: scan a cada B5_SCAN_INTERVAL segundos."""
    logger.info("Módulo B5 iniciado (intervalo=%ds mode=%s)",
                config.B5_SCAN_INTERVAL, config.MODE)
    while True:
        try:
            signals = oracle_b5.scan()
            for signal in signals:
                oracle_b5.execute(signal, client)
        except Exception as e:
            logger.exception("Erro inesperado no módulo B5: %s", e)
        time.sleep(config.B5_SCAN_INTERVAL)


def _phase_loop():
    """Verifica condições de fase a cada 10 minutos."""
    logger.info("Phase manager iniciado (intervalo=10min)")
    while True:
        try:
            phase_manager.check_and_update()
        except Exception as e:
            logger.exception("Erro no phase_manager: %s", e)
        time.sleep(600)


def _tracker_loop():
    """Verifica resoluções e imprime resumo a cada 6 horas."""
    logger.info("Tracker iniciado (intervalo=6h)")
    while True:
        try:
            tracker.check_resolutions()
            tracker.print_summary()
        except Exception as e:
            logger.exception("Erro inesperado no tracker: %s", e)
        time.sleep(6 * 3600)


def _build_client():
    """
    Inicializa o cliente CLOB do Polymarket.
    Em dry_run retorna None (não precisa de autenticação).
    """
    if config.MODE == "dry_run":
        return None

    from py_clob_client.client import ClobClient
    from py_clob_client.constants import POLYGON

    client = ClobClient(
        host        = config.CLOB_BASE_URL,
        key         = config.PRIVATE_KEY,
        chain_id    = POLYGON,
        signature_type = 1,  # EIP712
    )

    # Gera/atualiza API key L2 se não estiver no .env
    if not config.POLY_API_KEY:
        logger.info("Gerando nova API key L2 para a wallet %s…", config.WALLET_ADDRESS)
        api_creds = client.create_or_derive_api_creds()
        logger.info(
            "API key gerada — adicione ao .env:\n  POLY_API_KEY=%s\n  POLY_API_SECRET=%s\n  POLY_API_PASSPHRASE=%s",
            api_creds.api_key, api_creds.api_secret, api_creds.api_passphrase,
        )
    else:
        from py_clob_client.clob_types import ApiCreds
        client.set_api_creds(ApiCreds(
            api_key        = config.POLY_API_KEY,
            api_secret     = config.POLY_API_SECRET,
            api_passphrase = config.POLY_API_PASSPHRASE,
        ))

    return client


# ── Entry point ───────────────────────────────────────────────────────────────

def main():
    _setup_logging()
    config.validate()

    logger.info("=" * 60)
    logger.info("Polymarket Bot iniciando | modo=%s | capital=$%.0f", config.MODE, config.TOTAL_CAPITAL)
    logger.info("  A=$%.0f  B1=$%.0f  B2=$%.0f  Reserva=$%.0f",
                config.CAPITAL_A, config.CAPITAL_B1, config.CAPITAL_B2, config.CAPITAL_RSRV)
    logger.info("=" * 60)

    client = _build_client()

    strategy_map = [
        ("A",    config.STRATEGY_A_ENABLED,  config.A_SCAN_INTERVAL,
         scanner_a.scan,    scanner_a.execute,    config.CAPITAL_A),
        ("B1",   config.STRATEGY_B1_ENABLED, config.B1_SCAN_INTERVAL,
         oracle_b1.scan,    oracle_b1.execute,    config.CAPITAL_B1),
        ("B2",   config.STRATEGY_B2_ENABLED, config.B2_SCAN_INTERVAL,
         oracle_b2.scan,    oracle_b2.execute,    config.CAPITAL_B2),
        ("B3",   config.STRATEGY_B3_ENABLED, config.B3_SCAN_INTERVAL,
         oracle_b3.scan,    oracle_b3.execute,    0.0),
        ("CORR", config.STRATEGY_A_ENABLED,  config.CORR_SCAN_INTERVAL,
         scanner_corr.scan, scanner_corr.execute, 0.0),
    ]

    active = []
    for name, enabled, interval, scan_fn, execute_fn, capital in strategy_map:
        if enabled:
            active.append(name)
            t = threading.Thread(
                target=_run_loop,
                args=(name, interval, scan_fn, execute_fn, client),
                daemon=True, name=f"thread-{name}",
            )
            t.start()
        else:
            logger.info("Estratégia %s DESATIVADA (STRATEGY_%s_ENABLED=false)", name, name)

    if config.STRATEGY_B4_ENABLED:
        active.append("B4")
        t_b4 = threading.Thread(target=_run_b4_loop, daemon=True, name="thread-B4")
        t_b4.start()
    else:
        logger.info("Estratégia B4 DESATIVADA (STRATEGY_B4_ENABLED=false)")

    if config.STRATEGY_B5_ENABLED:
        active.append("B5")
        t_b5 = threading.Thread(target=_run_b5_loop, args=(client,), daemon=True, name="thread-B5")
        t_b5.start()
    else:
        logger.info("Estratégia B5 DESATIVADA (STRATEGY_B5_ENABLED=false)")

    if config.STRATEGY_B5_PRO_ENABLED:
        active.append("B5Pro")
        t_b5p = threading.Thread(
            target=_run_loop,
            args=("B5Pro", config.B5_SCAN_INTERVAL,
                  oracle_b5_pro.scan, oracle_b5_pro.execute, client),
            daemon=True, name="thread-B5Pro",
        )
        t_b5p.start()
    else:
        logger.info("Estratégia B5Pro DESATIVADA (STRATEGY_B5_PRO_ENABLED=false)")

    t_phase = threading.Thread(target=_phase_loop, daemon=True, name="thread-phase")
    t_phase.start()

    t_tracker = threading.Thread(target=_tracker_loop, daemon=True, name="thread-tracker")
    t_tracker.start()

    state = phase_manager.get_state()
    logger.info("Fase atual: %d | A, B1 e B2 ativas para teste",
                state["phase"])
    logger.info("Módulos em execução: %s | Pressione Ctrl+C para encerrar.", ", ".join(active))
    try:
        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        logger.info("Encerramento solicitado. Até logo.")


if __name__ == "__main__":
    main()
