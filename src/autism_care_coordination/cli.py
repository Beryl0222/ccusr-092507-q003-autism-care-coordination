"""从命令行校验领域事件（支持单个 JSON 事件或 JSONL 事件流）。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from .contracts import validate_event


def _check(event: object, schema: dict, index: int | None = None) -> list:
    issues = validate_event(event, schema)
    prefix = f"第 {index} 行: " if index is not None else ""
    if not issues:
        print(f"{prefix}valid")
    else:
        for issue in issues:
            print(f"{prefix}{issue.field}	{issue.code}	{issue.message}")
    return issues


def main() -> int:
    if len(sys.argv) != 3:
        print("用法: python -m autism_care_coordination.cli <schema.json> <event.json|events.jsonl>", file=sys.stderr)
        return 2
    schema = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    raw = Path(sys.argv[2]).read_text(encoding="utf-8")

    if sys.argv[2].endswith(".jsonl"):
        failed = 0
        for line_no, line in enumerate(raw.splitlines(), start=1):
            line = line.strip()
            if not line:
                continue
            issues = _check(json.loads(line), schema, line_no)
            if issues:
                failed += 1
        return 1 if failed else 0

    issues = _check(json.loads(raw), schema)
    return 1 if issues else 0


if __name__ == "__main__":
    raise SystemExit(main())
