"""Per-item prediction-probability calibration review.

``GET /api/items/{item_id}/calibration`` checks whether the 2PL probabilities
the engine predicted *before* scoring actually match the observed outcomes of
one item, helping item-bank maintainers spot systematically over- or
under-predicted items (e.g. after a mis-keyed difficulty/discrimination).

The review is strictly read-only and deliberately based on the values frozen
into ``responses`` at answer time:

* ``p_correct`` -- the 2PL probability of a correct answer computed from the
  examinee's theta *before* the answer was scored;
* ``score``     -- the observed outcome (1 correct / 0 wrong).

Neither number is ever recomputed from the current ability estimate or the
current item parameters, so later bank edits (re-imported difficulty,
discrimination or answer) cannot distort the review.

Sample construction
-------------------
Every ``responses`` row of a **finished** session that answered the target
item contributes one sample ``(score, p_correct)``; active/paused sessions are
excluded so in-progress quizzes cannot leak into the statistics.

Statistics
----------
* ``actual_correct_rate``         = ``Σscore / n``
* ``mean_predicted_correct_rate`` = ``Σp_correct / n``
* standardized deviation (a normal approximation under the null hypothesis
  that each response is an independent Bernoulli(``p_correct``) draw)::

      z = (Σscore − Σp_correct) / √ Σ[p_correct·(1 − p_correct)]

``z`` and the deviation flag are reported only when there are at least
:data:`MIN_SAMPLES` samples **and** the denominator is non-zero; otherwise
both are ``null``.  The item is flagged as deviating when
``|z| >= Z_CRITICAL`` (the two-sided 5 % critical value 1.96): positive ``z``
means the item is answered correctly more often than predicted
(under-predicted), negative ``z`` the opposite.
"""

from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass

# Below this many samples the normal approximation is not reported.
MIN_SAMPLES = 30

# Two-sided 5% critical value of the standard normal distribution.
Z_CRITICAL = 1.96


@dataclass(frozen=True)
class ItemCalibration:
    """Aggregated calibration result for one item."""

    sample_count: int
    actual_correct_rate: float | None
    mean_predicted_correct_rate: float | None
    z_score: float | None
    deviation_flag: bool | None


def collect_calibration_samples(
    conn: sqlite3.Connection, item_id: str
) -> list[tuple[int, float]]:
    """Build ``(score, p_correct)`` pairs for one item.

    One pair per ``responses`` row belonging to a *finished* session.  Both
    values come straight from the rows saved at answer time; the current
    ``items`` parameters and the current session theta are never consulted.
    """
    rows = conn.execute(
        """
        SELECT r.score AS score, r.p_correct AS p_correct
          FROM responses r
          JOIN sessions s ON s.id = r.session_id
         WHERE r.item_id = ? AND s.status = 'finished'
        """,
        (item_id,),
    ).fetchall()
    return [(row["score"], row["p_correct"]) for row in rows]


def calibrate_item(
    conn: sqlite3.Connection, item_id: str
) -> ItemCalibration:
    """Compute the calibration payload for one item from finished sessions."""
    samples = collect_calibration_samples(conn, item_id)
    n = len(samples)
    if n == 0:
        return ItemCalibration(
            sample_count=0,
            actual_correct_rate=None,
            mean_predicted_correct_rate=None,
            z_score=None,
            deviation_flag=None,
        )
    score_sum = math.fsum(score for score, _ in samples)
    p_sum = math.fsum(p for _, p in samples)
    z_score: float | None = None
    deviation_flag: bool | None = None
    if n >= MIN_SAMPLES:
        variance = math.fsum(p * (1.0 - p) for _, p in samples)
        if variance > 0.0:
            z_score = (score_sum - p_sum) / math.sqrt(variance)
            deviation_flag = abs(z_score) >= Z_CRITICAL
    return ItemCalibration(
        sample_count=n,
        actual_correct_rate=score_sum / n,
        mean_predicted_correct_rate=p_sum / n,
        z_score=z_score,
        deviation_flag=deviation_flag,
    )
