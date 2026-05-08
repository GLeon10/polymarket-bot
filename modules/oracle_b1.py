"""
Módulo B1 — Weather Oracle
Compara previsão Open-Meteo com preço implícito do mercado de temperatura no Polymarket.
Divergência > 20% + preço ≤ 40¢ + liquidez > $100 + resolução ≤ 24h → entrada.
"""

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

from config import config
from modules import clob_utils, tracker

logger = logging.getLogger("oracle_b1")


@dataclass
class WeatherSignal:
    market_id: str
    question: str
    city: str
    model_prob: float
    market_price: float
    divergence: float
    side: str           # "Yes" ou "No"
    liquidity: float
    found_at: datetime
    end_date: str | None = None
    market_slug: str | None = None


def _fetch_forecast(lat: float, lon: float) -> dict | None:
    try:
        resp = requests.get(
            config.OPEN_METEO_URL,
            params={
                "latitude":      lat,
                "longitude":     lon,
                "daily":         "temperature_2m_max,temperature_2m_min",
                "forecast_days": 1,
                "timezone":      "auto",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except requests.RequestException as e:
        logger.warning("Falha Open-Meteo (lat=%.4f lon=%.4f): %s", lat, lon, e)
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


def _model_probability(forecast: dict, question: str) -> float | None:
    """
    Probabilidade binária baseada na previsão vs. limiar extraído da pergunta.
    Margem de ±2°C para estimar probabilidade — suficiente para divergência > 20%.
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
    diff      = temp_max - threshold

    if any(k in direction for k in ("exceed", "above", "over", "higher")):
        if diff > 2:   return 0.85
        if diff > 0:   return 0.60
        if diff > -2:  return 0.40
        return 0.15
    else:
        if diff < -2:  return 0.85
        if diff < 0:   return 0.60
        if diff < 2:   return 0.40
        return 0.15


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

        if city not in forecasts:
            coords          = config.B1_CITIES[city]
            forecasts[city] = _fetch_forecast(coords["lat"], coords["lon"])

        forecast = forecasts.get(city)
        if not forecast:
            continue

        model_prob = _model_probability(forecast, question)
        if model_prob is None:
            continue

        yes_token = clob_utils.token_by_outcome(market, "Yes")
        if not yes_token:
            continue

        market_price = float(yes_token.get("price", 0))
        if market_price <= 0:
            continue

        divergence = abs(model_prob - market_price)

        if (
            divergence   >= config.B1_MIN_DIVERGENCE
            and market_price <= config.B1_MAX_SHARE_PRICE
        ):
            liquidity = clob_utils.get_orderbook_liquidity(yes_token["token_id"])
            if liquidity < config.B1_MIN_LIQUIDITY:
                continue

            side   = "Yes" if model_prob > market_price else "No"
            signal = WeatherSignal(
                market_id    = market.get("condition_id", ""),
                question     = question,
                city         = city,
                model_prob   = model_prob,
                market_price = market_price,
                divergence   = divergence,
                side         = side,
                liquidity    = liquidity,
                found_at     = datetime.utcnow(),
                end_date     = market.get("end_date_iso") or market.get("endDate"),
                market_slug  = market.get("market_slug"),
            )
            signals.append(signal)
            logger.info(
                "SINAL B1 | %s | %s | model=%.2f market=%.2f div=%.1f%%",
                city, question[:50], model_prob, market_price, divergence * 100,
            )

    logger.info("Varredura B1: %d mercados weather, %d sinais", len(weather_markets), len(signals))
    return signals


def execute(signal: WeatherSignal, client) -> bool:
    if config.MODE == "dry_run":
        logger.info(
            "[DRY-RUN] B1: %s | side=%s price=%.3f div=%.1f%%",
            signal.question[:60], signal.side, signal.market_price, signal.divergence * 100,
        )
        tracker.record_signal(
            "B1", signal.market_id, signal.question,
            signal.side, signal.market_price, signal.divergence,
            closes_at_iso=signal.end_date,
            market_slug=signal.market_slug,
        )
        return True
    logger.warning("Execução live não implementada: %s", signal.market_id)
    return False
