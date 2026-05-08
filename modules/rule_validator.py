"""
Validador de qualidade das regras de resolução para sinais de arbitragem.

Classifica regras de mercado em HIGH / MEDIUM / LOW via:
  1. Verificação rápida por palavras-chave
  2. Claude Haiku API para sinais não-HIGH (cache por condition_id)

Qualidade final determina se o sinal é emitido:
  HIGH   → emite automaticamente
  MEDIUM → emite (keyword confirmou fonte ou fallback)
  LOW    → descartado silenciosamente
"""

import json
import logging
import re

import requests

from config import config

logger = logging.getLogger("rule_validator")

# Cache permanente: condition_id → {"quality": str, "reason": str}
_cache: dict[str, dict] = {}

_FALLBACK_KEYWORDS = [
    "if no data", "if data is unavailable", "most recent available",
    "fallback", "in the absence of", "if the source is unavailable",
    "if the primary source", "if no official",
]

_RELIABLE_SOURCES = [
    "imf", "reuters", "associated press", "ap news", "bbc",
    "official government", "official results", "official website",
    "new york times", "espn", "fifa", "ioc", "world bank",
    "world health organization", " who ", "united nations",
    "portwatch", "imf portwatch",
]

_RANGE_LOWER = re.compile(r"\b(less than|below|under|lower than)\b", re.IGNORECASE)
_RANGE_UPPER = re.compile(r"\b(greater than|above|over|higher than|exceed)\b", re.IGNORECASE)


def _keyword_quality(text: str) -> str:
    lower = text.lower()
    has_fallback        = any(kw in lower for kw in _FALLBACK_KEYWORDS)
    has_reliable_source = any(src in lower for src in _RELIABLE_SOURCES)
    has_range_coverage  = bool(_RANGE_LOWER.search(lower) and _RANGE_UPPER.search(lower))
    score = sum([has_fallback, has_reliable_source, has_range_coverage])
    if score >= 2:
        return "HIGH"
    if score == 1:
        return "MEDIUM"
    return "LOW"


def _llm_validate(rules_text: str, condition_id: str) -> dict:
    """Chama Claude Haiku para avaliar qualidade das regras de resolução."""
    prompt = (
        "You are evaluating the resolution rules of a Polymarket prediction market.\n\n"
        f'Resolution rules:\n"""\n{rules_text[:2000]}\n"""\n\n'
        "Respond ONLY with a JSON object (no other text):\n"
        "{\n"
        '  "exhaustive": <bool: all outcomes covered including edge cases>,\n'
        '  "has_fallback": <bool: fallback mechanism when primary data unavailable>,\n'
        '  "edge_cases_handled": <bool: ties, cancellations, delays addressed>,\n'
        '  "source_reliable": <bool: well-known authoritative source>,\n'
        '  "quality": <"HIGH" | "MEDIUM" | "LOW">,\n'
        '  "reason": <one sentence>\n'
        "}\n\n"
        "Quality guidelines:\n"
        "- HIGH: reliable source + fallback + exhaustive outcome coverage\n"
        "- MEDIUM: reliable source OR fallback, but not both\n"
        "- LOW: vague rules, unreliable source, or no fallback mechanism"
    )
    try:
        resp = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": config.ANTHROPIC_API_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 300,
                "messages": [{"role": "user", "content": prompt}],
            },
            timeout=20,
        )
        resp.raise_for_status()
        content = resp.json()["content"][0]["text"].strip()
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            m = re.search(r"\{.*\}", content, re.DOTALL)
            if m:
                return json.loads(m.group(0))
            raise
    except (requests.RequestException, json.JSONDecodeError, KeyError, IndexError) as e:
        logger.warning("LLM validation failed for %s: %s", condition_id[:16], e)
        return {"quality": "LOW", "reason": f"falha na validação LLM: {e}"}


def assess(rules_text: str, condition_id: str) -> tuple[str, str]:
    """
    Retorna (quality, reason) para as regras do mercado.

    HIGH   → fonte confiável + fallback + cobertura exaustiva
    MEDIUM → ao menos um sinal positivo (keyword ou LLM)
    LOW    → regras vagas, fonte desconhecida, sem fallback
    """
    if condition_id in _cache:
        cached = _cache[condition_id]
        return cached["quality"], cached.get("reason", "")

    kw_quality = _keyword_quality(rules_text)

    if kw_quality == "HIGH":
        result: dict = {
            "quality": "HIGH",
            "reason": "fonte confiável, fallback e cobertura exaustiva confirmados por keywords",
        }
    elif not config.ANTHROPIC_API_KEY:
        # Sem chave LLM: usa resultado das keywords diretamente
        logger.debug(
            "ANTHROPIC_API_KEY ausente — usando qualidade por keywords (%s) para %s",
            kw_quality, condition_id[:16],
        )
        result = {
            "quality": kw_quality,
            "reason": "validação por keywords (ANTHROPIC_API_KEY não configurada)",
        }
    else:
        result = _llm_validate(rules_text, condition_id)
        if "quality" not in result:
            result["quality"] = "LOW"
        if "reason" not in result:
            result["reason"] = ""

    _cache[condition_id] = result
    logger.debug(
        "RULE_QUALITY | %s | kw=%s final=%s | %s",
        condition_id[:16], kw_quality, result["quality"], result.get("reason", "")[:60],
    )
    return result["quality"], result.get("reason", "")


def clear_cache() -> None:
    """Limpa o cache de avaliações (útil em testes)."""
    _cache.clear()
