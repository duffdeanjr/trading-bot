"""
agents/sentiment.py -- Real NLP sentiment scoring using a keyword-weighted
approach with context awareness. Replaces the simple keyword counter in
signal_generator.py.

Replace score_text() with a FinBERT call when you have GPU/API access.
"""

import re
import logging

logger = logging.getLogger(__name__)

# Weighted financial lexicon (positive terms)
_POSITIVE = {
    "beat": 2.0, "beats": 2.0, "exceed": 1.5, "exceeds": 1.5, "exceeded": 1.5,
    "record": 1.5, "surge": 1.5, "surges": 1.5, "rally": 1.0, "rallies": 1.0,
    "upgrade": 2.0, "upgraded": 2.0, "outperform": 2.0, "buy": 1.0,
    "growth": 1.0, "grew": 1.0, "profit": 1.0, "profits": 1.0,
    "strong": 1.0, "strength": 1.0, "positive": 1.0, "gain": 1.0, "gains": 1.0,
    "revenue": 0.5, "raise": 1.0, "raised": 1.0, "raises": 1.0,
    "dividend": 0.5, "buyback": 1.0, "repurchase": 1.0, "acquisition": 0.5,
    "partnership": 0.5, "contract": 0.5, "approve": 1.5, "approved": 1.5,
    "breakthrough": 2.0, "innovative": 0.5, "expansion": 0.5,
}

# Weighted financial lexicon (negative terms)
_NEGATIVE = {
    "miss": 2.0, "misses": 2.0, "missed": 2.0, "disappoint": 1.5,
    "drop": 1.0, "drops": 1.0, "fell": 1.0, "fall": 1.0, "falls": 1.0,
    "downgrade": 2.0, "downgraded": 2.0, "underperform": 2.0, "sell": 1.0,
    "loss": 1.5, "losses": 1.5, "decline": 1.0, "declines": 1.0,
    "weak": 1.0, "weakness": 1.0, "negative": 1.0, "cut": 1.5, "cuts": 1.5,
    "reduce": 1.0, "reduced": 1.0, "layoff": 2.0, "layoffs": 2.0,
    "lawsuit": 1.5, "investigation": 1.5, "fine": 1.0, "penalty": 1.0,
    "recall": 1.5, "breach": 1.5, "hack": 1.5, "fraud": 2.0,
    "bankruptcy": 3.0, "default": 2.0, "delisted": 2.0, "warning": 1.0,
    "concern": 0.5, "risk": 0.5, "uncertainty": 0.5, "volatile": 0.5,
}

# Negation words that flip sentiment
_NEGATORS = {"not", "no", "never", "neither", "nor", "without", "lack", "fail", "failed"}

# Intensifiers
_INTENSIFIERS = {"very", "extremely", "significantly", "substantially", "sharply",
                 "dramatically", "unexpectedly", "surprisingly"}


def score_text(text: str) -> float:
    """
    Score text sentiment. Returns float in [-1.0, +1.0].
    Positive = bullish, negative = bearish, 0 = neutral.
    """
    if not text:
        return 0.0

    text_lower = text.lower()
    words = re.findall(r'\b\w+\b', text_lower)

    pos_score = 0.0
    neg_score = 0.0

    for i, word in enumerate(words):
        # Check for negation in preceding 3 words
        context = words[max(0, i-3):i]
        negated = any(n in context for n in _NEGATORS)

        # Check for intensifier
        intensity = 1.5 if any(w in context for w in _INTENSIFIERS) else 1.0

        if word in _POSITIVE:
            weight = _POSITIVE[word] * intensity
            if negated:
                neg_score += weight
            else:
                pos_score += weight

        if word in _NEGATIVE:
            weight = _NEGATIVE[word] * intensity
            if negated:
                pos_score += weight
            else:
                neg_score += weight

    total = pos_score + neg_score
    if total == 0:
        return 0.0

    raw = (pos_score - neg_score) / total
    # Normalize to [-1, 1]
    return max(-1.0, min(1.0, raw))


def score_headline(headline: str, summary: str = "") -> float:
    """
    Score a news headline + optional summary.
    Headline carries 70% weight, summary 30%.
    """
    h_score = score_text(headline)
    if not summary:
        return h_score
    s_score = score_text(summary)
    return h_score * 0.7 + s_score * 0.3


def score_news_events(events: list) -> float:
    """
    Aggregate multiple news events for a symbol.
    Recency-weighted: newer articles count more.
    Returns float in [-1.0, +1.0].
    """
    if not events:
        return 0.0

    scores = []
    for i, event in enumerate(reversed(events)):  # most recent first
        weight = 1.0 / (i + 1)  # recency decay
        headline = getattr(event, "headline", "") or str(event)
        summary  = getattr(event, "summary", "")
        scores.append((score_headline(headline, summary), weight))

    if not scores:
        return 0.0

    total_weight = sum(w for _, w in scores)
    weighted_sum = sum(s * w for s, w in scores)
    return weighted_sum / total_weight if total_weight > 0 else 0.0
