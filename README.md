# Polymarket Bot

Bot de trading automatizado para o [Polymarket](https://polymarket.com) que detecta ineficiências de mercado e emite sinais via Telegram.

Roda em **dry-run por padrão** — registra sinais e calcula P&L hipotético sem executar trades reais.

---

## Como funciona

O bot roda 6 módulos em paralelo, cada um com sua própria estratégia de detecção:

```
main.py
├── Módulo A     — Arbitragem combinatória entre mercados relacionados
├── Módulo B1    — Oracle climático (modelo meteorológico vs. mercado)
├── Módulo B2    — Oracle de esports (win-rate histórico vs. mercado)
├── Módulo B3    — Oracle político (inconsistências em datas/candidatos)
├── Módulo B4    — Oracle esportivo ao vivo (ML vs. spread)
└── Módulo CORR  — Scanner de correlações lógicas entre mercados
```

Quando um sinal é detectado:
1. É gravado em `data/trades/signals.csv`
2. Uma notificação é enviada via Telegram
3. O tracker verifica a resolução e calcula P&L quando o mercado fecha

---

## Estratégias

### Módulo A — Arbitragem Combinatória
Detecta quando a soma das probabilidades de mercados mutuamente exclusivos diverge de 100%:
- **Tipo 1 (Soma de partes):** `P(A ganhar) + P(B ganhar) + P(C ganhar) ≠ 100%`
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
├── config/
│   └── config.py            # Todos os parâmetros e thresholds
├── modules/
│   ├── scanner_a.py         # Módulo A
│   ├── oracle_b1.py         # Módulo B1
│   ├── oracle_b2.py         # Módulo B2
│   ├── oracle_b3.py         # Módulo B3
│   ├── oracle_b4.py         # Módulo B4
│   ├── scanner_corr.py      # Módulo CORR
│   ├── tracker.py           # Gravação de sinais e P&L
│   ├── notifier.py          # Notificações Telegram
│   ├── phase_manager.py     # Gestão de fases de capital
│   ├── rule_validator.py    # Validação de regras via keywords + Claude Haiku
│   └── clob_utils.py        # Utilitários da API Polymarket
├── tests/
│   └── test_all.py          # 202 testes (todos os módulos)
└── data/
    ├── trades/
    │   ├── signals.csv      # Sinais emitidos
    │   └── resolved.csv     # Sinais resolvidos com P&L
    └── logs/
        ├── YYYY-MM-DD.log   # Log unificado do dia
        ├── scanner_a.log    # Log individual por módulo
        ├── oracle_b1.log
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
| `B1_MAX_SHARE_PRICE` | 40¢ | Preço máximo para entrar (reduz fee efetiva) |
| `B2_MIN_DIVERGENCE` | 10% | Divergência mínima win-rate vs. mercado (B2) |
| `B4_MIN_LIQUIDITY` | $150 | Liquidez mínima nos dois lados (B4) |
| `TOTAL_CAPITAL` | $500 | Capital total gerenciado pelo bot |

Todos os parâmetros estão em [config/config.py](config/config.py).

---

## Gestão de capital por fases

**Fase 1 (inicial):**
- 90% do capital vai para o Módulo A
- B1, B2, B3, B4 ficam em standby sem capital próprio

**Fase 2 (ativada quando A acumula $45 em P&L):**
- Os lucros de A formam a "Banca B"
- Banca B: 60% para B1, 40% para B2
- Stop-loss: -30% da Banca B em qualquer mês pausa B

---

## Notificações Telegram

Cada sinal detectado gera uma mensagem no formato:

```
[SINAL A]
Tipo 1 UNDERSUM — 'the championship' (3 candidatos): soma YES=72%

Lado: arb-t1 | Entrada: 0.240
Spread: 4.2% | P&L est: $2.10
Capital: $50.00 | Fecha: 2026-05-20 18:00 BRT
https://polymarket.com/event/...
```

Para configurar o bot do Telegram:
1. Abra [@BotFather](https://t.me/BotFather) e crie um bot — obtenha o `TELEGRAM_BOT_TOKEN`
2. Envie uma mensagem para o bot e abra `https://api.telegram.org/bot<TOKEN>/getUpdates` para obter o `TELEGRAM_CHAT_ID`

---

## Testes

```bash
python -m pytest tests/test_all.py -v
```

202 testes cobrindo todos os módulos: detecção, filtros, integração com mocks e casos de borda.

---

## Deploy em VPS (Linux)

```bash
# Instalar dependências
pip install -r requirements.txt

# Configurar como serviço systemd
cp polymarket-bot.service /etc/systemd/system/
systemctl enable polymarket-bot
systemctl start polymarket-bot

# Acompanhar logs em tempo real
journalctl -u polymarket-bot -f
```

---

## Segurança

- **Nunca compartilhe sua `PRIVATE_KEY`**
- O bot opera em `dry_run` por padrão — não executa trades reais
- Para operar em `live`, altere `MODE=live` no `.env` e certifique-se de que a wallet tem fundos em USDC na rede Polygon