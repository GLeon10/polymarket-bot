"""Notificações via Telegram para sinais do bot."""

import logging
import requests
from config import config

logger = logging.getLogger("notifier")

_API = "https://api.telegram.org/bot{token}/sendMessage"


def send(text: str) -> bool:
    """Envia mensagem de texto para o chat configurado. Retorna True se enviou."""
    if not config.TELEGRAM_BOT_TOKEN or not config.TELEGRAM_CHAT_ID:
        return False
    try:
        resp = requests.post(
            _API.format(token=config.TELEGRAM_BOT_TOKEN),
            json={"chat_id": config.TELEGRAM_CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException as e:
        logger.warning("Telegram: falha ao enviar mensagem — %s", e)
        return False


def notify_signal(module: str, question: str, side: str,
                  entry_price: float, edge: float,
                  size_usd: float, closes_at: str, url: str,
                  n_markets: int = 1) -> None:
    """Formata e envia notificação de novo sinal."""
    edge_pct  = f"{edge * 100:.1f}%"
    pnl_est   = f"${size_usd * edge:.2f}"
    price_str = f"{entry_price:.3f}"
    n_str     = f" ({n_markets} mercados)" if n_markets > 1 else ""

    lines = [
        f"<b>[SINAL {module}]{n_str}</b>",
        f"{question[:100]}",
        f"",
        f"Lado: {side} | Entrada: {price_str}",
        f"Spread: {edge_pct} | P&L est: {pnl_est}",
        f"Capital: ${size_usd:.2f} | Fecha: {closes_at}",
    ]
    if url:
        lines.append(f"{url}")

    send("\n".join(lines))
