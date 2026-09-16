"""
Módulo B1 — Weather Oracle
Compara previsão Open-Meteo com preço implícito do mercado de temperatura no Polymarket.
Divergência > 20% + preço ≤ 40¢ + liquidez > $100 + resolução ≤ 24h → entrada.
"""

import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from config import config
from modules import book, clob_utils, executor, fees, tracker

logger = logging.getLogger("oracle_b1")


@dataclass
class WeatherSignal:
    market_id: str
    question: str
    city: str
    model_prob: float
    market_price: float
    divergence: float
    side: str           # "Yes" ou "No" — o lado efetivamente comprado
    liquidity: float
    found_at: datetime
    end_date: str | None = None
    market_slug: str | None = None
    # Preço do LADO COMPRADO. Antes, o tracker recebia sempre o preço do YES,
    # mesmo quando o sinal era de NO: uma compra a 0.75 era registrada como
    # 0.25, inflando shares e P&L em 3x.
    entry_price: float = 0.0
    token_id: str = ""
    unit: str = "celsius"


# Cobre "75F", "75 °F", "75 degrees F" e "fahrenheit" por extenso. O sufixo
# colado no número é a forma mais comum nos enunciados do Polymarket.
_UNIT_F_RE = re.compile(r"(\d\s*°?\s*F\b|°\s*F|\bfahrenheit\b|\bdeg(?:rees)?\s*F\b)", re.IGNORECASE)
_UNIT_C_RE = re.compile(r"(\d\s*°?\s*C\b|°\s*C|\bcelsius\b|\bcentigrade\b|\bdeg(?:rees)?\s*C\b)", re.IGNORECASE)


def _question_unit(question: str, city: str | None) -> str | None:
    """
    Unidade em que o mercado está cotado: "fahrenheit" | "celsius" | None.

    Isto existe porque o Open-Meteo devolve Celsius por padrão e os mercados de
    cidades americanas são cotados em Fahrenheit. A versão anterior comparava
    um forecast de 22 (°C) com um limiar de 75 (°F) e concluía que o mercado
    estava 40% errado — sinal máximo de confiança, direção garantidamente
    errada, em todo mercado de Nova York.

    Primeiro tenta ler do enunciado; só cai no mapa de cidades se a pergunta
    não disser. Se nada resolver e B1_REQUIRE_KNOWN_UNIT estiver ligado, o
    chamador descarta o mercado em vez de chutar.
    """
    if _UNIT_F_RE.search(question):
        return "fahrenheit"
    if _UNIT_C_RE.search(question):
        return "celsius"
    if city and city in config.B1_FAHRENHEIT_CITIES:
        return "fahrenheit"
    if city and not config.B1_REQUIRE_KNOWN_UNIT:
        return "celsius"
    return None


def _fetch_forecast(lat: float, lon: float, unit: str = "celsius") -> dict | None:
    try:
        resp = requests.get(
            config.OPEN_METEO_URL,
            params={
                "latitude":         lat,
                "longitude":        lon,
                "daily":            "temperature_2m_max,temperature_2m_min",
                "forecast_days":    1,
                "timezone":         "auto",
                "temperature_unit": unit,
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.warning("Falha Open-Meteo (lat=%.4f lon=%.4f unit=%s): %s", lat, lon, unit, e)
        return None


def _resolution_within_hours(market: dict, hours: int) -> bool:
    end = market.get("end_date_iso") or market.get("endDate")
    if not end:
        return False
    try:
        end_dt  = datetime.fromisoformat(end.replace("Z", "+00:00"))
        now     = datetime.now(timezone.utc)
        delta_h = (end_dt - now).total_seconds() / 3600
        return 0 < delta_h <= hours
    except ValueError:
        return False


def _city_from_question(question: str) -> str | None:
    q = question.lower()
    for city in config.B1_CITIES:
        if city.lower() in q:
            return city
    return None


def _model_probability(forecast: dict, question: str,
                       unit: str = "celsius") -> float | None:
    """
    Probabilidade de o limiar da pergunta ser batido, dada a previsão.

    A versão anterior era uma função degrau de quatro constantes fixas
    (0.85/0.60/0.40/0.15). Divergir 8% de uma constante arbitrária não é edge —
    é a distância até um número inventado. Aqui a incerteza da previsão entra
    explicitamente: o erro do forecast de máxima em 24h é tratado como normal
    com desvio B1_FORECAST_SIGMA_C, e a probabilidade é a normal acumulada.

        P(max > limiar) = Φ((previsão − limiar) / σ)

    Com σ = 2°C, uma previsão 2° acima do limiar dá ~84%, na mesma — mas agora
    a curva é contínua, calibrável e o número tem significado.
    """
    daily    = forecast.get("daily", {})
    temp_max = (daily.get("temperature_2m_max") or [None])[0]
    if temp_max is None:
        return None

    match = re.search(
        r"(exceed|above|over|below|under|higher than|lower than)\s+(-?\d+(?:\.\d+)?)",
        question,
        re.IGNORECASE,
    )
    if not match:
        return None

    direction = match.group(1).lower()
    threshold = float(match.group(2))

    sigma = config.B1_FORECAST_SIGMA_C
    if unit == "fahrenheit":
        sigma *= 1.8
    if sigma <= 0:
        return None

    z = (temp_max - threshold) / sigma
    p_above = 0.5 * (1.0 + math.erf(z / math.sqrt(2.0)))

    # Trava nas pontas: nenhuma previsão meteorológica justifica 0% ou 100%.
    # Sem isso o modelo declararia certeza absoluta a 3 desvios e produziria
    # "divergências" de 99% contra qualquer preço de mercado.
    p_above = min(max(p_above, config.B1_PROB_FLOOR), 1.0 - config.B1_PROB_FLOOR)

    if any(k in direction for k in ("exceed", "above", "over", "higher")):
        return round(p_above, 4)
    return round(1.0 - p_above, 4)


def scan() -> list[WeatherSignal]:
    all_markets     = clob_utils.fetch_sampling_markets()
    weather_markets = [m for m in all_markets if clob_utils.has_tag(m, config.B1_TARGET_TAGS)]
    logger.info("Mercados weather: %d / %d totais", len(weather_markets), len(all_markets))

    signals: list[WeatherSignal] = []
    forecasts: dict[str, dict]   = {}

    for market in weather_markets:
        if not _resolution_within_hours(market, config.B1_MAX_RESOLUTION_H):
            continue

        question = market.get("question", "")
        city     = _city_from_question(question)
        if not city:
            continue

        unit = _question_unit(question, city)
        if unit is None:
            logger.debug("B1: unidade indeterminada, mercado ignorado — %s", question[:60])
            continue

        cache_key = f"{city}|{unit}"
        if cache_key not in forecasts:
            coords = config.B1_CITIES[city]
            forecasts[cache_key] = _fetch_forecast(coords["lat"], coords["lon"], unit)

        forecast = forecasts.get(cache_key)
        if not forecast:
            continue

        model_prob = _model_probability(forecast, question, unit)
        if model_prob is None:
            continue

        yes_token = clob_utils.token_by_outcome(market, "Yes")
        if not yes_token:
            continue

        market_price = float(yes_token.get("price", 0))
        if not (0 < market_price < 1):
            continue

        divergence = abs(model_prob - market_price)
        if divergence < config.B1_MIN_DIVERGENCE:
            continue

        # O lado comprado define token, preço e filtro de preço máximo.
        side  = "Yes" if model_prob > market_price else "No"
        token = yes_token if side == "Yes" else clob_utils.token_by_outcome(market, "No")
        if not token:
            continue

        budget      = tracker.trade_budget("B1")
        entry_price = book.executable_ask(token["token_id"], budget)
        if not (0 < entry_price < 1):
            logger.debug("B1: sem profundidade para $%.0f — %s", budget, question[:50])
            continue

        if entry_price > config.B1_MAX_SHARE_PRICE:
            continue

        # Edge líquido do lado comprado, já com fee: o modelo dá a probabilidade
        # do lado, o preço é o que se paga por ele.
        model_side = model_prob if side == "Yes" else 1.0 - model_prob
        net_edge   = fees.net_edge_directional(entry_price, model_side - entry_price)
        if net_edge < config.B1_MIN_DIVERGENCE:
            continue

        liquidity = clob_utils.get_orderbook_liquidity(token["token_id"])
        if liquidity < config.B1_MIN_LIQUIDITY:
            continue

        signals.append(WeatherSignal(
            market_id    = market.get("condition_id", ""),
            question     = question,
            city         = city,
            model_prob   = model_prob,
            market_price = market_price,
            divergence   = round(net_edge, 4),
            side         = side,
            liquidity    = liquidity,
            found_at     = datetime.utcnow(),
            end_date     = market.get("end_date_iso") or market.get("endDate"),
            market_slug  = market.get("market_slug"),
            entry_price  = round(entry_price, 4),
            token_id     = token["token_id"],
            unit         = unit,
        ))
        logger.info(
            "SINAL B1 | %s (%s) | %s | model=%.2f p_%s=%.3f edge_liq=%.1f%%",
            city, unit[:1].upper(), question[:50], model_prob,
            side.lower(), entry_price, net_edge * 100,
        )

    logger.info("Varredura B1: %d mercados weather, %d sinais", len(weather_markets), len(signals))
    return signals


def execute(signal: WeatherSignal, client) -> bool:
    budget = tracker.trade_budget("B1")
    legs   = tracker.build_legs(
        [(signal.market_id, signal.side, signal.entry_price, signal.token_id)], budget
    )
    if not legs:
        return False

    registered = tracker.record_signal(
        "B1", signal.market_id, signal.question,
        signal.side, signal.entry_price, signal.divergence,
        closes_at_iso=signal.end_date,
        market_slug=signal.market_slug,
        legs=legs,
    )
    if not registered:
        return False

    if executor.live_enabled("B1"):
        return executor.execute_single(client, "B1", signal.market_id, legs[0])

    logger.info(
        "[DRY-RUN] B1: %s | side=%s price=%.3f edge_liq=%.1f%%",
        signal.question[:60], signal.side, signal.entry_price, signal.divergence * 100,
    )
    return True
