# 孤独症干预协同档案

在 2026 版指南衔接场景下，把评估量表及版本、家庭优先事项、功能目标、干预方法与
循证依据、服务频次、共患问题转介、观察记录、风险计划和阶段复盘保持在同一条
可回溯时间线上。

## 分层结构

- `contracts/domain.schema.json`：事件信封、聚合/事件枚举与各事件必填载荷。
- `src/autism_care_coordination/contracts.py`：无副作用的交换契约校验。
- `src/autism_care_coordination/store.py`：SQLite 追加式台账（时间线、争议、
  责任槽位唯一认领、提醒、访问授权）。
- `src/autism_care_coordination/service.py`：协同业务服务
  `CareCoordinationService`——有效计划指向、幂等回执、争议保留、观察并存、
  紧急风险时限、提醒恢复补发、角色视图与主管复盘重放。
- `data/sample.json`：单事件校验样例；`data/sample_events.jsonl`：贯穿全流程
  的 15 个事件联调样例。
- `docs/domain.md`：领域对象、事件与业务规则说明。
- `tests/`：契约边界测试与业务规则测试（含真实跨线程并发认领、停机恢复补发）。

## 核心保证

- 每项活动必须指向其发生时有效的计划版本；关键评估变化只复审相关目标，不整版作废。
- 重复上传返回原回执；同标识异内容保留争议、原件不被覆盖。
- 服务责任槽位靠数据库唯一约束认领，跨机构并发只有一方成功；机构只能签认自己的服务。
- 家庭补记与专业观察并存不覆盖；普通工作人员只见最小必要资料，无授权案例不可见。
- 紧急风险可先执行最小必要动作，授权（默认 24h）与独立复核（默认 72h）限时补齐；
  系统恢复后按原始到期时间顺序补发逾期提醒。
- 家庭看到目标进展与下一责任人；主管可重放任一复盘所依据的观察、授权与方法版本。
- 系统只登记分数与筛查提示，不对儿童作自动诊断。

## 快速使用

```python
from autism_care_coordination import CareCoordinationService

svc = CareCoordinationService("care.db")  # 或 ":memory:"
svc.grant_staff_access("sup-1", "case-001", "supervisor")

svc.accept_assessment("case-001", "asmt-1", "2026-08-30T10:00:00+08:00",
                      instrument="PEP-3", instrument_version="C/2024",
                      scores={"communication": 42})
svc.approve_plan("case-001", "plan-1", "2026-09-01T09:00:00+08:00",
                 assessment_event_id="asmt-1",
                 goals=[{"goal_id": "g1", "wording": "主动提出需求 5 次/日"}],
                 methods=[{"name": "PRT", "evidence": "NCAEP 2020"}],
                 frequency={"org-A": "每周 3 次"}, guardian_consent=True)
```

## 测试 / 检查

```bash
python3 -m unittest discover -s tests
python3 -m compileall -q src tests
PYTHONPATH=src python3 -m autism_care_coordination.cli contracts/domain.schema.json data/sample_events.jsonl
```

CLI 接受单个 JSON 事件或 JSONL 事件流；逐行输出 `valid` 或「字段 代码 中文说明」，
存在任何不合法事件时以非零状态结束。
