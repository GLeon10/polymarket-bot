"""
Migração pontual dos CSVs:
  - Adiciona colunas 'spread' (signals) e 'edge'+'spread' (resolved)
  - Corrige pnl_usd errado em registros CORR/B3/B5 que usavam fórmula direcional
  - Corrige 'result' (WIN/LOSS) derivado do pnl_usd corrigido

Executar uma única vez: python3 fix_signals_csv.py
"""
import csv, io, sys
from pathlib import Path

ROOT = Path(__file__).parent

SIG_PATH = ROOT / "data" / "trades" / "signals.csv"
RES_PATH = ROOT / "data" / "trades" / "resolved.csv"

SIG_HEADER = [
    "timestamp", "module", "market_id", "question", "url",
    "side", "entry_price", "edge", "spread", "size_usd", "shares", "pnl_est_usd",
    "closes_at", "n_markets",
]
RES_HEADER = [
    "market_id", "question", "module", "side",
    "entry_price", "size_usd", "shares",
    "resolution", "pnl_usd", "result", "edge", "spread", "resolved_at",
]


def fmt_spread(edge: float) -> str:
    return f"{edge:.4f} ({edge * 100:g}% de spread)"


def is_arb(module: str, side: str) -> bool:
    return module in ("A", "CORR") or "+" in side or side.lower().startswith("arb")


# ── signals.csv ───────────────────────────────────────────────────────────────

if SIG_PATH.exists() and SIG_PATH.stat().st_size > 0:
    rows = list(csv.DictReader(SIG_PATH.read_text(encoding="utf-8").splitlines()))
    buf  = io.StringIO()
    w    = csv.DictWriter(buf, fieldnames=SIG_HEADER, lineterminator="\n",
                          extrasaction="ignore")
    w.writeheader()
    for r in rows:
        try:
            edge = float(r.get("edge", 0))
        except ValueError:
            edge = 0.0
        r["spread"] = fmt_spread(edge)

        # Recalcula pnl_est_usd com a fórmula correta
        try:
            size   = float(r["size_usd"])
            entry  = float(r["entry_price"])
            shares = float(r["shares"])
            if is_arb(r["module"], r.get("side", "")):
                r["pnl_est_usd"] = round(size * edge, 4)
            else:
                r["pnl_est_usd"] = round(shares * (1.0 - entry), 4)
        except (ValueError, KeyError):
            pass

        w.writerow(r)

    SIG_PATH.write_text(buf.getvalue(), encoding="utf-8")
    print(f"signals.csv  — {len(rows)} linha(s) migrada(s).")
else:
    print("signals.csv vazio ou inexistente, pulando.")


# ── resolved.csv ──────────────────────────────────────────────────────────────

if RES_PATH.exists() and RES_PATH.stat().st_size > 0:
    # Lê signals para recuperar edge de cada market_id
    edge_map: dict[str, float] = {}
    if SIG_PATH.exists():
        for r in csv.DictReader(SIG_PATH.read_text(encoding="utf-8").splitlines()):
            try:
                edge_map[r["market_id"]] = float(r["edge"])
            except (ValueError, KeyError):
                pass

    rows = list(csv.DictReader(RES_PATH.read_text(encoding="utf-8").splitlines()))
    buf  = io.StringIO()
    w    = csv.DictWriter(buf, fieldnames=RES_HEADER, lineterminator="\n",
                          extrasaction="ignore")
    w.writeheader()
    fixed = 0
    for r in rows:
        mid    = r.get("market_id", "")
        module = r.get("module", "")
        side   = r.get("side", "")

        # Recupera edge do signals.csv
        edge = edge_map.get(mid, 0.0)
        try:
            edge = edge or float(r.get("edge", 0))
        except ValueError:
            edge = 0.0
        r["edge"]   = round(edge, 4)
        r["spread"] = fmt_spread(edge)

        # Recalcula pnl_usd correto
        try:
            size_usd = float(r["size_usd"])
            entry    = float(r["entry_price"])
            shares   = float(r["shares"])
            if is_arb(module, side):
                correct_pnl = round(size_usd * edge, 4)
            else:
                resolution = r.get("resolution", "").lower()
                if resolution == side.lower():
                    correct_pnl = round(shares * (1.0 - entry), 4)
                else:
                    correct_pnl = round(-size_usd, 4)

            old_pnl = r.get("pnl_usd", "")
            if str(old_pnl) != str(correct_pnl):
                print(f"  Corrigindo pnl_usd: {old_pnl} → {correct_pnl}  ({r.get('question','')[:60]})")
                r["pnl_usd"] = correct_pnl
                fixed += 1
        except (ValueError, KeyError):
            pass

        r["result"] = "WIN" if float(r.get("pnl_usd", 0)) > 0 else "LOSS"
        w.writerow(r)

    RES_PATH.write_text(buf.getvalue(), encoding="utf-8")
    print(f"resolved.csv — {len(rows)} linha(s) migrada(s), {fixed} pnl_usd corrigido(s).")
else:
    print("resolved.csv vazio ou inexistente, pulando.")
