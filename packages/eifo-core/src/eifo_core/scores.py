"""Normalisation and score aggregation.

Every provider is normalised to 0-100, then combined as a weighted mean. Three
rules keep the result honest:

* **At least two providers.** An "aggregate" of a single number is just that
  number wearing a disguise, so it stays null until a second opinion arrives.
* **Thin votes count for less.** A rating backed by fewer than
  ``low_vote_threshold`` votes has its weight halved, so a 10/10-from-three-
  people cannot outrank a well-supported 8.
* **Very thin votes do not count at all.** Below ``[scores.min_votes]`` a
  rating is excluded from the arithmetic outright. It is still stored and still
  shown, with its vote count and a link to where it came from, because it is
  true and a reader can weigh it themselves - it just stops moving a number
  that is meant to summarise a consensus. Seret's audience score is the one
  provider this is set for by default.
* **A thinly-supported average is pulled towards the ordinary.** The three
  rules above are all about the weight one provider carries against another,
  and weight is only ever relative - so when every provider is thin *and they
  agree*, halving them all changes nothing. A 10/10 from one voter beside 100%
  from two produced a catalog-topping 100, and thirteen of the top fifteen
  titles rested on fewer than fifty votes between them. The mean is therefore
  moved towards ``prior_score`` in proportion to how little evidence stands
  behind it, which is the one thing damping cannot do.
* **No evidence, no number.** Under ``min_total_votes`` there is no aggregate:
  a score assembled almost entirely from the prior would be a statement about
  the catalog wearing a particular title's name.
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
    #: Left out of the arithmetic for want of votes. Carried in the working
    #: rather than dropped, so "why is this score not in the total" has an
    #: answer on the page instead of only in this file.
    excluded: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "normalized": self.normalized,
            "weight": round(self.weight, 3),
            "vote_count": self.vote_count,
            "damped": self.damped,
            "excluded": self.excluded,
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
        score=_weighted_mean(components, config.min_providers, config),
        # The Israeli aggregate is the point of a separate score, so a single
        # local provider is still worth showing.
        score_israeli=_weighted_mean(
            [component for component in components if component.provider.is_israeli],
            minimum=1,
            config=config,
        ),
        components={component.provider.value: component.as_dict() for component in components},
    )


def _component(rating: RatingInput, config: ScoresConfig) -> Component:
    weight = getattr(config.weights, rating.provider.value, 0.0)

    if _too_few_votes(rating, config):
        # Zero weight rather than a separate code path: _weighted_mean already
        # counts only components carrying weight, so this drops out of both the
        # global and the Israeli aggregate without either learning a new rule.
        return Component(
            provider=rating.provider,
            normalized=rating.score_normalized,
            weight=0.0,
            vote_count=rating.vote_count,
            damped=False,
            excluded=True,
        )

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


def _too_few_votes(rating: RatingInput, config: ScoresConfig) -> bool:
    """Whether this rating is too thinly voted to count towards an aggregate.

    A provider with no floor configured, or a rating that reports no vote count
    at all, is never excluded: "unknown" is not "few". Seret's critic score
    carries no count because it is one editorial figure rather than a poll.
    """
    floor = config.min_votes.get(rating.provider.value)
    if floor is None or rating.vote_count is None:
        return False
    return rating.vote_count <= floor


def _weighted_mean(components: list[Component], minimum: int, config: ScoresConfig) -> int | None:
    """The score these components support, or None if they support none.

    Two steps. The weighted mean says what the providers think; the pull
    towards the prior says how much of that to believe, which is a question
    about the number of people behind it rather than about the providers.
    """
    contributing = [component for component in components if component.weight > 0]
    if len(contributing) < minimum:
        return None

    total_weight = sum(component.weight for component in contributing)
    if total_weight <= 0:
        return None

    weighted = sum(component.normalized * component.weight for component in contributing)
    mean = weighted / total_weight

    counted = [
        component.vote_count for component in contributing if component.vote_count is not None
    ]
    if not counted:
        # Nothing here reports a count - Seret's critic figure is one editorial
        # opinion, not a poll. Unknown is not few, so it is neither floored nor
        # pulled: the providers' own number stands.
        return round_half_up(mean)

    votes = sum(counted)
    if votes < config.min_total_votes:
        return None

    return round_half_up(_towards_the_prior(mean, votes, config))


def _towards_the_prior(mean: float, votes: int, config: ScoresConfig) -> float:
    """Move a mean towards the prior by however little supports it.

    The weighted-rating shape IMDb's Top 250 uses. At ``confidence_votes`` the
    title's own mean and the prior weigh the same; far past it the prior stops
    mattering, and far short of it the title is described mostly by the company
    it keeps. Nothing here is clamped: both inputs are already 0-100, so
    anything between them is too.
    """
    strength = config.confidence_votes
    return (votes * mean + strength * config.prior_score) / (votes + strength)
