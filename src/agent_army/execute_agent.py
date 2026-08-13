"""Run an Agent Army role through the configured coding-agent CLI."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from agent_army.agent_executor import (
    AgentExecutionRequest,
    build_executor,
    role_reference_paths,
)
from agent_army.config import (
    BACKENDS,
    DEFAULT_CONFIG_PATH,
    load_agent_runtime_config,
    load_runtime_config,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run an Agent Army role with a coding-agent CLI.")
    parser.add_argument("--role", type=Path, required=True, help="Path to the role card.")
    parser.add_argument("--workspace", type=Path, required=True, help="Git repository workspace.")
    parser.add_argument("--work-item", type=Path, required=True, help="Normalized work-item JSON file.")
    parser.add_argument(
        "--output-schema",
        type=Path,
        default=Path("schemas/agent-analysis.schema.json"),
        help="JSON Schema for the agent's final response.",
    )
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help="Runtime configuration file (default: config.yaml).",
    )
    parser.add_argument(
        "--backend",
        choices=BACKENDS,
        default=None,
        help="Override the backend selected by the configuration file.",
    )
    args = parser.parse_args()

    try:
        config = load_agent_runtime_config(
            args.role.parent / "agent-config.yaml", load_runtime_config(args.config)
        ).with_backend(args.backend)
        work_item = json.loads(args.work_item.read_text(encoding="utf-8"))
        execution = build_executor(config).execute(
            AgentExecutionRequest(
                role_path=args.role,
                workspace=args.workspace,
                work_item=work_item,
                output_schema_path=args.output_schema,
                reference_paths=role_reference_paths(args.role),
            )
        )
    except (OSError, RuntimeError, ValueError, json.JSONDecodeError) as error:
        print(f"Agent execution failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    print(json.dumps(execution.output, indent=2))
    print(
        f"[{execution.backend}] reported cost ${execution.cost_usd:.4f}",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
