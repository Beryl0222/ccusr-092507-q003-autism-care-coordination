# 孤独症干预协同档案

为儿童干预计划、服务事实和阶段复盘定义可版本化的交换契约，区分家庭记录与专业观察，
并在事件时间线之上落实跨机构协同不变量（有效计划指向、目标争议保留、服务认领签认、
紧急风险限时复核、幂等上传、按原时间续发逾期提醒、分角色视图与复盘重放）。

## 目录

- `contracts/domain.schema.json`：事件信封、对象类型、事件—聚合归属和事件载荷约定。
- `data/sample.json`：可直接校验的中文联调样例。
- `src/autism_care_coordination/`：
  - `contracts.py`：契约校验与命令行入口。
  - `timeline.py`：仅追加事件时间线、幂等键、业务指纹与持久化恢复。
  - `service.py`：协同档案领域服务、提醒与家庭/工作人员/主管视图。
- `tests/`：基础字段、时间版本、事件载荷边界与全部协同不变量测试。
- `docs/domain.md`：领域对象、事件语义与协同不变量。

## 测试

```bash
python3 -m unittest discover -s tests
```

## 编译检查

```bash
python3 -m compileall -q src tests
```

## 样例校验

```bash
PYTHONPATH=src python3 -m autism_care_coordination.cli contracts/domain.schema.json data/sample.json
```

命令成功时输出 `valid`；校验失败时逐行输出字段、代码和中文说明，并以非零状态结束。
