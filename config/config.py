import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")

# ── Wallet & Auth ────────────────────────────────────────────────────────────
PRIVATE_KEY: str = os.getenv("PRIVATE_KEY", "")
WALLET_ADDRESS: str = os.getenv("WALLET_ADDRESS", "")
POLY_API_KEY: str = os.getenv("POLY_API_KEY", "")
POLY_API_SECRET: str = os.getenv("POLY_API_SECRET", "")
POLY_API_PASSPHRASE: str = os.getenv("POLY_API_PASSPHRASE", "")

MODE: str = os.getenv("MODE", "dry_run")  # "dry_run" | "live"

# ── Toggles de estratégia ─────────────────────────────────────────────────────
STRATEGY_A_ENABLED:  bool = os.getenv("STRATEGY_A_ENABLED",  "true").lower() == "true"
STRATEGY_B1_ENABLED: bool = os.getenv("STRATEGY_B1_ENABLED", "true").lower() == "true"
STRATEGY_B2_ENABLED: bool = os.getenv("STRATEGY_B2_ENABLED", "true").lower() == "true"
STRATEGY_B3_ENABLED: bool = os.getenv("STRATEGY_B3_ENABLED", "true").lower() == "true"
STRATEGY_B4_ENABLED: bool = os.getenv("STRATEGY_B4_ENABLED", "true").lower() == "true"

# ── Polymarket API ───────────────────────────────────────────────────────────
CLOB_BASE_URL = "https://clob.polymarket.com"
GAMMA_API_URL = "https://gamma-api.polymarket.com"

# ── Capital ($) ───────────────────────────────────────────────────────────────
TOTAL_CAPITAL = 500.0

# ── Fase 1: apenas A ativo ────────────────────────────────────────────────────
# B1 e B2 não recebem capital do principal — usam exclusivamente lucros de A
ALLOC_A    = float(os.getenv("ALLOC_A",    "0.90"))   # 90% em A
ALLOC_B1   = 0.0                                        # sem alocação do principal
ALLOC_B2   = 0.0
ALLOC_RSRV = float(os.getenv("ALLOC_RSRV", "0.10"))   # 10% reserva intocável

CAPITAL_A    = TOTAL_CAPITAL * ALLOC_A    # $450
CAPITAL_B1   = 0.0                         # determinado pela banca B em tempo de execução
CAPITAL_B2   = 0.0
CAPITAL_RSRV = TOTAL_CAPITAL * ALLOC_RSRV # $50

MAX_TRADE_B     = float(os.getenv("MAX_TRADE_B", "0.05"))  # cap por operação B (% do capital total)
MAX_TRADE_B_USD = TOTAL_CAPITAL * MAX_TRADE_B               # $25

# ── Fase 2: banca B (lucros de A) ────────────────────────────────────────────
PHASE2_TRIGGER_USD     = float(os.getenv("PHASE2_TRIGGER_USD", "45"))  # A acumula $45 → ativa B
BANCA_B_ALLOC_B1       = 0.60   # 60% da banca B para B1
BANCA_B_ALLOC_B2       = 0.40   # 40% da banca B para B2
B_STOP_LOSS_BANCA      = 0.30   # -30% da banca B em qualquer mês → pausar B
B1_MIN_RESOLVED_FOR_B2 = int(os.getenv("B1_MIN_RESOLVED_FOR_B2", "5"))  # resoluções de B1 para ativar B2

# ── Módulo A — Arbitragem Combinatória ───────────────────────────────────────
A_MIN_SPREAD          = 0.02   # retorno mínimo base (2%) para trades de 2 mercados
A_SPREAD_PER_MARKET   = 0.005  # 0.5% adicional por mercado (bid-ask + slippage sequencial)
A_MAX_FEE             = 0.025  # fee máxima aceita por mercado (2.5%) — padrão Polymarket é 2%
A_MIN_LIQUIDITY       = 100.0  # $100 mínimo no order book de cada lado
A_MAX_RESOLUTION_DAYS = 90     # mercados que resolvem em ≤ 90 dias
A_TRADE_SIZE_USD      = 50.0   # 10% do capital total ($50 com $500)
A_SCAN_INTERVAL       = 600    # segundos (10 min)

# ── Módulo CORR — Scanner de correlações ─────────────────────────────────────
CORR_SCAN_INTERVAL   = 600    # segundos (10 min)
CORR_MIN_SPREAD      = 0.03   # inconsistência mínima de 3% para alertar
CORR_MIN_LIQUIDITY   = 100.0
# Filtro primário: taker_base_fee == 0 (definido no CLOB por mercado)
# Tags de referência para log (não usadas como filtro de API)
A_TARGET_TAGS        = {"Geopolitics", "World"}

# ── Módulo B1 — Weather Oracle ───────────────────────────────────────────────
B1_MIN_DIVERGENCE    = 0.08   # 8% divergência modelo vs. mercado
B1_MAX_SHARE_PRICE   = 0.40   # ≤ 40¢ para reduzir fee efetiva
B1_MIN_LIQUIDITY     = 100.0
B1_MAX_RESOLUTION_H  = 24     # apenas mercados que resolvem em ≤ 24h
B1_SCAN_INTERVAL     = 3600   # segundos (1h)
B1_TARGET_TAGS       = {"Weather", "Daily Temperature", "Highest temperature", "Lowest temperature"}

B1_CITIES = {
    "New York":  {"lat": 40.7128,  "lon": -74.0060},
    "London":    {"lat": 51.5074,  "lon":  -0.1278},
    "Seoul":     {"lat": 37.5665,  "lon": 126.9780},
    "Hong Kong": {"lat": 22.3193,  "lon": 114.1694},
    "Madrid":    {"lat": 40.4168,  "lon":  -3.7038},
    "Tokyo":     {"lat": 35.6762,  "lon": 139.6503},
    "Sydney":    {"lat": -33.8688, "lon": 151.2093},
    "São Paulo": {"lat": -23.5505, "lon": -46.6333},
    "Mumbai":    {"lat": 19.0760,  "lon":  72.8777},
    "Berlin":    {"lat": 52.5200,  "lon":  13.4050},
    "Cairo":     {"lat": 30.0444,  "lon":  31.2357},
    "Singapore": {"lat":  1.3521,  "lon": 103.8198},
    "Paris":     {"lat": 48.8566,  "lon":   2.3522},
    "Toronto":   {"lat": 43.6532,  "lon": -79.3832},
    "Dubai":     {"lat": 25.2048,  "lon":  55.2708},
}

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# ── Módulo B2 — Esports Oracle ───────────────────────────────────────────────
B2_MIN_DIVERGENCE    = 0.09   # 9% divergência win-rate vs. mercado
B2_MAX_SHARE_PRICE   = 0.40
B2_MIN_LIQUIDITY     = 100.0
B2_MAX_RESOLUTION_H  = 4      # partidas que resolvem em ≤ 4h
B2_ENTRY_BEFORE_H    = 2      # entrar até 2h antes do início
B2_SCAN_INTERVAL     = 1800   # segundos (30 min)

B2_GAMES = ["lol", "dota2"]
B2_TARGET_TAGS = {
    "lol":   {"league of legends", "lol"},
    "dota2": {"Dota 2"},
}

# LoL Esports API (pública, usada pelo lolesports.com)
LOL_ESPORTS_API_KEY  = "0TvQnueqKa5mxJntVWt0w4LpLfEkrV1Ta8rQBb9Z"
LOL_ESPORTS_BASE_URL = "https://esports-api.lolesports.com/persisted/gw"
LOL_LEAGUES = {
    "LCK":   "98767991310872058",
    "LCS":   "98767991299243165",
    "LEC":   "98767991302996019",
    "LPL":   "98767991314006698",
    "CBLOL": "98767991332355509",
}

STRATZ_API_URL     = "https://api.stratz.com/graphql"

# ── Anthropic (Claude Haiku — validação de regras de resolução) ───────────────
ANTHROPIC_API_KEY: str = os.getenv("ANTHROPIC_API_KEY", "")

# ── Telegram (notificações de sinais) ─────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID:   str = os.getenv("TELEGRAM_CHAT_ID", "")

# ── Módulo B3 — Politics / Elections Oracle ──────────────────────────────────
B3_TARGET_TAGS = {
    "Politics", "Elections", "US Elections", "US Politics",
    "Global Politics", "2024 Elections", "2026 Elections",
}
B3_MAX_FEE             = 0.01
B3_MIN_SPREAD          = 0.025
B3_MIN_LIQUIDITY       = 100.0
B3_MAX_RESOLUTION_DAYS = 90
B3_SCAN_INTERVAL       = 900   # segundos (15 min)
B3_TRADE_SIZE_USD      = 50.0

# ── Módulo B4 — Sports Live-Game Oracle ──────────────────────────────────────
B4_LEAGUES = ["NBA", "NFL", "MLB", "NHL"]
B4_TARGET_TAGS = {
    "NBA", "NFL", "MLB", "NHL",
    "Basketball", "American Football", "Baseball", "Hockey",
}
B4_MAX_FEE           = 0.0075
B4_MIN_SPREAD        = 0.020
B4_MIN_LIQUIDITY     = 150.0
B4_STANDBY_INTERVAL  = 1800  # segundos (30 min)
B4_ACTIVE_INTERVAL   = 60    # segundos (1 min) quando há jogos ao vivo
B4_LIVE_WINDOW_H     = 4     # jogo resolve em ≤ 4h
B4_BLOCK_FINAL_MIN   = 5     # bloqueia nos últimos 5 min do jogo
B4_TRADE_SIZE_USD    = 50.0

# Cache de win-rate: quantas horas reutilizar antes de buscar novamente
B2_WINRATE_CACHE_H = int(os.getenv("B2_WINRATE_CACHE_H", "6"))
# Mínimo de partidas para considerar win-rate confiável
B2_MIN_MATCHES     = int(os.getenv("B2_MIN_MATCHES", "5"))

# ── Módulo B5 — Crypto 5-Min Spread Capture ──────────────────────────────────
STRATEGY_B5_ENABLED: bool = os.getenv("STRATEGY_B5_ENABLED", "true").lower() == "true"
B5_ASSETS         = ["BTC", "ETH", "SOL"]
B5_TRADE_SIZE_USD = 25.0      # por lado ($50 total)
B5_THRESHOLD      = 0.985     # p_up + p_down + fee_total < B5_THRESHOLD
B5_FEE_RATE       = 0.072     # taker fee crypto Polymarket: fee = rate × p × (1−p)
B5_T_ENTRY_MIN_S  = 30        # segundos após abertura do candle: início da janela
B5_T_ENTRY_MAX_S  = 240       # segundos após abertura do candle: fim da janela
B5_CANDLE_SECS    = 300       # duração do candle (5 minutos)
B5_SCAN_INTERVAL  = 60        # segundos (1 min)

# ── Módulo B5 Pro — Multi-Strategy ───────────────────────────────────────────
STRATEGY_B5_PRO_ENABLED: bool = os.getenv("STRATEGY_B5_PRO_ENABLED", "true").lower() == "true"
# ARB: mesmos parâmetros do B5 acima
# NEAR_RES
B5_NEAR_RES_PRICE_MIN      = 0.96    # preço mínimo do lado vencedor
B5_NEAR_RES_PRICE_MAX      = 0.995   # preço máximo do lado vencedor
B5_NEAR_RES_MIN_EDGE       = 0.005   # edge mínimo após fee (0.5%)
B5_NEAR_RES_WINDOW_START_S = 60      # T-60s antes do fechamento
B5_NEAR_RES_WINDOW_END_S   = 10      # T-10s antes do fechamento
# REPRICING
B5_REPRICING_MIN_DIVERGENCE = 0.05   # divergência mínima fair vs. poly
B5_REPRICING_MIN_LIQUIDITY  = 150.0  # liquidez mínima no lado a comprar
B5_REPRICING_VOL_CANDLES    = 20     # janela rolante de retornos para vol

# ── Risco ────────────────────────────────────────────────────────────────────
B_MONTHLY_STOP_LOSS = -0.10   # -10% do capital alocado em B → pausar B

# ── Google Sheets (espelho opcional dos CSVs) ─────────────────────────────────
# ID da planilha: string entre /d/ e /edit na URL do Google Sheets
GOOGLE_SHEETS_ID:            str = os.getenv("GOOGLE_SHEETS_ID", "")
# Caminho para o JSON da service account (relativo à raiz do projeto)
GOOGLE_SERVICE_ACCOUNT_JSON: str = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "credentials.json")

# ── Validação de inicialização ───────────────────────────────────────────────
def validate():
    errors = []
    if not PRIVATE_KEY:
        errors.append("PRIVATE_KEY ausente no .env")
    if not WALLET_ADDRESS:
        errors.append("WALLET_ADDRESS ausente no .env")
    if MODE not in ("dry_run", "live"):
        errors.append(f"MODE inválido: '{MODE}' — use 'dry_run' ou 'live'")
    total_alloc = ALLOC_A + ALLOC_RSRV
    if total_alloc > 1.001:
        errors.append(f"Alocações somam {total_alloc:.2%} — deve ser ≤ 100%")
    if not STRATEGY_A_ENABLED:
        errors.append("Estratégia A é obrigatória e não pode ser desativada")
    if errors:
        raise EnvironmentError("Configuração incompleta:\n" + "\n".join(f"  • {e}" for e in errors))
