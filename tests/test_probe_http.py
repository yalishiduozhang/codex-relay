from __future__ import annotations

import json
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest import mock

from codex_relay import cli

from tests.helpers import create_codex_home


class FakeHTTPResponse:
    def __init__(self, body: str, *, status: int = 200, content_type: str = "text/event-stream") -> None:
        self._body = body.encode("utf-8")
        self.status = status
        self.headers = {"Content-Type": content_type}

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "FakeHTTPResponse":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False


class HttpProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reply_text = "Hello from mock relay."
        self.sse_response = textwrap.dedent(
            f"""\
            event: response.output_text.delta
            data: {json.dumps({"type": "response.output_text.delta", "delta": self.reply_text})}

            event: response.completed
            data: {json.dumps({"type": "response.completed", "response": {"output_text": self.reply_text}})}

            """
        )

    def fake_urlopen_factory(self, captured: dict[str, object]):
        def fake_urlopen(request, timeout=0):  # type: ignore[no-untyped-def]
            captured["url"] = request.full_url
            captured["timeout"] = timeout
            captured["headers"] = dict(request.header_items())
            captured["payload"] = json.loads(request.data.decode("utf-8"))
            return FakeHTTPResponse(self.sse_response)

        return fake_urlopen

    def test_probe_via_http_extracts_reply_from_sse(self) -> None:
        captured: dict[str, object] = {}
        profile = {
            "name": "mock",
            "base_url": "https://relay.example.com",
            "api_key": "TEST_PROBE_KEY",
        }

        with mock.patch("codex_relay.cli.urllib.request.urlopen", side_effect=self.fake_urlopen_factory(captured)):
            result = cli.probe_via_http(
                profile,
                model="gpt-5.4",
                message="Hello, who are you?",
                timeout=5.0,
                expect=None,
            )

        self.assertTrue(result["ok"])
        self.assertEqual(result["status_code"], 200)
        self.assertEqual(result["reply"], self.reply_text)
        self.assertEqual(captured["url"], "https://relay.example.com/responses")

        headers = captured["headers"]
        assert isinstance(headers, dict)
        auth_value = next(value for key, value in headers.items() if key.lower() == "authorization")
        self.assertEqual(auth_value, "Bearer TEST_PROBE_KEY")

        payload = captured["payload"]
        assert isinstance(payload, dict)
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["model"], "gpt-5.4")
        self.assertEqual(payload["input"][0]["content"][0]["text"], "Hello, who are you?")

    def test_execute_probe_updates_store_metadata(self) -> None:
        captured: dict[str, object] = {}

        with tempfile.TemporaryDirectory() as tmp_dir:
            codex_home = create_codex_home(Path(tmp_dir) / ".codex")
            paths = cli.build_paths(codex_home)

            store = {
                "version": 1,
                "profiles": [
                    cli.make_profile(
                        "mock-relay",
                        "https://relay.example.com",
                        "TEST_PROBE_KEY",
                        "local mock relay",
                    )
                ],
            }
            cli.write_store(paths, store)

            with mock.patch("codex_relay.cli.urllib.request.urlopen", side_effect=self.fake_urlopen_factory(captured)):
                results, overall_ok = cli.execute_probe(
                    paths,
                    targets=list(enumerate(store["profiles"])),
                    via="http",
                    message="Ping",
                    expect="Hello",
                    model="gpt-5.4",
                    timeout=5.0,
                    workers=1,
                )

            self.assertTrue(overall_ok)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][1]["http"]["reply"], self.reply_text)

            saved_store, _ = cli.load_store(paths)
            last_probe = saved_store["profiles"][0]["last_probe"]
            self.assertEqual(last_probe["methods"]["http"]["reply"], self.reply_text)
            self.assertTrue(last_probe["methods"]["http"]["ok"])


if __name__ == "__main__":
    unittest.main()
