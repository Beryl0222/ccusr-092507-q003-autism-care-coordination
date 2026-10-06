# 领域约定

为儿童干预计划、服务事实和阶段复盘定义可版本化的交换契约，区分家庭记录与专业观察；
并在交换契约之上规定协同档案业务服务（`CareCoordinationService`）必须落实的规则。

## 分层

1. **交换契约层**（`contracts/domain.schema.json` + `contracts.py`）：只校验事件信封、
   枚举、时区时间、正整数版本与各事件的必填载荷，不替调用方改写输入，也不负责
   幂等/冲突语义。
2. **业务服务层**（`service.py` + `store.py`）：追加式时间线、幂等回执、争议保留、
   责任认领、有效计划指向、角色视图与复盘重放。

所有时间都必须携带时区；版本号从 1 开始，在同一聚合流上递增。时间线按
`occurred_at`（同刻按接收序号 `seq`）排序——晚到的补录按其真实发生时间归位，
事件一经写入不可修改、不可删除。

## 聚合与事件

聚合对象：`child_case`、`care_plan`、`goal`、`service_slot`、
`service_commitment`、`review_cycle`。

| 事件 | 含义 | 关键载荷 |
| --- | --- | --- |
| `ASSESSMENT_ACCEPTED` | 登记评估量表、版本与分数（2026 版指南衔接） | `instrument`, `instrument_version`, `scores`, `significant_change` |
| `PLAN_APPROVED` | 批准一版计划（目标、方法与循证依据、频次、家庭优先事项） | `assessment_event_id`, `assessment_version`, `guardian_consent`, `goals`, `methods`, `frequency` |
| `GOAL_REPHRASING_PROPOSED` | 某方对目标提出不同表述（只追加，不覆盖） | `goal_id`, `wording`, `source_party`, `plan_version` |
| `GOAL_RECONSIDERATION_FLAGGED` | 关键评估变化后，仅把相关目标送入复审 | `goal_ids`, `trigger_event_id` |
| `SERVICE_CLAIMED` | 机构认领某一服务责任槽位 | `provider_id`, `slot_key` |
| `SERVICE_RECORDED` | 机构签认自己实际提供的一次服务 | `plan_version`, `provider_id`, `slot_key`, `goal_ids` |
| `OBSERVATION_RECORDED` | 家庭补记或专业观察（二者并存） | `goal_id`, `observer_type`, `observer_id`, `plan_version`, `progress_value` |
| `REFERRAL_ISSUED` | 共患问题转介 | `issue`, `target_specialty`, `follow_up_due_at?` |
| `SCREENING_FLAG_RAISED` | 量表越线提示（建议进一步评估，**不是诊断**） | `instrument`, `threshold`, `is_diagnosis=false` |
| `RISK_ACTION_TAKEN` | 挑战性行为紧急处置（先执行最小必要动作） | `reason`, `action_taken`, `acted_by`, `authorization_due_at?`, `review_due_at` |
| `RISK_AUTHORIZATION_RECORDED` | 限时补齐监护人授权 | `risk_event_id`, `guardian_consent` |
| `RISK_INDEPENDENT_REVIEW_RECORDED` | 限时完成独立复核（非实施者本人的主管） | `risk_event_id`, `reviewer_id` |
| `REVIEW_COMPLETED` | 阶段复盘，固化当时有效计划版本 | `plan_version_at_review`, `plan_event_id`, `goal_decisions`, `next_actions` |

## 业务规则

- **每项活动指向当时有效的计划。** 服务签认、观察、措辞提议都按自己的
  `occurred_at` 解析有效计划版本；指向旧版或不存在版本将被拒绝。历史时间点
  的补录以该时刻的有效版本为准。
- **关键评估变化只触发相关目标复审**（`GOAL_RECONSIDERATION_FLAGGED`），
  整份计划不自动作废，其余目标继续执行。
- **机构只能签认自己认领的服务。** `(case_id, slot_key)` 有数据库唯一约束，
  跨机构并发认领只有一方成功，另一方收到 `ServiceClaimConflict`（含胜出方）。
  未认领或代他人签认抛出 `ProviderNotAuthorized`。
- **家庭补记与专业观察并存。** 两类记录各自追加，任何一方都不能修改或覆盖另一方。
- **幂等与争议。** 同一 `event_id` 以规范化内容哈希判定：内容一致 → 返回首次
  回执（`accepted_duplicate`，同一 `seq`，不产生重复副作用）；同标识而内容变化
  → 原件保持有效，冲突副本以 `excluded=1` 单独留存并登记争议
  （`conflict_kept`），争议内容重复上报不重复立案。
- **紧急风险处置。** 允许无事先授权先执行，但动作必须标记为最小必要；系统据此
  安排两条时限：补授权（默认 24h）与独立复核（默认 72h）。事先已授权时不生成
  补授权提醒。独立复核人不得是实施者本人，且必须是该案例主管。是否逾期只以
  时间线上是否已出现对应授权/复核事件为准。
- **提醒在恢复后按原时间继续。** 提醒的 `due_at` 由事件发生时间推导并持久化；
  停机恢复后 `pump_reminders` 按原始到期时间顺序补发错过的提醒，每条只投递一次。
- **最小必要可见。** 无案例授权的工作人员无法打开案例。普通机构人员只见本机构
  认领的责任、签认、所服务目标及方法频次；评估原始分数、筛查提示、转介、风险
  明细、家庭优先事项与家庭私密资料均不出现。主管可见完整时间线。
- **家庭视图**只呈现目标进展与下一责任人（取自最近一次复盘的 `next_actions`），
  不含诊断与敏感资料。
- **主管复盘重放**：给定某次 `REVIEW_COMPLETED`，返回截至该复盘时刻有效的计划
  版本（含方法循证版本）、全部观察、触发复审的评估与标记、风险处置及其授权/
  复核、当时未决争议；冲突副本与复盘后的材料不进入重放。
- **服务不自动诊断。** 系统只登记量表分数与筛查提示，`SCREENING_FLAG_RAISED`
  显式携带 `is_diagnosis=false` 与"建议进一步评估"措辞。

## 存储

SQLite 追加式台账（`store.py`）：`events`（部分唯一索引仅约束有效行，使争议
副本可同标识留存）、`disputes`、`service_slots`（认领唯一约束）、`reminders`
（按原始到期时间补发）、`access_grants`（工作人员—案例授权与角色）。
