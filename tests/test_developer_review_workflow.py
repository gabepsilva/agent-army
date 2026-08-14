import subprocess
import unittest
from pathlib import Path

from agent_army.git_worktrees import WorktreeResult
from agent_army.agent_executor import AgentExecutionResult
from agent_army.orchestrator import IssueOrchestrator
from agent_army.publishers import MAX_CONVERGENCE_ROUNDS, decode_payload, encode_payload
from agent_army.work_items import WorkItemReader


def developer_result() -> dict:
    return {
        "status": "completed",
        "summary": "Implemented the accepted change.",
        "evidence": ["Focused tests pass."],
        "questions": [],
        "recommended_actions": ["Run the normal review workflow."],
        "files_changed": ["src/example.py"],
        "commands_run": ["uv run python -m unittest"],
    }


def blocked_developer_result() -> dict:
    return {
        "status": "blocked",
        "summary": "Implementation needs a product decision.",
        "evidence": ["The acceptance criteria leave the target audience ambiguous."],
        "questions": ["Which audience should this behavior support?"],
        "recommended_actions": ["Project Owner should decide the audience."],
        "files_changed": [],
        "commands_run": [],
    }


def project_owner_decision_result() -> dict:
    return {
        "summary": "Resolved the review's concerns with a small, reversible scope clarification.",
        "evidence": ["The Optimization Reviewer flagged an ambiguous edge case."],
        "questions": [],
        "recommended_actions": ["Developer should implement the clarified scope."],
        "files_changed": [],
        "commands_run": [],
        "next_state": "ready-for-development",
    }


def review_result() -> dict:
    return {
        "outcome": "approved",
        "reviewed_commit": "developer-sha",
        "summary": "No blocking optimization findings.",
        "findings": [],
        "dispute_responses": [],
        "evidence": ["Tests and changed files were inspected."],
        "questions": [],
        "recommended_actions": [],
        "files_changed": [],
        "commands_run": ["uv run python -m unittest"],
    }


def blocking_review_result() -> dict:
    return {
        "outcome": "changes-requested",
        "reviewed_commit": "developer-sha",
        "summary": "One blocking finding on the retry path.",
        "findings": [
            {
                "id": "F1",
                "severity": "blocking",
                "claim": "The retry path re-runs the agent on every failure.",
                "evidence": ["src/example.py:42 runs before the guard."],
            }
        ],
        "dispute_responses": [],
        "evidence": ["Read src/example.py:42."],
        "questions": [],
        "recommended_actions": ["Move the guard ahead of the agent run."],
        "files_changed": [],
        "commands_run": ["uv run python -m unittest"],
    }


class FakeWorkflowGitHub:
    def __init__(self, labels: list[str], comments: list[dict] | None = None) -> None:
        self.labels = labels
        self.comments = comments or []
        self.pr_comments: list[dict] = []
        self.checks: list[dict] = []
        self.label_updates: list[list[str]] = []
        self.check_run_error: Exception | None = None
        self.pull_request = {
            "number": 9,
            "html_url": "https://github.com/acme/widgets/pull/9",
            "title": "Implement issue #7",
            "body": "Implements #7.",
            "state": "open",
            "user": {"login": "developer-app"},
            "head": {"ref": "agent-army/issue-7-test-issue", "sha": "developer-sha"},
            "base": {"ref": "main", "sha": "base-sha"},
        }

    def list_open_issues(self, owner: str, repository: str) -> list[dict]:
        return [{"number": 7}]

    def get_repository(self, owner: str, repository: str) -> dict:
        return {"default_branch": "main"}

    def get_issue(self, target) -> dict:
        if target.kind == "pull_request":
            return {
                "title": self.pull_request["title"],
                "body": self.pull_request["body"],
                "state": "open",
                "html_url": self.pull_request["html_url"],
                "labels": [],
            }
        return {
            "title": "Test issue",
            "body": "Issue body",
            "state": "open",
            "html_url": "https://github.com/acme/widgets/issues/7",
            "labels": [{"name": label} for label in self.labels],
        }

    def get_issue_comments(self, target) -> list[dict]:
        # GitHub serves PR-conversation comments from the issues endpoint, so
        # the pull request's own number selects the thread -- not the target
        # kind. create_issue_comment above already routes this way.
        if target.number == self.pull_request["number"]:
            return self.pr_comments
        return self.comments

    def create_issue_comment(self, target, body: str) -> dict:
        entry = {"body": body, "user": {"login": "agent-army"}}
        if target.number == self.pull_request["number"]:
            # A PR-conversation comment (posted via an "issue"-kind target
            # whose number is the PR's) lands in the PR's own thread, not
            # the originating issue's.
            self.pr_comments.append(entry)
        else:
            self.comments.append(entry)
        return {"html_url": "https://github.com/acme/widgets/issues/7#comment"}

    def update_issue_labels(self, target, labels: list[str]) -> dict:
        self.labels = labels
        self.label_updates.append(labels)
        return {}

    def list_open_pull_requests(self, owner: str, repository: str, *, head: str | None = None) -> list[dict]:
        return []

    def create_pull_request(self, owner: str, repository: str, **kwargs) -> dict:
        return self.pull_request

    def get_pull_request(self, target) -> dict:
        return self.pull_request

    def get_pull_request_files(self, target) -> list[dict]:
        return []

    def get_pull_request_reviews(self, target) -> list[dict]:
        return []

    def get_check_runs(self, owner: str, repository: str, head_sha: str) -> list[dict]:
        return [check for check in self.checks if check["head_sha"] == head_sha]

    def create_check_run(self, owner: str, repository: str, **kwargs) -> dict:
        if self.check_run_error is not None:
            raise self.check_run_error
        check = {"name": kwargs["name"], "head_sha": kwargs["head_sha"], "output": {"summary": kwargs["summary"]}}
        self.checks.append(check)
        return check


class FakeExecutor:
    def __init__(self, result: dict) -> None:
        self.result = result
        self.requests = []

    def execute(self, request) -> AgentExecutionResult:
        self.requests.append(request)
        return AgentExecutionResult(output=self.result, backend="claude", cost_usd=0.25)


class FakeWorktree:
    def __init__(self, branch: str) -> None:
        self.result = WorktreeResult(Path("/tmp/agent-army-test-worktree"), branch, "base-sha")

    def __enter__(self) -> WorktreeResult:
        return self.result

    def __exit__(self, *_: object) -> None:
        return None


class DeveloperReviewWorkflowTests(unittest.TestCase):
    def make_orchestrator(
        self,
        github: FakeWorkflowGitHub,
        executor: FakeExecutor,
        *,
        review_clean: bool = False,
    ) -> IssueOrchestrator:
        git_calls = {"rev_parse": 0}

        def git_runner(command, **kwargs):
            if command[-2:] == ["rev-parse", "main"]:
                return subprocess.CompletedProcess(command, 0, stdout="base-sha\n", stderr="")
            if command[-2:] == ["rev-parse", "HEAD"]:
                git_calls["rev_parse"] += 1
                sha = "base-sha" if git_calls["rev_parse"] == 1 else "developer-sha"
                return subprocess.CompletedProcess(command, 0, stdout=f"{sha}\n", stderr="")
            if command[-2:] == ["status", "--porcelain"]:
                status = "" if review_clean else " M src/example.py\n"
                return subprocess.CompletedProcess(command, 0, stdout=status, stderr="")
            if command[1:4] == ["show-ref", "--verify", "--quiet"]:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        return IssueOrchestrator(
            repository="acme/widgets",
            project_owner_github=github,
            documentation_github=github,
            developer_github=github,
            reviewer_github=github,
            workspace=Path("/workspace"),
            project_owner_role=Path("agents/project-owner/ROLE.md"),
            documentation_role=Path("agents/documentation/ROLE.md"),
            developer_role=Path("agents/developer/ROLE.md"),
            reviewer_role=Path("agents/optimization-reviewer/ROLE.md"),
            output_schema_path=Path("schemas/orchestrator-result.schema.json"),
            developer_output_schema_path=Path("schemas/developer-result.schema.json"),
            reviewer_output_schema_path=Path("schemas/optimization-review-result.schema.json"),
            executor=executor,
            work_item_reader=WorkItemReader(github),
            worktree_factory=lambda repository, **kwargs: FakeWorktree(kwargs["branch"]),
            git_command_runner=git_runner,
        )

    def test_developer_commits_pushes_creates_pr_and_routes_to_review(self) -> None:
        github = FakeWorkflowGitHub(["ready-for-development"])
        executor = FakeExecutor(developer_result())

        outcome = self.make_orchestrator(github, executor).run_once()

        self.assertEqual(outcome.status, "processed")
        self.assertEqual(github.labels, ["needs-optimization-review"])
        self.assertEqual(len(github.comments), 1)
        self.assertIn("pull/9", github.comments[0]["body"])
        self.assertEqual(executor.requests[0].workspace, Path("/tmp/agent-army-test-worktree"))
        self.assertEqual(
            executor.requests[0].reference_paths,
            (Path("agents/developer/references/mattpocock-skills/domain-modeling/SKILL.md"),),
        )

    def test_blocked_developer_question_is_durable_and_routes_to_project_owner(self) -> None:
        github = FakeWorkflowGitHub(["ready-for-development"])
        executor = FakeExecutor(blocked_developer_result())

        outcome = self.make_orchestrator(github, executor).run_once()

        self.assertEqual(outcome.status, "processed")
        self.assertEqual(github.labels, ["needs-decision"])
        self.assertEqual(len(github.comments), 1)
        self.assertIn("Focused question", github.comments[0]["body"])
        self.assertIn("Which audience", github.comments[0]["body"])

    def test_reviewer_publishes_check_and_routes_approved_head_to_merge_pause(self) -> None:
        developer_marker = {
            "body": "<!-- agent-army:result role=developer from=ready-for-development "
            "next=needs-optimization-review pr=9 branch=agent-army/issue-7-test-issue head=developer-sha -->"
        }
        github = FakeWorkflowGitHub(["needs-optimization-review"], [developer_marker])
        executor = FakeExecutor(review_result())

        outcome = self.make_orchestrator(github, executor, review_clean=True).run_once()

        self.assertEqual(outcome.status, "processed")
        self.assertEqual(github.labels, ["ready-for-merge"])
        self.assertEqual(len(github.checks), 1)
        self.assertIn("Outcome: **approved**", github.checks[0]["output"]["summary"])
        # Review discussion lives entirely on the pull request.
        self.assertEqual(len(github.pr_comments), 1)
        self.assertIn("Evidence", github.pr_comments[0]["body"])
        self.assertIn("round=1", github.pr_comments[0]["body"])
        # The issue gets no review discussion -- only the Developer's original
        # PR handoff and Project Owner's one-line housekeeping trace.
        self.assertEqual(len(github.comments), 2)
        self.assertNotIn("Evidence", github.comments[1]["body"])
        self.assertIn("ready to merge", github.comments[1]["body"])
        self.assertEqual(
            executor.requests[0].reference_paths,
            (
                Path(
                    "agents/optimization-reviewer/references/"
                    "mattpocock-skills/domain-modeling/SKILL.md"
                ),
            ),
        )

    def test_project_owner_sees_the_linked_pull_request_directly(self) -> None:
        # Project Owner picking up needs-decision used to only ever see
        # whatever got manually duplicated into an issue comment. It should
        # now see the linked PR's real content directly, the same way the
        # Reviewer already gets the issue attached as source_issue.
        developer_marker = {
            "body": "<!-- agent-army:result role=developer from=ready-for-development "
            "next=needs-optimization-review pr=9 branch=agent-army/issue-7-test-issue head=developer-sha -->"
        }
        github = FakeWorkflowGitHub(["needs-decision"], [developer_marker])
        executor = FakeExecutor(project_owner_decision_result())

        outcome = self.make_orchestrator(github, executor).run_once()

        self.assertEqual(outcome.status, "processed")
        pull_request = executor.requests[0].work_item.get("pull_request")
        self.assertIsNotNone(pull_request)
        self.assertEqual(pull_request["head"], "agent-army/issue-7-test-issue")

    def test_review_findings_reach_the_developer_and_round_is_tracked(self) -> None:
        # A blocking finding published in round 1 must be handed to the
        # Developer as structured state on its next run, so it can accept or
        # dispute it rather than guessing from prose.
        review = blocking_review_result()
        github = FakeWorkflowGitHub(["needs-optimization-review"], [
            {
                "body": "<!-- agent-army:result role=developer from=ready-for-development "
                "next=needs-optimization-review pr=9 branch=agent-army/issue-7-test-issue "
                "head=developer-sha -->"
            }
        ])
        executor = FakeExecutor(review)

        self.make_orchestrator(github, executor, review_clean=True).run_once()

        self.assertEqual(github.labels, ["ready-for-development"])
        # The argument -- and the payload the next round reads back -- is on
        # the pull request now, not the issue.
        marker = github.pr_comments[-1]["body"]
        self.assertIn("round=1", marker)
        self.assertEqual(decode_payload(marker)["findings"][0]["id"], "F1")

    def test_unconverged_argument_escalates_to_a_human_after_round_seven(self) -> None:
        developer_marker = {
            "body": "<!-- agent-army:result role=developer from=ready-for-development "
            "next=needs-optimization-review pr=9 branch=agent-army/issue-7-test-issue "
            "head=developer-sha -->"
        }
        # Seven rounds already argued without converging. Each round reviewed
        # a different commit, because the Developer pushed a revision between
        # them -- so none of them recovers against the current head.
        prior_rounds = [
            {
                "body": "<!-- agent-army:result role=optimization-reviewer "
                f"from=needs-optimization-review next=ready-for-development outcome=changes-requested "
                f"pr=9 head=round-{index}-sha round={index} -->\n"
                + encode_payload({"findings": [blocking_review_result()["findings"][0]], "round": index})
            }
            for index in range(1, MAX_CONVERGENCE_ROUNDS + 1)
        ]
        github = FakeWorkflowGitHub(["needs-optimization-review"], [developer_marker])
        # The rounds happened on the pull request, where the argument lives.
        github.pr_comments.extend(prior_rounds)
        executor = FakeExecutor(review_result())

        outcome = self.make_orchestrator(github, executor, review_clean=True).run_once()

        self.assertEqual(outcome.status, "escalated")
        self.assertEqual(github.labels, ["needs-user-guidance"])
        # The whole point of the bound: no further agent spend.
        self.assertEqual(len(executor.requests), 0)
        self.assertIn("did not converge", github.comments[-1]["body"])
        self.assertIn("F1", github.comments[-1]["body"])

    def test_blocked_revision_hands_over_instead_of_rerunning_forever(self) -> None:
        # A blocked result while revising an existing PR used to return with
        # no comment and no transition, leaving the issue in
        # ready-for-development so every later poll re-ran the whole agent.
        developer_marker = {
            "body": "<!-- agent-army:result role=developer from=ready-for-development "
            "next=needs-optimization-review pr=9 branch=agent-army/issue-7-test-issue "
            "head=developer-sha -->"
        }
        review_marker = {
            "body": "<!-- agent-army:result role=optimization-reviewer "
            "from=needs-optimization-review next=ready-for-development "
            "outcome=changes-requested pr=9 head=developer-sha round=1 -->"
        }
        github = FakeWorkflowGitHub(
            ["ready-for-development"], [developer_marker, review_marker]
        )
        github.pull_request["head"]["ref"] = "agent-army/issue-7-test-issue"
        executor = FakeExecutor(blocked_developer_result())

        outcome = self.make_orchestrator(github, executor).run_once()

        self.assertEqual(outcome.status, "processed")
        self.assertEqual(github.labels, ["needs-decision"])
        self.assertIn("Which audience", github.comments[-1]["body"])

    def test_developer_reads_findings_from_the_pull_request_not_the_issue(self) -> None:
        # Review findings live on the PR now. If the Developer kept reading the
        # issue, open_findings would silently be empty and the defend-or-concede
        # contract would never fire -- a break no other test would notice.
        developer_marker = {
            "body": "<!-- agent-army:result role=developer from=ready-for-development "
            "next=needs-optimization-review pr=9 branch=agent-army/issue-7-test-issue "
            "head=developer-sha -->"
        }
        github = FakeWorkflowGitHub(["ready-for-development"], [developer_marker])
        github.pull_request["head"]["ref"] = "agent-army/issue-7-test-issue"
        # A prior review round, recorded on the pull request.
        github.pr_comments.append(
            {
                "body": "<!-- agent-army:result role=optimization-reviewer "
                "from=needs-optimization-review next=ready-for-development "
                "outcome=changes-requested pr=9 head=developer-sha round=1 -->\n"
                + encode_payload(
                    {"findings": blocking_review_result()["findings"], "round": 1}
                )
            }
        )
        # The Developer is revising an existing PR, not opening a new one.
        github.list_open_pull_requests = lambda owner, repository, *, head=None: [
            github.pull_request
        ]
        result = developer_result()
        result["responses"] = [
            {
                "finding_id": "F1",
                "disposition": "accepted",
                "rationale": "Moved the guard ahead of the agent run.",
            }
        ]
        executor = FakeExecutor(result)

        outcome = self.make_orchestrator(github, executor).run_once()

        self.assertEqual(outcome.status, "processed")
        self.assertEqual(
            executor.requests[0].work_item["review_findings"][0]["id"], "F1"
        )

    def test_reviewer_check_run_failure_does_not_block_transition_or_rerun_executor(self) -> None:
        # A permissions error creating the check run (seen in production as a
        # 403 on /check-runs) used to fail the whole task, so every poll
        # re-ran the review agent from scratch until GitHub cooperated. The
        # check run is supplementary status; the issue comment marker is
        # what recovery keys off of, so this failure should not repeat work.
        developer_marker = {
            "body": "<!-- agent-army:result role=developer from=ready-for-development "
            "next=needs-optimization-review pr=9 branch=agent-army/issue-7-test-issue head=developer-sha -->"
        }
        github = FakeWorkflowGitHub(["needs-optimization-review"], [developer_marker])
        github.check_run_error = RuntimeError("HTTP Error 403: Forbidden")
        executor = FakeExecutor(review_result())

        outcome = self.make_orchestrator(github, executor, review_clean=True).run_once()

        self.assertEqual(outcome.status, "processed")
        self.assertIn("403", outcome.detail or "")
        self.assertEqual(github.labels, ["ready-for-merge"])
        self.assertEqual(len(github.checks), 0)
        self.assertEqual(len(github.pr_comments), 1)
        self.assertEqual(len(github.comments), 2)
        self.assertEqual(len(executor.requests), 1)

    def test_new_commit_on_merge_paused_pr_requires_fresh_review(self) -> None:
        comments = [
            {
                "body": "<!-- agent-army:result role=developer from=ready-for-development "
                "next=needs-optimization-review pr=9 branch=agent-army/issue-7 head=developer-sha -->"
            },
            {
                "body": "<!-- agent-army:result role=optimization-reviewer "
                "from=needs-optimization-review next=ready-for-merge outcome=approved "
                "pr=9 head=old-sha -->"
            },
        ]
        github = FakeWorkflowGitHub(["ready-for-merge"], comments)
        executor = FakeExecutor(review_result())

        task = self.make_orchestrator(github, executor)._select_task(
            {"issue": {"labels": ["ready-for-merge"], "comments": comments}}
        )

        self.assertIsNotNone(task)
        self.assertEqual(task.agent.name, "optimization-reviewer")
        self.assertEqual(task.source_state, "needs-optimization-review")

    def test_pushed_branch_with_no_open_pr_opens_pr_without_rerunning_executor(self) -> None:
        # Simulates a prior run where commit_and_push succeeded but
        # create_pull_request then failed (for example, a missing App
        # permission): the branch already carries commits ahead of main, but
        # no open pull request exists for it yet.
        def git_runner(command, **kwargs):
            if command[1:4] == ["show-ref", "--verify", "--quiet"]:
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            if command[-3:-1] == ["rev-list", "--count"]:
                return subprocess.CompletedProcess(command, 0, stdout="2\n", stderr="")
            if command[-2] == "rev-parse":
                return subprocess.CompletedProcess(command, 0, stdout="pushed-sha\n", stderr="")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        github = FakeWorkflowGitHub(["ready-for-development"])
        executor = FakeExecutor(developer_result())
        orchestrator = IssueOrchestrator(
            repository="acme/widgets",
            project_owner_github=github,
            documentation_github=github,
            developer_github=github,
            reviewer_github=github,
            workspace=Path("/workspace"),
            project_owner_role=Path("agents/project-owner/ROLE.md"),
            documentation_role=Path("agents/documentation/ROLE.md"),
            developer_role=Path("agents/developer/ROLE.md"),
            reviewer_role=Path("agents/optimization-reviewer/ROLE.md"),
            output_schema_path=Path("schemas/orchestrator-result.schema.json"),
            developer_output_schema_path=Path("schemas/developer-result.schema.json"),
            reviewer_output_schema_path=Path("schemas/optimization-review-result.schema.json"),
            executor=executor,
            work_item_reader=WorkItemReader(github),
            worktree_factory=lambda repository, **kwargs: FakeWorktree(kwargs["branch"]),
            git_command_runner=git_runner,
        )

        outcome = orchestrator.run_once()

        self.assertEqual(outcome.status, "recovered")
        self.assertEqual(github.labels, ["needs-optimization-review"])
        # The executor must not run again -- there is nothing left to implement.
        self.assertEqual(executor.requests, [])
        self.assertEqual(len(github.comments), 1)
        self.assertIn("pull/9", github.comments[0]["body"])
        self.assertIn("could not be created at that time", github.comments[0]["body"])

    def test_pushed_branch_not_ahead_of_base_still_reruns_executor(self) -> None:
        # A branch can exist locally without carrying new work (for example, a
        # failed first attempt that created the branch but never committed).
        # That case must still fall through to a normal Developer run.
        def git_runner(command, **kwargs):
            if command[1:4] == ["show-ref", "--verify", "--quiet"]:
                return subprocess.CompletedProcess(command, 0, stdout="", stderr="")
            if command[-3:-1] == ["rev-list", "--count"]:
                return subprocess.CompletedProcess(command, 0, stdout="0\n", stderr="")
            if command[-2:] == ["rev-parse", "main"]:
                return subprocess.CompletedProcess(command, 0, stdout="base-sha\n", stderr="")
            if command[-2:] == ["rev-parse", "HEAD"]:
                return subprocess.CompletedProcess(command, 0, stdout="developer-sha\n", stderr="")
            if command[-2:] == ["status", "--porcelain"]:
                return subprocess.CompletedProcess(command, 0, stdout=" M src/example.py\n", stderr="")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        github = FakeWorkflowGitHub(["ready-for-development"])
        executor = FakeExecutor(developer_result())
        orchestrator = IssueOrchestrator(
            repository="acme/widgets",
            project_owner_github=github,
            documentation_github=github,
            developer_github=github,
            reviewer_github=github,
            workspace=Path("/workspace"),
            project_owner_role=Path("agents/project-owner/ROLE.md"),
            documentation_role=Path("agents/documentation/ROLE.md"),
            developer_role=Path("agents/developer/ROLE.md"),
            reviewer_role=Path("agents/optimization-reviewer/ROLE.md"),
            output_schema_path=Path("schemas/orchestrator-result.schema.json"),
            developer_output_schema_path=Path("schemas/developer-result.schema.json"),
            reviewer_output_schema_path=Path("schemas/optimization-review-result.schema.json"),
            executor=executor,
            work_item_reader=WorkItemReader(github),
            worktree_factory=lambda repository, **kwargs: FakeWorktree(kwargs["branch"]),
            git_command_runner=git_runner,
        )

        outcome = orchestrator.run_once()

        self.assertEqual(outcome.status, "processed")
        self.assertEqual(len(executor.requests), 1)


if __name__ == "__main__":
    unittest.main()
