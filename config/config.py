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

# Módulos autorizados a executar ordens reais quando MODE=live.
# Tudo que não estiver aqui continua em dry-run mesmo com MODE=live — é o
# segundo cadeado, para que ligar o modo live não solte os 9 módulos de uma vez.
LIVE_MODULES: set[str] = {
    m.strip() for m in os.getenv("LIVE_MODULES", "").split(",") if m.strip()
}

# Exige confirmação explícita para o primeiro ciclo live (evita ligar sem querer)
LIVE_CONFIRMED: bool = os.getenv("LIVE_CONFIRMED", "false").lower() == "true"

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

# ── Fees ──────────────────────────────────────────────────────────────────────
# "min"     — rate × min(p, 1−p), fórmula publicada pela Polymarket (padrão)
# "product" — rate × p × (1−p), modelo legado que subestimava o custo em 2x
#             na região p≈0.50; mantido apenas para comparação histórica
# "auto"    — usa o mais caro dos dois (máxima conservadorismo)
FEE_MODEL: str = os.getenv("FEE_MODEL", "min")

# Fração mínima do tamanho alvo que o livro precisa comportar para a ordem
# ser considerada executável. 0.95 = aceita preencher 95% do alvo.
MIN_FILL_RATIO = float(os.getenv("MIN_FILL_RATIO", "0.95"))

# ── Execução ──────────────────────────────────────────────────────────────────
# Edge mínimo NA HORA DE ENVIAR. O livro se move entre detectar e executar; se
# a revalidação mostrar menos que isto, nenhuma ordem sai.
EXEC_MIN_EDGE      = float(os.getenv("EXEC_MIN_EDGE", "0.01"))
# Folga no preço limite para a ordem cruzar o book (0.3% acima do ask).
EXEC_PRICE_BUFFER  = float(os.getenv("EXEC_PRICE_BUFFER", "0.003"))
# Slippage máximo tolerado em ordem direcional antes de desistir.
EXEC_MAX_SLIPPAGE  = float(os.getenv("EXEC_MAX_SLIPPAGE", "0.02"))

# ── Risco (aplicado a TODOS os módulos, sem exceção) ──────────────────────────
MAX_TRADE_PCT            = float(os.getenv("MAX_TRADE_PCT", "0.05"))   # 5% do equity por operação
MAX_MODULE_EXPOSURE_PCT  = float(os.getenv("MAX_MODULE_EXPOSURE_PCT", "0.25"))
MAX_OPEN_EXPOSURE_PCT    = float(os.getenv("MAX_OPEN_EXPOSURE_PCT", "0.50"))
MAX_CONCURRENT_POSITIONS = int(os.getenv("MAX_CONCURRENT_POSITIONS", "8"))
DAILY_LOSS_LIMIT_PCT     = float(os.getenv("DAILY_LOSS_LIMIT_PCT", "0.03"))    # −3% do equity no dia
MONTHLY_LOSS_LIMIT_PCT   = float(os.getenv("MONTHLY_LOSS_LIMIT_PCT", "0.08"))  # −8% no mês
MAX_DRAWDOWN_PCT         = float(os.getenv("MAX_DRAWDOWN_PCT", "0.15"))        # −15% arma kill switch

# Intervalo do tracker. Com mercados de 5 minutos, 6 horas deixava o capital
# marcado como exposto muito depois da resolucao, travando novas entradas.
TRACKER_INTERVAL_S       = int(os.getenv("TRACKER_INTERVAL_S", "300"))

# ── Fase 2: banca B (lucros de A) ────────────────────────────────────────────
PHASE2_TRIGGER_USD     = float(os.getenv("PHASE2_TRIGGER_USD", "45"))  # A acumula $45 → ativa B
BANCA_B_ALLOC_B1       = 0.60   # 60% da banca B para B1
BANCA_B_ALLOC_B2       = 0.40   # 40% da banca B para B2
B_STOP_LOSS_BANCA      = 0.30   # -30% da banca B em qualquer mês → pausar B
B1_MIN_RESOLVED_FOR_B2 = int(os.getenv("B1_MIN_RESOLVED_FOR_B2", "5"))  # resoluções de B1 para ativar B2

# ── Módulo A — Arbitragem Combinatória ───────────────────────────────────────
# Thresholds revertidos aos valores originais. Foram afrouxados duas vezes
# (94cd668, 7058ef9) com a justificativa de "destravar detecções" — calibrados
# pela contagem de sinais, não por P&L. Só voltam a ser afrouxados contra
# resultado resolvido de verdade.
A_MIN_SPREAD          = 0.02   # retorno líquido mínimo (2%) para trades de 2 mercados
A_SPREAD_PER_MARKET   = 0.005  # 0.5% adicional por perna (risco de execução sequencial)
A_MAX_FEE             = 0.01   # fee máxima aceita por mercado (1%)
A_MIN_LIQUIDITY       = 100.0  # $100 mínimo nos 5 melhores níveis de cada lado
A_MAX_RESOLUTION_DAYS = 30     # mercados que resolvem em ≤ 30 dias
A_TRADE_SIZE_USD      = 50.0   # teto nominal; risk.py corta pelo caixa disponível
A_SCAN_INTERVAL       = 600    # segundos (10 min)

# Preço usado no cálculo de arbitragem: "ask" varre o livro e usa o preço médio
# real para o tamanho da ordem; "mid" usa o preço de referência da API (rápido,
# porém não executável — só para diagnóstico).
A_PRICING_MODE        = os.getenv("A_PRICING_MODE", "ask")

# Tipo 1 UNDERSUM (comprar YES em todos os candidatos) só paga se algum deles
# necessariamente vencer. O Polymarket não garante que a lista de candidatos de
# um evento seja exaustiva, e faixas numéricas descobertas já produziram o pior
# falso positivo do histórico: "soma YES=82.7%, spread 17.2%" era o preço da
# cauda ">60" que ninguém estava comprando. Para faixas, a exaustividade é
# verificada numericamente e o trade é liberado; para candidatos nomeados, não
# há como verificar — fica desligado.
A_ALLOW_UNDERSUM_WITHOUT_COVERAGE = os.getenv(
    "A_ALLOW_UNDERSUM_WITHOUT_COVERAGE", "false").lower() == "true"

# ── Módulo CORR — Scanner de correlações ─────────────────────────────────────
CORR_SCAN_INTERVAL   = 600    # segundos (10 min)
CORR_MIN_SPREAD      = 0.03   # inconsistência mínima de 3% para alertar
CORR_MIN_LIQUIDITY   = 100.0
# Filtro primário: taker_base_fee == 0 (definido no CLOB por mercado)
# Tags de referência para log (não usadas como filtro de API)
A_TARGET_TAGS        = {"Geopolitics", "World"}

# ── Módulo B1 — Weather Oracle ───────────────────────────────────────────────
B1_MIN_DIVERGENCE    = 0.20   # 20% divergência modelo vs. mercado (valor original)
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

# Unidade de temperatura por cidade. O Open-Meteo devolve Celsius por padrão,
# mas os mercados de cidades dos EUA são cotados em Fahrenheit — comparar 22
# com um threshold de 75 gerava divergência máxima e sinal confiantemente
# errado em todo mercado de Nova York.
# O módulo tenta primeiro ler a unidade do próprio enunciado (°F/°C); esta lista
# é só o fallback quando a pergunta não diz. Toronto não entra: o Canadá usa °C.
B1_FAHRENHEIT_CITIES = {
    c.strip() for c in os.getenv("B1_FAHRENHEIT_CITIES", "New York").split(",") if c.strip()
}

# Sem unidade explícita na pergunta e cidade fora da lista → descarta o mercado
# em vez de chutar. Um chute errado aqui vira sinal de 40% de "divergência".
B1_REQUIRE_KNOWN_UNIT = os.getenv("B1_REQUIRE_KNOWN_UNIT", "true").lower() == "true"

# Desvio-padrao do erro da previsao de maxima em 24h, em graus Celsius.
# Entra como sigma na normal que converte "previsao vs limiar" em probabilidade.
# Calibrar contra o historico resolvido: se B1 erra mais do que o modelo diz,
# este numero esta baixo demais.
B1_FORECAST_SIGMA_C = float(os.getenv("B1_FORECAST_SIGMA_C", "2.0"))

# Piso/teto de probabilidade do modelo. Previsao de tempo nao da certeza
# absoluta; sem essa trava o modelo declarava 100% a 3 desvios e gerava
# "divergencia" de 99% contra qualquer preco.
B1_PROB_FLOOR = float(os.getenv("B1_PROB_FLOOR", "0.005"))

# ── Módulo B2 — Esports Oracle ───────────────────────────────────────────────
B2_MIN_DIVERGENCE    = 0.15   # 15% divergência win-rate vs. mercado (valor original)
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
B4_MIN_LIQUIDITY     = 500.0
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
B5_TRADE_SIZE_USD = 25.0      # orcamento TOTAL da cesta YES+NO
                              # (o teto de risco por operacao pode reduzir ainda mais)
B5_FEE_RATE       = 0.072     # taxa taker crypto Polymarket (aplicada via modules/fees.py)

# Gate único da arbitragem: lucro líquido por par YES+NO, já descontados
# o fee real (modelo "min") e o slippage real (preço VWAP para o tamanho alvo).
# Substitui o antigo B5_THRESHOLD, que comparava custo contra 0.985 usando um
# fee subestimado — um par a 0.46/0.47 aparecia como "edge 3.4%" quando o
# líquido real era 0.31%.
B5_MIN_NET_EDGE   = float(os.getenv("B5_MIN_NET_EDGE", "0.015"))   # 1.5% líquido
B5_THRESHOLD      = 1.0 - B5_MIN_NET_EDGE   # custo all-in máximo por par
B5_MIN_DEPTH_USD  = float(os.getenv("B5_MIN_DEPTH_USD", "150"))    # profundidade por lado
B5_T_ENTRY_MIN_S  = 30        # segundos após abertura do candle: início da janela
B5_T_ENTRY_MAX_S  = 240       # segundos após abertura do candle: fim da janela
B5_CANDLE_SECS    = 300       # duração do candle (5 minutos)
B5_SCAN_INTERVAL  = 60        # segundos (1 min)

# ── Módulo B5 Pro — Multi-Strategy ───────────────────────────────────────────
# Desligado por padrao: o ARB do B5 Pro e o MESMO trade do modulo B5, nos
# mesmos mercados. Rodando os dois em paralelo a exposicao dobrava na mesma
# oportunidade, e o dedup do tracker fazia um sinal engolir o outro no CSV.
# Escolha um dos dois. B5 e o mais simples; B5 Pro so faz sentido quando
# NEAR_RES ou REPRICING forem religadas.
STRATEGY_B5_PRO_ENABLED: bool = os.getenv("STRATEGY_B5_PRO_ENABLED", "false").lower() == "true"

# NEAR_RES e REPRICING desligadas por padrão — ver docs/ESTRATEGIAS.md.
#
# NEAR_RES: comprar a 0.96–0.995 por 0.5% de edge é EV≈0 por construção (o preço
#   É a estimativa de probabilidade), e a distribuição é +2% em 98% dos casos
#   contra −98% em 2%. Uma perda apaga ~50 ganhos e os eventos são correlacionados
#   entre BTC/ETH/SOL. Kelly = 0; qualquer tamanho positivo é aposta perdedora.
#
# REPRICING: o modelo de fair value foi corrigido (vol escalada pelo tempo
#   restante), mas continua competindo em latência com HFT e depende de um strike
#   que o bot infere em vez de ler. Só religar depois de acumular P&L resolvido
#   positivo em dry-run com o modelo corrigido.
B5_NEAR_RES_ENABLED:  bool = os.getenv("B5_NEAR_RES_ENABLED",  "false").lower() == "true"
B5_REPRICING_ENABLED: bool = os.getenv("B5_REPRICING_ENABLED", "false").lower() == "true"

# ARB: mesmos parâmetros do B5 acima
# NEAR_RES
B5_NEAR_RES_PRICE_MIN      = 0.96    # preço mínimo do lado vencedor
B5_NEAR_RES_PRICE_MAX      = 0.995   # preço máximo do lado vencedor
B5_NEAR_RES_MIN_EDGE       = 0.005   # edge mínimo após fee (0.5%)
B5_NEAR_RES_WINDOW_START_S = 60      # T-60s antes do fechamento
B5_NEAR_RES_WINDOW_END_S   = 10      # T-10s antes do fechamento
# REPRICING
B5_REPRICING_MIN_DIVERGENCE = 0.08   # divergência mínima fair vs. poly
B5_REPRICING_MIN_LIQUIDITY  = 150.0  # liquidez mínima no lado a comprar
B5_REPRICING_VOL_CANDLES    = 20     # janela rolante de retornos para vol
# Nos ultimos segundos do candle a variancia restante tende a zero e o modelo
# fica numericamente instavel (z explode). Abaixo disso o detector nao opera.
B5_REPRICING_MIN_SECS_LEFT  = float(os.getenv("B5_REPRICING_MIN_SECS_LEFT", "30"))

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

    if FEE_MODEL not in ("min", "product", "auto"):
        errors.append(f"FEE_MODEL inválido: '{FEE_MODEL}' — use min | product | auto")
    if not (0.5 <= MIN_FILL_RATIO <= 1.0):
        errors.append(f"MIN_FILL_RATIO fora de faixa: {MIN_FILL_RATIO} — use 0.5..1.0")

    # Coerência dos limites de risco
    if MAX_TRADE_PCT > MAX_MODULE_EXPOSURE_PCT:
        errors.append("MAX_TRADE_PCT > MAX_MODULE_EXPOSURE_PCT — uma única operação estouraria o teto do módulo")
    if MAX_MODULE_EXPOSURE_PCT > MAX_OPEN_EXPOSURE_PCT:
        errors.append("MAX_MODULE_EXPOSURE_PCT > MAX_OPEN_EXPOSURE_PCT")
    if MAX_OPEN_EXPOSURE_PCT > 1.0 - ALLOC_RSRV:
        errors.append(
            f"MAX_OPEN_EXPOSURE_PCT ({MAX_OPEN_EXPOSURE_PCT:.0%}) invade a reserva "
            f"de {ALLOC_RSRV:.0%}"
        )
    if DAILY_LOSS_LIMIT_PCT >= MONTHLY_LOSS_LIMIT_PCT:
        errors.append("DAILY_LOSS_LIMIT_PCT deve ser menor que MONTHLY_LOSS_LIMIT_PCT")
    if MONTHLY_LOSS_LIMIT_PCT >= MAX_DRAWDOWN_PCT:
        errors.append("MONTHLY_LOSS_LIMIT_PCT deve ser menor que MAX_DRAWDOWN_PCT")

    # Modo live: exige os dois cadeados e credenciais completas
    if MODE == "live":
        if not LIVE_CONFIRMED:
            errors.append(
                "MODE=live exige LIVE_CONFIRMED=true no .env — confirmação explícita "
                "de que a operação com dinheiro real foi autorizada"
            )
        if not LIVE_MODULES:
            errors.append(
                "MODE=live exige LIVE_MODULES no .env (ex: LIVE_MODULES=A,B4). "
                "Nenhum módulo executa ordem real sem estar nessa lista"
            )
        unknown = LIVE_MODULES - {"A", "B1", "B2", "B3", "B4", "B5", "B5_ARB", "CORR"}
        if unknown:
            errors.append(f"LIVE_MODULES contém módulo desconhecido: {sorted(unknown)}")
        if not (POLY_API_KEY and POLY_API_SECRET and POLY_API_PASSPHRASE):
            errors.append(
                "MODE=live exige POLY_API_KEY/SECRET/PASSPHRASE no .env — rode uma vez "
                "em dry_run para gerá-las e cole o resultado"
            )

    if errors:
        raise EnvironmentError("Configuração incompleta:\n" + "\n".join(f"  • {e}" for e in errors))
