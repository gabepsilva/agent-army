"""Documentation-role interpretation of a shared work item."""

from __future__ import annotations

from typing import Any


def add_documentation_signals(work_item: dict[str, Any]) -> dict[str, Any]:
    """Add Doku-specific signals without changing the shared work-item format."""
    pull_request = work_item.get("pull_request")
    if pull_request is None:
        work_item["documentation_signals"] = {
            "documentation_files_changed": [],
            "test_files_changed": [],
        }
        return work_item

    paths = [changed_file["path"] for changed_file in pull_request["changed_files"]]
    work_item["documentation_signals"] = {
        "documentation_files_changed": [
            path
            for path in paths
            if path.lower().endswith((".md", ".rst", ".adoc")) or path.startswith("docs/")
        ],
        "test_files_changed": [
            path for path in paths if "test" in path.lower() or "spec" in path.lower()
        ],
    }
    return work_item
