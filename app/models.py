"""Pydantic request/response models."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, model_validator

from .importer import MAX_IMPORT_ITEMS
from .irt import THETA_MAX, THETA_MIN

# JSON booleans must not sneak in as quota values; strict ints reject them.
PositiveIntField = Annotated[int, Field(strict=True, gt=0)]


# ---- items ----------------------------------------------------------------

class OptionOut(BaseModel):
    key: str
    text: str


class ItemOut(BaseModel):
    """An item as served to an examinee (the correct answer is not exposed)."""

    id: str
    difficulty: float
    discrimination: float
    content_tag: str | None = None
    stem: str
    options: list[OptionOut]


class BankItemOut(BaseModel):
    """Item bank row for administration/inspection, including exposure."""

    id: str
    difficulty: float
    discrimination: float
    content_tag: str | None = None
    stem: str
    answer: str
    exposure_count: int
    is_active: bool = Field(
        ..., description="是否启用：false 表示已撤回，不再参与新取题"
    )


class ItemAvailabilityUpdate(BaseModel):
    """PATCH body enabling or withdrawing one bank item."""

    is_active: bool = Field(
        ...,
        strict=True,
        description="true=启用（参与新取题）；false=撤回（不再参与新取题）",
    )


class ItemDetailOut(BankItemOut):
    options: list[OptionOut]


# ---- item revision history -------------------------------------------------

RevisionChangeSource = Literal[
    "baseline", "import_create", "import_update", "withdraw", "re_enable"
]


class ItemRevisionSummaryOut(BaseModel):
    """One entry in an item's version list (metadata only, no snapshot body)."""

    item_id: str = Field(..., description="题目 ID")
    version: int = Field(
        ..., ge=1, description="版本号，每题内从 1 开始连续递增、永不复用"
    )
    change_source: RevisionChangeSource = Field(
        ...,
        description=(
            "变更来源：baseline=旧库首启基线（含初始演示题库）；"
            "import_create=正式批量导入新增；import_update=正式导入且内容实际变化；"
            "withdraw=启用状态实际变为撤回；re_enable=撤回后实际重新启用"
        ),
    )
    is_active: bool = Field(
        ...,
        description="该版本变更后的启用状态：false 为撤回（快照内同样保存该值）",
    )
    created_at: str = Field(
        ..., description="版本写入时刻（UTC，ISO-8601）；与原写事务同一事务内记录"
    )


class ItemRevisionListOut(BaseModel):
    item_id: str
    count: int = Field(..., description="该题历史版本总数")
    results: list[ItemRevisionSummaryOut] = Field(
        default_factory=list,
        description="版本列表，按 version 升序；未知题目返回 404",
    )


class ItemRevisionOut(BaseModel):
    """A specific version's complete immutable snapshot."""

    item_id: str
    version: int = Field(..., ge=1)
    change_source: RevisionChangeSource
    stem: str = Field(..., description="该版本的题干（变更后完整快照）")
    options: list[OptionOut] = Field(
        ..., description="该版本的全部选项（变更后完整快照）"
    )
    answer: str = Field(..., description="该版本的正确答案选项键")
    difficulty: float = Field(..., description="该版本的难度 b")
    discrimination: float = Field(..., description="该版本的区分度 a")
    content_tag: str | None = Field(
        None, description="该版本的内容标签；无标签时为 null"
    )
    is_active: bool = Field(
        ..., description="该版本的启用状态：false 表示该版本记录的是撤回状态"
    )
    created_at: str = Field(..., description="版本写入时刻（UTC，ISO-8601）")


class ItemBankOut(BaseModel):
    count: int
    items: list[BankItemOut]


class ItemAnalysisOut(BaseModel):
    """Post-hoc quality review of one item, computed from finished sessions."""

    item_id: str = Field(..., description="被复盘的题目 ID")
    sample_count: int = Field(
        ...,
        description=(
            "有效样本数：已结束（finished）会话中，答过该题且同场至少另答"
            "一题的会话数量"
        ),
    )
    correct_rate: float | None = Field(
        None,
        description=(
            "该题在有效样本中的历史正确率（分子为作答时保存的 score 之和）；"
            "无有效样本时为 null"
        ),
    )
    pearson_correlation: float | None = Field(
        None,
        description=(
            "目标题得分（0/1）与同场其余题答对比例的 Pearson 相关系数；"
            "样本少于 5 或任一序列无方差（相关系数未定义）时为 null"
        ),
    )


class ItemCalibrationOut(BaseModel):
    """Calibration of the stored 2PL predictions against the observed scores."""

    item_id: str = Field(..., description="被校准评估的题目 ID")
    sample_count: int = Field(
        ..., description="有效样本数：已结束（finished）会话中该题的作答条数"
    )
    actual_correct_rate: float | None = Field(
        None,
        description=(
            "实际正确率（Σscore/样本数，score 为作答时冻结值）；无样本时为 null"
        ),
    )
    mean_predicted_correct_rate: float | None = Field(
        None,
        description=(
            "平均预测正确率（Σp_correct/样本数，p_correct 为作答前保存的 2PL 概率）；"
            "无样本时为 null"
        ),
    )
    z_score: float | None = Field(
        None,
        description=(
            "标准化偏差 z=(Σscore−Σp_correct)/√Σ[p_correct(1−p_correct)]；"
            "样本少于 30 或分母为零时为 null"
        ),
    )
    deviation_flag: bool | None = Field(
        None,
        description=(
            "预测与实际是否存在系统性偏离（|z|≥1.96 时 true）；"
            "z_score 为 null 时同为 null"
        ),
    )


# ---- item bulk import ------------------------------------------------------

class ItemImportRequest(BaseModel):
    """Bulk import envelope: an item array plus the dry-run switch."""

    # ``Any`` (not ``dict``) on purpose: malformed rows (scalars/lists) must
    # reach :mod:`app.importer` so they are reported per-row as
    # ``invalid_row`` instead of failing the whole request at the boundary.
    items: list[Any] = Field(
        ...,
        min_length=1,
        max_length=MAX_IMPORT_ITEMS,
        description=(
            "待导入题目数组（1~"
            f"{MAX_IMPORT_ITEMS} 行），行号从 1 开始。每行对象包含："
            "id（非空白字符串主键）、difficulty（[-6,6] 有限数）、"
            "discrimination（(0,5] 正有限数）、content_tag（字符串或 null，"
            "空串归一化为 null）、stem（非空白题干）、"
            "options（2~8 个 {key,text} 对象，键本行唯一、自动去空白并大写）、"
            "answer（必须是 options 中出现过的键）。逐行语义校验由服务端完成，"
            "不会在第一个坏行处中断整批请求。"
        ),
    )
    dry_run: bool = Field(
        False,
        strict=True,
        description="预检开关：true 时只校验并报告 created/updated 计划，不写库",
    )


class ItemImportRowOut(BaseModel):
    """Per-row outcome of a successful (or dry-run) import, keyed by stable row."""

    row: int = Field(..., description="该题在请求 items 数组中的 1 基行号")
    id: str
    action: Literal["created", "updated"] = Field(
        ..., description="created=新增题目；updated=按 ID 更新既有题目（保留曝光次数）"
    )


class ItemImportErrorOut(BaseModel):
    """One validation error, attached to a stable 1-based row number."""

    row: int = Field(..., description="出错行在请求 items 数组中的 1 基行号")
    code: str = Field(
        ...,
        description=(
            "错误代码，如 missing_field / invalid_type / out_of_range / "
            "duplicate_option_key / answer_not_in_options / "
            "duplicate_id_in_batch / invalid_row"
        ),
    )
    message: str = Field(..., description="可读错误说明（中文）")
    field: str | None = Field(
        None, description="出错字段名（options 为数组级错误），行级错误为 null"
    )
    id: str | None = Field(None, description="若能解析出题目 ID 则一并返回")


class ItemImportResponse(BaseModel):
    dry_run: bool = Field(..., description="回显请求的预检开关")
    valid: bool = Field(
        ...,
        description="整批是否通过全部逐行校验；false 时正式导入未写任何数据",
    )
    created_count: int = Field(
        ..., description="新增题目数（dry_run 时为预检计划数；有错误时为 0）"
    )
    updated_count: int = Field(
        ..., description="更新题目数（dry_run 时为预检计划数；有错误时为 0）"
    )
    total: int = Field(..., description="提交的行数")
    results: list[ItemImportRowOut] = Field(
        default_factory=list,
        description="逐行处理结果，按稳定行号升序；有错误或 dry_run 见各字段说明",
    )
    errors: list[ItemImportErrorOut] = Field(
        default_factory=list, description="逐行错误，按稳定行号升序"
    )


# ---- wrong-question spaced review (SM-2) -----------------------------------

class ReviewItemOut(BaseModel):
    """One wrong answer frozen into a review plan, with its SM-2 schedule."""

    id: str = Field(..., description="复习题 ID（提交复习结果时使用）")
    plan_id: str = Field(..., description="所属复习计划 ID")
    item_id: str = Field(
        ..., description="来源题库题目 ID（内容已冻结，之后题库改题/撤回不影响复习）"
    )
    stem: str = Field(..., description="创建计划时冻结的题干")
    options: list[OptionOut] = Field(..., description="创建计划时冻结的选项")
    answer: str = Field(..., description="创建计划时冻结的正确答案选项键")
    ease_factor: float = Field(
        ..., description="当前熟练因子（初始 2.5，每次结果后更新，下限 1.3）"
    )
    interval_days: int = Field(..., description="当前间隔天数（初始 0）")
    repetitions: int = Field(
        ..., description="连续成功（评分 >= 3）次数；评分低于 3 分清零"
    )
    due_date: str = Field(
        ..., description="到期日（UTC 日期 YYYY-MM-DD）；初始为计划创建当日"
    )
    created_at: str
    updated_at: str


class ReviewPlanOut(BaseModel):
    """A spaced-repetition plan built from a finished session's wrong answers."""

    id: str = Field(..., description="复习计划 ID")
    session_id: str = Field(
        ..., description="来源已结束会话 ID（同一会话至多一份计划，重复创建返回原计划）"
    )
    item_count: int = Field(
        ..., description="计划内错题数（该会话作答时冻结 score=0 的题数）"
    )
    items: list[ReviewItemOut] = Field(
        default_factory=list, description="计划内全部复习题（按来源题目 ID 升序）"
    )
    created_at: str = Field(..., description="计划创建时刻（UTC，ISO-8601）")


class ReviewDueOut(BaseModel):
    date: str = Field(..., description="本次查询使用的 UTC 日期（YYYY-MM-DD）")
    count: int = Field(..., description="到期复习题数（due_date <= date）")
    results: list[ReviewItemOut] = Field(
        default_factory=list,
        description="到期复习题，按到期日升序、复习题 ID 升序排列",
    )


class ReviewResultCreate(BaseModel):
    """Submission body for one review result (0~5 self-graded score)."""

    review_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description=(
            "客户端生成的幂等键，建议使用 UUID；绑定首次提交的复习题与评分："
            "同题同分重放返回首次结果，题目或评分不同返回 409"
        ),
    )
    score: int = Field(
        ...,
        strict=True,
        ge=0,
        le=5,
        description="复习自评分（0~5 的整数）；低于 3 分次日重练并清零连续成功次数",
    )


class ReviewResultOut(BaseModel):
    """A recorded review result with the before/after schedule snapshot."""

    review_id: str = Field(..., description="回显客户端幂等键")
    review_item_id: str
    score: int = Field(..., description="本次提交的评分（0~5）")
    ease_factor_before: float
    ease_factor_after: float
    interval_before: int = Field(..., description="本次复习前的间隔天数")
    interval_after: int = Field(..., description="推进后的间隔天数")
    repetitions_before: int
    repetitions_after: int
    due_date_before: str = Field(..., description="本次复习前的到期日（UTC 日期）")
    due_date_after: str = Field(
        ..., description="推进后的到期日（UTC 日期）= 复习当日 + 新间隔天数"
    )
    created_at: str = Field(..., description="结果记录时刻（UTC，ISO-8601）")


# ---- sessions -------------------------------------------------------------

class SessionCreate(BaseModel):
    initial_theta: float = Field(
        0.0, ge=THETA_MIN, le=THETA_MAX, description="初始能力值"
    )
    max_items: int = Field(
        10, ge=1, le=50, description="本次测验最多作答题目数"
    )
    exposure_limit: int | None = Field(
        None,
        ge=1,
        le=10_000,
        description="单题全局曝光上限；缺省取服务端 DEFAULT_EXPOSURE_LIMIT",
    )
    duration_seconds: int | None = Field(
        None,
        ge=60,
        le=86_400,
        description=(
            "可选测验时限（秒，60~86400）；"
            "服务端按 UTC 记录截止时间，到期后 /next 与提交答案返回 409"
        ),
    )
    tag_minimums: dict[str, PositiveIntField] = Field(
        default_factory=dict,
        description=(
            "可选内容标签最低答题配额：键须为题库现有非空 content_tag，"
            "值为正整数，所有配额之和不超过 max_items；不传则不设配额"
        ),
    )
    target_se: float | None = Field(
        None,
        gt=0.0,
        allow_inf_nan=False,
        description=(
            "可选能力估计精度停测目标标准误（正有限值，SE = 1/√Σ Fisher 信息）；"
            "须与 min_precision_items 同时提供，不传则不按精度停测"
        ),
    )
    min_precision_items: PositiveIntField | None = Field(
        None,
        ge=1,
        le=50,
        description=(
            "触发精度停测前至少需要作答的题数（1～max_items）；"
            "须与 target_se 同时提供"
        ),
    )

    @model_validator(mode="after")
    def _validate_precision_stop(self) -> SessionCreate:
        if (self.target_se is None) != (self.min_precision_items is None):
            raise ValueError(
                "target_se 与 min_precision_items 必须同时提供或同时省略"
            )
        if (
            self.min_precision_items is not None
            and self.min_precision_items > self.max_items
        ):
            raise ValueError("min_precision_items 不能大于 max_items")
        return self


class SessionOut(BaseModel):
    id: str
    theta: float
    current_item_id: str | None
    max_items: int
    exposure_limit: int
    duration_seconds: int | None = None
    expires_at: str | None = None
    paused: bool = Field(
        False, description="会话是否处于暂停状态（暂停期间不消耗时限、不能取题或作答）"
    )
    paused_at: str | None = Field(
        None, description="本次暂停开始的 UTC 时间；未暂停时为 null"
    )
    remaining_seconds: int | None = Field(
        None,
        description=(
            "测验剩余秒数：暂停时为暂停瞬间冻结的值；进行中为按 UTC 实时计算的值；"
            "无时限会话为 null"
        ),
    )
    tag_minimums: dict[str, int] = Field(default_factory=dict)
    target_se: float | None = Field(
        None, description="精度停测目标标准误；未启用时为 null"
    )
    min_precision_items: int | None = Field(
        None, description="精度停测最少作答题数；未启用时为 null"
    )
    standard_error: float | None = Field(
        None, description="当前 θ 下全部已答题的估计标准误 1/√Σ信息；无已答题为 null"
    )
    status: str
    finish_reason: str | None
    answered_count: int
    created_at: str
    updated_at: str


class NextItemOut(BaseModel):
    session_id: str
    status: str
    item: ItemOut | None = None
    information: float | None = None
    exposure_count: int | None = None
    exposure_limit: int
    answered_count: int
    max_items: int
    paused: bool = False
    paused_at: str | None = None
    remaining_seconds: int | None = None
    target_se: float | None = None
    min_precision_items: int | None = None
    standard_error: float | None = None
    finish_reason: str | None = None
    note: str | None = None


# ---- responses ------------------------------------------------------------

class ResponseCreate(BaseModel):
    response_id: str = Field(
        ...,
        min_length=1,
        max_length=128,
        description="客户端生成的幂等键，建议使用 UUID",
    )
    item_id: str = Field(..., description="刚从 /next 取得的题目 ID")
    response: str = Field(..., min_length=1, description="作答选项键，如 'A'")


class ResponseOut(BaseModel):
    id: str
    session_id: str
    item_id: str
    response: str
    correct: bool
    score: int
    correct_answer: str
    content_tag: str | None = Field(
        None,
        description=(
            "该题作答时冻结的内容标签（取自当时题库 content_tag，无标签为 null）；"
            "之后导入改标签或撤回题目都不会改变它，配额统计始终按此冻结值"
        ),
    )
    theta_before: float
    theta_after: float
    theta_change: float
    p_correct: float
    information_at_theta: float
    exposure_count: int
    created_at: str


class ResponseHistoryOut(BaseModel):
    session_id: str
    status: str
    theta: float
    count: int
    results: list[ResponseOut]


# ---- similarity review -----------------------------------------------------

class SimilarityMatchOut(BaseModel):
    """One finished session whose wrong answers resemble the target's."""

    session_id: str = Field(..., description="与目标会话相似的已结束会话 ID")
    shared_count: int = Field(
        ..., description="双方共同作答的题目数（按 item_id 对齐）"
    )
    same_wrong_count: int = Field(
        ..., description="双方选择同一错误选项的题目数（正确作答不计）"
    )
    score: float = Field(
        ...,
        description=(
            "雷同分数：同错匹配权重之和 / 共答题数；权重为 "
            "log((该题已结束会话错误作答总数+2)/(选择该错误选项的次数+1))"
        ),
    )


class SessionSimilarityOut(BaseModel):
    session_id: str = Field(..., description="被复核的目标会话 ID")
    min_shared: int = Field(..., description="回显请求的共答题数下限（2~50）")
    limit: int = Field(..., description="回显请求的返回条数上限（1~50）")
    count: int = Field(..., description="本次返回的相似会话条数（<= limit）")
    results: list[SimilarityMatchOut] = Field(
        default_factory=list,
        description="按分数、同错数降序及会话 ID 升序排列的相似会话",
    )


# ---- progress -------------------------------------------------------------

class ProgressOut(BaseModel):
    session_id: str
    status: str
    theta: float
    answered_count: int
    max_items: int
    bank_size: int
    percent_complete: float
    exposure_limit: int
    duration_seconds: int | None = None
    expires_at: str | None = None
    paused: bool = False
    paused_at: str | None = None
    remaining_seconds: int | None = None
    tag_minimums: dict[str, int] = Field(
        default_factory=dict, description="各内容标签的最低答题配额"
    )
    tag_answered: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "各内容标签已答题数，按每条作答提交时冻结的 content_tag 统计；"
            "之后题库改标签或撤回题目不影响该统计"
        ),
    )
    target_se: float | None = Field(
        None, description="精度停测目标标准误；未启用时为 null"
    )
    min_precision_items: int | None = Field(
        None, description="精度停测最少作答题数；未启用时为 null"
    )
    standard_error: float | None = Field(
        None, description="当前 θ 下的能力估计标准误；无已答题或总信息为零时为 null"
    )
    finish_reason: str | None
    current_item: ItemOut | None
    answered_item_ids: list[str]


class HealthOut(BaseModel):
    status: str
    bank_size: int
