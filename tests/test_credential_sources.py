import os
import tempfile
import unittest
from pathlib import Path

from agent_army.credentials import (
    build_secret_loader,
    environment_variable_name,
    load_env_file,
    make_env_secret_loader,
)

REF = "agent-army/project-owner/github-app-private-key"
NAME = "AGENT_ARMY_PROJECT_OWNER_GITHUB_APP_PRIVATE_KEY"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIfake\n-----END RSA PRIVATE KEY-----\n"


class EnvNameTests(unittest.TestCase):
    def test_ref_maps_predictably_to_a_variable_name(self) -> None:
        self.assertEqual(environment_variable_name(REF), NAME)


class EnvFileTests(unittest.TestCase):
    def test_parses_comments_blanks_and_quotes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(
                "# a comment\n\n"
                f'{NAME}="quoted-value"\n'
                "OTHER=plain\n",
                encoding="utf-8",
            )
            values = load_env_file(path)
        self.assertEqual(values[NAME], "quoted-value")
        self.assertEqual(values["OTHER"], "plain")

    def test_a_missing_file_is_not_an_error(self) -> None:
        self.assertEqual(load_env_file(Path("/nonexistent/.env")), {})


class EnvSecretLoaderTests(unittest.TestCase):
    def test_reads_inline_key_material(self) -> None:
        loader = make_env_secret_loader(Path("/nonexistent/.env"), {NAME: PEM})
        self.assertEqual(loader(REF), PEM)

    def test_restores_newlines_in_a_single_line_pem(self) -> None:
        squeezed = PEM.replace("\n", "\\n")
        loader = make_env_secret_loader(Path("/nonexistent/.env"), {NAME: squeezed})
        self.assertEqual(loader(REF), PEM)

    def test_reads_a_key_file_when_the_value_is_a_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            key_path = Path(directory) / "app.pem"
            key_path.write_text(PEM, encoding="utf-8")
            loader = make_env_secret_loader(
                Path("/nonexistent/.env"), {NAME: str(key_path)}
            )
            self.assertEqual(loader(REF), PEM)

    def test_falls_back_to_the_env_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(f"{NAME}={PEM.replace(chr(10), chr(92) + 'n')}\n", encoding="utf-8")
            loader = make_env_secret_loader(path, {})
            self.assertEqual(loader(REF), PEM)

    def test_a_missing_secret_names_what_to_set(self) -> None:
        loader = make_env_secret_loader(Path("/nonexistent/.env"), {})
        with self.assertRaises(RuntimeError) as raised:
            loader(REF)
        self.assertIn(NAME, str(raised.exception))

    def test_env_file_values_do_not_leak_into_the_process_environment(self) -> None:
        # Agent subprocesses inherit os.environ; a credential loaded here must
        # never end up there.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text(f"{NAME}=secret-value\n", encoding="utf-8")
            make_env_secret_loader(path, {})(REF)
        self.assertIsNone(os.environ.get(NAME))


class BuildSecretLoaderTests(unittest.TestCase):
    def test_rejects_an_unknown_source(self) -> None:
        with self.assertRaises(ValueError):
            build_secret_loader("vault")

    def test_defaults_to_pass(self) -> None:
        from agent_army.credentials import read_from_pass

        self.assertIs(build_secret_loader(), read_from_pass)


if __name__ == "__main__":
    unittest.main()
