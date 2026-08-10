"""Normalisation and aggregation, asserted as exact arithmetic."""

from __future__ import annotations

import pytest

from eifo_core.enums import RatingProvider as P
from eifo_core.settings import ScoresConfig, ScoreWeights
from eifo_fetcher.scores import RatingInput, aggregate, format_score, normalise


def config(**overrides: object) -> ScoresConfig:
    return ScoresConfig(**overrides)  # type: ignore[arg-type]


class TestNormalise:
    @pytest.mark.parametrize(
        ("provider", "raw", "expected"),
        [
            (P.IMDB, 8.4, 84),
            (P.TMDB, 7.25, 73),
            (P.SERET_VIEWERS, 8.9, 89),
            (P.SERET_CRITICS, 10.0, 100),
            (P.EDB, 0.0, 0),
        ],
    )
    def test_ten_point_scales_are_multiplied(self, provider: P, raw: float, expected: int) -> None:
        assert normalise(provider, raw) == expected

    @pytest.mark.parametrize(("provider", "raw"), [(P.RT_CRITICS, 92.0), (P.RT_AUDIENCE, 45.0)])
    def test_percentages_pass_through(self, provider: P, raw: float) -> None:
        assert normalise(provider, raw) == int(raw)

    @pytest.mark.parametrize(
        ("provider", "raw"),
        [(P.IMDB, 11.0), (P.IMDB, -0.5), (P.RT_CRITICS, 101.0), (P.SERET_VIEWERS, 92.0)],
    )
    def test_a_score_outside_its_scale_is_rejected(self, provider: P, raw: float) -> None:
        """An out-of-scale score means the parser is wrong, not the film."""
        with pytest.raises(ValueError, match="outside its"):
            normalise(provider, raw)

    def test_a_seret_score_on_the_rt_scale_is_caught(self) -> None:
        """Guards the likeliest parser mistake: reading a percentage as /10."""
        with pytest.raises(ValueError):
            normalise(P.SERET_CRITICS, 89.0)


class TestFormatScore:
    def test_ten_point_scales_show_one_decimal(self) -> None:
        assert format_score(P.IMDB, 8.4) == "8.4"

    def test_percentages_show_a_sign(self) -> None:
        assert format_score(P.RT_CRITICS, 92.0) == "92%"


class TestAggregate:
    def test_a_single_provider_yields_no_aggregate(self) -> None:
        """An 'average' of one number is that number wearing a disguise."""
        result = aggregate([RatingInput(P.IMDB, 84, 9999)], config())

        assert result.score is None

    def test_two_providers_are_averaged_by_weight(self) -> None:
        # imdb weight 3.0, tmdb weight 1.0 -> (80*3 + 60*1) / 4 = 75
        result = aggregate(
            [RatingInput(P.IMDB, 80, 9999), RatingInput(P.TMDB, 60, 9999)],
            config(),
        )

        assert result.score == 75

    def test_thin_votes_halve_a_weight(self) -> None:
        # imdb damped to 1.5: (80*1.5 + 60*1.0) / 2.5 = 72
        result = aggregate(
            [RatingInput(P.IMDB, 80, 3), RatingInput(P.TMDB, 60, 9999)],
            config(),
        )

        assert result.score == 72

    def test_a_missing_vote_count_is_not_damped(self) -> None:
        result = aggregate(
            [RatingInput(P.IMDB, 80, None), RatingInput(P.TMDB, 60, 9999)],
            config(),
        )

        assert result.score == 75

    def test_a_zero_weight_provider_is_excluded(self) -> None:
        weights = ScoreWeights(edb=0.0)
        result = aggregate(
            [RatingInput(P.IMDB, 80, 9999), RatingInput(P.EDB, 10, 9999)],
            config(weights=weights),
        )

        assert result.score is None  # only one provider carries weight

    def test_the_minimum_provider_count_is_configurable(self) -> None:
        result = aggregate([RatingInput(P.IMDB, 84, 9999)], config(min_providers=1))

        assert result.score == 84


class TestIsraeliAggregate:
    def test_israeli_providers_are_scored_separately(self) -> None:
        result = aggregate(
            [
                RatingInput(P.IMDB, 60, 9999),
                RatingInput(P.SERET_CRITICS, 90, 9999),
                RatingInput(P.SERET_VIEWERS, 80, 9999),
            ],
            config(),
        )

        # seret_critics 2.0, seret_viewers 1.5 -> (90*2 + 80*1.5) / 3.5 = 85.7 -> 86
        assert result.score_israeli == 86
        assert result.score != result.score_israeli

    def test_one_israeli_provider_is_enough(self) -> None:
        """The Israeli score exists to surface local opinion, so it shows early."""
        result = aggregate([RatingInput(P.SERET_VIEWERS, 88, 9999)], config())

        assert result.score_israeli == 88
        assert result.score is None

    def test_no_israeli_ratings_yields_none(self) -> None:
        result = aggregate(
            [RatingInput(P.IMDB, 80, 9999), RatingInput(P.RT_CRITICS, 90, 9999)],
            config(),
        )

        assert result.score_israeli is None

    def test_only_seret_and_edb_count_as_israeli(self) -> None:
        assert P.SERET_CRITICS.is_israeli
        assert P.SERET_VIEWERS.is_israeli
        assert P.EDB.is_israeli
        assert not P.IMDB.is_israeli
        assert not P.TMDB.is_israeli
        assert not P.RT_CRITICS.is_israeli


class TestComponents:
    def test_every_input_is_recorded_with_its_weight(self) -> None:
        result = aggregate(
            [RatingInput(P.IMDB, 80, 9999), RatingInput(P.RT_CRITICS, 90, None)],
            config(),
        )

        assert result.components["imdb"] == {
            "normalized": 80,
            "weight": 3.0,
            "vote_count": 9999,
            "damped": False,
        }
        assert result.components["rt_critics"]["weight"] == 2.0

    def test_damping_is_visible_in_the_components(self) -> None:
        """The UI can explain why a thinly-voted score counted for less."""
        result = aggregate([RatingInput(P.IMDB, 80, 2), RatingInput(P.TMDB, 60, 5000)], config())

        assert result.components["imdb"]["damped"] is True
        assert result.components["imdb"]["weight"] == 1.5
        assert result.components["tmdb"]["damped"] is False

    def test_an_empty_rating_list_is_handled(self) -> None:
        result = aggregate([], config())

        assert result.score is None
        assert result.score_israeli is None
        assert result.components == {}
