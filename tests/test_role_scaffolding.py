import unittest
from pathlib import Path

import yaml

from agent_army.codex_executor import role_reference_paths


ROOT = Path(__file__).parents[1]
DOMAIN_REFERENCE = Path("references/mattpocock-skills/domain-modeling/SKILL.md")
GRILLING_REFERENCE = Path("references/mattpocock-skills/grilling/SKILL.md")
UPSTREAM_REFERENCE = Path("references/mattpocock-skills/UPSTREAM.md")


class NewRoleScaffoldingTests(unittest.TestCase):
    def test_new_roles_are_credential_ready_and_have_no_secret_material(self) -> None:
        expected = {
            "developer": (
                4579193,
                "Iv23liI49S3fuutyV3Nu",
                "agent-army/developer/github-app-private-key",
            ),
            "optimization-reviewer": (
                4287312,
                "Iv23lizPwnN2k9WRpmvp",
                "agent-army/optimization-reviewer/github-app-private-key",
            ),
        }

        for role_name, (app_id, client_id, secret_ref) in expected.items():
            with self.subTest(role=role_name):
                role_directory = ROOT / "agents" / role_name
                config = yaml.safe_load(
                    (role_directory / "agent-config.yaml").read_text(encoding="utf-8")
                )
                self.assertEqual(config["agent"]["name"], role_name)
                self.assertEqual(config["github_app"]["app_id"], app_id)
                self.assertEqual(config["github_app"]["client_id"], client_id)
                self.assertEqual(config["github_app"]["private_key_secret_ref"], secret_ref)
                self.assertTrue((role_directory / "ROLE.md").is_file())
                self.assertTrue((role_directory / "README.md").is_file())
                if role_name == "optimization-reviewer":
                    self.assertIn(
                        "requirements_challenge",
                        config["agent"]["invocation_modes"],
                    )
                self.assertTrue((role_directory / DOMAIN_REFERENCE).is_file())
                self.assertTrue((role_directory / UPSTREAM_REFERENCE).is_file())
                role_text = (role_directory / "ROLE.md").read_text(encoding="utf-8")
                self.assertIn("Domain Modeling reference", role_text)
                if role_name == "developer":
                    self.assertNotIn("Grilling", role_text)
                else:
                    self.assertIn("requirements_challenge", role_text)
                    self.assertTrue((role_directory / GRILLING_REFERENCE).is_file())
                    self.assertIn("grilling/SKILL.md", (role_directory / UPSTREAM_REFERENCE).read_text(encoding="utf-8"))

    def test_selected_domain_reference_is_injected_for_both_new_roles(self) -> None:
        source = ROOT / "agents/project-owner/references/mattpocock-skills/domain-modeling/SKILL.md"
        for role_name in ("developer", "optimization-reviewer"):
            with self.subTest(role=role_name):
                role_path = ROOT / "agents" / role_name / "ROLE.md"
                selected = role_reference_paths(role_path)
                self.assertEqual(selected, (role_path.parent / DOMAIN_REFERENCE,))
                self.assertEqual(selected[0].read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))
                self.assertNotIn("grilling", " ".join(str(path) for path in selected).lower())

    def test_grilling_reference_is_injected_only_for_requirements_challenge(self) -> None:
        role_path = ROOT / "agents/optimization-reviewer/ROLE.md"
        source = ROOT / "agents/documentation/references/mattpocock-skills/grilling/SKILL.md"

        selected = role_reference_paths(role_path, invocation_mode="requirements_challenge")

        self.assertEqual(selected[0], role_path.parent / DOMAIN_REFERENCE)
        self.assertEqual(selected[1], role_path.parent / GRILLING_REFERENCE)
        self.assertEqual(
            selected[1].read_text(encoding="utf-8").rstrip(),
            source.read_text(encoding="utf-8").rstrip(),
        )


if __name__ == "__main__":
    unittest.main()
