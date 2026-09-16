"""
Arquiva as abas atuais como *_v1 e cria abas novas para a contabilidade corrigida.

Por que arquivar em vez de continuar na mesma aba:

  Os registros antigos foram gravados quando o P&L de arbitragem era
  `size_usd × edge` — o edge teórico da detecção, sem olhar a resolução. Por
  isso as 38 operações resolvidas aparecem todas como WIN: a coluna que dizia
  o que aconteceu no mercado não entrava na conta. Misturar esses números com
  os novos tornaria impossível avaliar se o bot ganha dinheiro.

  As abas _v1 ficam intactas para consulta. As novas nascem vazias, com o
  esquema por pernas (legs_json, cost_usd, payout_usd, edge_realizado).

Uso:
    python rotate_sheets.py --dry-run   # mostra o que faria
    python rotate_sheets.py             # aplica
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from config import config
from modules import tracker

SUFIXO = "_v1"
ABAS = [
    ("Signals",  tracker._SIGNALS_HEADER),
    ("Resolved", tracker._RESOLVED_HEADER),
]

ROOT       = Path(__file__).parent
CSV_LOCAIS = [
    (ROOT / "data" / "trades" / "signals.csv",  ROOT / "data" / "trades" / "signals_v1.csv"),
    (ROOT / "data" / "trades" / "resolved.csv", ROOT / "data" / "trades" / "resolved_v1.csv"),
]


def _abrir():
    import gspread
    from google.oauth2.service_account import Credentials

    creds_path = Path(config.GOOGLE_SERVICE_ACCOUNT_JSON)
    if not creds_path.is_absolute():
        creds_path = ROOT / creds_path
    if not creds_path.exists():
        raise SystemExit(f"credenciais não encontradas em {creds_path}")
    if not config.GOOGLE_SHEETS_ID:
        raise SystemExit("GOOGLE_SHEETS_ID ausente no .env")

    scopes = [
        "https://spreadsheets.google.com/feeds",
        "https://www.googleapis.com/auth/drive",
    ]
    gc = gspread.authorize(Credentials.from_service_account_file(str(creds_path), scopes=scopes))
    return gc.open_by_key(config.GOOGLE_SHEETS_ID)


def rotacionar_sheets(dry: bool) -> None:
    sp = _abrir()
    existentes = {ws.title: ws for ws in sp.worksheets()}
    print(f"Planilha: {sp.title}")
    print(f"Abas atuais: {', '.join(existentes)}\n")

    for nome, header in ABAS:
        arquivo = nome + SUFIXO

        if arquivo in existentes:
            print(f"  '{arquivo}' já existe — nada a arquivar para '{nome}'")
        elif nome in existentes:
            ws = existentes[nome]
            linhas = len([r for r in ws.get_all_values() if any(c.strip() for c in r)])
            print(f"  '{nome}' ({linhas - 1} registros) -> renomear para '{arquivo}'")
            if not dry:
                ws.update_title(arquivo)
        else:
            print(f"  '{nome}' não existe — nada a arquivar")

        # Recria a aba de trabalho, vazia e com o esquema novo
        atuais = {ws.title for ws in sp.worksheets()} if not dry else set(existentes) | {arquivo}
        if nome in atuais and nome in existentes and arquivo in existentes:
            print(f"  '{nome}' já está no esquema de trabalho — conferindo cabeçalho")
            if not dry:
                ws = sp.worksheet(nome)
                if ws.row_values(1) != header:
                    ws.update("A1", [header])
                    print("    cabeçalho atualizado")
            continue

        print(f"  criar '{nome}' vazia com {len(header)} colunas")
        print(f"    {', '.join(header)}")
        if not dry:
            nova = sp.add_worksheet(title=nome, rows=5000, cols=len(header))
            nova.append_row(header, value_input_option="RAW")
            nova.freeze(rows=1)
        print()


def rotacionar_csv(dry: bool) -> None:
    print("CSVs locais:")
    for origem, destino in CSV_LOCAIS:
        if not origem.exists():
            print(f"  {origem.name}: não existe — nada a fazer")
            continue
        if destino.exists():
            print(f"  {destino.name} já existe — {origem.name} mantido como está")
            continue
        print(f"  {origem.name} -> {destino.name}")
        if not dry:
            origem.rename(destino)
    print("  (os novos serão criados pelo tracker no primeiro sinal)")


def main() -> None:
    dry = "--dry-run" in sys.argv
    if dry:
        print("MODO SIMULAÇÃO — nada será alterado\n")

    rotacionar_sheets(dry)
    rotacionar_csv(dry)

    print()
    if dry:
        print("Simulação concluída. Rode sem --dry-run para aplicar.")
    else:
        print("Pronto. As abas _v1 guardam o histórico da contabilidade antiga;")
        print("Signals e Resolved começam vazias, no esquema por pernas.")


if __name__ == "__main__":
    main()
