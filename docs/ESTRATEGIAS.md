# Estratégias: o que está ligado, o que não está, e por quê

Este documento registra as decisões de quais estratégias operam e quais ficam
desligadas. Cada decisão tem um motivo e um critério objetivo para ser revista.

---

## Ligadas

### A — Arbitragem combinatória

Detecta inconsistências lógicas entre mercados relacionados e monta uma cesta
que paga mais do que custa em **qualquer** resultado.

| Padrão | Trade | Paga no pior caso |
|---|---|---|
| OVERSUM (soma > 100%) | NO em todos | n−1 shares |
| UNDERSUM (soma < 100%) | YES em todos | 1 share |
| SUBSET: P(A) > P(A ou B) | YES no "A ou B" + NO em A | 1 share |
| THRESHOLD: P(>alto) > P(>baixo) | YES no baixo + NO no alto | 1 share |
| DATA: P(até cedo) > P(até tarde) | NO no cedo + YES no tarde | 1 share |
| IMPLICAÇÃO: P(título) > P(final) | NO no título + YES na final | 1 share |

O retorno é sempre `(payout_mínimo − custo) / custo`, com o custo calculado em
preço de execução (VWAP para o tamanho pretendido) e a taxa própria de cada
mercado.

**UNDERSUM sai desligado** (`A_ALLOW_UNDERSUM_WITHOUT_COVERAGE=false`).
Comprar YES em todas as opções só paga se alguma vencer, e nada garante que a
lista do Polymarket seja exaustiva. Para faixas numéricas a exaustividade é
verificada de fato — os limites são parseados e o conjunto precisa cobrir
−∞..+∞ sem buracos nem sobreposição —, então esse caminho fica liberado. Para
candidatos nomeados não há como verificar.

> O pior falso positivo do histórico veio daí: quatro faixas de trânsitos no
> Estreito de Ormuz (0-10, 10-20, 20-40, 40-60) somando 82,7%, registradas como
> "spread de 17,2%". Os 17% eram o preço da cauda ">60" que a cesta estaria
> vendendo. O detector de faixas hoje rejeita esse conjunto por falta de
> extremo superior.

### B3 — Política

Mesmos padrões do A aplicados a mercados eleitorais, com a mesma regra sobre
UNDERSUM.

### B4 — Esportes ao vivo

`P(vencer por N+) > P(vencer)` é impossível. A cesta YES-moneyline +
NO-spread paga no mínimo 1 share nos três cenários:

```
vence por margem  → ML 1 + NO-spread 0 = 1
vence sem margem  → ML 1 + NO-spread 1 = 2
perde             → ML 0 + NO-spread 1 = 1
```

É o arb mais limpo do conjunto. A dificuldade é operacional, não lógica:
janela curta e necessidade de preencher as duas pernas.

### B5 — Cripto 5 minutos (ARB)

YES + NO do mesmo mercado paga exatamente $1. Há lucro quando o custo all-in
fica abaixo disso com folga de `B5_MIN_NET_EDGE` (1,5%).

Duas correções mudaram a economia desta estratégia:

- **Taxa.** A fórmula era `rate × p × (1−p)`, que cobra metade do custo real
  em p ≈ 0,50 — justamente onde esta estratégia opera. Um par a 0,46/0,47
  aparecia com "edge de 3,4%" quando o líquido verdadeiro era **0,31%**.
- **Profundidade.** O gate usava só o melhor ask, que pode ter uma share
  atrás. Agora o preço é o VWAP para o tamanho e os dois lados precisam ter
  `B5_MIN_DEPTH_USD` de livro.

### B1 / B2 — Oráculos direcionais

Entram só na Fase 2, com a banca formada pelo lucro do A. B1 teve dois
consertos relevantes: a unidade de temperatura (Open-Meteo devolve Celsius, os
mercados americanos são cotados em Fahrenheit) e o preço registrado, que era
sempre o do YES mesmo em sinais de NO.

### CORR — Correlações

Alerta, e opera apenas no padrão OVERSUM, onde os lados de cada perna são
deriváveis. O padrão SUBSET fica como alerta: o detector não registra qual
mercado é o subconjunto, então montar a cesta exigiria chutar o lado de cada
perna — e chutar aqui produz posição direcional disfarçada de arbitragem.

---

## Desligadas

### B5 NEAR_RES — `B5_NEAR_RES_ENABLED=false`

Comprar o lado vencedor entre 0,96 e 0,995 nos últimos segundos do candle, por
0,5% de edge.

O problema não é de implementação, é de expectativa. **O preço já é a
estimativa de probabilidade do mercado.** Comprar a 0,98 exige acertar mais de
98% das vezes só para empatar — e ainda se paga o ask (acima do meio) e a
taxa. O "edge de 0,5%" é, na prática, a afirmação de que o mercado está certo.

A distribuição fecha o caso:

```
ganha  +2%  em ~98% das vezes
perde  −98% em  ~2% das vezes
```

Uma perda apaga cerca de cinquenta ganhos. Pior: as perdas chegam
correlacionadas — um movimento brusco no fechamento atinge BTC, ETH e SOL ao
mesmo tempo. Com $25 por operação numa banca de $500, cada perda é 5% do
capital. Kelly nesse jogo dá zero; qualquer tamanho positivo é aposta perdedora.

**Para religar:** seria preciso demonstrar que o preço nessa faixa é
*sistematicamente* baixo demais — ou seja, que o mercado erra numa direção
específica. Isso é uma afirmação empírica, e precisa de P&L resolvido, não de
intuição.

### B5 REPRICING — `B5_REPRICING_ENABLED=false`

Fair value via Binance contra o preço do Polymarket.

O modelo tinha um erro que apontava para o lado errado. `fair_prob` era
`Φ(pct_change / vol)`, com `vol` medida em candles **inteiros** mas aplicada a
um movimento **parcial**. No meio do candle só resta parte da variância, então
o denominador correto é `vol × √(tempo_restante / duração)`. Usar a vol cheia
deixava o |z| pequeno demais e empurrava `fair_prob` na direção de 0,50.

O efeito prático: o bot olhava o lado que estava ganhando, achava-o "caro", e
comprava o lado perdedor — com um "edge" que *crescia* conforme o candle
avançava e o mercado ficava mais certo.

Isso foi corrigido, junto com dois problemas menores: a abertura do candle
agora só é usada quando confiável (conectar no meio do candle registrava o
preço do momento como se fosse o strike), e o detector não opera nos últimos
segundos, onde a variância restante tende a zero e o modelo explode.

Mesmo corrigida, a estratégia continua competindo em latência com HFT e
inferindo o strike em vez de lê-lo do enunciado.

**Para religar:** acumular P&L resolvido positivo em dry-run com o modelo
corrigido, e implementar a leitura do strike a partir da pergunta.

### B5 Pro (módulo inteiro) — `STRATEGY_B5_PRO_ENABLED=false`

O ARB do B5 Pro é o mesmo trade do módulo B5, nos mesmos mercados. Rodando os
dois em paralelo a exposição dobrava na mesma oportunidade, e o dedup do
tracker fazia um sinal engolir o outro no CSV. Escolha um. O B5 é mais
simples; o B5 Pro só faz sentido quando NEAR_RES ou REPRICING voltarem.

---

## Thresholds: por que voltaram aos valores originais

Dois commits afrouxaram as margens, ambos justificados por contagem de sinais
e não por resultado:

| Parâmetro | Original | 94cd668 | 7058ef9 | Agora |
|---|---|---|---|---|
| `B1_MIN_DIVERGENCE` | 20% | 12% | 8% | **20%** |
| `B2_MIN_DIVERGENCE` | 15% | — | 9% | **15%** |
| `B4_MIN_LIQUIDITY` | $500 | $150 | — | **$500** |
| `A_MAX_FEE` | 1% | 2,5% | — | **1%** |
| `A_MAX_RESOLUTION_DAYS` | 30 | 60 | 90 | **30** |

As mensagens diziam "unblock detections" e "broader signal coverage". O
problema não é afrouxar — é afrouxar contra um P&L que, por construção, nunca
registrava prejuízo em arbitragem. O loop de feedback estava quebrado dos dois
lados.

Com a contabilidade por pernas, afrouxar passa a ser uma decisão verificável:
se o edge realizado acompanhar o prometido em N operações resolvidas, há
argumento. Antes disso, não.
