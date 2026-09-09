"""Normalisation and aggregation, asserted as exact arithmetic."""

from __future__ import annotations

import pytest

from eifo_core.enums import RatingProvider as P
from eifo_core.scores import RatingInput, aggregate, format_score, normalise
from eifo_core.settings import ScoresConfig, ScoreWeights


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

        # seret_critics 2.0, seret_viewers 1.0 -> (90*2 + 80*1.0) / 3.0 = 86.7 -> 87
        assert result.score_israeli == 87
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


class TestVotesTooThinToCount:
    """Seret's audience score can be a 9.1 from four people.

    Halving the weight does not make that harmless, so below the configured
    floor it is left out of the arithmetic entirely. It is still stored and
    still shown - it is true, and a reader looking at "6.7, 4 votes" can weigh
    it themselves - it just stops moving a number meant to summarise agreement.
    """

    def test_a_thinly_voted_seret_audience_score_is_excluded(self) -> None:
        result = aggregate(
            [
                RatingInput(P.SERET_VIEWERS, 91, 4),
                RatingInput(P.SERET_CRITICS, 62, None),
                RatingInput(P.IMDB, 71, 12004),
            ],
            config(),
        )

        assert result.components["seret_viewers"]["excluded"] is True
        assert result.components["seret_viewers"]["weight"] == 0.0
        # (62*2.0 + 71*3.0) / 5.0 = 67.4 -> 67, with the 91 nowhere in it.
        assert result.score == 67

    def test_the_floor_is_inclusive(self) -> None:
        """ "More than 10" means 11 counts and 10 does not."""
        at = aggregate(
            [RatingInput(P.SERET_VIEWERS, 91, 10), RatingInput(P.IMDB, 71, 9999)], config()
        )
        above = aggregate(
            [RatingInput(P.SERET_VIEWERS, 91, 11), RatingInput(P.IMDB, 71, 9999)], config()
        )

        assert at.components["seret_viewers"]["excluded"] is True
        assert above.components["seret_viewers"]["excluded"] is False

    def test_between_the_floor_and_the_damping_it_counts_half(self) -> None:
        """The two rules stack rather than replace each other."""
        result = aggregate(
            [RatingInput(P.SERET_VIEWERS, 91, 40), RatingInput(P.IMDB, 71, 9999)], config()
        )

        component = result.components["seret_viewers"]
        assert (component["excluded"], component["damped"], component["weight"]) == (
            False,
            True,
            0.5,
        )

    def test_well_voted_it_carries_its_full_weight(self) -> None:
        result = aggregate(
            [RatingInput(P.SERET_VIEWERS, 91, 300), RatingInput(P.IMDB, 71, 9999)], config()
        )

        assert result.components["seret_viewers"]["weight"] == 1.0

    def test_an_excluded_rating_cannot_carry_the_israeli_score_alone(self) -> None:
        """The Israeli aggregate needs only one provider - but a real one."""
        result = aggregate(
            [RatingInput(P.SERET_VIEWERS, 91, 4), RatingInput(P.IMDB, 71, 9999)], config()
        )

        assert result.score_israeli is None

    def test_it_is_kept_in_the_working_rather_than_dropped(self) -> None:
        """So "why is this not in the total" is answerable from the page."""
        result = aggregate(
            [RatingInput(P.SERET_VIEWERS, 91, 4), RatingInput(P.IMDB, 71, 9999)], config()
        )

        assert result.components["seret_viewers"]["normalized"] == 91
        assert result.components["seret_viewers"]["vote_count"] == 4

    def test_no_vote_count_is_not_a_thin_one(self) -> None:
        """Seret's critic score is one editorial figure, not a poll."""
        result = aggregate(
            [RatingInput(P.SERET_CRITICS, 62, None), RatingInput(P.IMDB, 71, 9999)], config()
        )

        assert result.components["seret_critics"]["excluded"] is False
        assert result.components["seret_critics"]["weight"] == 2.0

    def test_other_providers_have_no_floor_by_default(self) -> None:
        """Only Seret's audience score is thin-voted enough to warrant one."""
        result = aggregate([RatingInput(P.IMDB, 91, 3), RatingInput(P.TMDB, 71, 2)], config())

        assert result.components["imdb"]["excluded"] is False
        assert result.components["tmdb"]["excluded"] is False
        assert result.components["imdb"]["damped"] is True

    def test_a_floor_can_be_set_for_any_provider(self) -> None:
        result = aggregate(
            [RatingInput(P.IMDB, 91, 3), RatingInput(P.TMDB, 71, 9999)],
            config(min_votes={"imdb": 10}),
        )

        assert result.components["imdb"]["excluded"] is True


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
            "excluded": False,
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


class TestEvidenceBehindTheNumber:
    """Damping weighs providers against each other; this weighs the evidence.

    Weight is only ever relative, so when every provider is thin *and they
    agree*, halving them all changes nothing: a 10/10 from one voter beside
    100% from two averaged to a catalog-topping 100. Thirteen of the top
    fifteen titles rested on fewer than fifty votes between them.
    """

    def test_the_title_that_started_this(self) -> None:
        """TMDB 10.0 from one voter, 100% from two. It was the whole catalog's
        best film."""
        result = aggregate(
            [RatingInput(P.TMDB, 100, 1), RatingInput(P.RT_AUDIENCE, 100, 2)],
            config(),
        )

        assert result.score is None

    def test_agreement_does_not_make_thin_evidence_thick(self) -> None:
        # Both damped, so both weigh 0.5 and the mean is still 100 - the part
        # damping could never fix. 20 votes: (20*100 + 100*65) / 120 = 70.83
        result = aggregate(
            [RatingInput(P.TMDB, 100, 10), RatingInput(P.RT_AUDIENCE, 100, 10)],
            config(),
        )

        assert result.score == 71

    def test_at_the_confidence_point_the_prior_weighs_the_same(self) -> None:
        # The definition of the setting, asserted: 100 votes against a prior
        # worth 100 votes puts the answer exactly between them.
        result = aggregate(
            [RatingInput(P.IMDB, 95, 50), RatingInput(P.TMDB, 95, 50)],
            config(),
        )

        assert result.score == (95 + 65) // 2

    def test_a_well_supported_score_is_left_alone(self) -> None:
        # (80*3 + 60*1) / 4 = 75, and two million votes leave it there.
        result = aggregate(
            [RatingInput(P.IMDB, 80, 2_000_000), RatingInput(P.TMDB, 60, 300_000)],
            config(),
        )

        assert result.score == 75

    def test_it_pulls_a_low_score_up_as_readily_as_a_high_one_down(self) -> None:
        """Not a penalty for being obscure - a statement about confidence."""
        thin = aggregate(
            [RatingInput(P.TMDB, 10, 10), RatingInput(P.RT_AUDIENCE, 10, 10)],
            config(),
        )

        assert thin.score is not None
        assert 10 < thin.score < 65

    def test_no_number_at_all_below_the_floor(self) -> None:
        nine = aggregate(
            [RatingInput(P.TMDB, 90, 4), RatingInput(P.RT_AUDIENCE, 90, 5)],
            config(),
        )
        ten = aggregate(
            [RatingInput(P.TMDB, 90, 5), RatingInput(P.RT_AUDIENCE, 90, 5)],
            config(),
        )

        assert nine.score is None
        assert ten.score is not None

    def test_ratings_that_count_nobody_are_neither_floored_nor_pulled(self) -> None:
        """Seret's critic figure is one editor's opinion, not a poll.

        Unknown is not few - the same rule the per-provider floor already
        follows - so a title rated only by such providers keeps their number.
        """
        result = aggregate(
            [RatingInput(P.IMDB, 80, None), RatingInput(P.TMDB, 60, None)],
            config(),
        )

        assert result.score == 75

    def test_a_counted_rating_beside_an_uncounted_one_is_judged_on_what_it_has(
        self,
    ) -> None:
        # Only IMDb reports a count, so the evidence is its 200 votes.
        # (80*3 + 60*1) / 4 = 75, then (200*75 + 100*65) / 300 = 71.67
        result = aggregate(
            [RatingInput(P.IMDB, 80, 200), RatingInput(P.TMDB, 60, None)],
            config(),
        )

        assert result.score == 72

    def test_the_israeli_score_is_judged_the_same_way(self) -> None:
        """Its top three were 100s resting on twenty-odd votes each."""
        result = aggregate([RatingInput(P.SERET_VIEWERS, 100, 20)], config())

        assert result.score_israeli == 71

    def test_how_much_evidence_is_enough_is_configurable(self) -> None:
        lenient = aggregate(
            [RatingInput(P.TMDB, 100, 10), RatingInput(P.RT_AUDIENCE, 100, 10)],
            config(confidence_votes=10),
        )

        # 20 votes against a prior worth 10: (20*100 + 10*65) / 30 = 88.3
        assert lenient.score == 88
