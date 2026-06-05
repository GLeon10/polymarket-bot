"""
Migração: limpa as abas Signals e Resolved e as repopula a partir dos CSVs.
Pode ser executado quantas vezes for necessário — sempre sobrescreve.

Uso:
    python migrate_to_sheets.py

Pré-requisitos:
    - GOOGLE_SHEETS_ID e GOOGLE_SERVICE_ACCOUNT_JSON configurados no .env
    - credentials.json presente na raiz do projeto
    - pip install gspread
"""

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import config
from modules import sheets

_SIGNALS_PATH  = Path("data/trades/signals.csv")
_RESOLVED_PATH = Path("data/trades/resolved.csv")


def _read_csv(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _repopulate(tab_name: str, header: list[str], rows: list[dict]) -> None:
    _, sp = sheets._get_client()
    if sp is None:
        print(f"  Sheets não conectado — pulando aba {tab_name}")
        return

    try:
        try:
            ws = sp.worksheet(tab_name)
        except Exception:
            ws = sp.add_worksheet(title=tab_name, rows=5000, cols=len(header))

        ws.clear()
        data = [header] + [[str(r.get(k, "")) for k in header] for r in rows]
        ws.update("A1", data, value_input_option="USER_ENTERED")
        print(f"  OK — {len(rows)} linha(s) gravada(s) na aba '{tab_name}'")
    except Exception as e:
        print(f"  ERRO na aba '{tab_name}': {e}")


def migrate():
    if not config.GOOGLE_SHEETS_ID:
        print("GOOGLE_SHEETS_ID não configurado no .env")
        sys.exit(1)

    print("Signals...")
    _repopulate("Signals", sheets._SIGNALS_HEADER, _read_csv(_SIGNALS_PATH))

    print("Resolved...")
    _repopulate("Resolved", sheets._RESOLVED_HEADER, _read_csv(_RESOLVED_PATH))

    print("\nMigração concluída.")


if __name__ == "__main__":
    migrate()
