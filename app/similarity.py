"""Answer-sheet similarity review across finished sessions.

``GET /api/sessions/{session_id}/similarity`` compares the answer sheet of a
*finished* target session against every other finished session, looking for
suspiciously similar wrong-answer patterns (答卷雷同复核).  The review is
strictly read-only and deliberately based on the ``response``/``score`` values
frozen into ``responses`` at answer time: re-importing an item with a changed
correct answer never rewrites history, so later bank edits cannot change the
result.

Matching rules
--------------
Both sheets are aligned by ``item_id`` (the ``UNIQUE(session_id, item_id)``
constraint guarantees at most one stored answer per item per session).  A
shared item counts as a **same-wrong match** only when both sessions selected
the *same* option **and** both stored scores are 0 — matching correct answers
never count.  A candidate session qualifies only when the two sheets share at
least ``min_shared`` answered items.

Each same-wrong match is weighted by how rare the shared wrong option is
across all finished sessions (an IDF-style weight, natural log)::

    weight = log((item_wrong_total + 2) / (option_wrong_count + 1))

* ``item_wrong_total``   -- number of wrong answers (frozen ``score = 0``) to
  this item across all finished sessions;
* ``option_wrong_count`` -- number of those wrong answers that chose this
  particular option.

Common wrong options (chosen by many examinees) weigh little; two sessions
sharing a *rare* wrong option is a much stronger signal.  The ``+2``/``+1``
smoothing keeps every weight positive and finite.

The similarity **score** is the sum of the match weights divided by the number
of shared items, so longer overlaps do not inflate the score by themselves.
Results are ordered by score (desc), then same-wrong count (desc), then
session id (asc) and truncated to ``limit``.
"""

from __future__ import annotations

import math
import sqlite3
from collections import Counter
from dataclasses import dataclass


@dataclass(frozen=True)
class SimilarityMatch:
    """One finished session similar to the target session."""

    session_id: str
    shared_count: int
    same_wrong_count: int
    score: float


def find_similar_sessions(
    conn: sqlite3.Connection,
    session_id: str,
    *,
    min_shared: int,
    limit: int,
) -> list[SimilarityMatch]:
    """Rank other finished sessions by wrong-answer similarity.

    ``session_id`` must reference an already *finished* session (the endpoint
    enforces this).  Only sessions sharing at least ``min_shared`` answered
    items with the target are returned, at most ``limit`` of them, ordered by
    ``(-score, -same_wrong_count, session_id)``.
    """
    rows = conn.execute(
        """
        SELECT r.session_id AS session_id,
               r.item_id    AS item_id,
               r.response   AS response,
               r.score      AS score
          FROM responses r
          JOIN sessions s ON s.id = r.session_id
         WHERE s.status = 'finished'
        """
    ).fetchall()

    # Per-session answer sheets plus the wrong-answer statistics used by the
    # weights; both come from the same snapshot of frozen responses.
    sheets: dict[str, dict[str, tuple[str, int]]] = {}
    item_wrong_total: Counter[str] = Counter()
    option_wrong_count: Counter[tuple[str, str]] = Counter()
    for row in rows:
        sheets.setdefault(row["session_id"], {})[row["item_id"]] = (
            row["response"],
            row["score"],
        )
        if row["score"] == 0:
            item_wrong_total[row["item_id"]] += 1
            option_wrong_count[(row["item_id"], row["response"])] += 1

    target = sheets.get(session_id, {})
    matches: list[SimilarityMatch] = []
    for candidate_id, candidate in sheets.items():
        if candidate_id == session_id:
            continue
        shared_ids = target.keys() & candidate.keys()
        shared_count = len(shared_ids)
        if shared_count < min_shared:
            continue
        weight_sum = 0.0
        same_wrong = 0
        for item_id in shared_ids:
            t_response, t_score = target[item_id]
            c_response, c_score = candidate[item_id]
            if t_score == 0 and c_score == 0 and t_response == c_response:
                same_wrong += 1
                weight_sum += math.log(
                    (item_wrong_total[item_id] + 2)
                    / (option_wrong_count[(item_id, t_response)] + 1)
                )
        matches.append(
            SimilarityMatch(
                session_id=candidate_id,
                shared_count=shared_count,
                same_wrong_count=same_wrong,
                score=weight_sum / shared_count,
            )
        )
    matches.sort(key=lambda m: (-m.score, -m.same_wrong_count, m.session_id))
    return matches[:limit]
