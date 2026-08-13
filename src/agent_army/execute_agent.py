"""Run an Agent Army role through the shared Codex CLI executor."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent_army.codex_executor import CodexCliExecutor, CodexExecutionRequest


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an Agent Army role with Codex CLI.")
    parser.add_argument("--role", type=Path, required=True, help="Path to the role card.")
    parser.add_argument("--workspace", type=Path, required=True, help="Git repository workspace.")
    parser.add_argument("--work-item", type=Path, required=True, help="Normalized work-item JSON file.")
    parser.add_argument(
        "--output-schema",
        type=Path,
        default=Path("schemas/agent-analysis.schema.json"),
        help="JSON Schema for Codex's final response.",
    )
    args = parser.parse_args()

    try:
        work_item = json.loads(args.work_item.read_text(encoding="utf-8"))
        result = CodexCliExecutor().execute(
            CodexExecutionRequest(
                role_path=args.role,
                workspace=args.workspace,
                work_item=work_item,
                output_schema_path=args.output_schema,
            )
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"Agent execution failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
