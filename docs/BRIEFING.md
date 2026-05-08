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
  "greater than X" com o mesmo prefixo. Requer mínimo de 3 faixas no grupo para
  confirmar exaustividade. Ex: box office entre $0–10M, $10–20M, acima de $20M.

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

## Regra de mitigação — OBRIGATÓRIA E BLOQUEANTE

O bot só emite sinal se TODOS os checks passarem:
1. Buscar regras de resolução de cada mercado via API (`/markets/{id}`)
2. Verificar se a fonte é idêntica (mesmo oráculo/organização)
3. Verificar se o critério é compatível (não apenas semanticamente parecido)
4. Se qualquer check falhar → descartar silenciosamente (sem log)
5. Só emitir sinal se todos passarem

---

## Filtros de entrada

Spread líquido mínimo: 2% · Liquidez mínima por lado: $100 · Resolução em até 30 dias
Capital por operação: 10% do total ($50) · Prioridade: Tipo 1 > Tipo 2 > Tipo 3
Fee geopolítica: só mercados com `taker_base_fee = 0` são considerados

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
size_usd, shares, closes_at.

**resolved.csv** — gravado quando o mercado fecha e resolve.
Colunas: market_id, question, module, side, entry_price, size_usd, shares,
resolution (yes/no), pnl_usd, resolved_at.

`check_resolutions()` roda a cada 6 horas enquanto o bot está ativo. Se o bot estiver
fechado, basta rodar após a data de resolução — a função consulta a API no momento
em que executa e registra todos os mercados já resolvidos desde a última verificação.
O bot não precisa estar ativo no exato momento do fechamento do mercado.

---

## Estratégias B (fase futura)

Inativas. Gatilho: $80–100 lucro líquido acumulado com scanner principal.
Lucro vira banca separada — capital original nunca exposto a B.
B1 weather oracle ativa primeiro. B2 esports oracle após B1 validado.

---

## Output do log

Sinais descartados: silêncio total (sem log).
Sinais válidos: bloco compacto por sinal + linha de resumo por ciclo.

```
>> SINAL A | Tipo1 RANGE | spread=17.25% | P&L=$8.63 | liq=$450
   YES=0.207 | Will there be between 10 and 20... [0xaa9e788]
   YES=0.185 | Will there be between 20 and 30... [0xbb1c234]
   Resolucao: Fonte confirmada: official results
[A] 377 mercados | 181 candidatos | 1 sinal(is) valido(s)
```

---

## Estrutura de arquivos

```
polymarket_bot/
├── config/config.py          # Capital $500, filtros, fase ativa
├── modules/
│   ├── scanner_a.py          # Scanner combinatório — 6 detectores
│   ├── scanner_corr.py       # Scanner CORR — inconsistências cross-market (sinaliza, não executa)
│   ├── tracker.py            # Grava sinais e resoluções em CSV
│   ├── phase_manager.py      # Controle de fases (A → B)
│   ├── clob_utils.py         # Utilitários de acesso à CLOB API
│   ├── oracle_b1.py          # Fase futura: weather oracle
│   └── oracle_b2.py          # Fase futura: esports oracle
├── data/
│   ├── logs/                 # Logs diários
│   ├── trades/
│   │   ├── signals.csv       # Entradas hipotéticas
│   │   └── resolved.csv      # Resultados por mercado fechado
│   └── snapshots/
├── tests/
├── docs/BRIEFING.md
├── main.py
└── simulate.py               # Executa um ciclo único para teste manual
```

---

## APIs e notas técnicas

Polymarket CLOB API: `https://clob.polymarket.com`
Endpoints usados: `/sampling-markets` (scan), `/markets/{id}` (regras de resolução)
SDK: polymarket-clob-client (Python) · Token: USDC.e na Polygon
Fee: apenas mercados com `taker_base_fee = 0` · Gas: subsidiado pelo relayer
Ciclo do scanner: 10 minutos · Ciclo do tracker: 6 horas
Open-Meteo (B1, sem chave) · Liquipedia REST (B2, sem chave)

---

## Estado atual

- [x] config/config.py
- [x] modules/scanner_a.py (6 detectores + verificação de regras)
- [x] modules/tracker.py (signals.csv + resolved.csv)
- [x] main.py (dry-run, logging, loops de scanner/tracker/phase)
- [x] simulate.py (ciclo único para teste)
- [ ] Validar 1 semana em dry-run → avaliar sinais encontrados
- [ ] Após $80–100 lucro hipotético confirmado → ativar execução real
- [ ] Após execução real validada → oracle_b1.py → oracle_b2.py
