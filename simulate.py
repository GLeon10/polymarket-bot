"""
Simulação de um ciclo completo contra o Polymarket real.
Varre todos os módulos ativos, grava os sinais encontrados em signals.csv
e exibe o P&L hipotético acumulado.

Uso:
    .venv\Scripts\python.exe simulate.py
"""

import logging
import sys

from config import config
from modules import scanner_a, oracle_b1, oracle_b2, tracker, phase_manager

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger("simulate")


def main():
    config.validate()

    # Verifica e avança fase antes de qualquer varredura
    phase_manager.check_and_update()
    state = phase_manager.get_state()

    logger.info("=" * 60)
    logger.info("SIMULAÇÃO | Varrendo mercados reais do Polymarket…")
    logger.info("Fase atual: %d | Banca B: $%.2f", state["phase"], state["banca_b"])
    logger.info("=" * 60)

    total_sinais = 0

    if config.STRATEGY_A_ENABLED:
        logger.info("--- Módulo A (Arbitragem Combinatória) ---")
        sinais = scanner_a.scan()
        for s in sinais:
            scanner_a.execute(s, client=None)
        total_sinais += len(sinais)

    if config.STRATEGY_B1_ENABLED and phase_manager.is_b1_runnable():
        logger.info("--- Módulo B1 (Weather Oracle) ---")
        sinais = oracle_b1.scan()
        for s in sinais:
            oracle_b1.execute(s, client=None)
        total_sinais += len(sinais)
    elif config.STRATEGY_B1_ENABLED:
        a_pnl  = tracker.get_module_pnl("A")
        faltam = max(0.0, config.PHASE2_TRIGGER_USD - a_pnl)
        logger.info("--- Módulo B1 inativo (Fase 1) | A P&L: $%.2f | Faltam: $%.2f para Fase 2 ---", a_pnl, faltam)

    if config.STRATEGY_B2_ENABLED and phase_manager.is_b2_runnable():
        logger.info("--- Módulo B2 (Esports Oracle) ---")
        sinais = oracle_b2.scan()
        for s in sinais:
            oracle_b2.execute(s, client=None)
        total_sinais += len(sinais)
    elif config.STRATEGY_B2_ENABLED and phase_manager.is_b1_runnable():
        b1_res = tracker.get_module_resolved_count("B1")
        logger.info("--- Módulo B2 aguardando B1 | %d/%d resoluções de B1 ---",
                    b1_res, config.B1_MIN_RESOLVED_FOR_B2)

    logger.info("=" * 60)
    logger.info("Varredura concluída | %d novo(s) sinal(is) encontrado(s)", total_sinais)

    logger.info("Verificando resoluções de sinais anteriores…")
    tracker.check_resolutions()

    tracker.print_summary()


if __name__ == "__main__":
    main()
