"""
Migração pontual: adiciona coluna pnl_est_usd ao signals.csv existente.
Executar uma única vez na VPS: python3 fix_signals_csv.py
"""
import csv, io, sys
from pathlib import Path

path = Path(__file__).parent / "data" / "trades" / "signals.csv"

new_header = [
    "timestamp", "module", "market_id", "question", "url",
    "side", "entry_price", "edge", "size_usd", "shares", "pnl_est_usd",
    "closes_at", "n_markets",
]

if not path.exists():
    print("signals.csv não encontrado.")
    sys.exit(1)

lines = path.read_text(encoding="utf-8").splitlines()
rows  = list(csv.reader(lines))
header = rows[0]

if "pnl_est_usd" in header:
    print("Já migrado — nada a fazer.")
    sys.exit(0)

out = io.StringIO()
w   = csv.writer(out, lineterminator="\n")
w.writerow(new_header)

fixed = skipped = 0
for row in rows[1:]:
    if len(row) == 12:
        row = row[:10] + [""] + row[10:]   # insere pnl_est_usd vazio nas linhas antigas
        fixed += 1
    elif len(row) == 13:
        skipped += 1                        # linhas novas já estão corretas
    # linhas com outro comprimento são ignoradas (linha em branco, etc.)

    if len(row) in (12, 13):
        w.writerow(row)

path.write_text(out.getvalue(), encoding="utf-8")
print(f"OK — {fixed} linha(s) corrigida(s), {skipped} já estavam corretas.")