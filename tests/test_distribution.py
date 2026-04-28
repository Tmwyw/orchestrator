"""Tests for orchestrator.distribution.equal_share."""

from __future__ import annotations

import random


def test_equal_share_basic_no_remainder() -> None:
    from orchestrator.distribution import equal_share

    assert equal_share(1000, [9999] * 4) == [250, 250, 250, 250]


def test_equal_share_with_remainder() -> None:
    from orchestrator.distribution import equal_share

    result = equal_share(1001, [9999] * 4)
    assert sum(result) == 1001
    assert max(result) - min(result) <= 1


def test_equal_share_one_capped() -> None:
    from orchestrator.distribution import equal_share

    assert equal_share(1000, [9999, 100, 9999, 9999]) == [300, 100, 300, 300]


def test_equal_share_all_capped_below_total() -> None:
    from orchestrator.distribution import equal_share

    result = equal_share(10000, [100, 100, 100])
    assert result == [100, 100, 100]
    assert sum(result) == 300


def test_equal_share_zero_total() -> None:
    from orchestrator.distribution import equal_share

    assert equal_share(0, [100, 100]) == [0, 0]


def test_equal_share_empty_caps() -> None:
    from orchestrator.distribution import equal_share

    assert equal_share(100, []) == []


def test_equal_share_sum_invariant_property() -> None:
    """Random fuzz: sum(result) == min(total, sum(caps)) for any input."""
    from orchestrator.distribution import equal_share

    rng = random.Random(20260428)
    for _ in range(200):
        n = rng.randint(0, 12)
        caps = [rng.randint(0, 5000) for _ in range(n)]
        total = rng.randint(0, 50000)
        result = equal_share(total, caps)

        assert len(result) == n
        assert all(0 <= result[i] <= caps[i] for i in range(n))
        assert sum(result) == min(total, sum(caps))


def test_equal_share_respects_caps_when_total_exceeds_sum() -> None:
    from orchestrator.distribution import equal_share

    caps = [50, 200, 100]
    result = equal_share(10_000, caps)
    assert result == caps
