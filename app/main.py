"""FastAPI application: adaptive quiz item selection engine.

Flow
----
1. ``POST /api/sessions``            create an examinee session
2. ``POST /api/sessions/{id}/next``  get the most informative eligible item
3. ``POST /api/sessions/{id}/responses``  submit an answer; theta is updated
4. repeat 2-3 until the session finishes (max items, no eligible item,
   expired deadline, or -- when configured -- the precision stop rule
   ``finish_reason = 'precision_reached'``)

An active, unexpired session may be paused
(``POST /api/sessions/{id}/pause``): the remaining time is frozen and /next
plus new answers are rejected with 409.  Resuming
(``POST /api/sessions/{id}/resume``) recomputes the UTC deadline from the
frozen seconds and restores the pending item, stored answers and counted
exposures; time-limit-free sessions can be paused and resumed as well.

``POST /api/items/import`` bulk-loads the item bank with per-row
validation: a dry run only reports the planned created/updated rows, while
a real commit upserts every row in one serialised transaction (updates
keep ``exposure_count`` and ``is_active``) and leaves the bank untouched
if any row fails.

``PATCH /api/items/{id}/availability`` withdraws or re-enables one item.
A withdrawn item no longer takes part in new selections; a session whose
pending item was withdrawn clears it and re-selects inside the /next
transaction (the exposure already counted is not rolled back), and new
answers for it are rejected with 409 while a stored ``response_id``
replay still returns the first result.

``GET /api/sessions/{id}/similarity`` reviews one finished session against
every other finished session for answer-sheet plagiarism: sheets are aligned
by ``item_id``, only shared *wrong* options count as matches, and each match
is weighted by how rare that wrong option is across finished sessions.

``GET /api/items/{id}/calibration`` aggregates the item's stored answers from
finished sessions and contrasts the ``p_correct`` predicted before scoring
with the frozen ``score``: it reports the sample count, the actual and mean
predicted correct rates, and a standardised deviation
``z = (Σscore − Σp_correct) / √Σ[p_correct(1−p_correct)]`` (``null`` below 30
samples or when its denominator is zero), flagging systematic miscalibration
at ``|z| >= 1.96``.

Every item keeps an append-only revision history: a formal import appends a
version only for a real new id or an actual content change, and the
availability PATCH appends one only when the enabled state really flips;
same-value retries, dry runs and failed batches create no version.  A legacy
bank gets one version-1 baseline per item at first startup (idempotently).

The revision served to a session is frozen on the session row at selection
time (``sessions.current_item_version``): repeated /next calls return the
delivered stem/options even after a formal import changes the item, and the
answer is validated and scored -- option keys, correct answer and the 2PL
IRT parameters of the Newton update -- against that frozen version, so an
in-flight import can never re-judge an answer by content the examinee never
saw.  A pending item withdrawn and re-selected binds the replacement's
current version, and a stored ``response_id`` replay still returns the first
result.  The answer-time ``content_tag`` freeze is unchanged: it keeps
reading the bank tag as it is when the answer is submitted.

The served revision's 2PL parameters are frozen onto each response row at
answer time (``responses.difficulty`` / ``responses.discrimination``).  The
session standard error shown by progress, /next and the precision stop rule
re-evaluates every answer's Fisher information at the current theta from
those per-answer frozen parameters -- never from the bank's current values
-- so a formal import recalibrating an already-answered item cannot drift
the SE of a session that received no new answers, and can neither trigger
nor miss ``precision_reached`` retroactively.  Legacy answers are backfilled
exactly once at first upgrade from the bank parameters of that moment and
are never recomputed afterwards.

``POST /api/sessions/{id}/review-plan`` turns a finished session's wrong
answers (the ``score = 0`` frozen at answer time) into a spaced-repetition
review plan: stem, options and the correct answer are frozen into the plan at
creation, and re-creating the plan for the same session returns the original
one.  ``GET /api/review/due`` lists the review items due on a UTC date and
``POST /api/review/items/{id}/results`` records a 0~5 review score, advancing
the SM-2 schedule (ease factor floor 1.3, intervals 1/6/round(interval*EF)
days) in the same write transaction.  The client ``review_id`` is bound to
the first submission's review item *and* score: a same-item, same-score
retry replays the first result without advancing the schedule again, while a
different item or score is a 409 conflict (checked before the due-date rule)
that leaves the stored result and the schedule untouched.


Mutating endpoints use ``BEGIN IMMEDIATE`` so concurrent requests on the same
SQLite database are serialised; exposure counts and ability updates therefore
cannot be lost to read/write races.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from typing import NamedTuple

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse, Response

from . import irt
from . import review
from . import revisions
from .analysis import analyze_item
from .calibration import calibrate_item
from .database import get_db, init_db
from .importer import NormalizedItem, RowError, validate_rows
from .models import (
    BankItemOut,
    HealthOut,
    ItemAnalysisOut,
    ItemAvailabilityUpdate,
    ItemBankOut,
    ItemCalibrationOut,
    ItemDetailOut,
    ItemImportErrorOut,
    ItemImportRequest,
    ItemImportResponse,
    ItemImportRowOut,
    ItemOut,
    ItemRevisionListOut,
    ItemRevisionOut,
    ItemRevisionSummaryOut,
    NextItemOut,
    ProgressOut,
    ResponseCreate,
    ResponseHistoryOut,
    ResponseOut,
    ReviewDueOut,
    ReviewItemOut,
    ReviewPlanOut,
    ReviewResultCreate,
    ReviewResultOut,
    SessionCreate,
    SessionOut,
    SessionSimilarityOut,
    SimilarityMatchOut,
)
from .similarity import find_similar_sessions

DEFAULT_EXPOSURE_LIMIT = int(os.getenv("DEFAULT_EXPOSURE_LIMIT", "5"))

app = FastAPI(
    title="自适应测验选题 API",
    description=(
        "基于 2PL IRT 模型的计算机自适应测验（CAT）："
        "单次 Newton 更新估计能力，信息量最大选题，全局曝光上限控制；"
        "活动会话支持暂停（冻结剩余时限）与恢复（按剩余秒数重算 UTC 截止时间）；"
        "题目可撤回/启用，撤回题不再参与新取题；"
        "已结束会话支持答卷雷同复核（同错选项加权相似度）；"
        "单题预测概率校准对比作答前保存的 p_correct 与实际得分；"
        "题库修订历史在正式导入新增/内容变化、启用状态实际变化时"
        "于同一写事务内追加完整快照版本（旧库首启建立基线，幂等不重复）；"
        "取题时在会话中冻结当次送达的题目版本：重复 /next 始终返回送达时的"
        "题干与选项，提交答案按该冻结版本的选项、正确答案与 IRT 参数校验计分"
        "（取题后正式导入改题不影响在途作答；挂起题撤回重选改关联新题的当前版本；"
        "已存 response_id 重放仍返回首次结果），"
        "作答提交时把送达版本的难度/区分度冻结进答题记录：进度、/next 与"
        "精度停测的标准误都在当前 θ 下按各条作答的冻结参数重算 Fisher 信息，"
        "之后正式导入修改已答题参数不再让标准误漂移、不会误触发或错过 "
        "precision_reached（旧库首启按当时题库参数一次性回填，之后不再回填）；"
        "作答提交时同时冻结该题当时的 content_tag：进度、/next 配额缺口与末段"
        "欠额限制、精度停测配额门槛均按冻结标签统计，之后导入改标签或"
        "撤回已答题不追溯改写已答归属、不误判配额已满足或提前结束"
        "（旧库首启按当时题库标签一次性回填）；"
        "已结束会话的错题（作答时冻结 score=0）可创建间隔复习计划（SM-2）："
        "创建时冻结题干/选项/正确答案，同一会话重复创建返回原计划；"
        "支持按 UTC 日期查询到期题与提交 0~5 分复习结果"
        "（review_id 绑定首次提交的复习题与评分：同题同分重放返回首次结果且不重复推进日程，"
        "题目或评分不同返回 409 且不改动结果与日程，冲突检查先于到期判断；"
        "仅接受已到期题，同一写事务记录结果并推进日程）。"
    ),
    version="1.12.0",
)


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    _request: Request, exc: RequestValidationError
) -> Response:
    """422 handler whose body is always strict-JSON serialisable.

    FastAPI echoes the offending input in each error, and a non-finite
    float (``Infinity``/``NaN`` in a non-strict request body) makes the
    default handler itself crash with 500 while rendering that echo.
    Replace such values with a marker string instead.
    """

    def sanitize(value: object) -> object:
        if isinstance(value, float) and not math.isfinite(value):
            return "<non-finite>"
        if isinstance(value, dict):
            return {k: sanitize(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [sanitize(v) for v in value]
        if isinstance(value, (str, int, bool)) or value is None:
            return value
        # Pydantic keeps the original exception object in a model-validator
        # error's ctx; render anything else as its string form.
        return str(value)

    body = json.dumps(
        {"detail": sanitize(exc.errors())}, ensure_ascii=False, allow_nan=False
    ).encode("utf-8")
    return Response(
        content=body, status_code=422, media_type="application/json"
    )


# -------------------------------------------------------------------------
# Lifecycle / small helpers
# -------------------------------------------------------------------------

@app.on_event("startup")
def _startup() -> None:
    init_db()


@contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Serialised write transaction (BEGIN IMMEDIATE) with explicit rollback."""
    conn.isolation_level = None  # autocommit mode: we manage the transaction
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_utc(value: str) -> datetime:
    """Parse a stored ISO-8601 UTC timestamp into an aware datetime."""
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:  # defensive: timestamps are always written as UTC
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def expire_if_due(
    conn: sqlite3.Connection,
    session: sqlite3.Row,
    now: datetime,
) -> sqlite3.Row | None:
    """Finish an active session whose deadline has passed.

    Must be called first thing inside a serialised write transaction.  When
    the session has an ``expires_at`` in the past it is marked finished with
    ``finish_reason = 'time_expired'`` and the pending item is cleared; the
    pending item does *not* get another exposure (selection already counted
    one) and no response row is written.

    The finishing UPDATE is committed immediately (still inside the same
    serialised transaction, before any other write) and the connection is
    returned to autocommit mode, so the caller can raise the 409 without the
    surrounding ``transaction()`` rolling the expiry back.  Returns the
    refreshed row when the session was expired, otherwise ``None``.
    """
    if session["status"] != "active" or session["expires_at"] is None:
        return None
    expires_at = parse_utc(session["expires_at"])
    if now < expires_at:
        return None
    conn.execute(
        """UPDATE sessions
           SET status = 'finished', finish_reason = 'time_expired',
               current_item_id = NULL, current_item_version = NULL,
               updated_at = ?
           WHERE id = ? AND status = 'active'""",
        (now.isoformat(timespec="seconds"), session["id"]),
    )
    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session["id"],)
    ).fetchone()
    conn.commit()
    return row


def time_expired_409(expired: sqlite3.Row) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=(
            "测验时限已到（UTC 截止时间 "
            f"{expired['expires_at']}），会话已结束："
            "finish_reason=time_expired"
        ),
    )


def session_paused_409(session: sqlite3.Row) -> HTTPException:
    return HTTPException(
        status_code=409,
        detail=(
            f"会话已暂停（paused_at={session['paused_at']}）："
            "暂停期间不能取题或作答，请先调用 "
            f"POST /api/sessions/{session['id']}/resume 恢复"
        ),
    )


def session_not_active_409(session: sqlite3.Row, *, verb: str) -> HTTPException:
    if verb == "恢复":
        requirement = "只有暂停中（paused）的会话可以恢复"
    else:
        requirement = "只有进行中（active）且未到期的会话可以暂停"
    return HTTPException(
        status_code=409,
        detail=f"{requirement}，当前 status={session['status']}",
    )


def session_remaining_seconds(
    session: sqlite3.Row, now: datetime
) -> int | None:
    """Seconds left on the time limit.

    * paused session -> the value frozen at pause time (the clock does not
      tick while paused);
    * active session with a deadline -> live ``expires_at - now`` countdown,
      floored at zero (an active session sitting on its deadline is treated
      as having no time left and gets closed by the next write as
      ``time_expired``);
    * no time limit / finished session -> ``None``.
    """
    if session["status"] == "paused":
        return session["remaining_seconds"]
    if session["status"] != "active" or session["expires_at"] is None:
        return None
    delta = (parse_utc(session["expires_at"]) - now).total_seconds()
    return max(0, int(delta))


def get_session_or_404(conn: sqlite3.Connection, session_id: str) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM sessions WHERE id = ?", (session_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="session not found")
    return row


def parse_options(raw: str) -> list[dict]:
    return json.loads(raw)


def latest_item_version(conn: sqlite3.Connection, item_id: str) -> int | None:
    """Current (maximum) revision version of an item; ``None`` when it has no
    recorded history (defensive -- every bank item gets at least a baseline
    or ``import_create`` version, so selections always freeze a version)."""
    row = conn.execute(
        "SELECT MAX(version) AS v FROM item_revisions WHERE item_id = ?",
        (item_id,),
    ).fetchone()
    return row["v"] if row is not None else None


def served_item_content(
    conn: sqlite3.Connection,
    item_row: sqlite3.Row | None,
    version: int | None,
) -> sqlite3.Row | None:
    """The item content the examinee was actually served.

    ``item_row`` is the live ``items`` row and ``version`` the revision
    frozen on the session at selection time.  Returns the matching
    ``item_revisions`` snapshot -- it exposes the same content column names
    as ``items`` (stem/options/answer/difficulty/discrimination/
    content_tag) -- falling back to the live row when no version was frozen
    (sessions predating the column) or the snapshot is missing (defensive).
    """
    if item_row is None or version is None:
        return item_row
    revision = conn.execute(
        "SELECT * FROM item_revisions WHERE item_id = ? AND version = ?",
        (item_row["id"], version),
    ).fetchone()
    return revision if revision is not None else item_row


def serialize_served_item(
    item_row: sqlite3.Row, content_row: sqlite3.Row
) -> ItemOut:
    """ItemOut for a pending item: identity from the live ``items`` row,
    content from the revision frozen when it was served (or the live row
    itself when nothing was frozen)."""
    return ItemOut(
        id=item_row["id"],
        difficulty=content_row["difficulty"],
        discrimination=content_row["discrimination"],
        content_tag=content_row["content_tag"],
        stem=content_row["stem"],
        options=parse_options(content_row["options"]),
    )


def serialize_bank_item(row: sqlite3.Row) -> BankItemOut:
    return BankItemOut(
        id=row["id"],
        difficulty=row["difficulty"],
        discrimination=row["discrimination"],
        content_tag=row["content_tag"],
        stem=row["stem"],
        answer=row["answer"],
        exposure_count=row["exposure_count"],
        is_active=bool(row["is_active"]),
    )


def answered_count(conn: sqlite3.Connection, session_id: str) -> int:
    return conn.execute(
        "SELECT COUNT(*) FROM responses WHERE session_id = ?", (session_id,)
    ).fetchone()[0]


def session_standard_error(
    conn: sqlite3.Connection, session_id: str, theta: float
) -> float | None:
    """SE of the ability estimate: ``1 / sqrt(sum I_j(theta))``.

    Fisher information of *every* item already answered in this session is
    re-evaluated at the current theta (2PL), using the difficulty and
    discrimination **frozen on each response row at answer time** (the
    parameters of the revision the examinee was actually served and scored
    with) -- never the item bank's current values.  A formal import that
    recalibrates an already-answered item therefore cannot drift the
    standard error of a session that received no new answers, and can
    neither trigger nor miss ``precision_reached`` retroactively.  Returns
    ``None`` when no item has been answered or the summed information is
    not positive; the precision stop rule can never fire in that case.
    """
    rows = conn.execute(
        """
        SELECT discrimination AS a, difficulty AS b
        FROM responses
        WHERE session_id = ?
        """,
        (session_id,),
    ).fetchall()
    # Legacy rows whose bank item is defensively missing could carry NULL
    # parameters; they contribute no information.
    total_info = math.fsum(
        irt.information(theta, r["a"], r["b"])
        for r in rows
        if r["a"] is not None and r["b"] is not None
    )
    return irt.standard_error(total_info)


def tag_minimums_for(conn: sqlite3.Connection, session_id: str) -> dict[str, int]:
    """Persisted content-tag quotas of a session (empty dict when none set)."""
    return {
        row["content_tag"]: row["minimum"]
        for row in conn.execute(
            "SELECT content_tag, minimum FROM session_tag_minimums "
            "WHERE session_id = ?",
            (session_id,),
        ).fetchall()
    }


def answered_by_tag(conn: sqlite3.Connection, session_id: str) -> dict[str, int]:
    """Number of already-answered items per non-empty content tag.

    Counts come from the tag **frozen on the response row at answer time**
    (``responses.content_tag``), not from the live ``items.content_tag``.
    Progress, the /next quota deficit and endgame restriction, and the
    precision-stop quota gate therefore stay stable after a later bank
    import retags an answered item or after the item is withdrawn: an
    already-answered item keeps the attribution it had when it was
    answered, and a quota can neither look already met nor reopen
    retroactively.
    """
    counts: Counter[str] = Counter()
    rows = conn.execute(
        """
        SELECT content_tag, COUNT(*) AS n
        FROM responses
        WHERE session_id = ? AND content_tag IS NOT NULL
          AND content_tag <> ''
        GROUP BY content_tag
        """,
        (session_id,),
    ).fetchall()
    for row in rows:
        counts[row["content_tag"]] = row["n"]
    return dict(counts)


def tag_shortfalls(
    minimums: dict[str, int], answered: dict[str, int]
) -> dict[str, int]:
    """Remaining quota deficit per tag; tags already met are omitted."""
    return {
        tag: min_qty - answered.get(tag, 0)
        for tag, min_qty in minimums.items()
        if answered.get(tag, 0) < min_qty
    }


class EligibleItemScan(NamedTuple):
    """Result of one single streaming pass over the eligible items.

    Only scalars (and one ``(information, id)`` pair per deficient tag) are
    retained — the candidate rows themselves are never materialised into a
    list:

    * ``saw_any``        — at least one eligible item exists in the pool;
    * ``global_winner``  — ``(information, id)`` with maximum Fisher
                           information (ties broken on the smallest id) over
                           every eligible item, or ``None`` when the pool is
                           empty;
    * ``available_by_tag`` — eligible-item count per non-empty content tag;
    * ``tag_winners``    — per deficient tag ``(information, id)`` winner,
                           i.e. the restricted endgame candidate for that tag.
    """

    saw_any: bool
    global_winner: tuple[float, str] | None
    available_by_tag: Counter[str]
    tag_winners: dict[str, tuple[float, str]]


def _better(
    candidate: tuple[float, str], best: tuple[float, str] | None
) -> bool:
    """True when *candidate* wins the ``(information, id)`` comparison.

    Maximum information first; a strict information tie goes to the smaller
    item id, so the result is independent of cursor iteration order.
    """
    return best is None or (-candidate[0], candidate[1]) < (-best[0], best[1])


def scan_eligible_items(
    conn: sqlite3.Connection,
    session_id: str,
    exposure_limit: int,
    theta: float,
    shortfalls: dict[str, int],
) -> EligibleItemScan:
    """Stream every eligible item once and compute all selection statistics.

    Eligibility is exactly the old pool: enabled (``is_active = 1``),
    ``exposure_count < exposure_limit`` and not already answered in this
    session.  The database cursor is consumed in a single pass — no
    candidate list is built and the rows are never rescanned — while each
    eligible item contributes, at most once, to:

    * the global best ``(information, id)``;
    * the per-tag eligible-item counter (used for the
      "deficient tag cannot reach its quota" check);
    * that tag's own best ``(information, id)`` when the item's tag is
      deficient (used for the endgame restriction that keeps only
      deficient-tag candidates).
    """
    rows = conn.execute(
        """
        SELECT id, discrimination, difficulty, content_tag
        FROM items
        WHERE is_active = 1
          AND exposure_count < ?
          AND id NOT IN (
              SELECT item_id FROM responses WHERE session_id = ?
          )
        """,
        (exposure_limit, session_id),
    )
    saw_any = False
    global_winner: tuple[float, str] | None = None
    available_by_tag: Counter[str] = Counter()
    tag_winners: dict[str, tuple[float, str]] = {}
    for row in rows:
        saw_any = True
        tag = row["content_tag"]
        if tag:
            available_by_tag[tag] += 1
        info = irt.information(theta, row["discrimination"], row["difficulty"])
        contender = (info, row["id"])
        if _better(contender, global_winner):
            global_winner = contender
        if tag in shortfalls:
            if _better(contender, tag_winners.get(tag)):
                tag_winners[tag] = contender
    return EligibleItemScan(
        saw_any=saw_any,
        global_winner=global_winner,
        available_by_tag=available_by_tag,
        tag_winners=tag_winners,
    )


def serialize_session(
    row: sqlite3.Row,
    n_answered: int,
    tag_minimums: dict[str, int] | None = None,
    standard_error: float | None = None,
) -> SessionOut:
    return SessionOut(
        id=row["id"],
        theta=row["theta"],
        current_item_id=row["current_item_id"],
        max_items=row["max_items"],
        exposure_limit=row["exposure_limit"],
        duration_seconds=row["duration_seconds"],
        expires_at=row["expires_at"],
        paused=row["status"] == "paused",
        paused_at=row["paused_at"],
        remaining_seconds=session_remaining_seconds(
            row, datetime.now(timezone.utc)
        ),
        tag_minimums=tag_minimums or {},
        target_se=row["target_se"],
        min_precision_items=row["min_precision_items"],
        standard_error=standard_error,
        status=row["status"],
        finish_reason=row["finish_reason"],
        answered_count=n_answered,
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def serialize_response(row: sqlite3.Row) -> ResponseOut:
    return ResponseOut(
        id=row["id"],
        session_id=row["session_id"],
        item_id=row["item_id"],
        response=row["response"],
        correct=bool(row["score"]),
        score=row["score"],
        correct_answer=row["correct_answer"],
        content_tag=row["content_tag"],
        difficulty=row["difficulty"],
        discrimination=row["discrimination"],
        theta_before=row["theta_before"],
        theta_after=row["theta_after"],
        theta_change=row["theta_after"] - row["theta_before"],
        p_correct=row["p_correct"],
        information_at_theta=row["information_at_theta"],
        exposure_count=row["exposure_count_at_select"],
        created_at=row["created_at"],
    )


# -------------------------------------------------------------------------
# Health / docs
# -------------------------------------------------------------------------

@app.get("/", include_in_schema=False)
def root() -> RedirectResponse:
    return RedirectResponse(url="/docs")


@app.get("/health", response_model=HealthOut, tags=["meta"])
def health(conn: sqlite3.Connection = Depends(get_db)) -> HealthOut:
    bank_size = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    return HealthOut(status="ok", bank_size=bank_size)


# -------------------------------------------------------------------------
# Item bank (read-only inspection)
# -------------------------------------------------------------------------

@app.get("/api/items", response_model=ItemBankOut, tags=["items"])
def list_items(
    limit: int = 100,
    offset: int = 0,
    conn: sqlite3.Connection = Depends(get_db),
) -> ItemBankOut:
    rows = conn.execute(
        "SELECT * FROM items ORDER BY id LIMIT ? OFFSET ?", (limit, offset)
    ).fetchall()
    return ItemBankOut(count=len(rows), items=[serialize_bank_item(r) for r in rows])


@app.get("/api/items/{item_id}", response_model=ItemDetailOut, tags=["items"])
def get_item(
    item_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> ItemDetailOut:
    row = conn.execute(
        "SELECT * FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="item not found")
    base = serialize_bank_item(row).model_dump()
    base["options"] = parse_options(row["options"])
    return ItemDetailOut(**base)


# -------------------------------------------------------------------------
# Item availability (withdraw / re-enable)
# -------------------------------------------------------------------------

@app.patch(
    "/api/items/{item_id}/availability",
    response_model=BankItemOut,
    tags=["items"],
    summary="撤回 / 启用题目（撤回题不再参与新取题）",
)
def set_item_availability(
    item_id: str,
    body: ItemAvailabilityUpdate,
    conn: sqlite3.Connection = Depends(get_db),
) -> BankItemOut:
    """Withdraw or re-enable one item; unknown ids get a 404.

    ``{"is_active": false}`` withdraws the item: it stops being eligible
    for new selections, while every stored response, counted exposure and
    session history is kept.  A session whose *pending* item was withdrawn
    clears the pending marker and re-selects by the normal rules inside
    the next ``/next`` transaction (the exposure already counted for the
    withdrawn item is **not** rolled back), and a *new* answer for the
    withdrawn item is rejected with 409 — a replayed ``response_id`` is
    still served from the idempotency store.  ``{"is_active": true}``
    re-enables the item.  The toggle itself is idempotent: setting the
    same state again simply returns the current row.

    Revision history: only an actual state flip (enabled -> withdrawn or
    withdrawn -> enabled) appends a new immutable version inside this same
    transaction (``change_source = withdraw`` / ``re_enable``), snapshotting
    the post-change item.  A same-value retry writes nothing and creates no
    version.
    """
    with transaction(conn):
        row = conn.execute(
            "SELECT * FROM items WHERE id = ?", (item_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail="item not found")
        new_active = 1 if body.is_active else 0
        if row["is_active"] != new_active:
            conn.execute(
                "UPDATE items SET is_active = ? WHERE id = ?",
                (new_active, item_id),
            )
            row = conn.execute(
                "SELECT * FROM items WHERE id = ?", (item_id,)
            ).fetchone()
            # Actual availability change: append the post-change snapshot in
            # the same transaction; a same-value retry never reaches here.
            revisions.record_revision(
                conn,
                item_id=item_id,
                change_source=(
                    revisions.SOURCE_WITHDRAW
                    if new_active == 0
                    else revisions.SOURCE_RE_ENABLE
                ),
            )
    return serialize_bank_item(row)


@app.get(
    "/api/items/{item_id}/analysis",
    response_model=ItemAnalysisOut,
    tags=["items"],
)
def item_analysis(
    item_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> ItemAnalysisOut:
    """Review one item against the answers saved by finished sessions.

    Strictly read-only: no session, exposure or bank row is touched.  Each
    finished session that answered the item *and* at least one other item
    contributes one sample of (target score, ratio of correct peer items);
    both numbers use the ``score`` frozen at answer time, so later bank
    edits (including a changed correct answer) cannot change the result.
    Returns ``pearson_correlation = null`` when fewer than 5 samples exist or
    either series has zero variance, and 404 when the item is unknown.
    """
    exists = conn.execute(
        "SELECT 1 FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail="item not found")
    result = analyze_item(conn, item_id)
    return ItemAnalysisOut(
        item_id=item_id,
        sample_count=result.sample_count,
        correct_rate=result.correct_rate,
        pearson_correlation=result.pearson_r,
    )


@app.get(
    "/api/items/{item_id}/calibration",
    response_model=ItemCalibrationOut,
    tags=["items"],
)
def item_calibration(
    item_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> ItemCalibrationOut:
    """Check one item's stored 2PL predictions against the observed scores.

    Strictly read-only: no session, exposure or bank row is touched.  Every
    ``responses`` row of a *finished* session that answered the item
    contributes one sample of (frozen ``score``, pre-scoring ``p_correct``);
    neither value is recomputed from the current ability estimate or the
    current item parameters, so later bank edits cannot change the result.
    Returns the sample count, the actual and mean predicted correct rates and
    the standardised deviation
    ``z = (Σscore − Σp_correct) / √Σ[p_correct(1−p_correct)]``; ``z`` and the
    deviation flag (``|z| >= 1.96``) are ``null`` when fewer than 30 samples
    exist or the denominator is zero, the rates are ``null`` when there is no
    history at all, and an unknown item gets a 404.
    """
    exists = conn.execute(
        "SELECT 1 FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail="item not found")
    result = calibrate_item(conn, item_id)
    return ItemCalibrationOut(
        item_id=item_id,
        sample_count=result.sample_count,
        actual_correct_rate=result.actual_correct_rate,
        mean_predicted_correct_rate=result.mean_predicted_correct_rate,
        z_score=result.z_score,
        deviation_flag=result.deviation_flag,
    )


# -------------------------------------------------------------------------
# Item revision history (read-only)
# -------------------------------------------------------------------------

def serialize_revision_summary(row: sqlite3.Row) -> ItemRevisionSummaryOut:
    return ItemRevisionSummaryOut(
        item_id=row["item_id"],
        version=row["version"],
        change_source=row["change_source"],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
    )


def serialize_revision(row: sqlite3.Row) -> ItemRevisionOut:
    return ItemRevisionOut(
        item_id=row["item_id"],
        version=row["version"],
        change_source=row["change_source"],
        stem=row["stem"],
        options=parse_options(row["options"]),
        answer=row["answer"],
        difficulty=row["difficulty"],
        discrimination=row["discrimination"],
        content_tag=row["content_tag"],
        is_active=bool(row["is_active"]),
        created_at=row["created_at"],
    )


@app.get(
    "/api/items/{item_id}/revisions",
    response_model=ItemRevisionListOut,
    tags=["items"],
    summary="查询题目的修订历史版本列表（元数据，按版本号升序；未知题目 404）",
)
def list_item_revisions(
    item_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> ItemRevisionListOut:
    """List every recorded version of one item, oldest first.

    The list carries version metadata only (version number, change source,
    the post-change enabled state and the UTC creation time); fetch a full
    snapshot from ``GET /api/items/{id}/revisions/{version}``.  Versions are
    continuous per item starting at 1 (the startup ``baseline``) and are
    append-only, so later imports can never rewrite an older entry.  An
    unknown item returns 404.
    """
    exists = conn.execute(
        "SELECT 1 FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail="item not found")
    rows = conn.execute(
        "SELECT item_id, version, change_source, is_active, created_at "
        "FROM item_revisions WHERE item_id = ? ORDER BY version ASC",
        (item_id,),
    ).fetchall()
    return ItemRevisionListOut(
        item_id=item_id,
        count=len(rows),
        results=[serialize_revision_summary(r) for r in rows],
    )


@app.get(
    "/api/items/{item_id}/revisions/{version}",
    response_model=ItemRevisionOut,
    tags=["items"],
    summary="查询指定版本的完整快照（题干/选项/答案/难度/区分度/标签/启用状态；404）",
)
def get_item_revision(
    item_id: str,
    version: int = Path(..., ge=1, description="版本号（>= 1）"),
    conn: sqlite3.Connection = Depends(get_db),
) -> ItemRevisionOut:
    """Return one immutable version snapshot.

    The snapshot contains the complete post-change content: stem, options,
    answer, difficulty, discrimination, content tag and the enabled state.
    It deliberately excludes ``exposure_count``, which changes whenever the
    item is served and is not a bank revision.  Snapshot rows are never
    updated or deleted, so this response is stable for the item's lifetime.
    An unknown item or an unknown version number returns 404.
    """
    exists = conn.execute(
        "SELECT 1 FROM items WHERE id = ?", (item_id,)
    ).fetchone()
    if exists is None:
        raise HTTPException(status_code=404, detail="item not found")
    row = conn.execute(
        "SELECT * FROM item_revisions WHERE item_id = ? AND version = ?",
        (item_id, version),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail="revision not found")
    return serialize_revision(row)


# -------------------------------------------------------------------------
# Item bank bulk import / pre-check
# -------------------------------------------------------------------------

# SQLite caps the number of host parameters at 999 by default; keep every
# chunk of the existing-id lookup well below that limit.
_EXISTING_ID_CHUNK = 500

_UPSERT_ITEM_SQL = """
INSERT INTO items
    (id, difficulty, discrimination, content_tag, stem,
     options, answer, exposure_count)
VALUES (?, ?, ?, ?, ?, ?, ?, 0)
ON CONFLICT(id) DO UPDATE SET
    difficulty     = excluded.difficulty,
    discrimination = excluded.discrimination,
    content_tag    = excluded.content_tag,
    stem           = excluded.stem,
    options        = excluded.options,
    answer         = excluded.answer
    -- exposure_count is intentionally NOT updated: a re-import must never
    -- reset the global exposure accounting of an already-served item.
    -- is_active is intentionally NOT updated either: re-importing an
    -- existing item keeps its withdrawn/enabled state, while a brand-new
    -- row takes the column DEFAULT (1 = enabled).
"""


def _existing_item_rows(
    conn: sqlite3.Connection, item_ids: list[str]
) -> dict[str, sqlite3.Row]:
    """Full pre-upsert ``items`` rows keyed by id (chunks stay under SQLite's
    999-parameter bound).  Needed because revision history compares the
    incoming payload against the complete pre-change content."""
    found: dict[str, sqlite3.Row] = {}
    for start in range(0, len(item_ids), _EXISTING_ID_CHUNK):
        chunk = item_ids[start : start + _EXISTING_ID_CHUNK]
        placeholders = ",".join("?" for _ in chunk)
        found.update(
            {r["id"]: r for r in conn.execute(
                f"SELECT * FROM items WHERE id IN ({placeholders})", chunk
            ).fetchall()}
        )
    return found


def _import_payload(
    *,
    dry_run: bool,
    total: int,
    errors: list[RowError],
    items: list[NormalizedItem] | None = None,
    existing_ids: set[str] | None = None,
) -> tuple[ItemImportResponse, int]:
    """Build the import response.

    Returns ``(payload, status_code)``.  When *errors* is non-empty no
    created/updated classification is returned (the batch cannot be applied
    as-is), and a real (non-dry-run) request is answered with 422.  With a
    clean batch every valid row is classified against *existing_ids*:
    ids already in the bank are ``updated``, the rest ``created``.
    """
    error_out = [
        ItemImportErrorOut(
            row=e.row, code=e.code, message=e.message, field=e.field, id=e.id
        )
        for e in errors
    ]
    valid = not errors
    existing_ids = existing_ids or set()
    results: list[ItemImportRowOut] = []
    created = updated = 0
    if valid and items is not None:
        # validate_rows already returns rows in request order; sort once more
        # defensively so the stable 1-based row numbering is guaranteed.
        for item in sorted(items, key=lambda n: n.row):
            if item.id in existing_ids:
                action = "updated"
                updated += 1
            else:
                action = "created"
                created += 1
            results.append(ItemImportRowOut(row=item.row, id=item.id, action=action))
    status_code = 200
    if not valid and not dry_run:
        status_code = 422
    return (
        ItemImportResponse(
            dry_run=dry_run,
            valid=valid,
            created_count=created,
            updated_count=updated,
            total=total,
            results=results,
            errors=error_out,
        ),
        status_code,
    )


@app.post(
    "/api/items/import",
    response_model=ItemImportResponse,
    tags=["items"],
    summary="题库批量导入 / 预检（逐行错误、事务原子 upsert、更新保留曝光数与启用状态）",
)
def import_items(
    body: ItemImportRequest, conn: sqlite3.Connection = Depends(get_db)
) -> ItemImportResponse | JSONResponse:
    """Validate and (optionally) bulk-insert/update item-bank rows.

    Every row is validated independently and *all* problems are reported at
    once against the row's stable 1-based index in the ``items`` array:

    * ``id`` — required, non-blank string, at most 128 chars; duplicate ids
      inside the same batch are rejected on every occurrence after the first
      (``duplicate_id_in_batch``);
    * ``options`` — 2~8 objects, each with a non-blank ``key`` and ``text``;
      keys are trimmed/upper-cased and must be unique within the row;
    * ``answer`` — must equal one of the (normalised) option keys;
    * ``difficulty`` — finite number in [-6, 6] (booleans/NaN/Infinity
      rejected);
    * ``discrimination`` — positive finite number in (0, 5];
    * ``content_tag`` — string or null; a blank/whitespace tag is
      normalised to null.

    ``dry_run=true`` is a pure pre-check: nothing is written; each valid row
    is still classified as ``created`` or ``updated`` according to the
    current bank and returned with HTTP 200, while any row errors are
    reported too.

    A real commit applies every row in **one** serialised
    (``BEGIN IMMEDIATE``) transaction via upsert: unknown ids are inserted
    and existing ids are updated in place.  An update overwrites
    difficulty/discrimination/content_tag/stem/options/answer but
    **preserves ``exposure_count`` and ``is_active``**, so re-importing a
    bank never resets global exposure accounting and never re-enables a
    withdrawn item (a brand-new row starts enabled).  If any row fails
    validation nothing is written at all and the response is HTTP 422
    (``valid=false``, counts zero); a database-level failure rolls the
    whole batch back.

    ``results`` and ``errors`` are both ordered by the stable row number, so
    callers can splice them back into their original spreadsheet lines.
    """
    total = len(body.items)
    items, errors = validate_rows(body.items)

    if errors:
        payload, status_code = _import_payload(
            dry_run=body.dry_run, total=total, errors=errors
        )
        return JSONResponse(
            status_code=status_code, content=payload.model_dump()
        )

    if body.dry_run:
        # Read-only snapshot; no write lock is taken and nothing is written,
        # and in particular no revision version is produced by a pre-check.
        existing_rows = _existing_item_rows(conn, [i.id for i in items])
        payload, _ = _import_payload(
            dry_run=True,
            total=total,
            errors=[],
            items=items,
            existing_ids=set(existing_rows),
        )
        return payload

    now = utc_now()
    with transaction(conn):
        # Re-classify inside the write lock: the serialised transaction makes
        # the exists-check plus upsert atomic against concurrent imports.
        existing_rows = _existing_item_rows(conn, [i.id for i in items])
        conn.executemany(
            _UPSERT_ITEM_SQL,
            [
                (
                    item.id,
                    item.difficulty,
                    item.discrimination,
                    item.content_tag,
                    item.stem,
                    json.dumps(item.options, ensure_ascii=False),
                    item.answer,
                )
                for item in items
            ],
        )
        # Revision history, in the SAME transaction (so a failure rolls the
        # versions back together with the upsert): a brand-new id gets an
        # import_create snapshot; an existing id gets an import_update version
        # only when its content actually differs.  Re-importing identical
        # content produces no version; is_active/exposure_count are preserved
        # by the upsert and therefore never trigger one here.
        for item in sorted(items, key=lambda n: n.row):
            old_row = existing_rows.get(item.id)
            if old_row is None:
                source = revisions.SOURCE_IMPORT_CREATE
            elif revisions.import_row_changed(old_row, item):
                source = revisions.SOURCE_IMPORT_UPDATE
            else:
                continue
            revisions.record_revision(
                conn, item_id=item.id, change_source=source, now=now
            )
    payload, _ = _import_payload(
        dry_run=False,
        total=total,
        errors=[],
        items=items,
        existing_ids=set(existing_rows),
    )
    return payload


# -------------------------------------------------------------------------
# Sessions
# -------------------------------------------------------------------------

@app.post(
    "/api/sessions",
    response_model=SessionOut,
    status_code=201,
    tags=["sessions"],
)
def create_session(
    body: SessionCreate, conn: sqlite3.Connection = Depends(get_db)
) -> SessionOut:
    session_id = uuid.uuid4().hex
    created = datetime.now(timezone.utc)
    now = created.isoformat(timespec="seconds")
    duration = body.duration_seconds
    expires_at = (
        (created + timedelta(seconds=duration)).isoformat(timespec="seconds")
        if duration is not None
        else None
    )
    with transaction(conn):
        minimums = body.tag_minimums or {}
        if minimums:
            # Every quota key must reference a non-empty content tag that
            # actually exists in the bank; the quota total must leave room
            # inside max_items (positive ints are already enforced by the
            # request model).  422 mirrors Pydantic-level request errors.
            empty_keys = [k for k in minimums if not k.strip()]
            if empty_keys:
                raise HTTPException(
                    status_code=422,
                    detail="tag_minimums 的键不能为空白内容标签",
                )
            quota_total = sum(minimums.values())
            if quota_total > body.max_items:
                raise HTTPException(
                    status_code=422,
                    detail=(
                        f"tag_minimums 配额之和（{quota_total}）"
                        f"不能超过 max_items（{body.max_items}）"
                    ),
                )
            placeholders = ",".join("?" for _ in minimums)
            known = {
                r["content_tag"]
                for r in conn.execute(
                    f"SELECT DISTINCT content_tag FROM items "
                    f"WHERE content_tag IS NOT NULL AND content_tag <> '' "
                    f"AND content_tag IN ({placeholders})",
                    tuple(minimums),
                ).fetchall()
            }
            unknown = sorted(set(minimums) - known)
            if unknown:
                raise HTTPException(
                    status_code=422,
                    detail=f"tag_minimums 包含题库不存在的内容标签：{unknown}",
                )

        conn.execute(
            """
            INSERT INTO sessions
                (id, theta, current_item_id, max_items, exposure_limit,
                 duration_seconds, expires_at, target_se, min_precision_items,
                 status, finish_reason, created_at, updated_at)
            VALUES (?, ?, NULL, ?, ?, ?, ?, ?, ?, 'active', NULL, ?, ?)
            """,
            (
                session_id,
                body.initial_theta,
                body.max_items,
                body.exposure_limit or DEFAULT_EXPOSURE_LIMIT,
                duration,
                expires_at,
                body.target_se,
                body.min_precision_items,
                now,
                now,
            ),
        )
        if minimums:
            conn.executemany(
                "INSERT INTO session_tag_minimums (session_id, content_tag, minimum) "
                "VALUES (?, ?, ?)",
                [(session_id, tag, qty) for tag, qty in minimums.items()],
            )
        row = get_session_or_404(conn, session_id)
    return serialize_session(row, 0, minimums)


@app.get("/api/sessions/{session_id}", response_model=SessionOut, tags=["sessions"])
def get_session(
    session_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> SessionOut:
    row = get_session_or_404(conn, session_id)
    n_answered = answered_count(conn, session_id)
    se = session_standard_error(conn, session_id, row["theta"])
    return serialize_session(
        row, n_answered, tag_minimums_for(conn, session_id), se
    )


@app.get(
    "/api/sessions/{session_id}/progress",
    response_model=ProgressOut,
    tags=["sessions"],
)
def session_progress(
    session_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> ProgressOut:
    session = get_session_or_404(conn, session_id)
    n_answered = answered_count(conn, session_id)
    answered_ids = [
        r["item_id"]
        for r in conn.execute(
            "SELECT item_id FROM responses WHERE session_id = ? ORDER BY created_at",
            (session_id,),
        ).fetchall()
    ]
    current_item = None
    if session["current_item_id"]:
        item_row = conn.execute(
            "SELECT * FROM items WHERE id = ?", (session["current_item_id"],)
        ).fetchone()
        if item_row is not None:
            # Show the revision frozen when the item was served, matching
            # what /next re-serves and /responses scores against.
            current_item = serialize_served_item(
                item_row,
                served_item_content(
                    conn, item_row, session["current_item_version"]
                ),
            )
    bank_size = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    minimums = tag_minimums_for(conn, session_id)
    answered_tags = answered_by_tag(conn, session_id)
    se = session_standard_error(conn, session_id, session["theta"])
    # Report zero answered for every quota tag even if it has no response yet.
    tag_answered = {tag: answered_tags.get(tag, 0) for tag in minimums}
    tag_answered.update(answered_tags)
    return ProgressOut(
        session_id=session_id,
        status=session["status"],
        theta=session["theta"],
        answered_count=n_answered,
        max_items=session["max_items"],
        bank_size=bank_size,
        percent_complete=round(n_answered / session["max_items"] * 100, 1),
        exposure_limit=session["exposure_limit"],
        duration_seconds=session["duration_seconds"],
        expires_at=session["expires_at"],
        paused=session["status"] == "paused",
        paused_at=session["paused_at"],
        remaining_seconds=session_remaining_seconds(
            session, datetime.now(timezone.utc)
        ),
        tag_minimums=minimums,
        tag_answered=dict(sorted(tag_answered.items())),
        target_se=session["target_se"],
        min_precision_items=session["min_precision_items"],
        standard_error=se,
        finish_reason=session["finish_reason"],
        current_item=current_item,
        answered_item_ids=answered_ids,
    )


@app.get(
    "/api/sessions/{session_id}/responses",
    response_model=ResponseHistoryOut,
    tags=["sessions"],
)
def session_responses(
    session_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> ResponseHistoryOut:
    session = get_session_or_404(conn, session_id)
    rows = conn.execute(
        "SELECT * FROM responses WHERE session_id = ? ORDER BY created_at",
        (session_id,),
    ).fetchall()
    return ResponseHistoryOut(
        session_id=session_id,
        status=session["status"],
        theta=session["theta"],
        count=len(rows),
        results=[serialize_response(r) for r in rows],
    )


# -------------------------------------------------------------------------
# Answer-sheet similarity review
# -------------------------------------------------------------------------

@app.get(
    "/api/sessions/{session_id}/similarity",
    response_model=SessionSimilarityOut,
    tags=["sessions"],
)
def session_similarity(
    session_id: str,
    min_shared: int = Query(
        4, ge=2, le=50, description="候选会话至少共答的题数（2~50）"
    ),
    limit: int = Query(
        20, ge=1, le=50, description="返回的相似会话条数上限（1~50）"
    ),
    conn: sqlite3.Connection = Depends(get_db),
) -> SessionSimilarityOut:
    """Review one finished session for answer-sheet similarity.

    Strictly read-only: no session, exposure or bank row is touched.  The
    target session must exist (404) and be finished (409); every other
    *finished* session sharing at least ``min_shared`` answered items is a
    candidate.  Sheets are aligned by ``item_id``; a shared item is a
    same-wrong match only when both sessions chose the same option and both
    stored scores are 0 (correct answers never count).  Each match weighs
    ``log((item_wrong_total + 2) / (option_wrong_count + 1))`` — the counts
    come from the frozen ``responses`` of all finished sessions, so editing
    the bank's correct answers afterwards cannot change the result.  The
    score is the weight sum divided by the shared-item count; results are
    ordered by score (desc), same-wrong count (desc), session id (asc) and
    truncated to ``limit``.  No candidates -> an empty list.
    """
    session = get_session_or_404(conn, session_id)
    if session["status"] != "finished":
        raise HTTPException(
            status_code=409,
            detail=(
                "答卷雷同复核仅面向已结束（finished）会话，"
                f"当前 status={session['status']}"
            ),
        )
    matches = find_similar_sessions(
        conn, session_id, min_shared=min_shared, limit=limit
    )
    return SessionSimilarityOut(
        session_id=session_id,
        min_shared=min_shared,
        limit=limit,
        count=len(matches),
        results=[
            SimilarityMatchOut(
                session_id=m.session_id,
                shared_count=m.shared_count,
                same_wrong_count=m.same_wrong_count,
                score=m.score,
            )
            for m in matches
        ],
    )


# -------------------------------------------------------------------------
# Pause / resume
# -------------------------------------------------------------------------

def _pause_resume_payload(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> SessionOut:
    """Build the SessionOut body returned by the pause/resume endpoints."""
    n_answered = answered_count(conn, row["id"])
    se = session_standard_error(conn, row["id"], row["theta"])
    return serialize_session(
        row, n_answered, tag_minimums_for(conn, row["id"]), se
    )


@app.post(
    "/api/sessions/{session_id}/pause",
    response_model=SessionOut,
    tags=["adaptive"],
)
def pause_session(
    session_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> SessionOut:
    """Pause an active, not-yet-expired session.

    Only an ``active`` session can be paused; a session that is already
    paused or finished is rejected with 409.  When the session carries a
    time limit the remaining seconds (``expires_at - now``) are frozen into
    ``remaining_seconds`` and ``paused_at`` is recorded; the stored deadline
    is left untouched, so the time limit simply stops counting while paused.
    Sessions without a time limit can be paused and resumed too (their
    ``remaining_seconds`` stays ``null``).  An expired session cannot be kept
    alive by pausing: the deadline check runs first and finishes it as
    ``time_expired`` with a 409, exactly as /next and /responses do.
    """
    with transaction(conn):
        session = get_session_or_404(conn, session_id)

        expired = expire_if_due(conn, session, datetime.now(timezone.utc))
        if expired is not None:
            raise time_expired_409(expired)
        if session["status"] != "active":
            # Double pause (or pause on a finished session) -> 409.
            raise session_not_active_409(session, verb="暂停")

        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="seconds")
        remaining = session_remaining_seconds(session, now)
        conn.execute(
            """UPDATE sessions
               SET status = 'paused', paused_at = ?, remaining_seconds = ?,
                   updated_at = ?
               WHERE id = ?""",
            (now_text, remaining, now_text, session_id),
        )
        row = get_session_or_404(conn, session_id)
        payload = _pause_resume_payload(conn, row)
    return payload


@app.post(
    "/api/sessions/{session_id}/resume",
    response_model=SessionOut,
    tags=["adaptive"],
)
def resume_session(
    session_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> SessionOut:
    """Resume a paused session.

    Only a ``paused`` session can be resumed; resuming an active or finished
    session returns 409.  The UTC deadline is recomputed from the frozen
    ``remaining_seconds`` (``expires_at = now + remaining_seconds``), the
    pause markers are cleared and the session becomes ``active`` again with
    its pending item, stored answers, theta and counted exposures all
    intact — re-fetching the pending item via /next afterwards returns the
    same item without adding another exposure.  Time-limit-free sessions
    simply clear the pause markers.
    """
    with transaction(conn):
        session = get_session_or_404(conn, session_id)
        if session["status"] != "paused":
            raise session_not_active_409(session, verb="恢复")

        now = datetime.now(timezone.utc)
        now_text = now.isoformat(timespec="seconds")
        # NULL for a time-limit-free session (COALESCE keeps it NULL);
        # otherwise the stored deadline moves forward by exactly the seconds
        # that were frozen at pause time.
        conn.execute(
            """UPDATE sessions
               SET status = 'active',
                   expires_at = COALESCE(
                       strftime('%Y-%m-%dT%H:%M:%S+00:00', ?,
                                '+' || remaining_seconds || ' seconds'),
                       expires_at
                   ),
                   paused_at = NULL,
                   remaining_seconds = NULL,
                   updated_at = ?
               WHERE id = ?""",
            (now_text, now_text, session_id),
        )
        row = get_session_or_404(conn, session_id)
        payload = _pause_resume_payload(conn, row)
    return payload


# -------------------------------------------------------------------------
# Adaptive item selection
# -------------------------------------------------------------------------

@app.post(
    "/api/sessions/{session_id}/next",
    response_model=NextItemOut,
    tags=["adaptive"],
)
def next_item(
    session_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> NextItemOut:
    """Select the next item.

    Eligibility: the item is enabled (``is_active = 1``),
    ``exposure_count < exposure_limit`` and the item has not already been
    answered in this session.  Selection: highest Fisher information at the
    current theta; ties break on the smallest item id, so the result is
    fully deterministic.

    A pending item that was withdrawn after being served is cleared inside
    this same transaction and a replacement is selected by the normal
    rules; the exposure counted when it was originally selected is not
    rolled back, and the replacement is bound to its own current revision
    version.  (If no eligible replacement exists the session finishes
    with the usual ``no_available_items`` /
    ``tag_minimums_unfulfillable`` reason.)

    A pending item that is still enabled is re-served exactly as it was
    delivered: the stem, options and IRT parameters come from the revision
    version frozen on the session at selection time
    (``sessions.current_item_version``), so a formal import that changed
    the item after it was served cannot swap the content the examinee is
    about to answer.  Sessions predating the frozen-version column fall
    back to the live bank content.

    When the session carries ``tag_minimums``, the per-tag deficit is
    computed from the content tags **frozen on each stored response at
    answer time** -- never from the items' current tags, so retagging or
    withdrawing an already-answered item cannot change quota progress.
    If the number of remaining answer slots equals the total deficit,
    candidates are restricted to deficient tags (same information/id
    tie-break); otherwise all eligible items remain in the pool.  If a
    deficient tag cannot reach its quota with the items still eligible,
    the session finishes with
    ``finish_reason = 'tag_minimums_unfulfillable'`` without serving an
    item (hence without adding an exposure).

    The whole read-check-select-write sequence runs inside one serialised
    write transaction.  That way a burst of concurrent calls on the same
    session is linearised: the first selects and counts one exposure, every
    later call finds the pending item and returns it unchanged.

    If the session has a ``duration_seconds`` limit whose UTC ``expires_at``
    has passed, the session is first atomically finished
    (``finish_reason = 'time_expired'``) with its pending item cleared, and
    the request is rejected with 409; no exposure is added.  A paused
    session is likewise rejected with 409, but its pending item is kept
    (pause never finishes or expires a session).
    """
    outcome: dict
    target_se: float | None = None
    min_precision_items: int | None = None
    se: float | None = None
    session: sqlite3.Row | None = None
    now_dt = datetime.now(timezone.utc)

    with transaction(conn):
        session = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")

        # Case 0: the time limit has elapsed -> finish atomically and reject
        # the call with 409.  No item is selected (no new exposure) and no
        # response row is written; any pending item is cleared.
        now_dt = datetime.now(timezone.utc)
        expired = expire_if_due(conn, session, now_dt)
        if expired is not None:
            raise time_expired_409(expired)

        # Case 0b: the session is paused -> /next is rejected with 409 and no
        # exposure is added; the pending item is preserved untouched.
        if session["status"] == "paused":
            raise session_paused_409(session)

        target_se = session["target_se"]
        min_precision_items = session["min_precision_items"]
        n_answered = answered_count(conn, session_id)
        # /next never changes theta or the response set, so the current SE is
        # the same for every branch below.
        se = session_standard_error(conn, session_id, session["theta"])
        now = utc_now()

        # Resolve the pending item first.  A pending item that has been
        # withdrawn since it was served can no longer be answered: clear the
        # pending marker here, inside the same serialised transaction, and
        # fall through to the normal selection rules below.  The exposure
        # counted when the item was selected is NOT rolled back.
        pending_row: sqlite3.Row | None = None
        withdrawn_pending: str | None = None
        if session["current_item_id"]:
            candidate_pending = conn.execute(
                "SELECT * FROM items WHERE id = ?", (session["current_item_id"],)
            ).fetchone()
            if candidate_pending is not None and candidate_pending["is_active"]:
                pending_row = candidate_pending
            else:
                # Withdrawn (or, defensively, no longer existing) pending
                # item: drop the marker and re-select below.
                withdrawn_pending = session["current_item_id"]
                conn.execute(
                    """UPDATE sessions
                       SET current_item_id = NULL, current_item_version = NULL,
                           updated_at = ?
                       WHERE id = ?""",
                    (now, session_id),
                )

        # Case 1: a served, unanswered (and still enabled) item is pending
        # -> return it as it was delivered: the content comes from the
        # revision version frozen at selection time, so an in-flight formal
        # import cannot swap the stem/options/IRT parameters the examinee
        # is answering.  No new exposure is counted.
        if pending_row is not None:
            content_row = served_item_content(
                conn, pending_row, session["current_item_version"]
            )
            outcome = {
                "status": session["status"],
                "item_row": pending_row,
                "content_row": content_row,
                "information": irt.information(
                    session["theta"],
                    content_row["discrimination"],
                    content_row["difficulty"],
                ),
                "exposure_limit": session["exposure_limit"],
                "answered_count": n_answered,
                "max_items": session["max_items"],
                "target_se": target_se,
                "min_precision_items": min_precision_items,
                "standard_error": se,
                "finish_reason": session["finish_reason"],
                "note": "pending_item: 该题尚未作答，重复请求不重复计曝光",
            }

        # Case 2: session already finished.
        elif session["status"] == "finished":
            outcome = {
                "status": "finished",
                "item_row": None,
                "content_row": None,
                "information": None,
                "exposure_limit": session["exposure_limit"],
                "answered_count": n_answered,
                "max_items": session["max_items"],
                "target_se": target_se,
                "min_precision_items": min_precision_items,
                "standard_error": se,
                "finish_reason": session["finish_reason"],
                "note": "测验已结束",
            }

        # Case 3: item quota reached -> finish eagerly.
        elif n_answered >= session["max_items"]:
            conn.execute(
                """UPDATE sessions
                   SET status = 'finished', finish_reason = 'max_items',
                       updated_at = ?
                   WHERE id = ?""",
                (now, session_id),
            )
            outcome = {
                "status": "finished",
                "item_row": None,
                "content_row": None,
                "information": None,
                "exposure_limit": session["exposure_limit"],
                "answered_count": n_answered,
                "max_items": session["max_items"],
                "target_se": target_se,
                "min_precision_items": min_precision_items,
                "standard_error": se,
                "finish_reason": "max_items",
                "note": "已达到最大题量",
            }

        else:
            minimums = tag_minimums_for(conn, session_id)
            shortfalls = (
                tag_shortfalls(minimums, answered_by_tag(conn, session_id))
                if minimums
                else {}
            )
            total_shortfall = sum(shortfalls.values())
            slots_left = session["max_items"] - n_answered
            # When the remaining answer slots equal the deficit exactly,
            # every remaining item must come from a deficient tag.
            restricted = bool(minimums and total_shortfall == slots_left)

            # One single streaming pass over the eligible items (enabled,
            # exposure under the limit, not yet answered here).  The cursor
            # is consumed once: the global winner, the per-tag available
            # counts and the per-deficient-tag winners are all computed in
            # that pass, so the candidates are never fetched into a list and
            # re-filtered/scanned.  The read happens under the write lock,
            # so the exposure counts seen here cannot change before the
            # update below.  Withdrawn items (is_active = 0) never take part
            # in new selections.
            scan = scan_eligible_items(
                conn,
                session_id,
                session["exposure_limit"],
                session["theta"],
                shortfalls,
            )

            # Whether the (possibly restricted) pool has any candidate.
            pool_has_item = (
                any(tag in scan.tag_winners for tag in shortfalls)
                if restricted
                else scan.saw_any
            )

            # Case 4: no eligible item left in the (possibly restricted)
            # pool -> finish.  With quotas set this also covers the case in
            # which some deficient tag has no eligible item left while
            # non-quota items are still available: the session cannot be
            # completed either way.
            if not pool_has_item:
                if minimums:
                    finish_reason = "tag_minimums_unfulfillable"
                    note = (
                        "欠额标签已没有满足曝光上限的候选题，无法补齐标签配额："
                        f"缺口 {shortfalls}"
                    )
                else:
                    finish_reason = "no_available_items"
                    note = "没有满足曝光上限的候选题"
                conn.execute(
                    """UPDATE sessions
                       SET status = 'finished',
                           finish_reason = ?,
                           updated_at = ?
                       WHERE id = ?""",
                    (finish_reason, now, session_id),
                )
                outcome = {
                    "status": "finished",
                    "item_row": None,
                    "content_row": None,
                    "information": None,
                    "exposure_limit": session["exposure_limit"],
                    "answered_count": n_answered,
                    "max_items": session["max_items"],
                    "target_se": target_se,
                    "min_precision_items": min_precision_items,
                    "standard_error": se,
                    "finish_reason": finish_reason,
                    "note": note,
                }

            else:
                # Even with slots to spare, if any deficient tag cannot
                # possibly reach its quota (not enough eligible items left
                # for that tag), finishing early is the only correct outcome:
                # no item is served and therefore no exposure is added.  In
                # the restricted endgame the per-tag counts are taken over
                # deficient tags only (non-deficient candidates are excluded),
                # matching the old endgame filtering.
                infeasible: dict[str, int] = {}
                if minimums:
                    available_by_tag = (
                        {
                            tag: scan.available_by_tag.get(tag, 0)
                            for tag in shortfalls
                        }
                        if restricted
                        else scan.available_by_tag
                    )
                    infeasible = {
                        tag: deficit
                        for tag, deficit in shortfalls.items()
                        if available_by_tag.get(tag, 0) < deficit
                    }
                if infeasible:
                    conn.execute(
                        """UPDATE sessions
                           SET status = 'finished',
                               finish_reason = 'tag_minimums_unfulfillable',
                               updated_at = ?
                           WHERE id = ?""",
                        (now, session_id),
                    )
                    outcome = {
                        "status": "finished",
                        "item_row": None,
                        "content_row": None,
                        "information": None,
                        "exposure_limit": session["exposure_limit"],
                        "answered_count": n_answered,
                        "max_items": session["max_items"],
                        "target_se": target_se,
                        "min_precision_items": min_precision_items,
                        "standard_error": se,
                        "finish_reason": "tag_minimums_unfulfillable",
                        "note": (
                            "欠额标签的可用题数不足以补齐配额，"
                            f"无法满足的缺口 {infeasible}"
                        ),
                    }

                # Case 5: select the maximum-information item (id tie-break),
                # count exactly one exposure and mark it pending atomically.
                else:
                    if restricted:
                        # Endgame: the pool is the deficient-tag items; pick
                        # the best (max info, min id) among the per-tag
                        # winners already computed in the single pass.
                        info, best_id = min(
                            (
                                scan.tag_winners[tag]
                                for tag in shortfalls
                                if tag in scan.tag_winners
                            ),
                            key=lambda winner: (-winner[0], winner[1]),
                        )
                    else:
                        # pool_has_item guarantees the global winner exists.
                        winner = scan.global_winner
                        assert winner is not None
                        info, best_id = winner
                    # Freeze the revision version being served on the session:
                    # repeated /next calls re-serve and the eventual answer is
                    # judged against this snapshot, even if a formal import
                    # changes the item before it is answered.  (The latest
                    # revision always mirrors the live bank content at this
                    # instant; a re-selection after a withdrawn pending item
                    # takes this same path and binds the replacement's
                    # current version.)
                    served_version = latest_item_version(conn, best_id)
                    conn.execute(
                        "UPDATE items SET exposure_count = exposure_count + 1 "
                        "WHERE id = ?",
                        (best_id,),
                    )
                    conn.execute(
                        "UPDATE sessions "
                        "SET current_item_id = ?, current_item_version = ?, "
                        "    updated_at = ? "
                        "WHERE id = ?",
                        (best_id, served_version, now, session_id),
                    )
                    item_row = conn.execute(
                        "SELECT * FROM items WHERE id = ?", (best_id,)
                    ).fetchone()
                    outcome = {
                        "status": "active",
                        "item_row": item_row,
                        "content_row": item_row,
                        "information": info,
                        "exposure_limit": session["exposure_limit"],
                        "answered_count": n_answered,
                        "max_items": session["max_items"],
                        "target_se": target_se,
                        "min_precision_items": min_precision_items,
                        "standard_error": se,
                        "finish_reason": None,
                        "note": None,
                    }

    if withdrawn_pending is not None:
        # The pending item was withdrawn: it was cleared and (when possible)
        # replaced inside the transaction above.  Surface that in the note;
        # the exposure counted at its original selection stays as-is.
        cleared_note = (
            f"原挂起题 {withdrawn_pending} 已撤回：已在同一事务内清除挂起项"
            "并按既有选题规则重选（该题已计曝光不回退）"
        )
        outcome["note"] = (
            f"{outcome['note']}；{cleared_note}"
            if outcome["note"]
            else cleared_note
        )

    item_row = outcome["item_row"]
    content_row = (
        outcome["content_row"]
        if outcome["content_row"] is not None
        else item_row
    )
    return NextItemOut(
        session_id=session_id,
        status=outcome["status"],
        item=(
            serialize_served_item(item_row, content_row)
            if item_row is not None
            else None
        ),
        information=outcome["information"],
        exposure_count=item_row["exposure_count"] if item_row is not None else None,
        exposure_limit=outcome["exposure_limit"],
        answered_count=outcome["answered_count"],
        max_items=outcome["max_items"],
        paused=outcome["status"] == "paused",
        paused_at=session["paused_at"] if outcome["status"] == "paused" else None,
        remaining_seconds=session_remaining_seconds(session, now_dt),
        target_se=outcome["target_se"],
        min_precision_items=outcome["min_precision_items"],
        standard_error=outcome["standard_error"],
        finish_reason=outcome["finish_reason"],
        note=outcome["note"],
    )


# -------------------------------------------------------------------------
# Answer submission (idempotent, one Newton update)
# -------------------------------------------------------------------------

@app.post(
    "/api/sessions/{session_id}/responses",
    response_model=ResponseOut,
    status_code=201,
    tags=["adaptive"],
)
def submit_response(
    session_id: str,
    body: ResponseCreate,
    conn: sqlite3.Connection = Depends(get_db),
) -> ResponseOut | JSONResponse:
    """Score one answer and apply exactly one Newton update.

    Idempotency contract: the client-generated ``response_id`` is the
    dedupe key.  The deadline check is the *first* statement inside the
    serialised write transaction: once ``expires_at`` has passed the session
    is atomically finished (``time_expired``) with its pending item cleared,
    and the call is rejected with 409 — no exposure is added and no response
    row is written.  Afterwards the idempotency lookup is checked under the
    same write lock, so even a burst of concurrent retries observes the
    first committed record and is replayed (HTTP 200) instead of being
    scored a second time.  A paused session rejects *new* answers with 409
    (the pending item is preserved), but a replayed ``response_id`` is still
    served by the idempotency lookup above.  A *new* answer for a withdrawn
    item (``is_active = 0``) is likewise rejected with 409 — no response
    row is written and theta is not updated — while a replayed
    ``response_id`` still returns the first stored result.

    The answer is judged against the revision version frozen on the session
    when the item was served (``sessions.current_item_version``): the
    submitted option key is validated against that version's options, the
    score compares it with that version's correct answer, and the Newton
    update plus the stored ``p_correct``/``information_at_theta`` use that
    version's 2PL parameters — so a formal import changing the item after
    it was served can never re-judge the answer by content the examinee
    never saw, and the stored result stays consistent with the ability
    update.  (Sessions predating the frozen-version column fall back to the
    live bank content.)  The served revision's difficulty/discrimination
    are also frozen onto the response row itself: the session standard
    error (progress, /next and the precision stop below) is always
    recomputed from those per-answer frozen parameters, so a later bank
    recalibration cannot drift the SE of already-answered items.  The
    frozen ``content_tag`` is still the bank tag as it is at answer time,
    exactly as before.

    After the theta update the optional precision stop is evaluated in the
    same transaction: when the session has a ``target_se``, the summed 2PL
    Fisher information of every answered item is recomputed at the new
    theta **from the per-answer frozen parameters** and the session
    finishes with ``finish_reason = 'precision_reached'`` once the answered
    count reaches ``min_precision_items``, the standard error is at most
    ``target_se`` and all tag quotas are met.  The quota check counts the
    tags frozen on each response at answer time, so a later bank retag
    cannot make a missing quota look met and trigger the stop early; just
    as a later bank recalibration cannot move the SE itself.  A replayed
    request takes the idempotency branch above and never re-evaluates it.
    """
    response_key = body.response.strip().upper()
    stored_row: sqlite3.Row | None = None
    replay = False

    with transaction(conn):
        # 1) Deadline check, first inside the serialised write transaction:
        #    expiry finishes the session atomically and rejects the call;
        #    nothing else (exposure, response row) is written.
        session = conn.execute(
            "SELECT * FROM sessions WHERE id = ?", (session_id,)
        ).fetchone()
        if session is None:
            raise HTTPException(status_code=404, detail="session not found")
        expired = expire_if_due(conn, session, datetime.now(timezone.utc))
        if expired is not None:
            raise time_expired_409(expired)

        # 2) Idempotency replay, checked after acquiring the write lock.
        existing = conn.execute(
            "SELECT * FROM responses WHERE id = ?", (body.response_id,)
        ).fetchone()
        if existing is not None:
            if existing["session_id"] != session_id or existing["item_id"] != body.item_id:
                raise HTTPException(
                    status_code=409,
                    detail="response_id 已被其他会话或题目使用",
                )
            stored_row, replay = existing, True
        else:
            # 3) Validate the pending item state (existence, deadline and
            #    finished status have already been checked above).
            if session["status"] == "paused":
                # A *new* answer is rejected while paused; the pending item
                # and every stored answer are preserved.  An idempotent
                # replay took the branch above and is still served.
                raise session_paused_409(session)
            if session["status"] == "finished":
                raise HTTPException(status_code=409, detail="测验已结束")
            if session["current_item_id"] != body.item_id:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        "item_id 与当前待作答题不一致或该题已作答："
                        "请先调用 "
                        f"POST /api/sessions/{session_id}/next 获取题目；"
                        "如为重试请求，请复用原 response_id"
                    ),
                )

            item = conn.execute(
                "SELECT * FROM items WHERE id = ?", (body.item_id,)
            ).fetchone()
            if item is None:
                raise HTTPException(status_code=404, detail="item not found")
            if not item["is_active"]:
                # A withdrawn item accepts no new answers: no response row is
                # written and theta is left untouched.  A replayed
                # response_id took the idempotency branch above and is still
                # served; the pending marker is cleared by the next /next.
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"题目 {body.item_id} 已撤回，不再接受新的作答；"
                        "请调用 "
                        f"POST /api/sessions/{session_id}/next 重新选题"
                    ),
                )

            # Judge the answer against the revision frozen when the item
            # was served: options, correct answer and IRT parameters all
            # come from that snapshot, so an in-flight formal import cannot
            # re-judge the answer by content the examinee never saw.  A
            # legacy session without a frozen version falls back to the
            # live row (the pre-freeze behaviour).
            served = served_item_content(
                conn, item, session["current_item_version"]
            )
            option_keys = {opt["key"] for opt in parse_options(served["options"])}
            if response_key not in option_keys:
                raise HTTPException(
                    status_code=422,
                    detail=f"无效作答选项：{body.response!r}，可选 {sorted(option_keys)}",
                )

            # 4) Score and apply exactly one Newton-Raphson update.
            score = 1 if response_key == served["answer"] else 0
            theta_before = session["theta"]
            theta_after, p_correct, _, info = irt.newton_step(
                theta_before, served["discrimination"], served["difficulty"], score
            )
            now = utc_now()
            exposure_at_select = item["exposure_count"]
            # Freeze the item's content tag at answer time.  Every later
            # quota statistic (progress, /next endgame restriction,
            # precision-stop gate) reads this copy, so retagging the bank
            # item afterwards cannot move this answer between tags.  This
            # deliberately keeps reading the LIVE bank tag at answer time
            # (unchanged behaviour), independent of the served-version
            # snapshot used for scoring above.
            frozen_tag = item["content_tag"] or None
            # Freeze the SERVED revision's 2PL parameters onto the response
            # row.  The session standard error (progress, /next and the
            # precision stop rule) re-evaluates Fisher information from
            # these copies, so a later formal import recalibrating the bank
            # item can never drift the SE of this session or re-judge the
            # precision stop retroactively.  The values are exactly those
            # the Newton update above was scored with.
            frozen_difficulty = served["difficulty"]
            frozen_discrimination = served["discrimination"]

            try:
                conn.execute(
                    """
                    INSERT INTO responses (
                        id, session_id, item_id, response, score, correct_answer,
                        theta_before, theta_after, p_correct,
                        information_at_theta, exposure_count_at_select,
                        content_tag, difficulty, discrimination, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        body.response_id,
                        session_id,
                        body.item_id,
                        response_key,
                        score,
                        served["answer"],
                        theta_before,
                        theta_after,
                        p_correct,
                        info,
                        exposure_at_select,
                        frozen_tag,
                        frozen_difficulty,
                        frozen_discrimination,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                # Database-level backstop for a concurrent key collision.
                conn.rollback()
                racer = conn.execute(
                    "SELECT * FROM responses WHERE id = ?", (body.response_id,)
                ).fetchone()
                if racer is not None:
                    if racer["session_id"] != session_id or racer["item_id"] != body.item_id:
                        raise HTTPException(
                            status_code=409,
                            detail="response_id 已被其他会话或题目使用",
                        )
                    # Re-read outside the rolled-back transaction below.
                    stored_row, replay = racer, True
                else:
                    raise HTTPException(status_code=409, detail="重复作答冲突")
            else:
                # 5) Advance session state; finish eagerly at max_items, or
                #    when the optional precision stop rule is satisfied.
                conn.execute(
                    """UPDATE sessions
                       SET theta = ?, current_item_id = NULL,
                           current_item_version = NULL, updated_at = ?
                       WHERE id = ?""",
                    (theta_after, now, session_id),
                )
                total = conn.execute(
                    "SELECT COUNT(*) FROM responses WHERE session_id = ?",
                    (session_id,),
                ).fetchone()[0]
                if total >= session["max_items"]:
                    # A full answer sheet wins over every other stop rule.
                    conn.execute(
                        """UPDATE sessions
                           SET status = 'finished', finish_reason = 'max_items',
                               updated_at = ?
                           WHERE id = ?""",
                        (now, session_id),
                    )
                elif session["target_se"] is not None:
                    # Optional precision stop: re-evaluate the summed 2PL
                    # Fisher information of *all* answered items at the new
                    # theta, from the parameters frozen on each response at
                    # answer time (a bank recalibration after the fact can
                    # never move this SE).  The session ends in this same
                    # transaction only when the answer count has reached the
                    # configured floor, SE <= target_se and every tag quota
                    # is already met.  A zero/non-positive total gives
                    # SE = NULL and can never fire; otherwise the selection
                    # flow continues unchanged.
                    se = session_standard_error(conn, session_id, theta_after)
                    quota_met = not tag_shortfalls(
                        tag_minimums_for(conn, session_id),
                        answered_by_tag(conn, session_id),
                    )
                    if (
                        se is not None
                        and total >= session["min_precision_items"]
                        and se <= session["target_se"]
                        and quota_met
                    ):
                        conn.execute(
                            """UPDATE sessions
                               SET status = 'finished',
                                   finish_reason = 'precision_reached',
                                   updated_at = ?
                               WHERE id = ?""",
                            (now, session_id),
                        )
                stored_row = conn.execute(
                    "SELECT * FROM responses WHERE id = ?", (body.response_id,)
                ).fetchone()

    payload = serialize_response(stored_row).model_dump()
    return JSONResponse(status_code=200 if replay else 201, content=payload)


# -------------------------------------------------------------------------
# Wrong-question spaced review (SM-2)
# -------------------------------------------------------------------------

def serialize_review_item(row: sqlite3.Row) -> ReviewItemOut:
    return ReviewItemOut(
        id=row["id"],
        plan_id=row["plan_id"],
        item_id=row["item_id"],
        stem=row["stem"],
        options=parse_options(row["options"]),
        answer=row["answer"],
        ease_factor=row["ease_factor"],
        interval_days=row["interval_days"],
        repetitions=row["repetitions"],
        due_date=row["due_date"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def serialize_review_result(row: sqlite3.Row) -> ReviewResultOut:
    return ReviewResultOut(
        review_id=row["review_id"],
        review_item_id=row["review_item_id"],
        score=row["score"],
        ease_factor_before=row["ease_factor_before"],
        ease_factor_after=row["ease_factor_after"],
        interval_before=row["interval_before"],
        interval_after=row["interval_after"],
        repetitions_before=row["repetitions_before"],
        repetitions_after=row["repetitions_after"],
        due_date_before=row["due_date_before"],
        due_date_after=row["due_date_after"],
        created_at=row["created_at"],
    )


def review_plan_payload(
    conn: sqlite3.Connection, plan_row: sqlite3.Row
) -> ReviewPlanOut:
    items = conn.execute(
        "SELECT * FROM review_items WHERE plan_id = ? ORDER BY item_id ASC",
        (plan_row["id"],),
    ).fetchall()
    return ReviewPlanOut(
        id=plan_row["id"],
        session_id=plan_row["session_id"],
        item_count=len(items),
        items=[serialize_review_item(r) for r in items],
        created_at=plan_row["created_at"],
    )


@app.post(
    "/api/sessions/{session_id}/review-plan",
    response_model=ReviewPlanOut,
    status_code=201,
    tags=["review"],
    summary="按已结束会话的错题（作答时冻结 score=0）创建间隔复习计划；重复创建返回原计划",
)
def create_review_plan(
    session_id: str, conn: sqlite3.Connection = Depends(get_db)
) -> ReviewPlanOut | JSONResponse:
    """Create the spaced-repetition plan of one finished session.

    The session must exist (404) and be finished (409).  Every stored
    response whose ``score`` — frozen at answer time — is 0 contributes one
    review item; the stem, options and correct answer are copied from the
    bank **at creation time** and frozen into the plan, so later bank edits
    or withdrawals never change the review content.  Each item starts with
    ``ease_factor = 2.5``, ``interval_days = 0``, ``repetitions = 0`` and a
    ``due_date`` of the creation day (UTC): initial items are due the same
    day.  A session with no wrong answers yields an empty plan.

    The creation is idempotent per session: ``review_plans.session_id`` is
    unique and the lookup runs under the same write lock, so a repeated
    creation returns the original plan unchanged (HTTP 200 instead of 201)
    and concurrent duplicates cannot create a second plan.
    """
    replay = False
    with transaction(conn):
        session = get_session_or_404(conn, session_id)
        if session["status"] != "finished":
            raise HTTPException(
                status_code=409,
                detail=(
                    "只有已结束（finished）的会话才能创建错题复习计划，"
                    f"当前 status={session['status']}"
                ),
            )
        plan_row = conn.execute(
            "SELECT * FROM review_plans WHERE session_id = ?", (session_id,)
        ).fetchone()
        if plan_row is not None:
            # Same-session retry: return the original plan as-is; no row is
            # added or modified.
            replay = True
        else:
            now_dt = datetime.now(timezone.utc)
            now = now_dt.isoformat(timespec="seconds")
            today = now_dt.date().isoformat()
            plan_id = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO review_plans (id, session_id, created_at) "
                "VALUES (?, ?, ?)",
                (plan_id, session_id, now),
            )
            wrong_rows = conn.execute(
                """
                SELECT r.item_id AS item_id, i.stem AS stem,
                       i.options AS options, i.answer AS answer
                  FROM responses r JOIN items i ON i.id = r.item_id
                 WHERE r.session_id = ? AND r.score = 0
                 ORDER BY r.item_id ASC
                """,
                (session_id,),
            ).fetchall()
            for wrong in wrong_rows:
                conn.execute(
                    """
                    INSERT INTO review_items
                        (id, plan_id, item_id, stem, options, answer,
                         ease_factor, interval_days, repetitions,
                         due_date, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0, ?, ?, ?)
                    """,
                    (
                        uuid.uuid4().hex,
                        plan_id,
                        wrong["item_id"],
                        wrong["stem"],
                        wrong["options"],
                        wrong["answer"],
                        review.INITIAL_EASE_FACTOR,
                        today,
                        now,
                        now,
                    ),
                )
            plan_row = conn.execute(
                "SELECT * FROM review_plans WHERE id = ?", (plan_id,)
            ).fetchone()
        payload = review_plan_payload(conn, plan_row)
    return JSONResponse(
        status_code=200 if replay else 201, content=payload.model_dump()
    )


@app.get(
    "/api/review/due",
    response_model=ReviewDueOut,
    tags=["review"],
    summary="按 UTC 日期查询到期复习题（due_date <= date，缺省为今天；只读）",
)
def list_due_review_items(
    date: date | None = Query(
        None, description="UTC 日期（YYYY-MM-DD），缺省为今天；非法日期返回 422"
    ),
    conn: sqlite3.Connection = Depends(get_db),
) -> ReviewDueOut:
    """List every review item due on or before a UTC date.

    Strictly read-only.  ``date`` defaults to today (UTC); items whose
    ``due_date`` is on or before it are returned (overdue items stay due),
    ordered by due date then review-item id for a stable result.  Each entry
    carries the frozen stem/options/answer plus the current SM-2 schedule
    state (ease factor, interval, consecutive successes, due date).
    """
    day = (date or datetime.now(timezone.utc).date()).isoformat()
    rows = conn.execute(
        """
        SELECT * FROM review_items
         WHERE due_date <= ?
         ORDER BY due_date ASC, id ASC
        """,
        (day,),
    ).fetchall()
    return ReviewDueOut(
        date=day,
        count=len(rows),
        results=[serialize_review_item(r) for r in rows],
    )


def review_result_conflict_409(
    stored: sqlite3.Row, review_item_id: str, score: int
) -> HTTPException | None:
    """409 to raise when a stored review result clashes with this submission.

    ``review_id`` is bound to the review item *and* the score of its first
    submission: only a same-item, same-score retry is an idempotent replay.
    Returns ``None`` when the submission matches the stored first result.
    """
    if stored["review_item_id"] != review_item_id:
        return HTTPException(
            status_code=409, detail="review_id 已被其他复习题使用"
        )
    if stored["score"] != score:
        return HTTPException(
            status_code=409,
            detail=(
                f"review_id 已绑定首次提交的评分（{stored['score']}），"
                f"本次提交的评分（{score}）不同：评分冲突，"
                "已存结果与复习日程均未改动；"
                "如确需重新评分，请使用新的 review_id"
            ),
        )
    return None


@app.post(
    "/api/review/items/{review_item_id}/results",
    response_model=ReviewResultOut,
    status_code=201,
    tags=["review"],
    summary="提交 0~5 分复习结果（review_id 绑定首次题目与评分：同题同分重放幂等，不同则 409；仅接受已到期题；同事务记录结果并推进日程）",
)
def submit_review_result(
    review_item_id: str,
    body: ReviewResultCreate,
    conn: sqlite3.Connection = Depends(get_db),
) -> ReviewResultOut | JSONResponse:
    """Record one 0~5 review score and advance the SM-2 schedule.

    Idempotency/conflict contract: the client-generated ``review_id`` is
    bound to the review item **and** the score of its first submission.  The
    binding is checked first inside the serialised write transaction —
    *before* the due-date check — so:

    * a retry with the same item and the same score is an idempotent
      replay: the stored first result is returned (HTTP 200) and the
      schedule is **not** advanced again, even though the first result
      already pushed the item's due date into the future;
    * a ``review_id`` submitted for a different review item or with a
      different score is a conflict: HTTP 409, and neither the stored
      result nor the schedule is modified (the conflict is reported even
      when the item is currently not due).

    An unknown review item gets a 404.

    Only **due** items accept a *new* result: when ``due_date`` is later
    than today (UTC) the call is rejected with 409 and nothing is written.
    A first result always moves the due date at least one day out, so a
    second *new* result for the same item on the same day cannot slip
    through.

    The result row (with the full before/after snapshot) and the schedule
    update are written in the **same** ``BEGIN IMMEDIATE`` transaction:

    * score < 3: retrain the next day (interval 1) and reset the consecutive
      success count to 0;
    * score >= 3: the 1st/2nd consecutive successes use intervals of 1/6
      days, later ones ``round(旧间隔 × 熟练因子)`` (half-up, using the
      pre-update ease factor);
    * after every result the ease factor is updated to
      ``max(1.3, EF + 0.1 - (5-q) * (0.08 + (5-q) * 0.02))``;
    * the new due date is the review day (UTC) plus the new interval.
    """
    stored_row: sqlite3.Row | None = None
    replay = False

    with transaction(conn):
        item = conn.execute(
            "SELECT * FROM review_items WHERE id = ?", (review_item_id,)
        ).fetchone()
        if item is None:
            raise HTTPException(status_code=404, detail="review item not found")

        # 1) Idempotency + conflict check, under the write lock and BEFORE
        #    the due check: review_id is bound to the review item and the
        #    score of the first submission.  A same-item/same-score retry is
        #    replayed (even though the first result already moved the due
        #    date); a different item or a different score is a 409 conflict
        #    that leaves the stored result and the schedule untouched.
        existing = conn.execute(
            "SELECT * FROM review_results WHERE review_id = ?", (body.review_id,)
        ).fetchone()
        if existing is not None:
            conflict = review_result_conflict_409(
                existing, review_item_id, body.score
            )
            if conflict is not None:
                raise conflict
            stored_row, replay = existing, True
        else:
            # 2) Only due items accept a new result (UTC calendar dates
            #    compare lexicographically).
            today_dt = datetime.now(timezone.utc).date()
            today = today_dt.isoformat()
            if item["due_date"] > today:
                raise HTTPException(
                    status_code=409,
                    detail=(
                        f"复习题尚未到期（due_date={item['due_date']}），"
                        "仅接受计划中已到期题；"
                        "请通过 GET /api/review/due 查询到期题"
                    ),
                )

            # 3) Record the result and advance the schedule atomically.
            ef_after, interval_after, reps_after = review.advance(
                body.score,
                item["ease_factor"],
                item["interval_days"],
                item["repetitions"],
            )
            due_after = (today_dt + timedelta(days=interval_after)).isoformat()
            now = utc_now()
            try:
                conn.execute(
                    """
                    INSERT INTO review_results
                        (review_id, review_item_id, score,
                         ease_factor_before, ease_factor_after,
                         interval_before, interval_after,
                         repetitions_before, repetitions_after,
                         due_date_before, due_date_after, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        body.review_id,
                        review_item_id,
                        body.score,
                        item["ease_factor"],
                        ef_after,
                        item["interval_days"],
                        interval_after,
                        item["repetitions"],
                        reps_after,
                        item["due_date"],
                        due_after,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                # Database-level backstop for a concurrent key collision.
                conn.rollback()
                racer = conn.execute(
                    "SELECT * FROM review_results WHERE review_id = ?",
                    (body.review_id,),
                ).fetchone()
                if racer is not None:
                    conflict = review_result_conflict_409(
                        racer, review_item_id, body.score
                    )
                    if conflict is not None:
                        raise conflict
                    stored_row, replay = racer, True
                else:
                    raise HTTPException(status_code=409, detail="复习结果提交冲突")
            else:
                conn.execute(
                    """
                    UPDATE review_items
                       SET ease_factor = ?, interval_days = ?, repetitions = ?,
                           due_date = ?, updated_at = ?
                     WHERE id = ?
                    """,
                    (
                        ef_after,
                        interval_after,
                        reps_after,
                        due_after,
                        now,
                        review_item_id,
                    ),
                )
                stored_row = conn.execute(
                    "SELECT * FROM review_results WHERE review_id = ?",
                    (body.review_id,),
                ).fetchone()

    payload = serialize_review_result(stored_row).model_dump()
    return JSONResponse(status_code=200 if replay else 201, content=payload)
