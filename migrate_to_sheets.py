"""
Migração única: sobe o conteúdo dos CSVs existentes para o Google Sheets.

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

# Garante que o config é carregado corretamente
sys.path.insert(0, str(Path(__file__).parent))

from config import config
from modules import sheets

_SIGNALS_PATH  = Path("data/trades/signals.csv")
_RESOLVED_PATH = Path("data/trades/resolved.csv")

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


def _read_csv(path: Path) -> list[dict]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def migrate():
    if not config.GOOGLE_SHEETS_ID:
        print("❌  GOOGLE_SHEETS_ID não configurado no .env")
        sys.exit(1)

    # ── Signals ───────────────────────────────────────────────────────────────
    signals = _read_csv(_SIGNALS_PATH)
    if signals:
        print(f"📤  Enviando {len(signals)} sinal(is) para a aba Signals...")
        for row in signals:
            sheets.append_signal(row)
        print(f"✅  {len(signals)} sinal(is) migrado(s)")
    else:
        print("ℹ️   signals.csv vazio ou inexistente — nada para migrar")

    # ── Resolved ──────────────────────────────────────────────────────────────
    resolved = _read_csv(_RESOLVED_PATH)
    if resolved:
        print(f"📤  Enviando {len(resolved)} resolução(ões) para a aba Resolved...")
        for row in resolved:
            sheets.append_resolved(row)
        print(f"✅  {len(resolved)} resolução(ões) migrada(s)")
    else:
        print("ℹ️   resolved.csv vazio ou inexistente — nada para migrar")

    print("\n🎉  Migração concluída!")


if __name__ == "__main__":
    migrate()