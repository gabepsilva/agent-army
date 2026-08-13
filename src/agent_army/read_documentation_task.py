"""Read one GitHub work item and add Doku's documentation signals."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from agent_army.documentation_context import add_documentation_signals
from agent_army.read_work_item import read_work_item


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read an issue or pull request for Doku without writing to GitHub."
    )
    parser.add_argument("url", help="HTTPS GitHub issue or pull-request URL.")
    parser.add_argument(
        "--agent-directory",
        type=Path,
        default=Path("agents/documentation"),
        help="Directory containing agent-config.yaml.",
    )
    args = parser.parse_args()
    try:
        work_item = read_work_item(args.url, args.agent_directory)
    except (RuntimeError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Read-only task failed: {error}", file=sys.stderr)
        raise SystemExit(1) from error

    print(json.dumps(add_documentation_signals(work_item), indent=2))

if __name__ == "__main__":
    main()
