from __future__ import annotations

import re
import tempfile
import unittest
from pathlib import Path

from codex_relay import cli

from tests.helpers import create_codex_home, create_official_codex_home


class TuiAndHygieneTests(unittest.TestCase):
    def test_visible_profiles_respects_filter(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = create_codex_home(Path(tmp_dir) / ".codex")
            app = cli.RelayTUI(cli.build_paths(codex_home))
            app.store = {
                "version": cli.STORE_VERSION,
                "profiles": [
                    cli.make_profile("alpha", "https://alpha.example.com", "KEY_ALPHA", "primary"),
                    cli.make_profile("beta", "https://beta.example.com", "KEY_BETA", "backup relay"),
                    cli.make_profile("gamma", "https://gamma.example.com", "KEY_GAMMA", "archive"),
                ],
            }

            app.filter_text = "backup"
            visible = app.visible_profiles()
            self.assertEqual([profile["name"] for _, profile in visible], ["beta"])

            app.selected = 999
            entry = app.current_profile_entry()
            self.assertIsNotNone(entry)
            assert entry is not None
            self.assertEqual(entry[1]["name"], "beta")
            self.assertEqual(app.selected, entry[0])

    def test_type_filter_and_marking_support_official_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = create_codex_home(Path(tmp_dir) / ".codex")
            official_home = create_official_codex_home(Path(tmp_dir) / ".codex-official", account_id="acct-ui-1001")
            official_state = cli.read_live_state(cli.build_paths(official_home))
            official_profile = cli.build_profile_from_state("official-ui", official_state, "official")

            app = cli.RelayTUI(cli.build_paths(codex_home))
            store = {
                "version": cli.STORE_VERSION,
                "profiles": [
                    cli.make_profile("relay-ui", "https://relay.example.com", "KEY_ALPHA", "primary"),
                    official_profile,
                ],
            }
            cli.write_store(cli.build_paths(codex_home), store)
            app.refresh()

            app.set_type_filter("official")
            visible = app.visible_profiles()
            self.assertEqual([profile["name"] for _, profile in visible], ["official-ui"])

            app.selected = visible[0][0]
            app.toggle_mark_current()
            self.assertIn("official-ui", app.marked_profiles)
            marked_entries = app.marked_target_entries()
            self.assertEqual([profile["name"] for _, profile in marked_entries], ["official-ui"])

            app.clear_marked()
            self.assertFalse(app.marked_profiles)

    def test_repository_is_sanitized_for_public_upload(self) -> None:
        repo_root = Path(__file__).resolve().parents[1]
        forbidden_literals = [
            "ice." + "v.ua",
            "sub.jia4u" + ".de",
            "/home/" + "yalishiduozhang",
            "help." + "4188everyone@gmail.com",
            "codex-" + "2026-03-31-093940",
            "fae2d9a8af7ff0f70ed3e9331e9c500b75a7cf8d1898aa74deb0" + "e238b791a662",
            "48ee925b8b8c710ea194605dfb948299de51e87ce79444600a45" + "f3f70762cda4",
        ]
        api_key_pattern = re.compile(r"sk-[A-Za-z0-9]{24,}")

        scanned_files = 0
        for path in repo_root.rglob("*"):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts or ".git" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            scanned_files += 1
            for literal in forbidden_literals:
                self.assertNotIn(literal, text, f"Found forbidden literal {literal!r} in {path}")
            self.assertIsNone(api_key_pattern.search(text), f"Found real-looking API key in {path}")

        self.assertGreater(scanned_files, 0)


if __name__ == "__main__":
    unittest.main()
