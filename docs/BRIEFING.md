# Briefing — Polymarket Arbitrage Bot v3

Leia este arquivo inteiro antes de escrever qualquer código.

---

## Visão geral

Bot de trading automatizado para o Polymarket (mercado de previsão descentralizado).
Opera com arbitragem combinatória — explorando inconsistências lógicas entre mercados
relacionados dentro do próprio Polymarket.

Plataforma: Polymarket internacional (não o US). Colateral: USDC na rede Polygon.
Linguagem: Python 3.11+. Capital: $500.

---

## Estratégia principal — Arbitragem combinatória

Tipo: Arbitragem real — lucro garantido matematicamente se regras de resolução forem
idênticas. Janelas duram horas a dias. Bot de ciclo de 10 minutos consegue capturar.
Scanner YES+NO simples foi descontinuado.

### Detectores implementados (scanner_a.py)

**Tipo 1 — Soma de partes (prioridade máxima)**

Mercados mutuamente exclusivos e exaustivos cuja soma das probabilidades fica fora de
[0.98, 1.02]. Compra todos os lados.

- **Tipo 1a — "Will X win Y?"**: grupo de candidatos ao mesmo evento.
  Ex: Brasil (40¢) + Argentina (35¢) + Alemanha (18¢) = 93¢ → UNDERSUM, comprar YES.

- **Tipo 1b — Ranges exclusivos**: faixas do tipo "between X and Y", "less than X",
  "greater than X" com o mesmo prefixo. Requer mínimo de 3 faixas **e obrigatoriamente
  um extremo inferior** (less than / below / under / fewer than) **e um extremo superior**
  (greater than / above / more than / over) no grupo — garante que o conjunto é fechado
  e exaustivo. Sem os dois extremos o grupo é descartado silenciosamente.
  Ex: box office entre $0–10M, $10–20M, acima de $20M.

**Tipo 2 — Violação de ordenação (subconjunto / hierarquia)**

- **Tipo 2a — Subconjunto lógico**: P(A) > P(A ou B) — impossível matematicamente.
  Ex: "Brazil wins" (45¢) > "Brazil or Argentina wins" (38¢).

- **Tipo 2b — Threshold ordering**: P(above N_alto) > P(above N_baixo) — barra mais
  alta não pode ser mais provável que barra mais baixa.
  Ex: P(above 60B) = 65¢ > P(above 50B) = 60¢ → violação.

- **Tipo 2c — Date ordering**: P(by data_anterior) > P(by data_posterior) — prazo
  menor não pode custar mais que prazo maior.
  Ex: P(by January 2026) = 50¢ > P(by June 2026) = 25¢ → violação.

**Tipo 3 — Implicação direta**

Evento A implica B logicamente, mas P(A) > P(B).
Ex: "win championship" (45¢) > "qualify for semifinals" (35¢) — impossível.
Só sinaliza se fonte e critério de resolução forem exatamente os mesmos.

---

## Filtro de qualidade de regras de resolução

Além de verificar fonte idêntica entre mercados do grupo, o bot avalia a qualidade
das regras de resolução via `modules/rule_validator.py`:

- **HIGH**: fonte confiável (Reuters, IMF, oficial) + mecanismo de fallback + cobertura
  exaustiva de outcomes. Passa automaticamente.
- **MEDIUM**: ao menos um sinal positivo (fonte ou fallback). Passa automaticamente
  se `ANTHROPIC_API_KEY` não configurada; caso contrário, Claude Haiku valida.
- **LOW**: regras vagas, fonte desconhecida, sem fallback. Descartado silenciosamente.

Claude Haiku (`claude-haiku-4-5-20251001`) é chamado apenas para sinais MEDIUM/LOW,
com resposta JSON estruturada. Resultado cacheado por `condition_id` — nenhum mercado
é enviado duas vezes à API.

Requer `ANTHROPIC_API_KEY` no `.env` para validação LLM. Sem a chave, usa apenas
a checagem por palavras-chave.

---

## Regra de mitigação — OBRIGATÓRIA E BLOQUEANTE

O bot só emite sinal se TODOS os checks passarem:
1. Buscar regras de resolução de cada mercado via API (`/markets/{id}`)
2. Verificar se a fonte é idêntica (mesmo oráculo/organização)
3. Verificar se o critério é compatível (não apenas semanticamente parecido)
4. Avaliar qualidade das regras (HIGH ou MEDIUM) — LOW descarta silenciosamente
5. Se qualquer check falhar → descartar silenciosamente (sem log)

---

## Filtros de entrada

Spread líquido mínimo: 2% base + 0.5% por mercado adicional · Liquidez mínima: $100
Resolução em até 30 dias · Capital por operação: 10% do total ($50)
Prioridade: Tipo 1 > Tipo 2 > Tipo 3 · Fee: só mercados com `taker_base_fee = 0`

O spread gravado em signals.csv é o **retorno real sobre o capital investido**:
- UNDERSUM: `gap / (1 - gap)`
- OVERSUM: `gap / (N - 1 - gap)`

---

## Capital e risco

| Destino              | %   | Valor  |
|----------------------|-----|--------|
| Scanner combinatório | 90% | $450   |
| Reserva líquida      | 10% | $50    |

Stop-loss mensal: perda > 5% do capital total → pausar tudo e revisar.
Reserva intocável. Dry-run obrigatório na primeira semana.

---

## Tracking de operações

Dois arquivos em `data/trades/`:

**signals.csv** — gravado no momento em que o sinal é emitido (entrada hipotética).
Colunas: timestamp, module, market_id, question, url, side, entry_price, edge,
size_usd, shares, closes_at, n_markets.

**resolved.csv** — gravado quando o mercado fecha e resolve.
Colunas: market_id, question, module, side, entry_price, size_usd, shares,
resolution (yes/no), pnl_usd, resolved_at.

`check_resolutions()` roda a cada 6 horas. Não precisa estar ativo no fechamento.

---

## Notificações Telegram

Toda vez que um sinal é gravado em signals.csv, o bot envia automaticamente uma
mensagem no Telegram via `modules/notifier.py`. Configurado via `.env`:
`TELEGRAM_BOT_TOKEN` e `TELEGRAM_CHAT_ID`. Funciona para todos os módulos (A, B1, B2).

---

## Infraestrutura

Bot rodando em VPS DigitalOcean (159.89.232.170) como serviço systemd:
- Serviço: `polymarket-bot` — inicia automaticamente no boot, reinicia em falhas
- Código versionado em repositório privado: github.com/GLeon10/polymarket-bot
- Para atualizar após mudança no código:
  ```bash
  cd /root/polymarket_bot && git pull && systemctl restart polymarket-bot
  ```
- `.env` criado manualmente no servidor (não versionado por segurança)

---

## Estratégias B (ativas para teste)

Todas as estratégias B rodam em paralelo com A desde o início, sem gatilho de lucro.
Capital de B ainda não alocado do principal — operam em dry-run para validação.

### B1 — Weather Oracle
Compara previsão Open-Meteo com preço implícito de mercados de temperatura.
Divergência > 20% + preço ≤ 40¢ + liquidez > $100 + resolução ≤ 24h → entrada.
Cidades monitoradas: New York, London, Seoul, Hong Kong, Madrid. Ciclo: 1h.

### B2 — Esports Oracle (LoL + Dota 2)
Compara win-rate histórico (LoL Esports API / Stratz) com preço implícito.
Divergência > 15% + preço ≤ 40¢ + liquidez > $100 + partida em ≤ 4h → entrada.
Ligas LoL: LCK, LCS, LEC, LPL, CBLOL. Dota 2 via Stratz GraphQL. Ciclo: 30min.

### B3 — Politics / Elections Oracle
Detecta inconsistências lógicas entre mercados eleitorais:
- **Tipo 4a** — soma de candidatos ≠ 1 (UNDERSUM/OVERSUM)
- **Tipo 4b** — ordenação de datas: P(by earlier) > P(by later)
- **Tipo 4c** — P(candidato) > P(partido) — impossível matematicamente

Guarda extra: rejeita grupos com critérios de resolução divergentes (popular vote
vs. electoral college detectado nas regras do mercado).
Fee ≤ 1% · spread ≥ 2.5% · liquidez > $100 · resolução ≤ 90 dias. Ciclo: 15min.

### B4 — Sports Live-Game Oracle (NBA / NFL / MLB / NHL)
Detecta violação: P(win by N+ points) > P(win outright) — matematicamente impossível.
Exemplo: P(Lakers +5) = 62¢ > P(Lakers win) = 55¢ → comprar YES ML + NO spread.

Modo dinâmico:
- **Standby** (padrão): sem jogos ao vivo → ciclo de 30min.
- **Ativo**: ao detectar jogo com resolução < 4h → ciclo cai para 60s.
- **Bloqueio**: sem entrada nos últimos 5 min do jogo.

Fee ≤ 0.75% · spread ≥ 2% · liquidez > $500.

---

## Output do log

Sinais descartados: silêncio total (sem log).
Sinais válidos: bloco compacto por sinal + linha de resumo por ciclo.

```
>> SINAL A | Tipo1 RANGE | spread=17.25% | P&L=$8.63 | liq=$450
   YES=0.207 | Will there be between 10 and 20... [0xaa9e788]
   YES=0.185 | Will there be between 20 and 30... [0xbb1c234]
   Resolucao: Fonte confirmada: official results | Qualidade: HIGH
[A] 377 mercados | 181 candidatos | 1 sinal(is) valido(s)
```

---

## Estrutura de arquivos

```
polymarket_bot/
├── config/config.py          # Capital $500, filtros, fase ativa
├── modules/
│   ├── scanner_a.py          # Scanner combinatório — 6 detectores
│   ├── scanner_corr.py       # Scanner CORR — inconsistências cross-market
│   ├── tracker.py            # Grava sinais e resoluções em CSV
│   ├── notifier.py           # Notificações Telegram
│   ├── rule_validator.py     # Qualidade de regras (keywords + Claude Haiku)
│   ├── phase_manager.py      # Controle de fases (A → B)
│   ├── clob_utils.py         # Utilitários CLOB API + Gamma API
│   ├── oracle_b1.py          # Weather oracle
│   ├── oracle_b2.py          # Esports oracle (LoL + Dota 2)
│   ├── oracle_b3.py          # Politics / Elections oracle
│   └── oracle_b4.py          # Sports live-game oracle (NBA/NFL/MLB/NHL)
├── data/
│   ├── logs/                 # Logs diários
│   ├── trades/
│   │   ├── signals.csv       # Entradas hipotéticas
│   │   └── resolved.csv      # Resultados por mercado fechado
│   └── snapshots/
├── tests/                    # 178 testes (pytest)
├── docs/BRIEFING.md
├── main.py
└── simulate.py               # Executa um ciclo único para teste manual
```

---

## APIs e notas técnicas

Polymarket CLOB API: `https://clob.polymarket.com`
Gamma API: `https://gamma-api.polymarket.com` (usado para resolver event slug das URLs)
Endpoints: `/sampling-markets` (scan), `/markets/{id}` (regras), `/markets?slug=` (URL)
SDK: polymarket-clob-client (Python) · Token: USDC.e na Polygon
Fee: apenas mercados com `taker_base_fee = 0` · Gas: subsidiado pelo relayer
Ciclo do scanner: 10 minutos · Ciclo do tracker: 6 horas
Open-Meteo (B1, sem chave) · Liquipedia REST (B2, sem chave)

---

## Estado atual

- [x] config/config.py
- [x] modules/scanner_a.py (6 detectores + verificação de regras + qualidade)
- [x] modules/rule_validator.py (keywords + Claude Haiku + cache)
- [x] modules/tracker.py (signals.csv + resolved.csv + notificação Telegram)
- [x] modules/notifier.py (Telegram)
- [x] main.py (dry-run, logging, loops de scanner/tracker/phase)
- [x] VPS DigitalOcean configurada e rodando (systemd)
- [x] Repositório privado GitHub (GLeon10/polymarket-bot)
- [x] modules/oracle_b3.py (Politics — 3 detectores + filtro de critério de voto)
- [x] modules/oracle_b4.py (Sports live-game — ML vs spread, modo standby/ativo)
- [ ] Validar 1 semana em dry-run → avaliar sinais encontrados (A, B1, B2, B3, B4)
- [ ] Após lucro hipotético confirmado → ativar execução real
