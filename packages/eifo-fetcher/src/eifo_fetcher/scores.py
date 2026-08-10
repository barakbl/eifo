"""Normalisation and score aggregation.

Every provider is normalised to 0-100, then combined as a weighted mean. Two
rules keep the result honest:

* **At least two providers.** An "aggregate" of a single number is just that
  number wearing a disguise, so it stays null until a second opinion arrives.
* **Thin votes count for less.** A rating backed by a handful of votes has its
  weight halved, so a 10/10-from-three-people cannot outrank a well-supported 8.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from eifo_core.enums import RatingProvider
from eifo_core.settings import ScoresConfig

#: Providers whose native scale is already a percentage.
_PERCENT_PROVIDERS = frozenset({RatingProvider.RT_CRITICS, RatingProvider.RT_AUDIENCE})


def round_half_up(value: float) -> int:
    """Round to the nearest integer, halves upward.

    Python's built-in ``round`` rounds halves to even, so 7.25 and 7.35 would
    normalise to 72 and 74 - a user-visible score should not depend on the
    parity of the digit before it. Scores are never negative here.
    """
    return int(value + 0.5)


def normalise(provider: RatingProvider, score_raw: float) -> int:
    """Convert a provider's native score to 0-100.

    Raises:
        ValueError: if the score falls outside the provider's scale, which means
            the parser is wrong and storing it would corrupt the aggregate.
    """
    limit = 100.0 if provider in _PERCENT_PROVIDERS else 10.0
    if not 0.0 <= score_raw <= limit:
        raise ValueError(f"{provider} score {score_raw} outside its 0-{limit:g} scale")

    if provider in _PERCENT_PROVIDERS:
        return round_half_up(score_raw)
    return round_half_up(score_raw * 10)


def format_score(provider: RatingProvider, score_raw: float) -> str:
    """The score as the provider itself would show it."""
    if provider in _PERCENT_PROVIDERS:
        return f"{round_half_up(score_raw)}%"
    return f"{score_raw:.1f}"


@dataclass(frozen=True, slots=True)
class Component:
    """One provider's contribution to an aggregate."""

    provider: RatingProvider
    normalized: int
    weight: float
    vote_count: int | None
    damped: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "normalized": self.normalized,
            "weight": round(self.weight, 3),
            "vote_count": self.vote_count,
            "damped": self.damped,
        }


@dataclass(frozen=True, slots=True)
class Aggregate:
    """The computed scores and the working behind them."""

    score: int | None
    score_israeli: int | None
    components: dict[str, Any]


@dataclass(frozen=True, slots=True)
class RatingInput:
    """A stored rating, as aggregation sees it."""

    provider: RatingProvider
    score_normalized: int
    vote_count: int | None = None


def aggregate(ratings: list[RatingInput], config: ScoresConfig) -> Aggregate:
    """Combine ratings into a global score and an Israeli-only score."""
    components = [_component(rating, config) for rating in ratings]

    return Aggregate(
        score=_weighted_mean(components, config.min_providers),
        # The Israeli aggregate is the point of a separate score, so a single
        # local provider is still worth showing.
        score_israeli=_weighted_mean(
            [component for component in components if component.provider.is_israeli],
            minimum=1,
        ),
        components={component.provider.value: component.as_dict() for component in components},
    )


def _component(rating: RatingInput, config: ScoresConfig) -> Component:
    weight = getattr(config.weights, rating.provider.value, 0.0)
    damped = rating.vote_count is not None and rating.vote_count < config.low_vote_threshold
    if damped:
        weight /= 2
    return Component(
        provider=rating.provider,
        normalized=rating.score_normalized,
        weight=weight,
        vote_count=rating.vote_count,
        damped=damped,
    )


def _weighted_mean(components: list[Component], minimum: int) -> int | None:
    """Weighted mean of the components, or None if too few carry any weight."""
    contributing = [component for component in components if component.weight > 0]
    if len(contributing) < minimum:
        return None

    total_weight = sum(component.weight for component in contributing)
    if total_weight <= 0:
        return None

    weighted = sum(component.normalized * component.weight for component in contributing)
    return round_half_up(weighted / total_weight)
