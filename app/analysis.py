"""Post-hoc item quality analysis.

``GET /api/items/{item_id}/analysis`` reviews one item against the historical
sessions that answered it.  The analysis is strictly read-only and deliberately
based on the scores frozen into ``responses`` at answer time: re-importing an
item with a changed correct answer never rewrites historical ``score`` values,
so later bank edits cannot distort the review.

Sample construction
-------------------
A finished session contributes one sample ``(x, y)`` when it answered the
target item **and at least one other item**:

* ``x`` -- the target item's stored ``score`` (1 correct / 0 wrong);
* ``y`` -- the share of the *other* answered items in the same session whose
  stored ``score`` is 1 (a proxy for the examinee's overall performance).

Only ``finished`` sessions are counted; active/paused sessions are excluded so
in-progress quizzes cannot leak into the statistics.

The reported Pearson correlation is ``null`` unless there are at least
:data:`MIN_SAMPLES` samples **and** both series have non-zero variance (a
constant target score or every session sharing the same peer rate leaves the
correlation undefined).
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

# Below this many samples a correlation is not reported.
MIN_SAMPLES = 5


@dataclass(frozen=True)
class ItemAnalysis:
    """Aggregated review result for one item."""

    sample_count: int
    correct_rate: float | None
    pearson_r: float | None


def collect_item_samples(
    conn: sqlite3.Connection, item_id: str
) -> list[tuple[float, float]]:
    """Build ``(target score, peer correct ratio)`` pairs for one item.

    One pair per finished session that answered *item_id* and at least one
    other item.  Scores come straight from the ``responses`` rows saved at
    answer time; the current ``items.answer`` is never consulted.
    """
    rows = conn.execute(
        """
        SELECT
            (SELECT score
               FROM responses rt
              WHERE rt.session_id = s.id AND rt.item_id = ?) AS target_score,
            (SELECT SUM(score)
               FROM responses ro
              WHERE ro.session_id = s.id AND ro.item_id != ?) AS peer_correct,
            (SELECT COUNT(*)
               FROM responses rn
              WHERE rn.session_id = s.id AND rn.item_id != ?) AS peer_total
          FROM sessions s
         WHERE s.status = 'finished'
           AND EXISTS (
                   SELECT 1
                     FROM responses rt
                    WHERE rt.session_id = s.id AND rt.item_id = ?
               )
           AND (SELECT COUNT(*)
                  FROM responses rc
                 WHERE rc.session_id = s.id AND rc.item_id != ?) >= 1
        """,
        (item_id, item_id, item_id, item_id, item_id),
    ).fetchall()
    return [
        (float(r["target_score"]), r["peer_correct"] / r["peer_total"])
        for r in rows
    ]


def pearson(xs: list[float], ys: list[float]) -> float | None:
    """Pearson product-moment correlation of two equal-length series.

    Returns ``None`` when there are fewer than :data:`MIN_SAMPLES` pairs or
    either series has zero variance (correlation is undefined there).  Counts
    are small and the values bounded, so the raw-score formula is adequate;
    the result is clamped to ``[-1, 1]`` to absorb rounding drift.
    """
    n = len(xs)
    if n < MIN_SAMPLES or n != len(ys):
        return None
    mean_x = math.fsum(xs) / n
    mean_y = math.fsum(ys) / n
    dx = [x - mean_x for x in xs]
    dy = [y - mean_y for y in ys]
    # Exact constant check first: binary target scores are the most common
    # zero-variance case (all sessions got the item right or all wrong).
    if all(d == 0.0 for d in dx) or all(d == 0.0 for d in dy):
        return None
    sxx = math.fsum(d * d for d in dx)
    syy = math.fsum(d * d for d in dy)
    sxy = math.fsum(a * b for a, b in zip(dx, dy))
    denominator = math.sqrt(sxx * syy)
    if denominator <= 0.0:  # defensive: zero variance after floating point
        return None
    return min(max(sxy / denominator, -1.0), 1.0)


def analyze_item(
    conn: sqlite3.Connection, item_id: str
) -> ItemAnalysis:
    """Compute the review payload for one item from historical sessions."""
    samples = collect_item_samples(conn, item_id)
    n = len(samples)
    if n == 0:
        return ItemAnalysis(sample_count=0, correct_rate=None, pearson_r=None)
    target_scores = [x for x, _ in samples]
    peer_rates = [y for _, y in samples]
    correct_rate = math.fsum(target_scores) / n
    return ItemAnalysis(
        sample_count=n,
        correct_rate=correct_rate,
        pearson_r=pearson(target_scores, peer_rates),
    )
