"""Equal-share distribution algorithm with per-slot caps."""

from __future__ import annotations


def equal_share(total: int, caps: list[int]) -> list[int]:
    """Distribute ``total`` units across ``len(caps)`` slots as evenly as possible,
    honouring each slot's cap.

    Guarantees:
        * len(result) == len(caps)
        * 0 <= result[i] <= caps[i]
        * sum(result) == min(total, sum(caps))
        * If no cap is hit, max(result) - min(result) <= 1.

    Algorithm: iteratively give each still-active slot an equal share of the
    remaining budget (with leftover going to the lowest-index slots first).
    Slots that hit their cap drop out and the residual is redistributed.
    """
    n = len(caps)
    if n == 0 or total <= 0:
        return [0] * n

    result = [0] * n
    remaining = total
    active = list(range(n))

    while remaining > 0 and active:
        m = len(active)
        base, rem = divmod(remaining, m)
        new_active: list[int] = []
        new_remaining = 0
        for i, slot in enumerate(active):
            share = base + (1 if i < rem else 0)
            available = caps[slot] - result[slot]
            assigned = min(share, available)
            result[slot] += assigned
            new_remaining += share - assigned
            if result[slot] < caps[slot]:
                new_active.append(slot)
        if new_remaining == remaining:
            break
        remaining = new_remaining
        active = new_active

    return result
