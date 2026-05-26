"""
Integração com Google Sheets — espelho dos CSVs signals.csv e resolved.csv.

Configuração no .env:
  GOOGLE_SHEETS_ID             = ID da planilha (string após /d/ na URL)
  GOOGLE_SERVICE_ACCOUNT_JSON  = caminho para o JSON da service account
                                  (padrão: credentials.json na raiz do projeto)

Se não configurado, o módulo é silenciosamente ignorado.
Erros de conexão não interrompem o bot — são apenas logados como WARNING.

Abas criadas automaticamente:
  "Signals"  — espelho de data/trades/signals.csv
  "Resolved" — espelho de data/trades/resolved.csv
"""

import logging
import threading
from pathlib import Path

from config import config

logger = logging.getLogger("sheets")

_lock     = threading.Lock()
_gc       = None   # gspread.Client   (inicializado uma vez)
_spreadsheet = None  # gspread.Spreadsheet

_SIGNALS_HEADER = [
    "timestamp", "module", "market_id", "question", "url",
    "side", "entry_price", "edge", "size_usd", "shares", "closes_at",
    "n_markets",
]
_RESOLVED_HEADER = [
    "market_id", "question", "module", "side",
    "entry_price", "size_usd", "shares",
    "resolution", "pnl_usd", "resolved_at",
]


def _enabled() -> bool:
    return bool(config.GOOGLE_SHEETS_ID)


def _get_client():
    """Retorna (gc, spreadsheet) com lazy-init. Thread-safe."""
    global _gc, _spreadsheet
    if _gc is not None:
        return _gc, _spreadsheet

    try:
        import gspread
        from google.oauth2.service_account import Credentials
    except ImportError:
        logger.warning(
            "Sheets: gspread não instalado — execute: pip install gspread"
        )
        return None, None

    creds_path = Path(config.GOOGLE_SERVICE_ACCOUNT_JSON)
    if not creds_path.exists():
        logger.warning("Sheets: credenciais não encontradas em %s", creds_path)
        return None, None

    try:
        scopes = [
            "https://spreadsheets.google.com/feeds",
            "https://www.googleapis.com/auth/drive",
        ]
        creds       = Credentials.from_service_account_file(str(creds_path), scopes=scopes)
        _gc         = gspread.authorize(creds)
        _spreadsheet = _gc.open_by_key(config.GOOGLE_SHEETS_ID)
        logger.info("Sheets: conectado à planilha '%s'", _spreadsheet.title)
    except Exception as e:
        logger.warning("Sheets: falha ao conectar — %s", e)
        _gc          = None
        _spreadsheet = None

    return _gc, _spreadsheet


def _get_or_create_ws(tab_name: str, header: list[str]):
    """
    Retorna a aba pelo nome. Se não existir, cria com cabeçalho.
    Retorna None se a planilha não estiver acessível.
    """
    _, sp = _get_client()
    if sp is None:
        return None
    try:
        try:
            ws = sp.worksheet(tab_name)
        except Exception:
            ws = sp.add_worksheet(title=tab_name, rows=5000, cols=len(header))
            ws.append_row(header, value_input_option="RAW")
            logger.info("Sheets: aba '%s' criada com cabeçalho", tab_name)
            return ws

        # Garante cabeçalho caso a aba exista mas esteja vazia
        if not ws.row_values(1):
            ws.append_row(header, value_input_option="RAW")

        return ws
    except Exception as e:
        logger.warning("Sheets: erro ao acessar aba '%s' — %s", tab_name, e)
        return None


def append_signal(row: dict) -> None:
    """
    Grava uma linha na aba 'Signals'.
    Chamado pelo tracker logo após gravar no CSV.
    """
    if not _enabled():
        return
    with _lock:
        try:
            ws = _get_or_create_ws("Signals", _SIGNALS_HEADER)
            if ws is None:
                return
            values = [str(row.get(k, "")) for k in _SIGNALS_HEADER]
            ws.append_row(values, value_input_option="USER_ENTERED")
            logger.debug("Sheets: sinal gravado (%s)", row.get("market_id", ""))
        except Exception as e:
            logger.warning("Sheets: falha ao gravar sinal — %s", e)


def append_resolved(row: dict) -> None:
    """
    Grava uma linha na aba 'Resolved'.
    Chamado pelo tracker logo após gravar no CSV.
    """
    if not _enabled():
        return
    with _lock:
        try:
            ws = _get_or_create_ws("Resolved", _RESOLVED_HEADER)
            if ws is None:
                return
            values = [str(row.get(k, "")) for k in _RESOLVED_HEADER]
            ws.append_row(values, value_input_option="USER_ENTERED")
            logger.debug("Sheets: resolução gravada (%s)", row.get("market_id", ""))
        except Exception as e:
            logger.warning("Sheets: falha ao gravar resolução — %s", e)
