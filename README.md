# 自适应测验选题 API（FastAPI + SQLite）

基于 **2PL IRT（项目反应理论）** 的计算机自适应测验（CAT）后端：

- 题库保存每题的**难度 `b`、区分度 `a`、曝光次数 `exposure_count`**；
- 考生提交答案后，服务端用**一次 Newton-Raphson 更新**估计能力值 `θ`；
- 下一题在满足**曝光上限**的候选题中，按 **Fisher 信息量最大**选择，信息量并列时取**最小（稳定的）题目 ID**；
- `response_id` 为客户端幂等键：**重复提交返回首次结果，不重复更新能力值、不重复计曝光**；
- 支持可选测验时限 `duration_seconds`（60～86400 秒）：创建时按 UTC 记录截止时间 `expires_at`，到期后取题/作答原子结束会话并返回 `409`；
- 活动会话可**暂停 / 恢复**：暂停时冻结剩余秒数、暂停期间不消耗时限，恢复时按剩余秒数重算 UTC 截止时间；无时限会话同样可以暂停/恢复；暂停期间取题与新作答返回 `409`，恢复后挂起题、已有作答与已计曝光原样保留；
- 支持可选**内容标签最低配额** `tag_minimums`：按 `content_tag` 声明本场至少要答的题数；剩余答题位仅够补缺口时只从欠额标签选题，欠额标签题量不足时以明确的 `finish_reason` 结束且不额外计曝光。**每条作答在提交时把该题当时的 `content_tag` 冻结记录**，进度已答数、剩余缺口、`/next` 的欠额末段限制与精度停测的配额门槛**一律按冻结标签统计**：测验进行中再通过导入修改已答题的标签、或撤回已答题目，都不会改变已答题的标签归属，既不会让配额被误判为已满足（提前 `precision_reached` / 错误进入末段），也不会让已满足的配额重新打开或因欠额标签题被撤回而提前 `tag_minimums_unfulfillable`（候选题仍按题库**当前**标签与启用状态过滤）；历史 `responses` 在旧库首次升级时按当时题库标签**一次性回填**冻结标签，之后不再随题库变化；
- 支持可选**能力估计精度停测** `target_se` + `min_precision_items`：每次作答更新 θ 后，在当前 θ 处按**各条作答提交时冻结的送达版本难度/区分度**重算本场全部已答题的 2PL Fisher 信息总和，`SE = 1/√Σ信息`；已答数达到下限、`SE ≤ target_se` 且标签配额（**按作答时冻结标签统计**）均已满足时，在同一事务内结束会话（`finish_reason = precision_reached`）。**标准误不读题库现值**：正式导入事后修改已答题参数不会改变任何既有会话（无新增作答）的标准误，不会误触发或错过停测；历史 `responses` 在旧库首次升级时按当时题库参数**一次性回填**冻结参数，之后不再回填；
- 提供会话进度查询与完整答题记录查询，会话状态/进度/取题响应均返回当前标准误 `standard_error` 与停测配置；
- 支持**题库批量导入与预检** `POST /api/items/import`：一次提交题目数组与 `dry_run`，逐行校验 ID、选项键、答案、难度/区分度、`content_tag` 与批内重复 ID；预检只报告 `created`/`updated` 计划不写库，正式提交在单个 `BEGIN IMMEDIATE` 事务内原子新增或更新（更新保留 `exposure_count` 与启用状态），任一错误整批不变；
- 支持**题目撤回 / 启用** `PATCH /api/items/{id}/availability`：撤回题不再参与新取题；会话挂起题被撤回后，再次 `/next` 在同一事务内清除挂起项并按既有规则重选（已计曝光不回退）；对撤回题提交新作答返回 `409`（不写答题记录、不更新能力），已保存的 `response_id` 重放仍返回首次结果；
- 提供**单题质量复盘** `GET /api/items/{id}/analysis`：只统计**已结束会话**的历史作答，并使用作答时冻结保存的 `score`（之后修改题目答案不影响复盘结果）；对每个答过目标题且同场至少另答一题的会话，以「目标题得分 + 同场其余题答对比例」组成样本，返回有效样本数、历史正确率及两者的 Pearson 相关系数（样本少于 5 或任一序列无方差时为 `null`；未知题目 `404`；接口只读）；
- 提供**答卷雷同复核** `GET /api/sessions/{id}/similarity`：面向**已结束会话**，按 `item_id` 对齐双方历史作答，仅「双方选择同一错误选项」计为同错匹配（正确作答不计），每个匹配按该错误选项在全体已结束会话中的稀有程度加权（`log((该题错误作答总数+2)/(选择该错误选项的次数+1))`），分数为权重之和除以共答题数；候选至少共答 `min_shared` 题（默认 4），按分数、同错数降序及会话 ID 升序返回前 `limit` 条；统计只读 `responses` 中冻结的 `score` 与 `response`，题库改答案后结果不变；
- 提供**单题预测概率校准** `GET /api/items/{id}/calibration`：只统计**已结束会话**中该题的历史作答，使用作答时保存的作答前 `p_correct` 与冻结 `score`（**不**按当前能力值或改动后的题目参数重算），返回样本数、实际正确率、平均预测正确率与标准化偏差 `z=(Σscore−Σp_correct)/√Σ[p_correct(1−p_correct)]`；样本少于 30 或分母为零时 `z` 与偏离标记为 `null`，其余按 `|z|≥1.96` 给出偏离标记（未知题目 `404`，无历史样本时比率为 `null`，接口只读）；
- 提供**题库修订历史**：每道题保存连续、只增不改的版本链。**正式导入新增题目**（`import_create`）、**正式导入导致内容实际变化**（题干/选项/答案/难度/区分度/标签，`import_update`）与**启用状态实际翻转**（`withdraw` / `re_enable`）时，在**原写事务内**记录变更来源与变更后的完整快照；同值重试、`dry_run` 预检及整批校验失败**不产生版本**。快照含题干、选项、答案、难度、区分度、标签与启用状态，**不含随作答变化的曝光次数**。提供按题目查询版本列表 `GET /api/items/{id}/revisions` 与指定版本快照 `GET /api/items/{id}/revisions/{version}`，未知题目或版本返回 `404`；后续导入**永不改写历史版本**。**旧库首次启动**为所有现存题（含初始演示题库）建立幂等的版本 1 `baseline`，重复启动不重复生成；
- **取题版本冻结**：取题时在会话中持久关联当次送达的题目修订版本（`sessions.current_item_version`）。重复调用 `/next` 始终返回**送达时的题干与选项**；提交答案按**该冻结版本**的选项、正确答案与 IRT 参数校验计分，保存的作答结果与能力更新一致——取题后正式导入改动同一题，不会让在途作答被按考生从未见过的新版本判分。挂起题被撤回后的重选改关联新选中题的当前版本；已保存 `response_id` 的重放仍返回首次结果；作答时 `content_tag` 继续按当时题库标签冻结（行为不变）。**作答提交时同时把送达版本的难度 `b`、区分度 `a` 随答题记录冻结**（`responses.difficulty` / `responses.discrimination`）：会话状态、进度、取题与精度停测的标准误一律在当前 θ 下按各条作答的冻结参数重算 Fisher 信息，题库后续改参不追溯影响已答题（旧库首启按当时题库参数一次性回填，之后不再回填）；
- 提供**错题间隔复习**（SM-2）：已结束会话可按作答时冻结的 `score=0` 错题创建复习计划 `POST /api/sessions/{id}/review-plan`（创建时冻结题干/选项/正确答案，**同一会话重复创建返回原计划**）；每题初始熟练因子 `2.5`、当日（UTC）到期；支持按 UTC 日期查询到期题 `GET /api/review/due` 与提交 0~5 分复习结果 `POST /api/review/items/{id}/results`（`review_id` 绑定首次提交的复习题与评分：同题同分重放返回首次结果且不重复推进日程，题目或评分不同返回 `409` 且不改动结果与日程，冲突检查先于到期判断；仅接受已到期题；同一写事务记录结果并推进日程）：评分低于 3 分次日重练并清零连续成功次数，第 1、2 次成功间隔分别为 1、6 天，其后间隔为旧间隔×熟练因子四舍五入，每次结果后按 `max(1.3, EF+0.1−(5−q)×(0.08+(5−q)×0.02))` 更新熟练因子；
- 自带 30 题确定性演示题库，容器启动后即可调用。

## 目录结构

```
.
├── app/
│   ├── main.py        # FastAPI 路由：会话 / 选题 / 作答 / 暂停恢复 / 进度 / 记录 / 批量导入 / 撤回启用 / 修订历史 / 取题版本冻结 / 雷同复核 / 概率校准 / 错题复习
│   ├── irt.py         # 2PL 概率、Fisher 信息量、单次 Newton 更新
│   ├── analysis.py    # 单题质量复盘：历史样本构建与 Pearson 相关系数
│   ├── calibration.py # 单题预测概率校准：p_correct 与实际得分的标准化偏差
│   ├── similarity.py  # 答卷雷同复核：同错选项加权相似度（已结束会话）
│   ├── review.py      # 错题间隔复习：SM-2 熟练因子更新与日程推进（纯算法）
│   ├── importer.py    # 题库批量导入的逐行校验（ID/选项/答案/难度区分度/标签/重复 ID）
│   ├── revisions.py   # 题库修订历史：变更判定、同事务版本记录、首启基线（幂等）
│   ├── database.py    # SQLite 连接、建表、初始化
│   ├── models.py      # Pydantic 请求/响应模型
│   └── seed.py        # 确定性演示题库（ITEM-001 ~ ITEM-030）
├── examples/demo.py   # 仅用标准库的端到端示例（含幂等演示）
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 快速开始

### 方式一：Docker Compose（推荐）

单条命令即可构建镜像并在后台启动服务：

```bash
docker compose up -d --build
```

前台运行（直接看日志，`Ctrl+C` 停止）可用：

```bash
docker compose up --build
```

启动后 Compose 把容器内 API 的 **8000** 端口映射到主机 **8000** 端口
（`ports: "8000:8000"`，冒号左侧为主机端口，需要更换时只改左侧即可）：

- API 根地址： http://localhost:8000
- 交互式文档（Swagger UI）： http://localhost:8000/docs
- 健康检查： http://localhost:8000/health
- SQLite 数据持久化在命名卷 `quiz-data`（容器内 `/data/quiz.db`）

### 方式二：本地直接运行

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

uvicorn app.main:app --host 0.0.0.0 --port 8000
```

首次启动自动建表并灌入 30 道演示题。

### 一键跑示例

服务启动后，另开终端：

```bash
python examples/demo.py
```

脚本会：创建带 600 秒时限的会话（演示 `duration_seconds` 越界校验与 `expires_at`）→ 循环「取题 → 作答」5 轮 → 演示重复 `response_id` 幂等、重复 `/next` 不重复计曝光 → 打印进度与答题记录 → 演示 `tag_minimums` 配额选题（含配额校验、末段只从欠额标签选题、无法补齐时 `tag_minimums_unfulfillable` 结束且不增加曝光）→ 演示精度停测（参数校验、SE 逐题下降、`precision_reached` 触发与重复 `response_id` 不重复触发状态变化）→ 演示**暂停 / 恢复**（冻结 `remaining_seconds`、暂停期间取题/新作答/重复暂停返回 409、恢复后重算 `expires_at` 且挂起题与曝光原样保留、无时限会话暂停/恢复、已结束会话 409）→ 演示**题目撤回 / 启用**（未知题目 404、撤回题不再被选中、挂起题撤回后 `/next` 同事务清除并重选且曝光不回退、撤回题新作答 409、已保存 `response_id` 重放仍返回首次结果、批量导入更新保留启用状态）→ 演示**作答标签冻结**（提交作答持久化当时的 `content_tag`；作答后导入改标签或撤回题目，进度归属、`/next` 欠额末段限制与精度停测配额门槛均不被追溯改写：不会误判配额已满足、不会提前结束；历史库首次升级按当时标签一次性回填）→ 演示**取题版本冻结**（取题后正式导入改题干/选项/答案/参数，重复 `/next` 仍返回送达版本且不重复计曝光，作答按送达版本的选项/答案/IRT 参数判分且记录与能力更新一致，`response_id` 重放返回首次结果；新会话取到新版本；挂起题撤回重选改关联新题当前版本）→ 演示**作答参数冻结防精度漂移**（作答记录冻结送达版本难度/区分度；正式导入修改已答题参数后，无新增作答的标准误逐位不变、不会误触发 `precision_reached`；再次改参后新会话作答冻结新版本参数）→ 演示**题库修订历史**（首启 baseline、预检/同值重试/失败批次不产生版本、正式导入新增与内容变化产生版本、撤回/启用翻转产生版本、快照不含曝光次数、历史版本不被改写、未知题目/版本 404）。

## 核心算法

### 2PL 模型与信息量

```
P(θ) = 1 / (1 + exp(-a(θ - b)))          # 答对概率
I(θ) = a² · P(θ) · (1 - P(θ))            # 单题 Fisher 信息量
SE(θ) = 1 / sqrt( Σ_j I_j(θ) )           # 本场已答题总信息量对应的估计标准误
```

### 作答后一次 Newton 更新

对单条二元作答 `u`（1 对 / 0 错），对数似然关于 θ 的一、二阶导数为：

```
g  = a(u - P(θ))
h  = -a²·P·(1-P) = -I(θ)
θ' = θ - g/h = θ + a(u - P) / I(θ)       # 仅一步
```

为数值稳定：分母有 `1e-4` 下限，θ 被截断在 `[-6, 6]`。注意题目要求的是
**一次** Newton 更新（非迭代至收敛），实现见 `app/irt.py: newton_step`。

### 选题规则（`POST /api/sessions/{id}/next`）

候选题必须同时满足：

1. 题目处于**启用**状态（`is_active = 1`，撤回题不参与新取题）；
2. `exposure_count < exposure_limit`（**全局**曝光上限，所有会话共享）；
3. 该题在本会话中尚未作答（不重复出题）。

候选题按 `(-信息量, 题目 ID)` 排序取第一题 —— 信息量最大优先，**完全并列时
ID 更小的题胜出**，因此选择结果确定可复现。题目被选中的同一事务内
`exposure_count += 1`。

- 若存在「已取出但未作答」的挂起题，重复调 `/next` 返回**同一题且不重复加曝光**；
  题干、选项与 IRT 参数按取题时冻结的**送达版本**返回（取题后导入改题不影响在途
  挂起题，见「取题版本冻结」一节）；
- 若挂起题已被**撤回**，`/next` 在**同一事务内**清除挂起项并按上述规则重选
  （该题已计曝光**不回退**），响应 `note` 会说明；无题可重选时按既有规则结束会话；
- 已答满 `max_items` → 会话结束，`finish_reason = max_items`；
- 没有满足曝光上限的候选题 → 结束，`finish_reason = no_available_items`；
- 设置了标签配额且欠额标签已无法补齐 → 结束，
  `finish_reason = tag_minimums_unfulfillable`（见下节）。
- `precision_reached` 不在 `/next` 内产生：精度停测只在作答提交、θ 更新之后判定（见下节）。

### 内容标签最低配额（可选 `tag_minimums`）

- 创建会话时可传 `tag_minimums`：键必须是题库中**现有且非空的 `content_tag`**，
  值必须为**正整数**，且所有配额之和 **不超过 `max_items`**；
  不传或传 `{}` 表示不设配额，行为与旧版完全一致。校验失败返回 `422`。
- 配额存入独立的 `session_tag_minimums` 表并随会话持久化；旧版本数据库在
  服务启动时自动建表迁移（旧会话无配额行）。
- **已答标签在作答时冻结**：每条 `responses` 记录在提交答案的同一事务内写入该题
  **当时**的 `content_tag`（`responses.content_tag`，无标签为 `NULL`）。`/next`
  在原有的「未作答 + 曝光上限 + 启用」候选范围内（候选题仍按题库**当前**标签），
  按 `responses` 中**冻结标签**统计各标签**缺口** = 配额 − 已答数：
  - **剩余答题位 = 缺口总数**时，候选只保留欠额标签（按冻结标签判定）的题，
    再按信息量最大、ID 最小的既有规则选出（保证每场测验结束时配额一定被满足）；
  - 其余时候仍在全部候选题中选题，缺口约束只在「末段」介入。
- **进行中改题库不追溯改写配额**：会话进行中再通过 `POST /api/items/import`
  修改某道**已答题**的 `content_tag`、或撤回（`PATCH .../availability`）该题，
  都只影响今后的新取题（候选题、挂起题撤回重选等仍看题库当前状态），**不会**
  改变这条已答记录的冻结标签与归属。因此：
  - 把已答题改到欠额标签，**不会**让该欠额标签的缺口凭空变小、不会让配额被
    误判为已满足（也就不会因此提前 `precision_reached` 或放松末段限制）；
  - 把已答题改离某标签或撤回该题，**不会**让该标签已满足的配额重新打开，
    已答题也不再是候选题（`/next` 本来就排除已答题），故不会因此提前以
    `tag_minimums_unfulfillable` 结束；
  - 选题候选、曝光与挂起题撤回重选等行为完全沿用题库当前状态，不变。
- 一旦发现某欠额标签的**当前可用题数**（题库当前标签、启用且未达曝光上限、
  本场未作答）已不足以补齐**按冻结标签计算的缺口**，立即结束会话：
  `status = finished`、`finish_reason = tag_minimums_unfulfillable`，`note`
  给出无法满足的缺口；本次**不选题、不返回题目、不增加任何曝光计数**
  （挂起题的幂等返回不受影响）。
- `GET /api/sessions/{id}/progress` 返回 `tag_minimums`（配额）与
  `tag_answered`（各标签已答数，**按冻结标签统计**；出现过的标签与全部配额
  标签均会列出）。答题记录 `GET /api/sessions/{id}/responses` 的每条结果额外
  返回作答时冻结的 `content_tag`。
- **历史数据兼容迁移**：旧库 `responses` 表没有该列，服务首次启动时自动
  `ALTER TABLE` 增加 `content_tag` 列，并按**当时**题库的 `content_tag`
  为全部历史作答**一次性回填**（空白标签归一为 `NULL`）。回填仅在首次加列时
  发生：之后再次启动即使题库标签已改也**不会**重新回填或改写，冻结值永久生效。
- 未设置配额的会话即便无题可出，仍沿用原 `no_available_items` 结束原因。

### 测验时限（可选 `duration_seconds`）

- 创建会话时可传 `duration_seconds`，取值 **60～86400 秒**；不传则无时限（行为与旧版一致）。
- 服务端在创建时按 **UTC** 计算并持久化截止时间 `expires_at = created_at + duration_seconds`
  （ISO-8601，秒级），在创建响应以及 `GET /api/sessions/{id}`、`/progress` 中一并返回。
- `POST /next` 与 `POST /responses` 在**写事务内最先检查**截止时间：一旦 `now(UTC) >= expires_at`，
  即在同一事务内原子地把会话置为 `finished`、`finish_reason = time_expired`、清空挂起题，
  并以 **HTTP 409** 拒绝本次操作；**不会**增加任何曝光计数，也**不会**写入答题记录。
  未到期时完全保留原有行为（包括挂起题不重复计曝光、`response_id` 幂等）。
- 只读的会话/进度/记录查询不触发自动结束；客户端可据 `expires_at` 自行倒计时。

### 暂停与恢复（`POST /pause`、`POST /resume`）

- `POST /api/sessions/{id}/pause`：仅 **`active` 且尚未到期** 的会话可暂停。
  - 暂停瞬间把剩余秒数 `expires_at − now(UTC)` 冻结到 `remaining_seconds`，记录 `paused_at`（UTC），
    会话状态变为 `paused`；暂停期间**不消耗时限**（`expires_at` 保持不动）。
  - **已到期会话不能通过暂停续命**：暂停请求与 `/next`、`/responses` 一样先做到期检查，
    到期时原子结束会话（`finish_reason = time_expired`）并返回 **409**。
  - 已暂停或已结束的会话重复暂停返回 **409**。
  - **无时限会话**（未传 `duration_seconds`）也可以暂停与恢复，`remaining_seconds` 始终为 `null`。
- `POST /api/sessions/{id}/resume`：仅 **`paused`** 的会话可恢复，重复恢复（或对已结束会话恢复）返回 **409**。
  - 恢复时按冻结的剩余秒数重算截止时间：`expires_at = now(UTC) + remaining_seconds`，
    随后清空 `paused_at` / `remaining_seconds`，状态回到 `active`。
  - 恢复**原样保留**：当前待答题（挂起题）、全部已有作答、能力值 θ 以及已计的全局曝光。
    恢复后重复调用 `/next` 仍返回同一道待答题，**不会再次增加曝光**。
- 暂停期间的访问规则：
  - `POST /next` 取题 → **409**（挂起题保留，不增加曝光）；
  - `POST /responses` 提交**新**作答 → **409**（不写入答题记录、不更新 θ、不增加曝光）；
  - 已存储 `response_id` 的**幂等重放**仍返回首次结果（HTTP 200），与已结束会话上的重放行为一致；
  - 只读查询（会话状态 / 进度 / 答题记录）在暂停期间照常可用。
- 暂停状态与剩余时长在会话状态、进度查询及取题响应中返回：
  `paused`（布尔）、`paused_at`（UTC，未暂停为 `null`）、`remaining_seconds`
  （暂停时为冻结值；进行中为按 UTC 实时计算的值，下限 0；无时限为 `null`）。
  答题记录查询额外返回 `status`（可能为 `paused`）。

### 能力估计精度停测（可选 `target_se` + `min_precision_items`）

- 创建会话时可同时传：
  - `target_se`：目标能力估计标准误，须为**正有限**浮点数（`> 0`，不接受 `0`、负数、`Infinity`、`NaN`）；
  - `min_precision_items`：触发停测前的**最少作答数**，正整数，取值 **1～`max_items`**。
  - 两者必须**同时提供或同时省略**；校验失败返回 `422`。不传时不启用精度停测，流程与旧版完全一致。
- 配置持久化在 `sessions` 表（`target_se` / `min_precision_items` 两列）；旧版本数据库在服务启动时
  通过 `ALTER TABLE ... ADD COLUMN` 自动迁移（旧会话两列为 `NULL`，行为不变）。
- 每次成功作答并完成 θ 更新后，在**同一写事务**内于**当前 θ** 处，对本场**全部已答题**重新计算
  2PL Fisher 信息总和 `Σ I_j(θ)`，标准误为 `SE = 1/√Σ信息`。**每道题的信息量按该条作答提交时
  随记录冻结的送达版本参数**（`responses.difficulty` / `responses.discrimination`，与当次
  Newton 更新所用参数一致）计算，**不再读题库现值**：测验进行中或结束后，正式导入修改某道
  已答题的难度/区分度，都不会改变任何无新增作答会话的标准误——既不会让 `SE` 漂移跌破
  `target_se` 而**误触发** `precision_reached`，也不会让本应停测的会话**错过**停测。当：
  1. 已答数 `>= min_precision_items`，且
  2. `SE <= target_se`，且
  3. 所有 `tag_minimums` 标签配额均已满足（未设置配额时自动满足；满足与否
     只按每条作答 `responses.content_tag` 的**冻结标签**统计），

  即在该事务内把会话置为 `finished`、`finish_reason = precision_reached`、清空挂起题；
  任一条件不满足则继续原有「信息量最大」选题流程。信息总和为零（非正）时 `standard_error` 返回
  **`null`**，此时不可能触发停测。进行中把已答题改到欠额标签不会改变冻结归属，
  因此不会让配额被误判满足而提前停测。
- **作答参数随记录冻结**：提交答案的同一事务内，把送达版本快照的难度/区分度写入
  `responses.difficulty` / `responses.discrimination`（答题记录查询会逐条返回）；
  之后题库改参只影响**新作答**的冻结值（新作答冻结的是其送达版本的新参数），
  已产生的记录永不改写。**历史数据兼容迁移**：旧库 `responses` 表没有这两列，服务首次启动时
  自动 `ALTER TABLE` 加列，并按**当时**题库的难度/区分度为全部历史作答**一次性回填**；
  回填仅在首次加列时发生，之后再次启动即使题库参数已改也**不会**重新回填或改写。
- 原有规则优先级不变：`time_expired`（到期最先检查）与 `max_items`（答满）优先于精度停测；
  精度停测只发生在作答提交之后，`/next` 不会替它结束会话。
- 会话状态 `GET /api/sessions/{id}`、进度 `/progress` 与取题响应 `/next` 均返回
  `target_se`、`min_precision_items` 与当前 `standard_error`（无已答题时为 `null`）。
- 重复提交同一 `response_id` 走幂等回放分支：以 `200` 返回首次结果，**不会**重算 θ、重算 SE，
  也**不会**再次触发或改变 `precision_reached` 状态。

### 幂等作答（`POST /api/sessions/{id}/responses`）

- 客户端为每次作答生成唯一 `response_id`（建议 UUID），与请求体一起提交；
- 服务端首次收到时评分（提交的选项键与**取题时送达版本**的选项、正确答案比对）、
  按送达版本的 IRT 参数做一次 Newton 更新、清空挂起题、写入不可变记录；
  该记录同时**冻结本题当时的 `content_tag`** 与**送达版本的难度/区分度**
  （之后题库改标签/改参数/撤回题目都不改变它们：配额统计始终读冻结标签，
  标准误始终按冻结参数重算）；
- **同一 `response_id` 再次提交：以 `200` 返回首次存储的结果（首次为 `201`），不再更新 θ、不再计曝光。**
  若该 `response_id` 对应的是其他会话/题目，返回 `409`。

所有写操作使用 `BEGIN IMMEDIATE` 事务，SQLite WAL 模式下并发请求串行化，
避免曝光计数与能力更新丢失；`responses(id)` 主键与 `UNIQUE(session_id, item_id)`
在数据库层面兜底并发竞争。

### 取题版本冻结（按送达版本判分）

会话「已取题但尚未作答」期间，正式导入可能改动同一道题。为避免在途作答被按
考生从未见过的新版本判分，取题时服务端把**当次送达的题目修订版本**持久关联到
会话（`sessions.current_item_version`，对应 `item_revisions` 中选题瞬间的最新
快照，与当时题库内容一致）：

- **重复 `/next` 始终返回送达版本**：取题后正式导入改动同一题（题干/选项/答案/
  难度/区分度/标签），挂起题仍按冻结版本返回题干与选项，且**不重复计曝光**；
  `GET /api/sessions/{id}/progress` 的 `current_item` 同样展示送达版本。
- **作答按送达版本校验与计分**：提交答案时，选项键校验、正确答案比对以及
  Newton 更新所用的 IRT 参数（`a`/`b`）全部取自冻结版本快照；保存的
  `correct_answer`、`score`、`p_correct`、`information_at_theta` 与 θ 更新一致。
- **新取题用新版本**：该题被答完后，其他会话（或新会话）再次选中它时，送达的
  是题库当前内容对应的最新版本，并按新版本判分。
- **挂起题撤回后的重选**：`/next` 在同一事务内清除挂起项并重选，重选出的题
  关联其**当前**版本，之后同样按送达版本返回与判分。
- **既有行为不变**：已保存 `response_id` 的重放仍返回首次结果（HTTP 200）；
  作答时冻结的 `content_tag` 继续按**作答当时**的题库标签记录（与送达版本快照
  相互独立）；曝光计数、撤回题新作答 409、暂停/恢复语义均不变。
- **送达版本参数随作答冻结，标准误不漂移**：提交答案时把送达版本的难度/区分度
  一并写入答题记录（`responses.difficulty` / `responses.discrimination`）；
  会话状态、进度、`/next` 与精度停测返回/使用的 `standard_error` 一律在当前 θ 下
  按**各条作答的冻结参数**重算 Fisher 信息总和，不再 JOIN 题库现值。正式导入
  事后修改已答题参数，不会改变任何无新增作答会话的标准误，也就不会误触发或
  错过 `precision_reached`；新作答冻结的则是其送达版本的新参数。旧库首次升级
  自动加列并按当时题库参数**一次性回填**，之后不再回填（见「能力估计精度停测」
  与「说明与边界」）。
- **自动迁移**：旧库启动时自动为 `sessions` 表新增 `current_item_version` 列
  （早于暂停功能的库随表重建一并获得）。升级前已取出未作答的挂起题该列为
  `NULL`，其重复 `/next` 与作答回退为旧行为（按题库当前内容）；之后每次新取题
  都会关联送达版本。

### 题目撤回与启用（`PATCH /api/items/{id}/availability`）

题目支持**撤回**（不再参与新取题）与**重新启用**，请求体只需一个布尔字段：

```jsonc
// PATCH /api/items/ITEM-001/availability
{"is_active": false}   // 撤回；true 表示重新启用
```

- **语义**：`is_active=false` 撤回题目，`true` 重新启用；操作幂等，重复设置同一状态返回 `200`
  与当前题目行；`is_active` 必须是 JSON 布尔值（非布尔如 `1`、`"false"` 返回 `422`）；
  **未知题目返回 `404`**。响应为题库行（含 `is_active`、`exposure_count` 等）。
  仅当启用状态**实际翻转**时，服务端在**同一写事务**内追加一条修订版本
  （`withdraw` / `re_enable`，快照为翻转后的完整题目）；同值重试不产生版本。
- **不参与新取题**：`/next` 的候选题必须 `is_active = 1`，撤回题不会被新会话或进行中的
  会话再次选中；已产生的历史答题记录、曝光计数与复盘统计全部保留。
- **挂起题被撤回**：若某会话「已取出但未作答」的挂起题被撤回，下一次 `/next` 会在
  **同一事务内**清除挂起项并按既有选题规则（启用 + 曝光上限 + 未作答，信息量最大、
  ID 最小）重选一题；**该题已计的曝光计数不回退**，响应 `note` 会说明本次清除与重选；
  若没有可重选的题，按既有规则结束会话（`no_available_items` /
  `tag_minimums_unfulfillable`）。
- **作答限制**：对**已撤回**的题目提交**新**作答返回 **`409`**——不写答题记录、不更新
  能力值 θ；而已保存的 `response_id` **幂等重放仍返回首次结果**（HTTP 200），与暂停/
  结束会话上的重放行为一致。
- **导入交互**：批量导入**更新既有题目时保留其启用状态**（重新导入不会把撤回题重新启用），
  **新导入的题目默认启用**。
- **查询与迁移**：题库列表 `GET /api/items` 与详情 `GET /api/items/{id}` 均返回
  `is_active`；旧版本数据库在服务启动时自动迁移（`items` 表新增
  `is_active INTEGER NOT NULL DEFAULT 1`），**既有题目全部迁移为启用**，行为与旧版一致。

### 题库批量导入与预检（`POST /api/items/import`）

一次提交一个题目数组（1～1000 行）与预检开关：

```jsonc
{
  "dry_run": false,                 // true=只预检不写库
  "items": [
    {
      "id": "ITEM-101",             // 必填，非空白字符串（≤128），题库主键
      "difficulty": 1.25,           // 必填，有限数值，范围 [-6, 6]
      "discrimination": 1.1,        // 必填，正有限数值，范围 (0, 5]（a<=0 无信息量）
      "content_tag": "几何",         // 字符串或 null；纯空白串归一化为 null
      "stem": "...",                // 必填，非空白题干
      "options": [                  // 必填，2~8 个 {key, text}
        {"key": "A", "text": "..."},
        {"key": "B", "text": "..."}
      ],
      "answer": "A"                 // 必填，必须出现在本行 options 的 key 中
    }
  ]
}
```

- **逐行校验，一次报全**：不会在第一个坏行处中断。每行独立检查
  `id`（非空白、长度）、`options`（2～8 项、`key`/`text` 非空白、行内键唯一）、
  `answer`（属于选项键）、`difficulty`/`discrimination`（有限数且在界内；
  布尔值、`NaN`、`Infinity` 均拒绝）、`content_tag`（字符串或 null）、
  以及**批内重复 ID**（同一 ID 首次出现正常、之后每行报 `duplicate_id_in_batch`，
  并指明首次出现的行号）。标量/数组等不是对象的行报 `invalid_row`。
  选项键与答案自动去首尾空白并转大写（如 `"a"` → `"A"`），ID 区分大小写、仅去空白。
- **稳定行号**：请求体（非数组/空数组/超过 1000 行/`dry_run` 非布尔）在 422 边界处理；
  语义错误全部进 200/422 响应体的 `errors`，每条带
  `row`（**1 基行号**，与提交数组下标一一对应）、`field`、`code`、中文 `message`，
  可解析出 ID 时附 `id`；`errors` 与 `results` 均按行号升序，方便对回原始表格行。
- **预检 `dry_run=true`**：只读、不写库（HTTP 始终 `200`）。对每行给出
  `action`：库中不存在 → `created`，已存在 → `updated`；同时返回
  `created_count` / `updated_count` 计划数。即使存在错误行也照常报告全部错误。
- **正式提交 `dry_run=false`（缺省）**：
  - 全部行合法时，在**单个 `BEGIN IMMEDIATE` 事务**内以
    `INSERT ... ON CONFLICT(id) DO UPDATE` 批量 upsert：新 ID 新增、旧 ID 就地更新；
  - 更新覆盖 `difficulty` / `discrimination` / `content_tag` / `stem` / `options` / `answer`，
    **保留 `exposure_count` 与 `is_active`**——重新导入题库不会把全局曝光计数清零，
    也不会把已撤回的题目重新启用（新导入的题目默认启用）；已被会话引用的题目照常可用；
  - **修订版本**：在同一事务内，新增的 ID 追加 `import_create` 版本；既有题目仅在上述六个
    内容字段**实际变化**时追加 `import_update` 版本（选项以归一化后的 JSON 比较，键相同但
    顺序改变也算变化；难度 `1` 与 `1.0` 等存储值相同视为同值）。**内容完全相同的重新导入
    不产生版本**；快照不含 `exposure_count`，历史版本此后不被改写；
  - **任一错误整批不变**：只要有一行校验失败即不写任何数据（题库与修订历史都不写），
    返回 HTTP `422`（`valid=false`、两个计数均为 0、`results` 为空、`errors` 列出全部问题）；
    事务内的数据库异常同样整体回滚。
- 成功响应（预检与正式提交同构）：

  ```json
  {
    "dry_run": false, "valid": true,
    "created_count": 1, "updated_count": 1, "total": 2,
    "results": [
      {"row": 1, "id": "ITEM-101", "action": "created"},
      {"row": 2, "id": "ITEM-005", "action": "updated"}
    ],
    "errors": []
  }
  ```

  有错误的正式提交返回 HTTP `422` 且 `valid=false`、计数为 0；
  预检即使有错误也是 HTTP `200`，便于把预检集成进上传 UI。

  有错误的正式提交返回 HTTP `422` 且 `valid=false`、计数为 0；
  预检即使有错误也是 HTTP `200`，便于把预检集成进上传 UI。

### 题库修订历史（连续版本、完整快照、只增不改）

每道题维护一条**连续、只增不改**的版本链：版本号在每题内从 `1` 开始
连续递增，历史行从不被 `UPDATE` 或 `DELETE`——**后续导入不会改写任何
历史版本**。

- **什么情况下产生新版本**（都在**原写事务内**记录，事务回滚则版本一并回滚）：
  - `baseline`：**旧库首次启动**（升级到含本模块的版本）时，为所有现存题
    建立的版本 1 基线；初始的 30 道演示题同样拥有版本 1。基线建立是
    **幂等**的：已有任意版本的题被跳过，因此**重复启动不重复生成**；
  - `import_create`：正式（`dry_run=false`）批量导入**新增**了一个题目 ID；
  - `import_update`：正式批量导入**更新既有题目且其内容实际发生变化**——比较
    题干、选项、答案、难度、区分度、内容标签六个字段（选项按归一化后的
    JSON 列表比较，键一致但顺序调整也算变化）；
  - `withdraw`：`PATCH .../availability` 把启用状态由启用**实际翻转为撤回**；
  - `re_enable`：把撤回题**实际重新启用**。
- **什么情况下不产生版本**：
  - **同值重试**：用完全相同的内容重复导入（难度 `1` 与 `1.0` 等存储值相同
    也算同值）、对已经是撤回的题再次撤回 / 对已启用题再次启用；
  - **预检** `dry_run=true`：全程只读，不写题库也不写版本；
  - **整批校验失败**：只要有一行错误，正式导入不写任何数据（HTTP `422`），
    自然也不产生版本；事务内数据库异常同样整体回滚。
- **版本快照内容**：变更后的**完整**题目快照——题干 `stem`、选项 `options`、
  正确答案 `answer`、难度 `difficulty`、区分度 `discrimination`、内容标签
  `content_tag` 与启用状态 `is_active`。**不包含 `exposure_count`**（曝光次数
  随每次取题/作答变化，属于运行数据而非题库修订）；题库的曝光计数照常通过
  `GET /api/items` 与单题详情查看。
- **导入与启用状态**：导入更新沿用既有语义——保留 `exposure_count` 与
  `is_active`，因此「内容不变的重新导入」即使题目处于撤回状态也不会产生版本；
  新导入的题目默认启用。启用状态的变化只由撤回/启用接口产生
  `withdraw` / `re_enable` 版本。

查询接口（均为只读）：

```jsonc
// GET /api/items/ITEM-001/revisions  —— 版本列表（元数据，按 version 升序）
{
  "item_id": "ITEM-001",
  "count": 3,
  "results": [
    {"item_id": "ITEM-001", "version": 1, "change_source": "baseline",
     "is_active": true,  "created_at": "2026-09-25T00:00:00+00:00"},
    {"item_id": "ITEM-001", "version": 2, "change_source": "import_update",
     "is_active": true,  "created_at": "2026-09-25T01:00:00+00:00"},
    {"item_id": "ITEM-001", "version": 3, "change_source": "withdraw",
     "is_active": false, "created_at": "2026-09-25T02:00:00+00:00"}
  ]
}

// GET /api/items/ITEM-001/revisions/2  —— 指定版本的完整快照
{
  "item_id": "ITEM-001", "version": 2, "change_source": "import_update",
  "stem": "更新后的题干……",
  "options": [{"key": "A", "text": "1"}, {"key": "B", "text": "2"}],
  "answer": "B", "difficulty": -2.9, "discrimination": 0.9,
  "content_tag": "算术", "is_active": true,
  "created_at": "2026-09-25T01:00:00+00:00"
  // 注意：快照没有 exposure_count 字段
}
```

- 列表只返回版本元数据（版本号、变更来源、该版本后的启用状态、UTC 写入时刻）；
  完整内容通过指定版本接口获取。
- **未知题目返回 `404`**（`{"detail":"item not found"}`）；题目存在但**版本号
  不存在同样返回 `404`**（`{"detail":"revision not found"}`）；`version` 必须是
  ≥ 1 的整数（非整数/负数由参数校验返回 `422`）。
- 版本写入 `item_revisions` 表（`(item_id, version)` 复合主键、外键引用
  `items(id)`），服务启动时旧库自动建表，**无需额外配置**。

### 单题质量复盘（`GET /api/items/{id}/analysis`）

只读地复盘单道题在历史测验中的表现，用于发现偏易/偏难或区分度异常的题目：

- **样本来源**：仅统计状态为 `finished` 的**已结束会话**；进行中/暂停中的会话不计入。
  每个会话必须**答过目标题且同场至少另答一题**，否则不成样本。
- **样本构成**：每个有效会话贡献一对数值：
  - `x` = 该会话在目标题上的得分（`responses.score`，1 对 / 0 错）；
  - `y` = 同场**其余**已答题的答对比例（其余题得分之和 / 其余题题数），可看作该考生本场整体水平的代理。
- **使用作答时保存的 `score`**：复盘只读历史 `responses` 记录，**不**按题库当前答案重新判分；
  之后通过批量导入修改该题答案，不会改变历史复盘结果。
- **返回字段**：
  - `sample_count`：有效样本数；
  - `correct_rate`：目标题在有效样本中的历史正确率（无样本时为 `null`）；
  - `pearson_correlation`：`x` 与 `y` 的 Pearson 相关系数——正值表示目标题答得好的考生
    同场其他题也答得好（区分方向正常）。**样本少于 5 个，或任一序列无方差
    （如所有样本目标题都答对）时相关系数未定义，返回 `null`。**
- 接口完全只读：不创建/结束会话、不改变曝光计数、不写题库。
- 未知题目返回 **HTTP 404**。

```jsonc
// GET /api/items/ITEM-001/analysis
{
  "item_id": "ITEM-001",
  "sample_count": 12,
  "correct_rate": 0.6666666666666666,
  "pearson_correlation": 0.7144345080641217
}
// 样本不足 5 或存在常值序列：
// {"item_id":"ITEM-002","sample_count":3,"correct_rate":1.0,"pearson_correlation":null}
```

### 单题预测概率校准（`GET /api/items/{id}/calibration`）

只读地检查单题的 **2PL 预测概率与实际得分是否系统性偏离**，帮助题库维护者发现
标定不准（难度/区分度失真）的题目：

- **样本来源**：仅统计状态为 `finished` 的**已结束会话**中该题的作答记录；
  进行中/暂停中的会话不计入。每条作答记录贡献一个样本。
- **使用作答时保存的数值**：每个样本取 `responses` 里作答前算好并保存的
  `p_correct`（2PL 答对概率）与冻结的 `score`（1 对 / 0 错），**不**按当前能力值
  或改动后的题目参数重算——之后通过批量导入修改该题难度/区分度/答案，都不会
  改变校准结果。
- **返回字段**：
  - `sample_count`：有效样本数；
  - `actual_correct_rate`：实际正确率 `Σscore/n`（无样本时为 `null`）；
  - `mean_predicted_correct_rate`：平均预测正确率 `Σp_correct/n`（无样本时为 `null`）；
  - `z_score`：标准化偏差 `z=(Σscore−Σp_correct)/√Σ[p_correct(1−p_correct)]`
    （各次作答近似独立 Bernoulli(p_correct) 的正态近似）；**样本少于 30 或分母
    为零时返回 `null`**；
  - `deviation_flag`：是否存在系统性偏离——`|z|≥1.96`（双侧 5% 临界值）时为
    `true`，`z_score` 为 `null` 时同为 `null`。`z>0` 表示实际答对多于预测
    （该题被低估，偏易），`z<0` 表示实际答对少于预测（该题被高估，偏难）。
- 接口完全只读：不创建/结束会话、不改变曝光计数、不写题库。
- 未知题目返回 **HTTP 404**。

```jsonc
// GET /api/items/ITEM-001/calibration
{
  "item_id": "ITEM-001",
  "sample_count": 42,
  "actual_correct_rate": 0.7619047619047619,
  "mean_predicted_correct_rate": 0.5831228904710254,
  "z_score": 2.5012,
  "deviation_flag": true        // 实际正确率显著高于预测：该题可能比标定偏易
}
// 样本少于 30 或分母为零：
// {"item_id":"ITEM-002","sample_count":12,"actual_correct_rate":0.75,
//  "mean_predicted_correct_rate":0.71,"z_score":null,"deviation_flag":null}
// 无历史样本：{"item_id":"ITEM-003","sample_count":0,"actual_correct_rate":null,
//  "mean_predicted_correct_rate":null,"z_score":null,"deviation_flag":null}
```

### 答卷雷同复核（`GET /api/sessions/{id}/similarity`）

只读地复核一份**已结束**答卷与其他已结束会话的雷同程度，用于发现抄袭/串通作答：

- **目标会话**：必须存在（未知返回 **404**）且已结束（`status = finished`；
  进行中/暂停中的会话返回 **409**）。接口完全只读，不改变任何会话、曝光或题库数据。
- **候选会话**：所有其他 `finished` 会话；与目标会话按 `item_id` 对齐历史作答，
  **共同作答的题数（共答数）至少为 `min_shared`**（查询参数，默认 `4`，范围 `2～50`）
  才进入排名。无候选时返回空列表（`count = 0`）。
- **同错匹配**：一道共答题仅当**双方选择了同一选项且双方冻结的 `score` 均为 0**
  （即选了同一个错误选项）时计为一次匹配；答对的题、以及「一方对一方错」或
  「错但选项不同」的题均不计。
- **匹配权重**（自然对数，IDF 式——越少人选的错误选项越可疑）：

  ```
  weight = log((该题已结束会话错误作答总数 + 2) / (选择该错误选项的次数 + 1))
  ```

  两个计数都来自**已结束会话**在 `responses` 中冻结的 `score` 与 `response`；
  `+2`/`+1` 平滑保证权重恒为正。
- **分数**：`score = 同错匹配权重之和 / 共答题数`（除以共答数而非同错数，
  共答题多本身不会抬高分数）。
- **返回**：按 **分数降序 → 同错数降序 → 会话 ID 升序** 排列，最多返回
  `limit` 条（查询参数，默认 `20`，范围 `1～50`）；每条包含
  `session_id`、`shared_count`（共答数）、`same_wrong_count`（同错数）与 `score`（分数）。
- **统计与题库解耦**：只读 `responses` 里作答时冻结的 `score`/`response`，
  之后通过批量导入修改题目正确答案**不会**改变复核结果。

```jsonc
// GET /api/sessions/<SID>/similarity?min_shared=4&limit=20
{
  "session_id": "<SID>",
  "min_shared": 4,
  "limit": 20,
  "count": 2,
  "results": [
    {"session_id": "ab12...", "shared_count": 6, "same_wrong_count": 4,
     "score": 0.156152},
    {"session_id": "cd34...", "shared_count": 4, "same_wrong_count": 2,
     "score": 0.122656}
  ]
}
// 无候选：{"session_id":"<SID>","min_shared":4,"limit":20,"count":0,"results":[]}
```

### 错题间隔复习（SM-2）

把已结束会话里的错题纳入间隔重复复习，帮助考生按遗忘曲线巩固薄弱点：

- **创建计划** `POST /api/sessions/{id}/review-plan`：仅**已结束**（`finished`）会话可创建
  （进行中/暂停中返回 `409`，未知会话 `404`）。计划收录该场**作答时冻结 `score = 0`** 的
  每一道错题，并在创建时把**题干、选项与正确答案冻结**进计划——之后题库改题、撤回都不影响
  复习内容。每道复习题初始：熟练因子 `ease_factor = 2.5`、间隔 `0` 天、连续成功次数 `0`、
  **到期日为创建当日（UTC）**，即初始题目当日即可复习。会话没有错题时创建空计划
  （`item_count = 0`）。首次创建返回 `201`；**同一会话重复创建返回原计划**（`200`，
  不新增任何行；`review_plans.session_id` 唯一约束在数据库层兜底并发）。
- **查询到期题** `GET /api/review/due?date=YYYY-MM-DD`：按 **UTC 日期**返回
  `due_date <= date` 的全部复习题（`date` 缺省为今天，非法日期 `422`；已过期未复习的题
  保持到期状态），按到期日、复习题 ID 升序排列，含冻结的题干/选项/答案与当前日程状态。
  接口只读。
- **提交复习结果** `POST /api/review/items/{review_item_id}/results`，
  请求体 `{"review_id": "...", "score": 0..5}`：
  - `review_id` 为客户端幂等键，**绑定首次提交的复习题与评分**：同一复习题、同一评分的
    **重放返回首次结果**（`200`，首次为 `201`），不会重复推进日程；同一 `review_id`
    用于**其他复习题或不同评分**时返回 `409`，**已存结果与复习日程均不改动**；
    **冲突检查先于到期判断**——即使首次结果已把题目推到未到期，评分/题目冲突也返回
    `409` 的冲突提示而非到期错误；未知复习题 `404`；
  - **仅接受计划中已到期题**（`due_date <=` 今天 UTC）：未到期返回 `409` 且不写任何数据；
    `score` 必须为 `0~5` 的整数（越界、小数、布尔均 `422`）；
  - 结果记录（含前后快照）与日程推进在**同一 `BEGIN IMMEDIATE` 写事务**内完成：
    - 评分 **< 3**：次日重练（间隔 1 天），连续成功次数清零；
    - 评分 **>= 3**：第 1、2 次连续成功的间隔分别为 1、6 天，第 3 次起间隔为
      `旧间隔 × 熟练因子` **四舍五入**（使用**更新前**的熟练因子）；
    - 每次结果后熟练因子按 `EF' = max(1.3, EF + 0.1 − (5−q)×(0.08 + (5−q)×0.02))`
      更新（无论评分高低，下限 1.3）；
    - 新到期日 = 复习当日（UTC）+ 新间隔天数；首次结果总会把到期日至少推后一天，
      因此同一复习题同一天不会被第二个**新** `review_id` 重复推进。

```jsonc
// POST /api/review/items/<REVIEW_ITEM_ID>/results  {"review_id":"rev-001","score":4}
// HTTP 201 —— 首次记录并推进日程
{
  "review_id": "rev-001",
  "review_item_id": "4d10...",
  "score": 4,
  "ease_factor_before": 2.5, "ease_factor_after": 2.5,   // q=4 时 EF 增减相抵
  "interval_before": 0, "interval_after": 1,             // 第 1 次成功 -> 1 天
  "repetitions_before": 0, "repetitions_after": 1,
  "due_date_before": "2026-09-25", "due_date_after": "2026-09-26",
  "created_at": "2026-09-25T08:00:00+00:00"
}
// 相同 review_id + 相同评分重放 -> HTTP 200，原样返回上表，不重复推进日程；
// 同一 review_id 提交不同评分（或用于其他复习题）-> HTTP 409，结果与日程均不改动
```

## API 一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET  | `/health` | 健康检查、题库题量 |
| GET  | `/api/items` | 题库列表（含曝光次数与启用状态 `is_active`，分页 `limit/offset`） |
| GET  | `/api/items/{item_id}` | 单题详情（管理用，含正确答案与启用状态） |
| GET  | `/api/items/{item_id}/revisions` | **题库修订历史版本列表**（元数据按版本号升序；未知题目 404；只读） |
| GET  | `/api/items/{item_id}/revisions/{version}` | **指定版本完整快照**（题干/选项/答案/难度/区分度/标签/启用状态，不含曝光次数；未知题目或版本 404；只读） |
| PATCH | `/api/items/{item_id}/availability` | **撤回 / 启用题目**（`{"is_active": bool}`；仅实际翻转产生 withdraw/re_enable 版本；未知题目 404；撤回题不参与新取题） |
| GET  | `/api/items/{item_id}/analysis` | **单题质量复盘**（已结束会话样本、历史正确率、与同场表现的 Pearson 相关系数；只读） |
| GET  | `/api/items/{item_id}/calibration` | **单题预测概率校准**（已结束会话样本、实际/平均预测正确率、标准化偏差 z 与偏离标记；只读） |
| POST | `/api/items/import` | **题库批量导入/预检**（逐行错误、dry_run、事务原子 upsert、更新保留曝光数与启用状态；正式导入新增/内容实际变化在同事务记录修订版本，预检/同值/失败批次不记录） |
| POST | `/api/sessions` | 创建测验会话 |
| GET  | `/api/sessions/{id}` | 会话状态 |
| GET  | `/api/sessions/{id}/progress` | **会话进度**（θ、已答数、挂起题、已答题目、暂停状态与剩余秒数） |
| GET  | `/api/sessions/{id}/responses` | **答题记录**（逐题评分与 θ 轨迹；每条含作答时冻结的 `content_tag` 与送达版本 `difficulty`/`discrimination`） |
| GET  | `/api/sessions/{id}/similarity` | **答卷雷同复核**（已结束会话的同错加权相似度排名；`min_shared`/`limit` 可调；只读） |
| POST | `/api/sessions/{id}/next` | 自适应选下一题 |
| POST | `/api/sessions/{id}/responses` | 提交答案（幂等，一次 Newton 更新） |
| POST | `/api/sessions/{id}/pause` | **暂停**活动会话（冻结剩余时限；无时限会话也可暂停） |
| POST | `/api/sessions/{id}/resume` | **恢复**暂停中的会话（按剩余秒数重算 UTC 截止时间） |
| POST | `/api/sessions/{id}/review-plan` | **创建错题复习计划**（已结束会话作答时冻结 score=0 的错题；创建时冻结题干/选项/答案；同一会话重复创建返回原计划） |
| GET  | `/api/review/due` | **按 UTC 日期查询到期复习题**（`date` 缺省为今天；含冻结内容与日程状态；只读） |
| POST | `/api/review/items/{review_item_id}/results` | **提交 0~5 分复习结果**（`review_id` 绑定首次题目与评分：同题同分重放返回首次结果，不同则 409 且不改动结果与日程；仅接受已到期题；同一写事务记录结果并按 SM-2 推进日程） |

完整字段与可交互试用见 http://localhost:8000/docs。

## curl 调用示例

```bash
# 1) 健康检查
curl -s http://localhost:8000/health
# {"status":"ok","bank_size":30}

# 1b) 题库批量导入：先预检（dry_run=true，不写库），看 created/updated 计划与逐行错误
curl -s -X POST http://localhost:8000/api/items/import \
  -H 'Content-Type: application/json' \
  -d '{
    "dry_run": true,
    "items": [
      {"id":"ITEM-101","difficulty":1.25,"discrimination":1.1,"content_tag":"几何",
       "stem":"一个三角形内角和是多少度？",
       "options":[{"key":"A","text":"90"},{"key":"B","text":"180"},
                  {"key":"C","text":"270"},{"key":"D","text":"360"}],
       "answer":"B"},
      {"id":"ITEM-001","difficulty":-2.9,"discrimination":0.9,"content_tag":"算术",
       "stem":"更新既有题目（曝光次数保留）",
       "options":[{"key":"A","text":"1"},{"key":"B","text":"2"}],"answer":"A"}
    ]
  }'
# {"dry_run":true,"valid":true,"created_count":1,"updated_count":1,"total":2,
#  "results":[{"row":1,"id":"ITEM-101","action":"created"},
#             {"row":2,"id":"ITEM-001","action":"updated"}],"errors":[]}

# 预检（或正式提交）遇到坏行：一次返回全部逐行错误（row 为 1 基行号）
curl -s -X POST http://localhost:8000/api/items/import \
  -H 'Content-Type: application/json' \
  -d '{"dry_run": true, "items": [
    {"id":"ITEM-201","difficulty":9.9,"discrimination":1.0,"stem":"难度越界",
     "options":[{"key":"A","text":"1"},{"key":"B","text":"2"}],"answer":"A"},
    {"id":"ITEM-201","difficulty":0.0,"discrimination":0.0,"stem":"重复ID且区分度非法",
     "options":[{"key":"A","text":"1"},{"key":"B","text":"2"}],"answer":"Z"},
    "不是对象的坏行"
  ]}'
# HTTP 200（预检）/ 422（正式提交）：
# {"valid":false,"created_count":0,"updated_count":0,"results":[],
#  "errors":[
#    {"row":1,"code":"out_of_range","field":"difficulty","message":"难度 difficulty必须在 [-6.0, 6.0] 之间，收到 9.9", ...},
#    {"row":2,"code":"out_of_range","field":"discrimination", ...},
#    {"row":2,"code":"answer_not_in_options","field":"answer","message":"正确答案 'Z' 不在选项键 ['A', 'B'] 中", ...},
#    {"row":2,"code":"duplicate_id_in_batch","field":"id","id":"ITEM-201",
#     "message":"题目 ID 'ITEM-201' 在本批第 1 行已出现，同一批次不能重复"},
#    {"row":3,"code":"invalid_row","field":null,"message":"每一行必须是 JSON 对象（键值对），收到 str", ...}
#  ]}

# 预检通过后正式提交（去掉 dry_run 或置 false）：单事务原子 upsert，
# 任一错误整批不变（HTTP 422，题库保持原样）；更新既有题目保留 exposure_count 与 is_active
curl -s -X POST http://localhost:8000/api/items/import \
  -H 'Content-Type: application/json' \
  -d '{"items":[ ...同上... ]}'
# {"dry_run":false,"valid":true,"created_count":1,"updated_count":1, ...}

# 1c) 题目撤回 / 启用：撤回后该题不再参与新取题；重复设置幂等
curl -s -X PATCH http://localhost:8000/api/items/ITEM-001/availability \
  -H 'Content-Type: application/json' \
  -d '{"is_active": false}'
# {"id":"ITEM-001", "is_active":false, "exposure_count":3, ...}
# 未知题目 -> HTTP 404 {"detail":"item not found"}；is_active 非布尔 -> HTTP 422
curl -s -X PATCH http://localhost:8000/api/items/ITEM-001/availability \
  -H 'Content-Type: application/json' \
  -d '{"is_active": true}'        # 重新启用
# 若某会话的挂起题被撤回：再次 /next 会在同一事务内清除挂起项并重选
# （已计曝光不回退，响应 note 有说明）；对撤回题提交新作答 -> HTTP 409，
# 已保存的 response_id 重放仍返回首次结果（HTTP 200）

# 1d) 题库修订历史：版本列表 + 指定版本快照
#     每个现存题首启时已有 version=1 的 baseline（30 道演示题都有；重复启动不重复生成）
curl -s http://localhost:8000/api/items/ITEM-001/revisions
# {"item_id":"ITEM-001","count":1,"results":[{"item_id":"ITEM-001","version":1,
#   "change_source":"baseline","is_active":true,"created_at":"..."}]}

# 正式导入一个新题 -> 产生 import_create 版本；再次提交完全相同内容 -> 不产生版本
curl -s -X POST http://localhost:8000/api/items/import -H 'Content-Type: application/json' \
  -d '{"items":[{"id":"ITEM-101","difficulty":1.25,"discrimination":1.1,"content_tag":"几何",
       "stem":"一个三角形内角和是多少度？",
       "options":[{"key":"A","text":"90"},{"key":"B","text":"180"}], "answer":"B"}]}'
curl -s http://localhost:8000/api/items/ITEM-101/revisions
# ... "change_source":"import_create"（新题默认启用）
# dry_run 预检 / 整批失败（HTTP 422）都不会产生版本；
# 改题干/答案/参数/标签后再正式导入 -> 追加 import_update 版本（版本号连续）
curl -s http://localhost:8000/api/items/ITEM-101/revisions/2
# {"item_id":"ITEM-101","version":2,"change_source":"import_update",
#  "stem":"...","options":[...],"answer":"B","difficulty":1.25,
#  "discrimination":1.1,"content_tag":"几何","is_active":true,"created_at":"..."}
# 快照没有 exposure_count；历史版本此后不被任何导入改写
# 未知题目 -> 404 item not found；版本不存在 -> 404 revision not found
curl -s http://localhost:8000/api/items/ITEM-101/revisions/999
# HTTP 404 {"detail":"revision not found"}

# 2) 创建会话（初始 θ=0，最多 10 题，单题曝光上限 5）
curl -s -X POST http://localhost:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{"initial_theta": 0.0, "max_items": 10, "exposure_limit": 5}'
# 记下返回的 "id"，导出 SID=...

# 2b) 也可以在创建时指定测验时限（秒，60~86400）；
#     返回中会带 UTC 截止时间 "expires_at"
curl -s -X POST http://localhost:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{"initial_theta": 0.0, "max_items": 10, "duration_seconds": 600}'
# {"id":"...", "duration_seconds":600,
#  "expires_at":"2026-09-23T08:10:00+00:00", "status":"active", ...}

# 2c) 也可以在创建时声明内容标签最低配额 tag_minimums：
#     键须为题库现有非空 content_tag，值为正整数，总和 <= max_items
curl -s -X POST http://localhost:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{"initial_theta": 0.0, "max_items": 10,
       "tag_minimums": {"几何": 3, "概率统计": 2}}'
# {"id":"...", "max_items":10,
#  "tag_minimums":{"几何":3,"概率统计":2}, "status":"active", ...}
# 配额之和超过 max_items、值非正整数或标签不存在 -> HTTP 422

# 2d) 也可以启用能力估计精度停测：target_se 为正有限数，
#     min_precision_items 为 1~max_items 的正整数，二者必须同时提供
curl -s -X POST http://localhost:8000/api/sessions \
  -H 'Content-Type: application/json' \
  -d '{"initial_theta": 0.0, "max_items": 15,
       "target_se": 0.8, "min_precision_items": 5}'
# {"id":"...", "max_items":15, "target_se":0.8,
#  "min_precision_items":5, "standard_error":null, "status":"active", ...}
# 只给一个参数、target_se<=0/Infinity/NaN、min_precision_items>max_items -> HTTP 422

# 3) 选下一题
curl -s -X POST http://localhost:8000/api/sessions/$SID/next
# item.id / item.stem / item.options / information / exposure_count ...
# 若已过 expires_at：HTTP 409，
# {"detail":"测验时限已到（UTC 截止时间 ...），会话已结束：finish_reason=time_expired"}
# 若欠额标签已无题可补（不选题、不增加曝光）：
# {"status":"finished","finish_reason":"tag_minimums_unfulfillable",
#  "item":null,"note":"欠额标签的可用题数不足以补齐配额，无法满足的缺口 {...}"}

# 4) 提交答案（response_id 为客户端幂等键，务必唯一）
curl -s -X POST http://localhost:8000/api/sessions/$SID/responses \
  -H 'Content-Type: application/json' \
  -d '{"response_id": "'$(uuidgen)'", "item_id": "ITEM-xxx", "response": "A"}'
# 返回 correct、score、theta_before、theta_after、p_correct ...
# 到期后提交同样返回 409，且不写入答题记录、不增加曝光

# 5) 用完全相同的请求体重放一次 -> 返回首次结果，theta_after 不变

# 5b) 暂停 / 恢复（限时会话）
curl -s -X POST http://localhost:8000/api/sessions/$SID/pause
# {"id":"...", "status":"paused", "paused":true,
#  "paused_at":"2026-09-23T08:05:10+00:00",
#  "remaining_seconds":290,   # 暂停瞬间冻结的剩余秒数
#  "expires_at":"2026-09-23T08:10:00+00:00", ...}
# 暂停期间不消耗时限；/next 与新作答返回 409；
# 重复暂停、对已结束会话暂停也返回 409；到期会话不能借暂停续命（409 + time_expired）
curl -s -X POST http://localhost:8000/api/sessions/$SID/next
# HTTP 409 {"detail":"会话已暂停（paused_at=...）：暂停期间不能取题或作答，请先调用 POST /api/sessions/.../resume 恢复"}
curl -s -X POST http://localhost:8000/api/sessions/$SID/pause      # 重复暂停 -> 409

curl -s -X POST http://localhost:8000/api/sessions/$SID/resume
# {"id":"...", "status":"active", "paused":false, "paused_at":null,
#  "remaining_seconds":289,                     # 时钟重新走动
#  "expires_at":"2026-09-23T08:14:59+00:00",    # = 恢复时刻 + 冻结的剩余秒数
#  "current_item_id":"ITEM-...", ...}           # 挂起题保留
curl -s -X POST http://localhost:8000/api/sessions/$SID/resume     # 重复恢复 -> 409
# 恢复后再调 /next 仍返回同一道待答题，且不重复计曝光

# 6) 查询会话（含 expires_at / paused / remaining_seconds）、进度与答题记录
curl -s http://localhost:8000/api/sessions/$SID
curl -s http://localhost:8000/api/sessions/$SID/progress
# progress 还返回 "tag_minimums"（配额）、"tag_answered"（各标签已答数）
# 以及 "paused" / "paused_at" / "remaining_seconds"（暂停状态与剩余时长）、
# "target_se" / "min_precision_items"（停测配置）和
# "standard_error"（当前 θ 下按各条作答冻结的难度/区分度重算的
# 1/√ΣFisher 信息；无已答题为 null；题库事后改参不影响已答题）
curl -s http://localhost:8000/api/sessions/$SID/responses
```

重复步骤 3-4 即可完成整场测验；当 `status` 变为 `finished` 时，可从
`finish_reason` 得知结束原因（`max_items` / `no_available_items` /
`time_expired` / `tag_minimums_unfulfillable` / `precision_reached`）。
其中 `precision_reached` 仅在启用了精度停测的会话上出现，且只由一次**新的**
成功作答在同一事务内触发；答满 `max_items` 与到期停测的优先级高于精度停测。

# 7) 单题质量复盘（只读；只统计已结束会话，使用作答时保存的 score）
curl -s http://localhost:8000/api/items/ITEM-001/analysis
# {"item_id":"ITEM-001","sample_count":12,"correct_rate":0.6667,
#  "pearson_correlation":0.7144}
# 样本少于 5 或任一序列无方差 -> "pearson_correlation":null
# 未知题目 -> HTTP 404 {"detail":"item not found"}

# 7b) 单题预测概率校准（只读；用作答前保存的 p_correct 对比冻结的 score）
curl -s http://localhost:8000/api/items/ITEM-001/calibration
# {"item_id":"ITEM-001","sample_count":42,"actual_correct_rate":0.7619,
#  "mean_predicted_correct_rate":0.5831,"z_score":2.5012,"deviation_flag":true}
# deviation_flag=true 表示 |z|>=1.96，预测与实际存在系统性偏离
# 样本少于 30 或分母为零 -> "z_score":null,"deviation_flag":null
# 无历史样本 -> 两个比率为 null；未知题目 -> HTTP 404

# 8) 答卷雷同复核（只读；目标会话须已结束）
#    min_shared：候选至少共答的题数（默认 4，2~50）；limit：返回条数上限（默认 20，1~50）
curl -s "http://localhost:8000/api/sessions/$SID/similarity?min_shared=4&limit=20"
# {"session_id":"...","min_shared":4,"limit":20,"count":1,
#  "results":[{"session_id":"...","shared_count":6,"same_wrong_count":4,
#              "score":0.156152}]}
# 结果按 分数降序 -> 同错数降序 -> 会话 ID 升序 排列；无候选时 results 为空列表
# 目标会话不存在 -> HTTP 404；目标会话未结束（active/paused）-> HTTP 409；
# min_shared/limit 越界 -> HTTP 422

# 9) 错题间隔复习（SM-2）：已结束会话 -> 创建计划 -> 查询到期 -> 提交 0~5 分结果
#    先启动服务：docker compose up -d --build（API 地址 http://localhost:8000）
#    以下 $SID 为一场已结束（finished）且答错过题的会话
curl -s -X POST http://localhost:8000/api/sessions/$SID/review-plan
# HTTP 201：
# {"id":"<PLAN_ID>","session_id":"$SID","item_count":2,
#  "items":[{"id":"<REVIEW_ITEM_ID>","item_id":"ITEM-003",
#            "stem":"计算：47 − 29 = ?","options":[{"key":"A","text":"18"},...],
#            "answer":"A","ease_factor":2.5,"interval_days":0,"repetitions":0,
#            "due_date":"2026-09-25",   # 初始题目当日（UTC）到期
#            "created_at":"...","updated_at":"..."}, ...],
#  "created_at":"..."}
# 题干/选项/正确答案在创建时冻结，之后题库改题/撤回不影响复习内容；
# 会话未结束 -> HTTP 409；会话不存在 -> HTTP 404

# 同一会话重复创建 -> HTTP 200，返回原计划（不新增任何行）
curl -s -X POST http://localhost:8000/api/sessions/$SID/review-plan

# 按 UTC 日期查询到期题（date 缺省为今天；已过期未复习的题保持到期）
curl -s "http://localhost:8000/api/review/due"
curl -s "http://localhost:8000/api/review/due?date=2026-09-25"
# {"date":"2026-09-25","count":2,"results":[ ...复习题（含冻结内容与日程状态）... ]}
# date 非法（如 2026-13-99）-> HTTP 422

# 提交复习结果：score 为 0~5 的整数，review_id 为客户端幂等键
curl -s -X POST http://localhost:8000/api/review/items/<REVIEW_ITEM_ID>/results \
  -H 'Content-Type: application/json' \
  -d '{"review_id": "'$(uuidgen)'", "score": 4}'
# HTTP 201：
# {"review_id":"...","review_item_id":"<REVIEW_ITEM_ID>","score":4,
#  "ease_factor_before":2.5,"ease_factor_after":2.5,
#  "interval_before":0,"interval_after":1,        # 第 1 次成功 -> 间隔 1 天
#  "repetitions_before":0,"repetitions_after":1,
#  "due_date_before":"2026-09-25","due_date_after":"2026-09-26",  # 复习当日+新间隔
#  "created_at":"..."}
# 评分 < 3 -> 次日重练（间隔 1 天）并清零连续成功次数；
# 第 1、2 次成功间隔 1、6 天，其后为 旧间隔×熟练因子 四舍五入；
# 每次结果后 EF = max(1.3, EF + 0.1 − (5−q)×(0.08+(5−q)×0.02))

# 相同 review_id + 相同题目 + 相同评分重放 -> HTTP 200，返回首次结果，不重复推进日程
curl -s -X POST http://localhost:8000/api/review/items/<REVIEW_ITEM_ID>/results \
  -H 'Content-Type: application/json' \
  -d '{"review_id": "<同一个 review_id>", "score": 4}'
# 同一 review_id 提交不同评分或用于其他复习题 -> HTTP 409（结果与日程均不改动；
# 冲突检查先于到期判断）；复习题未到期（due_date 在未来）-> HTTP 409；
# 未知复习题 -> HTTP 404；score 越界/非整数 -> HTTP 422

## 环境变量

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `QUIZ_DB_PATH` | `data/quiz.db`（本地）/ `/data/quiz.db`（镜像） | SQLite 文件路径 |
| `DEFAULT_EXPOSURE_LIMIT` | `5` | 创建会话未指定曝光上限时的默认值 |

## 说明与边界

- 模型为 2PL（无猜测参数）；演示题为简单算术题，重点展示选题与能力更新机制。
- 曝光计数是**跨会话全局**的：用于模拟真实 CAT 的曝光控制（曝光上限可按会话放宽/收紧）。
- 暂停/恢复**无需额外配置**：状态与冻结秒数随会话存放在同一 SQLite 库；服务启动时旧库自动迁移（新增 `paused_at` / `remaining_seconds` 列，并把 `status` 的 CHECK 放宽到 `active/paused/finished`），历史会话与答题记录原样保留。所有时间戳均为 **UTC**、秒级精度，因此暂停冻结与恢复重算的截止时间最多有 ±1 秒的取整误差。
- 题目撤回**无需额外配置**：旧库启动时自动为 `items` 表新增 `is_active` 列（`DEFAULT 1`），**既有题目全部迁移为启用**；撤回只影响新取题与新的作答，历史答题记录、曝光计数与单题复盘统计均不受影响。
- **配额按作答时冻结标签统计，不随题库追溯改写**：提交答案时把该题当时的 `content_tag` 冻结进 `responses.content_tag`，会话进度的 `tag_answered`、`/next` 的缺口与末段欠额限制、欠额不可补齐判定、以及精度停测的配额门槛全部读这份冻结值；进行中再导入修改已答题标签或撤回已答题，只影响今后新取题，不改变已答归属、不会误判配额已满足或提前结束。旧库首次启动自动为 `responses` 增加 `content_tag` 列并按**当时**题库标签**一次性回填**（空白归一为 `NULL`），此后启动不再回填、不随后续题库变化。未设置配额的会话完全不受影响。
- **标准误按作答时冻结参数计算，不随题库改参漂移**：提交答案时把送达版本的难度/区分度冻结进 `responses.difficulty` / `responses.discrimination`，会话状态、进度、`/next` 与精度停测的 `standard_error` 都在当前 θ 下按各条作答的冻结参数重算 Fisher 信息总和（不再读题库现值）；进行中或结束后正式导入修改已答题参数，不会改变任何无新增作答会话的标准误，不会误触发或错过 `precision_reached`。旧库首次启动自动为 `responses` 增加这两列并按**当时**题库参数**一次性回填**（题目只撤回不删除，回填总能解析；此后启动不再回填、不随后续题库变化）。未作答的挂起题、选题候选的信息量仍按题库当前参数计算，行为不变。
- 答卷雷同复核**无需额外配置**：不新增任何表，直接复用现有 `sessions`/`responses`；只统计 `finished` 会话，且只读作答时冻结的 `score` 与 `response`——题库改答案、题目撤回都不影响复核结果；接口完全只读，可安全地对历史会话随时调用。
- 单题预测概率校准**无需额外配置**：同样只读 `responses` 中作答前保存的 `p_correct` 与冻结的 `score`，不按当前能力值或改动后的题目参数重算；只统计 `finished` 会话，接口完全只读。
- 题库修订历史**无需额外配置**：服务启动时自动创建 `item_revisions` 表，并**仅在第一次**为所有现存题补建版本 1 `baseline`（幂等，重复启动不重复生成）。历史只增不改：正式导入新增/内容实际变化、启用状态实际翻转都在原写事务内追加版本；同值重试、预检与整批校验失败不产生版本；快照不含 `exposure_count`。
- 取题版本冻结**无需额外配置**：旧库启动时自动为 `sessions` 表新增 `current_item_version` 列（早于暂停功能的库随表重建一并迁移）。每次新取题在选题事务内记录送达的修订版本；升级前遗留的挂起题该列为 `NULL`，按旧行为回退到题库当前内容，新取题立即生效。
- 错题间隔复习**无需额外配置**：服务启动时自动创建 `review_plans` / `review_items` / `review_results` 三张表（旧库启动即完成迁移）；容器启动即自动完成依赖安装与建表（`docker compose up -d --build` 后即可调用）。计划内容在创建时冻结，题库后续改题/撤回不影响复习；到期日均按 **UTC 日期**计算；复习结果的 `review_id` 绑定首次提交的复习题与评分（同题同分重放返回首次结果，题目或评分不同返回 `409` 且不改动结果与日程），写操作同样使用 `BEGIN IMMEDIATE` 串行化。
- SQLite + WAL 适合单机/演示与中小流量；写事务已串行化（暂停/恢复同样使用 `BEGIN IMMEDIATE`），水平扩展时可替换连接层。
