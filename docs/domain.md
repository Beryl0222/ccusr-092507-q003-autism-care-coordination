# 领域约定

为儿童干预计划、服务事实和阶段复盘定义可版本化的交换契约，区分家庭记录与专业观察。

聚合对象包括`child_case`、`care_plan`、`service_commitment`、`review_cycle`。事件类型包括`ASSESSMENT_ACCEPTED`、`PLAN_APPROVED`、`SERVICE_RECORDED`、`RISK_ACTION_TAKEN`、`REVIEW_COMPLETED`。所有时间都必须携带时区，版本号从 1 开始递增，校验层不会替调用方改写输入。

## 事件载荷

- `PLAN_APPROVED`：还需包含 `assessment_version`, `guardian_consent`。
- `SERVICE_RECORDED`：还需包含 `plan_version`, `provider_id`。
- `RISK_ACTION_TAKEN`：还需包含 `reason`, `review_due_at`。

同一事件标识的幂等与冲突处理属于上层业务服务职责；交换层只负责稳定报告结构、枚举、时间、版本和必需载荷问题。
