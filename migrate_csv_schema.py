"""
Migração dos CSVs para o esquema com contabilidade por pernas.

Substitui o antigo `fix_signals_csv.py`, que remendava o esquema anterior — o
mesmo em que o P&L de arbitragem era `size_usd * edge` e nunca podia ser
negativo.

O que faz:
  signals.csv   — acrescenta signal_id, cost_usd, fee_usd e legs_json
  resolved.csv  — acrescenta signal_id, cost_usd, fee_usd, payout_usd, roi_pct
                  e edge_realizado
  ledger.json   — reconstrói caixa e exposição a partir dos dois arquivos

Sobre os registros antigos: eles não guardam qual lado foi comprado em cada
mercado da cesta, então as pernas são reconstruídas da melhor forma possível a
partir de `side` e `entry_price` — uma perna só. Isso é suficiente para o
tracker não travar, mas o P&L histórico de arbitragem continua sendo o número
antigo, que é justamente o não-confiável. Por isso as linhas migradas ganham
`migrado=1`: ao avaliar resultado, filtre-as fora e conte só o que foi gravado
depois da migração.

Uso:
    python migrate_csv_schema.py            # aplica
    python migrate_csv_schema.py --dry-run  # só mostra o que faria
"""

import csv
import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from modules import fees, tracker

ROOT      = Path(__file__).parent
SIG_PATH  = ROOT / "data" / "trades" / "signals.csv"
RES_PATH  = ROOT / "data" / "trades" / "resolved.csv"
LEDGER    = ROOT / "data" / "trades" / "ledger.json"


def _read(path: Path) -> tuple[list[str], list[dict]]:
    if not path.exists() or path.stat().st_size == 0:
        return [], []
    with path.open(encoding="utf-8") as f:
        reader = csv.DictReader(f)
        rows   = [r for r in reader if any((v or "").strip() for v in r.values())]
        return list(reader.fieldnames or []), rows


def _backup(path: Path) -> None:
    if path.exists():
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        destino = path.with_suffix(f".{stamp}.bak")
        shutil.copy2(path, destino)
        print(f"  backup: {destino.name}")


def _f(row: dict, key: str, default: float = 0.0) -> float:
    try:
        return float(row.get(key) or default)
    except (TypeError, ValueError):
        return default


def _legs_for(row: dict) -> list[dict]:
    """Reconstrói uma perna a partir do registro antigo."""
    price  = _f(row, "entry_price")
    shares = _f(row, "shares")
    side   = (row.get("side") or "Yes").strip()
    outcome = "No" if side.lower().startswith("no") else "Yes"
    if price <= 0 or shares <= 0:
        return []
    return [{
        "market_id": row.get("market_id", ""),
        "outcome":   outcome,
        "price":     round(price, 6),
        "shares":    round(shares, 6),
        "fee_usd":   round(fees.fee_usd(price, shares, 0.0), 6),
        "token_id":  "",
    }]


def migrate_signals(dry: bool) -> int:
    header, rows = _read(SIG_PATH)
    if not rows:
        print("signals.csv: vazio ou inexistente — nada a fazer")
        return 0
    if header == tracker._SIGNALS_HEADER:
        print("signals.csv: já está no esquema novo")
        return 0

    novos = []
    for r in rows:
        legs = _legs_for(r)
        cost = sum(l["price"] * l["shares"] + l["fee_usd"] for l in legs) or _f(r, "size_usd")
        novos.append({
            **r,
            "signal_id": f"{r.get('module','?')}|{r.get('market_id','')}",
            "cost_usd":  round(cost, 4),
            "fee_usd":   round(sum(l["fee_usd"] for l in legs), 4),
            "legs_json": json.dumps(legs, separators=(",", ":")),
            "migrado":   1,
        })

    print(f"signals.csv: {len(novos)} linha(s) migradas")
    if dry:
        return len(novos)
    _backup(SIG_PATH)
    with SIG_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=tracker._SIGNALS_HEADER + ["migrado"],
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(novos)
    return len(novos)


def migrate_resolved(dry: bool) -> int:
    header, rows = _read(RES_PATH)
    if not rows:
        print("resolved.csv: vazio ou inexistente — nada a fazer")
        return 0
    if header == tracker._RESOLVED_HEADER:
        print("resolved.csv: já está no esquema novo")
        return 0

    novos = []
    for r in rows:
        cost   = _f(r, "size_usd")
        pnl    = _f(r, "pnl_usd")
        payout = cost + pnl
        novos.append({
            **r,
            "signal_id":      f"{r.get('module','?')}|{r.get('market_id','')}",
            "cost_usd":       round(cost, 4),
            "fee_usd":        0.0,
            "payout_usd":     round(payout, 4),
            "roi_pct":        round(pnl / cost * 100, 2) if cost > 0 else 0.0,
            "edge_realizado": round(pnl / cost, 4) if cost > 0 else 0.0,
            "result":         r.get("result") or ("WIN" if pnl > 0 else "LOSS"),
            "migrado":        1,
        })

    print(f"resolved.csv: {len(novos)} linha(s) migradas")
    if dry:
        return len(novos)
    _backup(RES_PATH)
    with RES_PATH.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=tracker._RESOLVED_HEADER + ["migrado"],
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(novos)
    return len(novos)


def rebuild_ledger(dry: bool) -> None:
    """Reconstrói o ledger a partir dos CSVs já migrados."""
    _, sig_rows = _read(SIG_PATH)
    _, res_rows = _read(RES_PATH)

    resolvidos = {r.get("signal_id") for r in res_rows}
    abertos = {}
    for r in sig_rows:
        sid = r.get("signal_id") or f"{r.get('module','?')}|{r.get('market_id','')}"
        if sid in resolvidos:
            continue
        abertos[sid] = {
            "module":    r.get("module", "?"),
            "cost_usd":  round(_f(r, "cost_usd") or _f(r, "size_usd"), 4),
            "opened_at": r.get("timestamp", ""),
        }

    por_dia, por_mes, por_modulo, total = {}, {}, {}, 0.0
    for r in res_rows:
        pnl = _f(r, "pnl_usd")
        dia = (r.get("resolved_at") or "")[:10]
        mes = (r.get("resolved_at") or "")[:7]
        mod = r.get("module", "?")
        if dia:
            por_dia[dia] = round(por_dia.get(dia, 0.0) + pnl, 4)
        if mes:
            por_mes[mes] = round(por_mes.get(mes, 0.0) + pnl, 4)
        por_modulo[mod] = round(por_modulo.get(mod, 0.0) + pnl, 4)
        total += pnl

    from config import config
    estado = {
        "open":               abertos,
        "realized_by_day":    por_dia,
        "realized_by_month":  por_mes,
        "realized_by_module": por_modulo,
        "realized_total":     round(total, 4),
        "peak_equity":        round(max(config.TOTAL_CAPITAL,
                                        config.TOTAL_CAPITAL + total), 4),
        "kill_switch":        {"active": False, "reason": "", "at": ""},
    }

    print(f"ledger.json: {len(abertos)} posição(ões) aberta(s), "
          f"P&L histórico ${total:.2f}")
    if dry:
        return
    _backup(LEDGER)
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(estado, indent=2, ensure_ascii=False), encoding="utf-8")


def main() -> None:
    dry = "--dry-run" in sys.argv
    if dry:
        print("MODO SIMULAÇÃO — nada será escrito\n")

    migrate_signals(dry)
    migrate_resolved(dry)
    rebuild_ledger(dry)

    print("\nPronto." if not dry else "\nSimulação concluída.")
    print("Lembrete: as linhas com migrado=1 vêm da contabilidade antiga, em que")
    print("arbitragem nunca registrava prejuízo. Avalie resultado só pelo que for")
    print("gravado a partir de agora.")


if __name__ == "__main__":
    main()
