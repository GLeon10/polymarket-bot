"""
Gerenciador de fases do bot.

Fase 1: apenas Módulo A ativo (90% capital, 10% reserva)
Fase 2: B ativa quando A acumula PHASE2_TRIGGER_USD de lucro líquido
  - Banca B = P&L acumulado de A no momento da ativação
  - B1: 60% da banca B (ativa primeiro)
  - B2: 40% da banca B (ativa após B1 ter B1_MIN_RESOLVED_FOR_B2 resoluções)
  - Stop-loss: -30% da banca B em qualquer mês → pausar B (retoma no mês seguinte)
  - A nunca para, independente da fase

Estado persistido em data/trades/phase.json
"""

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

from config import config
from modules import tracker

logger = logging.getLogger("phase_manager")

_STATE_PATH = Path(__file__).parent.parent / "data" / "trades" / "phase.json"

_DEFAULT: dict = {
    "phase":          1,
    "banca_b":        0.0,
    "b1_active":      False,
    "b2_active":      False,
    "b_paused":       False,
    "b_paused_month": "",
}


def _load() -> dict:
    if not _STATE_PATH.exists():
        return dict(_DEFAULT)
    try:
        state = json.loads(_STATE_PATH.read_text(encoding="utf-8"))
        for k, v in _DEFAULT.items():
            state.setdefault(k, v)
        return state
    except Exception:
        return dict(_DEFAULT)


def _save(state: dict) -> None:
    _STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    _STATE_PATH.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")


def get_state() -> dict:
    return _load()


def is_b1_runnable() -> bool:
    s = _load()
    return s["phase"] >= 2 and s["b1_active"] and not s["b_paused"]


def is_b2_runnable() -> bool:
    s = _load()
    return s["phase"] >= 2 and s["b2_active"] and not s["b_paused"]


def check_and_update() -> None:
    """Verifica condições de fase e atualiza o estado. Chamado a cada ciclo do phase_loop."""
    state   = _load()
    changed = False
    a_pnl   = tracker.get_module_pnl("A")

    # ── Fase 1 → Fase 2 ──────────────────────────────────────────────────────
    if state["phase"] == 1:
        if a_pnl >= config.PHASE2_TRIGGER_USD:
            state["phase"]   = 2
            state["banca_b"] = round(a_pnl, 2)
            changed = True
            logger.info(
                "FASE 2 ATIVADA | A acumulou $%.2f | "
                "Banca B = $%.2f (B1: $%.2f | B2: $%.2f)",
                a_pnl, state["banca_b"],
                state["banca_b"] * config.BANCA_B_ALLOC_B1,
                state["banca_b"] * config.BANCA_B_ALLOC_B2,
            )
        else:
            faltam = config.PHASE2_TRIGGER_USD - a_pnl
            logger.debug("Fase 1 | A P&L $%.2f | Faltam $%.2f para Fase 2", a_pnl, faltam)
            if changed:
                _save(state)
            return

    banca_b       = state["banca_b"]
    current_month = datetime.now(timezone.utc).strftime("%Y-%m")

    # ── Reset de pausa no início de novo mês ─────────────────────────────────
    if state["b_paused"] and state["b_paused_month"] != current_month:
        state["b_paused"]       = False
        state["b_paused_month"] = ""
        changed = True
        logger.info("BANCA B | Novo mês (%s) — pausa encerrada, B reativado.", current_month)

    # ── Stop-loss mensal da banca B ───────────────────────────────────────────
    if not state["b_paused"] and (state["b1_active"] or state["b2_active"]):
        b_monthly = tracker.get_banca_b_monthly_pnl()
        stop_limit = -(config.B_STOP_LOSS_BANCA * banca_b)
        if b_monthly <= stop_limit:
            state["b_paused"]       = True
            state["b_paused_month"] = current_month
            changed = True
            logger.warning(
                "STOP-LOSS BANCA B | P&L mensal $%.2f ≤ limite $%.2f | "
                "B pausado em %s (retoma em %s)",
                b_monthly, stop_limit, current_month,
                _next_month(current_month),
            )

    if state["b_paused"]:
        if changed:
            _save(state)
        return

    # ── Ativar B1 ─────────────────────────────────────────────────────────────
    if not state["b1_active"]:
        state["b1_active"] = True
        changed = True
        logger.info(
            "B1 ATIVADO | Banca B1 = $%.2f (%.0f%% de $%.2f)",
            banca_b * config.BANCA_B_ALLOC_B1,
            config.BANCA_B_ALLOC_B1 * 100,
            banca_b,
        )

    # ── Ativar B2 após B1 validado ───────────────────────────────────────────
    if state["b1_active"] and not state["b2_active"]:
        b1_resolved = tracker.get_module_resolved_count("B1")
        if b1_resolved >= config.B1_MIN_RESOLVED_FOR_B2:
            state["b2_active"] = True
            changed = True
            logger.info(
                "B2 ATIVADO | B1 validado (%d resoluções) | Banca B2 = $%.2f",
                b1_resolved,
                banca_b * config.BANCA_B_ALLOC_B2,
            )
        else:
            logger.debug(
                "B2 aguardando B1: %d/%d resoluções",
                b1_resolved, config.B1_MIN_RESOLVED_FOR_B2,
            )

    if changed:
        _save(state)


def _next_month(ym: str) -> str:
    year, month = int(ym[:4]), int(ym[5:7])
    month += 1
    if month > 12:
        month, year = 1, year + 1
    return f"{year:04d}-{month:02d}"
