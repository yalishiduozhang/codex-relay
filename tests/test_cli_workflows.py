from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from codex_relay import cli

from tests.helpers import create_codex_home, create_official_codex_home


class CliWorkflowTests(unittest.TestCase):
    def run_cli(self, *args: str) -> tuple[int, str, str]:
        stdout = io.StringIO()
        stderr = io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = cli.main(list(args))
        return code, stdout.getvalue(), stderr.getvalue()

    def test_add_use_and_current_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = create_codex_home(Path(tmp_dir) / ".codex")

            code, stdout, stderr = self.run_cli(
                "--codex-home",
                str(codex_home),
                "add",
                "relay-a",
                "--url",
                "https://relay.example.com",
                "--key",
                "TEST_KEY_RELAY_A",
                "--note",
                "primary relay",
            )
            self.assertEqual(code, 0, stderr or stdout)
            self.assertIn("Saved profile 'relay-a'.", stdout)

            store = json.loads((codex_home / "relay_profiles.json").read_text(encoding="utf-8"))
            names = {profile["name"] for profile in store["profiles"]}
            self.assertIn("relay-a", names)
            self.assertEqual(len(store["profiles"]), 2)

            code, stdout, stderr = self.run_cli(
                "--codex-home",
                str(codex_home),
                "use",
                "relay-a",
            )
            self.assertEqual(code, 0, stderr or stdout)
            self.assertIn("Activated profile 'relay-a'.", stdout)

            config_text = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertIn('base_url = "https://relay.example.com"', config_text)

            auth_payload = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
            self.assertEqual(auth_payload["OPENAI_API_KEY"], "TEST_KEY_RELAY_A")

            backup_dir = codex_home / "relay_backups"
            self.assertTrue((backup_dir / "config.toml.last.bak").exists())
            self.assertTrue((backup_dir / "auth.json.last.bak").exists())
            backup_text = (backup_dir / "config.toml.last.bak").read_text(encoding="utf-8")
            self.assertIn('base_url = "https://origin.example.com"', backup_text)

            code, stdout, stderr = self.run_cli(
                "--codex-home",
                str(codex_home),
                "current",
            )
            self.assertEqual(code, 0, stderr or stdout)
            self.assertIn("Base URL : https://relay.example.com", stdout)
            self.assertIn("Profile  : relay-a", stdout)

    def test_edit_updates_live_config_for_active_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = create_codex_home(Path(tmp_dir) / ".codex")

            code, stdout, stderr = self.run_cli(
                "--codex-home",
                str(codex_home),
                "add",
                "relay-a",
                "--url",
                "https://relay.example.com",
                "--key",
                "TEST_KEY_RELAY_A",
                "--activate",
            )
            self.assertEqual(code, 0, stderr or stdout)

            code, stdout, stderr = self.run_cli(
                "--codex-home",
                str(codex_home),
                "edit",
                "relay-a",
                "--url",
                "https://edited.example.com",
                "--key",
                "UPDATED_TEST_KEY",
                "--note",
                "edited relay",
            )
            self.assertEqual(code, 0, stderr or stdout)
            self.assertIn("Updated the live Codex config because this profile was active.", stdout)

            config_text = (codex_home / "config.toml").read_text(encoding="utf-8")
            self.assertIn('base_url = "https://edited.example.com"', config_text)
            auth_payload = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
            self.assertEqual(auth_payload["OPENAI_API_KEY"], "UPDATED_TEST_KEY")

            store = json.loads((codex_home / "relay_profiles.json").read_text(encoding="utf-8"))
            relay_a = next(profile for profile in store["profiles"] if profile["name"] == "relay-a")
            self.assertEqual(relay_a["note"], "edited relay")

    def test_save_current_and_use_official_profile(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = create_official_codex_home(Path(tmp_dir) / ".codex", account_id="acct-official-1234")

            code, stdout, stderr = self.run_cli(
                "--codex-home",
                str(codex_home),
                "save-current",
                "official-main",
                "--note",
                "official snapshot",
            )
            self.assertEqual(code, 0, stderr or stdout)
            self.assertIn("Saved the current official live config as profile 'official-main'.", stdout)

            store = json.loads((codex_home / "relay_profiles.json").read_text(encoding="utf-8"))
            official = next(profile for profile in store["profiles"] if profile["name"] == "official-main")
            self.assertEqual(official["type"], "official")
            self.assertEqual(official["auth_mode"], "chatgpt")
            self.assertIn("auth_snapshot", official)

            code, stdout, stderr = self.run_cli(
                "--codex-home",
                str(codex_home),
                "use",
                "official-main",
            )
            self.assertEqual(code, 0, stderr or stdout)
            self.assertIn("Activated profile 'official-main'.", stdout)
            self.assertIn("Type     -> official", stdout)
            self.assertIn("Auth     -> chatgpt", stdout)

            auth_payload = json.loads((codex_home / "auth.json").read_text(encoding="utf-8"))
            self.assertEqual(auth_payload["auth_mode"], "chatgpt")
            self.assertEqual(auth_payload["tokens"]["account_id"], "acct-official-1234")

    def test_import_official_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = create_codex_home(Path(tmp_dir) / ".codex")
            official_source = create_official_codex_home(Path(tmp_dir) / ".codex-official", account_id="acct-import-9999")

            code, stdout, stderr = self.run_cli(
                "--codex-home",
                str(codex_home),
                "import",
                str(official_source),
                "--name",
                "official-imported",
            )
            self.assertEqual(code, 0, stderr or stdout)
            self.assertIn("Imported official profile 'official-imported'", stdout)

            store = json.loads((codex_home / "relay_profiles.json").read_text(encoding="utf-8"))
            imported = next(profile for profile in store["profiles"] if profile["name"] == "official-imported")
            self.assertEqual(imported["type"], "official")
            self.assertEqual(imported["official_id"], "account:acct-import-9999")


if __name__ == "__main__":
    unittest.main()
