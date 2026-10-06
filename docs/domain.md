# 领域约定

为儿童干预计划、服务事实和阶段复盘定义可版本化的交换契约，区分家庭记录与专业观察。
交换层（`contracts/` + `contracts.py`）只负责稳定报告结构、枚举、时间、版本、事件—聚合归属和必需载荷；
协同不变量由 `service.py` 在可回溯时间线（`timeline.py`）之上落实。

所有时间都必须携带时区，版本号从 1 开始、按聚合连续递增，校验层不会替调用方改写输入。

## 聚合

- `child_case`：儿童个案，评估、家庭优先事项、风险动作、共患转介、逾期提醒都归属个案。
- `care_plan`：干预计划流。每次批准/修订新开版本，旧版本永久保留并可重放。
- `functional_goal`：功能目标。目标身份（`goal_id`）跨表述变化保持稳定。
- `service_commitment`：服务责任槽位。先认领（`SERVICE_CLAIMED`）后签认（`SERVICE_RECORDED`）。
- `review_cycle`：阶段复盘窗口。

## 事件类型

`ASSESSMENT_ACCEPTED`、`FAMILY_PRIORITY_RECORDED`、`PLAN_APPROVED`、`PLAN_REVISED`、
`GOAL_DEFINED`、`GOAL_RESTATEMENT_PROPOSED`、`GOAL_RESTATEMENT_DISPUTED`、
`GOAL_REVIEW_FLAGGED`、`GOAL_REVIEW_RESOLVED`、`OBSERVATION_RECORDED`、
`SERVICE_CLAIMED`、`SERVICE_RECORDED`、`RISK_ACTION_TAKEN`、
`RISK_AUTHORIZATION_COMPLETED`、`RISK_INDEPENDENT_REVIEW_COMPLETED`、
`REFERRAL_ISSUED`、`REFERRAL_RESOLVED`、`REVIEW_COMPLETED`、
`UPLOAD_DISPUTED`、`OVERDUE_REMINDER_FIRED`。

各事件的必需载荷见 `contracts/domain.schema.json` 的 `payload_required_by_event`；
枚举字段（观察来源、风险授权状态、复审结论）见 `payload_enums`；
事件与聚合的归属见 `aggregate_by_event`。

## 协同不变量

### 一条可回溯时间线

- 事件仅追加，按实际发生时间回放；迟到的家庭补记带 `observed_at`，不改变既有事件。
- 每次活动（观察、服务、复盘）载荷必须携带 `plan_version`，且该版本是活动**发生当时**生效的计划；
  计划修订后补记旧活动仍指向旧版本，错挂版本被拒绝。
- 计划按版本连续递增：`PLAN_APPROVED` 从 v1 起，`PLAN_REVISED` 只能替代当前版本；旧版本不作废。

### 同一目标、不同表述

- 目标身份不变；各方改述走 `GOAL_RESTATEMENT_PROPOSED`，不直接改写原表述。
- 不同方提出不同文本时登记 `GOAL_RESTATEMENT_DISPUTED`，所有表述以 `held_texts` 并存保留，
  目标进入 `disputed`，等待复审结论（`GOAL_REVIEW_RESOLVED`：`kept`/`revised`/`retired`）。

### 评估变化触发的是复审，不是作废

- `ASSESSMENT_ACCEPTED` 标记 `key_change=true` 时，仅把链接到该量表旧版本的在役目标置为
  `under_review`（`GOAL_REVIEW_FLAGGED`）；已按新评估定义的目标不受牵连，计划版本继续生效。
- 评估事件只记录量表、版本、分值与结果，载荷中没有诊断字段；服务不对儿童作自动诊断。

### 服务责任与签认边界

- 服务槽位先认领班签认。跨机构并发认领同一槽位时，检查与追加在同一临界区完成，只有一方成功，
  另一方收到 `ServiceSlotAlreadyClaimed`；同一方重复认领返回原回执。
- `SERVICE_RECORDED` 只允许槽位认领方登记自己实际提供的服务。

### 家庭补记与专业观察并存

- 两类观察都写为 `OBSERVATION_RECORDED`，以 `observer_kind=family|professional` 区分，
  只追加，永不互相覆盖。

### 紧急风险先动作、限时补齐

- `RISK_ACTION_TAKEN` 必须记录 `minimal_action`（最小必要动作）。
- `authorization_status=emergency_pending` 时必须给出晚于处置时刻的
  `authorization_due_at` 与 `independent_review_due_at`。
- 授权与独立复核完成事件回报 `on_time`；独立复核人不能是现场处置人。
- 逾期提醒由截止时间事实推导：`fire_due_reminders` 对每个未办且已逾期的步骤
  发一次 `OVERDUE_REMINDER_FIRED`。时间线可持久化，系统恢复后重放即按原截止时间续发，
  已发键不重发，完成事项不再提醒。

### 幂等上传与争议保留

- 所有上传可带幂等键：相同键、相同业务指纹返回原事件（`_status=duplicate`），不产生新事件。
- 相同键但事件类型、聚合或载荷内容变化时抛出 `ContentMismatch`，同时登记
  `UPLOAD_DISPUTED` 指向原始事件；首次上传内容原样保留，不被覆盖。

### 视图与访问范围

- 家庭视图（`family_dashboard`）只呈现目标进展（专业观察数、家庭补记数、最近专业记录）、
  生命周期与下一责任人（争议/复审中为复盘协调人），不呈现量表分值等临床字段。
- 普通工作人员（`staff_case_view`）只能访问本机构已认领服务的个案；返回内容遮蔽量表结果、
  家庭优先事项等与服务无关的诊断与家庭资料，越权访问抛出 `AccessDenied`。
- 主管可按复盘（`replay_review`）重放窗口内当时有效的观察、服务事实、风险授权链与
  方法版本（`plan_basis.methods`），并标记每条观察是否对窗口计划有效。
