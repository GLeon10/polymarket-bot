"""
Integração com Google Sheets — espelho dos CSVs signals.csv e resolved.csv.

Configuração no .env:
  GOOGLE_SHEETS_ID             = ID da planilha (string após /d/ na URL)
  GOOGLE_SERVICE_ACCOUNT_JSON  = caminho para o JSON da service account
                                  (padrão: credentials.json na raiz do projeto)

Se não configurado, o módulo é silenciosamente ignorado.
Erros de conexão não interrompem o bot — são apenas logados como WARNING.

Abas:
  "Signals"     — espelho de data/trades/signals.csv (esquema por pernas)
  "Resolved"    — espelho de data/trades/resolved.csv (P&L real por perna)
  "Signals_v1"  — arquivo morto: registros da contabilidade antiga
  "Resolved_v1" — idem

As abas _v1 guardam o que foi gravado enquanto o P&L de arbitragem era
`size_usd × edge`, sem olhar a resolução — por isso 38 de 38 operações
aparecem lá como WIN. Elas ficam intactas para consulta, e o bot não escreve
mais nelas. `rotate_sheets.py` faz esse arquivamento.
"""

import logging
import threading
from pathlib import Path

from config import config

logger = logging.getLogger("sheets")

_lock     = threading.Lock()
_gc       = None   # gspread.Client   (inicializado uma vez)
_spreadsheet = None  # gspread.Spreadsheet

# Espelham exatamente modules/tracker.py — mudou lá, muda aqui.
_SIGNALS_HEADER = [
    "signal_id", "timestamp", "module", "market_id", "question", "url",
    "side", "entry_price", "edge", "spread", "size_usd", "shares", "pnl_est_usd",
    "cost_usd", "fee_usd", "legs_json", "closes_at", "n_markets",
]
_RESOLVED_HEADER = [
    "signal_id", "market_id", "question", "module", "side",
    "entry_price", "size_usd", "shares",
    "resolution", "cost_usd", "fee_usd", "payout_usd", "pnl_usd", "roi_pct",
    "result", "edge", "spread", "edge_realizado", "resolved_at",
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

        existing = ws.row_values(1)
        if existing == header:
            return ws

        # Aba vazia (ou só com cabeçalho): pode reescrever sem perder nada.
        tem_dados = len([r for r in ws.get_all_values()[1:] if any(c.strip() for c in r)]) > 0
        if not tem_dados:
            ws.update("A1", [header])
            logger.info("Sheets: cabeçalho da aba '%s' atualizado", tab_name)
            return ws

        # Aba COM dados e cabeçalho diferente: recusar.
        #
        # Reescrever o cabeçalho aqui manteria as linhas antigas no lugar mas
        # passaria a anexar as novas na ordem de colunas nova — os dados
        # ficariam deslocados coluna a coluna, sem erro visível. É o mesmo
        # risco que o tracker já barra nos CSVs.
        logger.error(
            "Sheets: aba '%s' tem %d colunas e o esquema atual tem %d, com dados dentro. "
            "Nada será gravado. Rode: python rotate_sheets.py",
            tab_name, len(existing), len(header),
        )
        return None
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
