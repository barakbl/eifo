"""When a long phase should say how far it has got."""

from __future__ import annotations

from eifo_fetcher.progress import ProgressTicker, tally


class FakeClock:
    """A clock the test moves by hand, so no test waits on a real one."""

    def __init__(self) -> None:
        self.seconds = 0.0

    def __call__(self) -> float:
        return self.seconds


class TestProgressTicker:
    def test_the_first_line_comes_early(self) -> None:
        """Something that will take an hour should look alive in the first seconds."""
        ticker = ProgressTicker(every=100, seconds=15, first=10, now=FakeClock())

        assert [count for count in range(1, 12) if ticker.due(count)] == [10]

    def test_then_on_the_round_numbers(self) -> None:
        """Not every hundredth item since the last line: the hundreds themselves."""
        clock = FakeClock()
        ticker = ProgressTicker(every=100, seconds=15, first=10, now=clock)

        due = [count for count in range(1, 401) if ticker.due(count)]

        assert due == [10, 100, 200, 300, 400]

    def test_a_slow_source_still_speaks_up(self) -> None:
        """The point of the clock trigger: one item a second must not go quiet."""
        clock = FakeClock()
        ticker = ProgressTicker(every=100, seconds=15, first=10, now=clock)
        ticker.due(10)

        spoke = []
        for count in range(11, 31):
            clock.seconds += 1.0
            if ticker.due(count):
                spoke.append(count)

        assert spoke == [25]

    def test_the_clock_does_not_shift_the_round_numbers(self) -> None:
        """A line the clock brought forward at 25 still leaves the next at 100."""
        clock = FakeClock()
        ticker = ProgressTicker(every=100, seconds=15, first=10, now=clock)
        ticker.due(10)
        clock.seconds += 15
        assert ticker.due(25) is True

        assert [count for count in range(26, 151) if ticker.due(count)] == [100]

    def test_a_fast_source_is_not_asked_to_repeat_itself(self) -> None:
        """The item trigger holds a busy loop to one line per batch, not per tick."""
        clock = FakeClock()
        ticker = ProgressTicker(every=100, seconds=15, first=10, now=clock)
        ticker.due(10)
        clock.seconds += 60

        assert ticker.due(11) is True
        assert ticker.due(12) is False

    def test_a_loop_that_has_not_moved_has_nothing_to_say(self) -> None:
        clock = FakeClock()
        ticker = ProgressTicker(every=1, seconds=0, first=1, now=clock)
        assert ticker.due(5) is True

        clock.seconds += 100
        assert ticker.due(5) is False
        assert ticker.due(4) is False


class TestTally:
    def test_it_leaves_out_the_zeroes(self) -> None:
        """On a settled catalog most of these are zero, every night."""
        assert (
            tally(new_titles=3, new_offers=0, already_listed=12)
            == "3 new titles, 12 already listed"
        )

    def test_underscores_read_as_words(self) -> None:
        assert tally(parked_for_review=2) == "2 parked for review"

    def test_big_numbers_are_grouped(self) -> None:
        assert tally(already_listed=12345) == "12,345 already listed"

    def test_nothing_at_all_still_says_something(self) -> None:
        assert tally(new_titles=0, errors=0) == "nothing yet"
