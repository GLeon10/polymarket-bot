# Polymarket Bot

Bot de trading automatizado para o [Polymarket](https://polymarket.com) que detecta ineficiências de mercado e emite sinais via Telegram.

Roda em **dry-run por padrão** — registra sinais e calcula P&L hipotético sem executar trades reais.

---

## Como funciona

O bot roda 8 módulos em paralelo, cada um com sua própria estratégia de detecção:

```
main.py
├── Módulo A       — Arbitragem combinatória entre mercados relacionados
├── Módulo B1      — Oracle climático (modelo meteorológico vs. mercado)
├── Módulo B2      — Oracle de esports (win-rate histórico vs. mercado)
├── Módulo B3      — Oracle político (inconsistências em datas/candidatos)
├── Módulo B4      — Oracle esportivo ao vivo (ML vs. spread)
├── Módulo B5      — Spread capture BTC/ETH/SOL 5-min via WebSocket (ARB)
├── Módulo B5 Pro  — 3 estratégias crypto 5-min em loop asyncio único
│                    ARB · NEAR_RES · REPRICING
└── Módulo CORR    — Scanner de correlações lógicas entre mercados
```

Quando um sinal é detectado:
1. É gravado em `data/trades/signals.csv`
2. Uma notificação é enviada via Telegram
3. O tracker verifica a resolução e calcula P&L quando o mercado fecha
4. O sinal é espelhado em tempo real no Google Sheets (se configurado)

---

## Estratégias

### Módulo A — Arbitragem Combinatória
Detecta quando a soma das probabilidades de mercados mutuamente exclusivos diverge de 100%:
- **Tipo 1 (Soma de partes):** `P(A) + P(B) + P(C) ≠ 100%`
- **Tipo 2 (Subconjunto):** `P(A ganhar torneio) > P(A ou B ganhar torneio)` — impossível matematicamente
- **Tipo 3 (Implicação):** `P(ganhar o título) > P(chegar à final)` — quem ganha o título necessariamente passou pela final

### Módulo B1 — Weather Oracle
Compara a previsão do modelo Open-Meteo com o preço do mercado de temperatura.
Emite sinal quando a divergência supera 12%.
Cobre 15 cidades: Nova York, Londres, Seoul, Hong Kong, Tóquio, Sydney, São Paulo, Mumbai, Berlim, Cairo, Singapura, Paris, Toronto, Dubai e Madri.

### Módulo B2 — Esports Oracle
Compara o win-rate histórico de equipes de LoL e Dota 2 com o preço do mercado.
Emite sinal quando a divergência supera 10%, para partidas que ocorrem em até 4 horas.

### Módulo B3 — Politics Oracle
Detecta inconsistências lógicas em mercados políticos:
- Soma de candidatos diverge de 100%
- Ordenação de datas impossível (evento A antes de B, mas P(A) < P(B))
- Candidato / partido inconsistente com o evento

### Módulo B4 — Sports Live Oracle
Detecta quando o preço moneyline (ML) de uma equipe é maior que o preço do spread para a mesma equipe — matematicamente impossível.
Modo standby (30 min) quando não há jogos ao vivo, modo ativo (60s) quando detecta partidas.

### Módulo B5 — Crypto 5-Min Spread Capture
Arbitragem intra-candle em mercados binários de 5 minutos (BTC, ETH, SOL).
Compra YES e NO do mesmo mercado quando o custo total (incluindo fee) é menor que o threshold:

```
p_up_ask + p_down_ask + fee(p_up) + fee(p_down) < 0.982
fee(p) = 0.072 × p × (1 − p)
```

- Preços em tempo real via WebSocket CLOB
- Fallback automático para REST quando WebSocket indisponível
- Janela de entrada: T+30s a T+240s após abertura do candle
- $25 por lado ($50 total), ordens FAK

### Módulo B5 Pro — Crypto 5-Min Multi-Strategy
Versão avançada do B5 com 3 estratégias rodando num único loop asyncio com dois WebSockets em paralelo (CLOB + Binance):

**ARB** — mesma lógica do B5, FAK separados por lado para eliminar risco de leg parcial.

**NEAR_RES** — Near-resolution: entra no lado vencedor nos últimos 60s do candle quando o preço está entre 0.96 e 0.995. Edge mínimo de 0.5%. Emite flag `⚠️ TAIL_RISK` no Telegram.

**REPRICING** — Fair value via Binance aggTrade WebSocket:
```
pct_change = (spot_now − open_candle) / open_candle
z_score    = pct_change / volatilidade_20_candles
fair_prob  = norm.cdf(z_score)          # scipy.stats
sinal quando |fair_prob − poly_price| > 6%  e  liquidez ≥ $150
```
Loga sempre: `fair_prob`, `poly_price`, divergência e latência Binance→Poly.

O campo `module` no CSV identifica a estratégia: `B5_ARB` / `B5_NEAR_RES` / `B5_REPRICING`.

### Módulo CORR — Correlation Scanner
Detecta inconsistências de correlação entre mercados:
- **UNDERSUM:** soma de YES < 97% (falta de candidatos)
- **OVERSUM:** soma de YES > 103% (sobreposição impossível)
- **SUBSET:** P(resultado específico) > P(resultado genérico)

Todo sinal passa por 4 filtros de qualidade: fee, liquidez, exaustividade e consistência de resolução.

---

## Estrutura de arquivos

```
polymarket_bot/
├── main.py                  # Orquestrador — inicia threads dos módulos
├── simulate.py              # Simulação manual de um ciclo completo (dev)
├── config/
│   └── config.py            # Todos os parâmetros e thresholds
├── modules/
│   ├── scanner_a.py         # Módulo A
│   ├── oracle_b1.py         # Módulo B1
│   ├── oracle_b2.py         # Módulo B2
│   ├── oracle_b3.py         # Módulo B3
│   ├── oracle_b4.py         # Módulo B4
│   ├── oracle_b5.py         # Módulo B5
│   ├── oracle_b5_pro.py     # Módulo B5 Pro (ARB + NEAR_RES + REPRICING)
│   ├── scanner_corr.py      # Módulo CORR
│   ├── tracker.py           # Gravação de sinais e P&L
│   ├── notifier.py          # Notificações Telegram
│   ├── sheets.py            # Espelho Google Sheets (opcional)
│   ├── phase_manager.py     # Gestão de fases de capital
│   ├── rule_validator.py    # Validação de regras via keywords + Claude Haiku
│   └── clob_utils.py        # Utilitários da API Polymarket
├── migrate_to_sheets.py     # Migração única CSV → Google Sheets
├── tests/
│   └── test_all.py          # 264 testes (todos os módulos)
└── data/
    ├── trades/
    │   ├── signals.csv      # Sinais emitidos (inclui pnl_est_usd por sinal)
    │   └── resolved.csv     # Sinais resolvidos com P&L real
    └── logs/
        ├── YYYY-MM-DD.log   # Log unificado do dia
        ├── scanner_a.log    # Log individual por módulo
        ├── oracle_b5.log
        ├── oracle_b5_pro.log
        └── ...
```

---

## Configuração

### 1. Clonar e instalar dependências

```bash
git clone https://github.com/GLeon10/polymarket-bot.git
cd polymarket_bot
pip install -r requirements.txt
```

> **VPS (Ubuntu/Debian):** se aparecer o erro `externally-managed-environment`, use virtualenv:
> ```bash
> python3 -m venv venv
> source venv/bin/activate
> pip install -r requirements.txt
> ```

### 2. Criar o arquivo `.env`

```env
# Wallet Ethereum/Polygon
PRIVATE_KEY=sua_chave_privada
WALLET_ADDRESS=0xseu_endereco

# Modo de operação
MODE=dry_run   # ou "live" para operar de verdade

# API Polymarket (opcional — gerada automaticamente no primeiro run em live)
POLY_API_KEY=
POLY_API_SECRET=
POLY_API_PASSPHRASE=

# Telegram (notificações de sinais)
TELEGRAM_BOT_TOKEN=token_do_seu_bot
TELEGRAM_CHAT_ID=seu_chat_id

# Google Sheets (opcional — espelho dos CSVs)
GOOGLE_SHEETS_ID=id_da_planilha
GOOGLE_SERVICE_ACCOUNT_JSON=credentials.json

# Claude Haiku (validação de regras de resolução — opcional)
ANTHROPIC_API_KEY=sua_chave_anthropic
```

### 3. Rodar o bot

```bash
python main.py
```

---

## Parâmetros principais

| Parâmetro | Valor | Descrição |
|-----------|-------|-----------|
| `A_MAX_FEE` | 2.5% | Fee máxima aceita por mercado no módulo A |
| `A_MIN_SPREAD` | 2% | Retorno mínimo líquido para sinal A |
| `A_MAX_RESOLUTION_DAYS` | 60 dias | Janela de resolução máxima para módulo A |
| `B1_MIN_DIVERGENCE` | 12% | Divergência mínima modelo vs. mercado (B1) |
| `B2_MIN_DIVERGENCE` | 10% | Divergência mínima win-rate vs. mercado (B2) |
| `B4_MIN_LIQUIDITY` | $150 | Liquidez mínima nos dois lados (B4) |
| `B5_THRESHOLD` | 0.982 | Custo máximo p_up + p_down + fee (B5 / B5 Pro ARB) |
| `B5_FEE_RATE` | 7.2% | Taxa taker crypto: `rate × p × (1−p)` |
| `B5_TRADE_SIZE_USD` | $25/lado | Capital por lado por operação B5/B5 Pro |
| `B5_NEAR_RES_PRICE_MIN` | 0.96 | Preço mínimo para NEAR_RES |
| `B5_NEAR_RES_PRICE_MAX` | 0.995 | Preço máximo para NEAR_RES |
| `B5_REPRICING_MIN_DIVERGENCE` | 6% | Divergência mínima fair vs. poly (REPRICING) |
| `B5_REPRICING_MIN_LIQUIDITY` | $150 | Liquidez mínima no lado a comprar (REPRICING) |
| `TOTAL_CAPITAL` | $500 | Capital total gerenciado pelo bot |

Todos os parâmetros estão em [config/config.py](config/config.py).

---

## Gestão de capital por fases

**Fase 1 (inicial):**
- 90% do capital vai para o Módulo A
- B1, B2, B3, B4, B5, B5 Pro ficam em standby sem capital próprio

**Fase 2 (ativada quando A acumula $45 em P&L):**
- Os lucros de A formam a "Banca B"
- Banca B: 60% para B1, 40% para B2
- Stop-loss: -30% da Banca B em qualquer mês pausa B

---

## Notificações Telegram

**Módulo A / CORR:**
```
[SINAL A]
Tipo 1 UNDERSUM — 'the championship' (3 candidatos): soma YES=72%

Lado: arb-t1 | Entrada: 0.240
Spread: 4.2% | P&L est: $2.10
Capital: $50.00 | Fecha: 2026-05-20 18:00 BRT
https://polymarket.com/event/...
```

**Módulo B5:**
```
[SINAL B5] (2 mercados)
[B5] BTC 5-min: Will BTC be above $90k in 5 min?

Lado: Yes+No | Entrada: 0.440
Spread: 6.8% | P&L est: $3.40
Capital: $25.00 | Fecha: 2026-05-13 19:55 BRT
https://polymarket.com/event/...
```

**Módulo B5 Pro — ARB:**
```
[SINAL B5_ARB] (2 mercados)
[ARB] BTC edge=6.8%: Will BTC be above $90k in 5 min?

Lado: Yes+No | Entrada: 0.440
Spread: 6.8% | P&L est: $1.70
Capital: $25.00 | Fecha: 2026-05-13 19:55 BRT
```

**Módulo B5 Pro — NEAR_RES:**
```
[SINAL B5_NEAR_RES]
[NEAR_RES] ⚠️ TAIL_RISK BTC edge=1.2%: Will BTC be above $90k in 5 min?

Lado: Yes | Entrada: 0.982
Spread: 1.2% | P&L est: $0.30
Capital: $25.00 | Fecha: 2026-05-13 19:59 BRT
```

**Módulo B5 Pro — REPRICING:**
```
[SINAL B5_REPRICING]
[REPRICING] BTC edge=4.1% | fair=0.720 poly=0.670 lat=18ms: Will BTC...

Lado: Yes | Entrada: 0.670
Spread: 4.1% | P&L est: $1.03
Capital: $25.00 | Fecha: 2026-05-13 19:55 BRT
```

Para configurar o bot do Telegram:
1. Abra [@BotFather](https://t.me/BotFather) e crie um bot — obtenha o `TELEGRAM_BOT_TOKEN`
2. Envie uma mensagem para o bot e abra `https://api.telegram.org/bot<TOKEN>/getUpdates` para obter o `TELEGRAM_CHAT_ID`

---

## Google Sheets

Os sinais e resoluções são espelhados automaticamente em uma planilha Google (abas **Signals** e **Resolved**). Configuração:

1. Crie um projeto no [Google Cloud Console](https://console.cloud.google.com) e ative as APIs **Google Sheets** e **Google Drive**
2. Crie uma **Service Account**, baixe o JSON e salve como `credentials.json` na raiz do projeto
3. Compartilhe a planilha com o e-mail da service account (permissão **Editor**)
4. Configure no `.env`: `GOOGLE_SHEETS_ID=...` e `GOOGLE_SERVICE_ACCOUNT_JSON=credentials.json`
5. Para migrar sinais existentes: `python migrate_to_sheets.py`

---

## Testes

```bash
python -m pytest tests/test_all.py -v
```

264 testes cobrindo todos os módulos: detecção, filtros, WebSocket, Binance feed, fair value, integração com mocks e casos de borda.

---

## Deploy em VPS (Linux)

```bash
# Clonar e criar virtualenv
git clone https://github.com/GLeon10/polymarket-bot.git
cd polymarket_bot
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

# Configurar .env
nano .env   # preencha com suas chaves

# Configurar como serviço systemd
cp polymarket-bot.service /etc/systemd/system/
systemctl enable polymarket-bot
systemctl start polymarket-bot

# Acompanhar logs em tempo real
journalctl -u polymarket-bot -f
```

### Atualizar a VPS após push

```bash
cd ~/polymarket_bot
git pull
source venv/bin/activate
pip install -r requirements.txt
sudo systemctl restart polymarket-bot
```

### Consultar sinais na VPS

```bash
# Sinais emitidos
cat ~/polymarket_bot/data/trades/signals.csv

# Formatado em colunas
column -t -s, ~/polymarket_bot/data/trades/signals.csv

# Sinais resolvidos com P&L
cat ~/polymarket_bot/data/trades/resolved.csv
```

---

## Segurança

- **Nunca compartilhe sua `PRIVATE_KEY` ou `credentials.json`** — ambos estão no `.gitignore`
- O bot opera em `dry_run` por padrão — não executa trades reais
- Para operar em `live`, altere `MODE=live` no `.env` e certifique-se de que a wallet tem fundos em USDC na rede Polygon
