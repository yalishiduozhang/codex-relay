from __future__ import annotations

import json
import textwrap
from pathlib import Path


def create_codex_home(
    codex_home: Path,
    *,
    base_url: str = "https://origin.example.com",
    api_key: str = "ORIGINAL_TEST_KEY",
    model: str = "gpt-5.4",
) -> Path:
    codex_home.mkdir(parents=True, exist_ok=True)
    config_text = textwrap.dedent(
        f"""\
        model = "{model}"
        review_model = "{model}"
        model_provider = "OpenAI"

        [model_providers.OpenAI]
        name = "OpenAI"
        base_url = "{base_url}"
        wire_api = "responses"
        requires_openai_auth = true
        """
    )
    (codex_home / "config.toml").write_text(config_text, encoding="utf-8")
    (codex_home / "auth.json").write_text(
        json.dumps({"OPENAI_API_KEY": api_key}, indent=2) + "\n",
        encoding="utf-8",
    )
    return codex_home
