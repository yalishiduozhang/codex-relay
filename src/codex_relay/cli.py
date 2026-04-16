#!/usr/bin/env python3

from __future__ import annotations

import argparse
import concurrent.futures
import copy
import datetime as dt
import fcntl
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import tomllib


DEFAULT_MESSAGE = "hi"
DEFAULT_HTTP_TIMEOUT = 20.0
DEFAULT_CODEX_TIMEOUT = 90.0
DEFAULT_HTTP_WORKERS = 8
DEFAULT_CODEX_WORKERS = 3
DEFAULT_HTTP_RETRIES = 3
DEFAULT_REPLY_DISPLAY_LIMIT = 300
PROBE_TEXT_STORAGE_LIMIT = 4000
RIGHT_PANEL_TEXT_LIMIT = 220
STORE_VERSION = 2
VERSION = "0.2.0"
CODEX_PROBE_INSTRUCTIONS = (
    "You are Codex, based on GPT-5. You are running as a coding agent in the Codex CLI "
    "on a user's computer."
)


@dataclass
class Paths:
    codex_home: Path
    config_path: Path
    auth_path: Path
    profiles_path: Path
    backup_dir: Path
    lock_path: Path


class RelayError(Exception):
    pass


def now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).astimezone().isoformat(timespec="seconds")


def normalize_url(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise RelayError("URL cannot be empty.")
    parsed = urllib.parse.urlparse(value)
    if not parsed.scheme or not parsed.netloc:
        raise RelayError(f"Invalid URL: {raw}")
    path = parsed.path.rstrip("/")
    normalized = urllib.parse.urlunparse(
        (
            parsed.scheme,
            parsed.netloc,
            path,
            parsed.params,
            parsed.query,
            parsed.fragment,
        )
    )
    return normalized


def toml_quote(value: str) -> str:
    return json.dumps(value)


def ensure_dir(path: Path, mode: int = 0o700) -> None:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def path_is_under_tmp(path: Path) -> bool:
    try:
        resolved = path.expanduser().resolve()
    except FileNotFoundError:
        resolved = path.expanduser()
    return str(resolved) == "/tmp" or str(resolved).startswith("/tmp/")


def atomic_write_text(path: Path, content: str, mode: int | None = None) -> None:
    ensure_dir(path.parent)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=path.parent, delete=False
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temp_name = handle.name
    if mode is None:
        try:
            mode = path.stat().st_mode & 0o777
        except FileNotFoundError:
            mode = 0o600
    os.chmod(temp_name, mode)
    os.replace(temp_name, path)


def atomic_write_json(path: Path, payload: dict[str, Any], mode: int = 0o600) -> None:
    atomic_write_text(
        path,
        json.dumps(payload, indent=2, sort_keys=False, ensure_ascii=False) + "\n",
        mode=mode,
    )


@contextmanager
def store_lock(paths: Paths):
    ensure_dir(paths.codex_home)
    with open(paths.lock_path, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def build_paths(codex_home: str | Path) -> Paths:
    root = Path(codex_home).expanduser().resolve()
    return Paths(
        codex_home=root,
        config_path=root / "config.toml",
        auth_path=root / "auth.json",
        profiles_path=root / "relay_profiles.json",
        backup_dir=root / "relay_backups",
        lock_path=root / "relay_profiles.lock",
    )


def read_config(paths: Paths) -> tuple[str, dict[str, Any]]:
    if not paths.config_path.exists():
        raise RelayError(f"Missing Codex config: {paths.config_path}")
    text = paths.config_path.read_text(encoding="utf-8")
    try:
        parsed = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise RelayError(f"Failed to parse {paths.config_path}: {exc}") from exc
    return text, parsed


def read_auth(paths: Paths) -> dict[str, Any]:
    if not paths.auth_path.exists():
        return {}
    try:
        return json.loads(paths.auth_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RelayError(f"Failed to parse {paths.auth_path}: {exc}") from exc


def current_provider(parsed_config: dict[str, Any]) -> str:
    provider = parsed_config.get("model_provider")
    if isinstance(provider, str) and provider.strip():
        return provider.strip()
    return "OpenAI"


def config_snapshot_from_parsed(parsed_config: dict[str, Any]) -> dict[str, Any]:
    provider_name = current_provider(parsed_config)
    providers = parsed_config.get("model_providers", {})
    provider_cfg = None
    if isinstance(providers, dict):
        maybe_provider = providers.get(provider_name)
        if isinstance(maybe_provider, dict):
            provider_cfg = copy.deepcopy(maybe_provider)
    model_provider = parsed_config.get("model_provider")
    return {
        "model_provider": model_provider if isinstance(model_provider, str) and model_provider.strip() else None,
        "provider_name": provider_name,
        "provider_section": provider_cfg,
    }


def official_identifier_from_auth(auth_payload: dict[str, Any]) -> str | None:
    tokens = auth_payload.get("tokens")
    if isinstance(tokens, dict):
        account_id = tokens.get("account_id")
        if isinstance(account_id, str) and account_id.strip():
            return f"account:{account_id.strip()}"
        refresh_token = tokens.get("refresh_token")
        if isinstance(refresh_token, str) and refresh_token.strip():
            return f"refresh:{refresh_token.strip()}"
        access_token = tokens.get("access_token")
        if isinstance(access_token, str) and access_token.strip():
            return f"access:{access_token.strip()}"
        id_token = tokens.get("id_token")
        if isinstance(id_token, str) and id_token.strip():
            return f"id:{id_token.strip()}"
    auth_mode = auth_payload.get("auth_mode")
    if isinstance(auth_mode, str) and auth_mode.strip():
        return f"mode:{auth_mode.strip()}"
    return None


def normalize_config_snapshot(snapshot: Any) -> dict[str, Any]:
    if not isinstance(snapshot, dict):
        return {
            "model_provider": None,
            "provider_name": "OpenAI",
            "provider_section": None,
        }
    model_provider = snapshot.get("model_provider")
    provider_name = snapshot.get("provider_name")
    provider_section = snapshot.get("provider_section")
    normalized_section: dict[str, Any] | None = None
    if isinstance(provider_section, dict):
        normalized_section = {}
        for key, value in provider_section.items():
            if not isinstance(key, str):
                continue
            if isinstance(value, (str, bool, int, float)):
                normalized_section[key] = value
    return {
        "model_provider": model_provider if isinstance(model_provider, str) and model_provider.strip() else None,
        "provider_name": provider_name if isinstance(provider_name, str) and provider_name.strip() else "OpenAI",
        "provider_section": normalized_section,
    }


def build_live_state(
    config_text: str, parsed_config: dict[str, Any], auth_payload: dict[str, Any]
) -> dict[str, Any]:
    provider_name = current_provider(parsed_config)
    providers = parsed_config.get("model_providers", {})
    provider_cfg: dict[str, Any] = {}
    if isinstance(providers, dict):
        maybe_provider = providers.get(provider_name)
        if isinstance(maybe_provider, dict):
            provider_cfg = maybe_provider
    base_url = provider_cfg.get("base_url")
    normalized_base_url = (
        normalize_url(base_url) if isinstance(base_url, str) and base_url.strip() else None
    )
    api_key = auth_payload.get("OPENAI_API_KEY")
    normalized_api_key = api_key.strip() if isinstance(api_key, str) and api_key.strip() else None
    auth_mode = auth_payload.get("auth_mode")
    normalized_auth_mode = auth_mode.strip() if isinstance(auth_mode, str) and auth_mode.strip() else None
    official_id = official_identifier_from_auth(auth_payload)
    state_type = "unknown"
    if normalized_base_url and normalized_api_key:
        state_type = "relay"
    elif official_id or normalized_auth_mode:
        state_type = "official"
    return {
        "type": state_type,
        "provider_name": provider_name,
        "base_url": normalized_base_url,
        "api_key": normalized_api_key,
        "model": parsed_config.get("model") if isinstance(parsed_config.get("model"), str) else None,
        "wire_api": provider_cfg.get("wire_api") if isinstance(provider_cfg.get("wire_api"), str) else None,
        "auth_mode": normalized_auth_mode,
        "official_id": official_id,
        "auth_snapshot": copy.deepcopy(auth_payload),
        "config_snapshot": config_snapshot_from_parsed(parsed_config),
        "config_text": config_text,
        "parsed_config": parsed_config,
    }


def read_live_state(paths: Paths) -> dict[str, Any]:
    config_text, parsed = read_config(paths)
    auth = read_auth(paths)
    return build_live_state(config_text, parsed, auth)


def make_profile(name: str, url: str, api_key: str, note: str = "") -> dict[str, Any]:
    timestamp = now_iso()
    return {
        "type": "relay",
        "name": name,
        "base_url": normalize_url(url),
        "api_key": api_key.strip(),
        "note": note.strip(),
        "created_at": timestamp,
        "updated_at": timestamp,
        "last_used_at": None,
        "last_probe": None,
    }


def make_official_profile(
    name: str,
    auth_payload: dict[str, Any],
    config_snapshot: dict[str, Any],
    note: str = "",
    source_path: str | None = None,
) -> dict[str, Any]:
    official_id = official_identifier_from_auth(auth_payload)
    if not official_id:
        raise RelayError("Official profile import requires auth_mode or tokens in auth.json.")
    timestamp = now_iso()
    profile = {
        "type": "official",
        "name": name,
        "note": note.strip(),
        "created_at": timestamp,
        "updated_at": timestamp,
        "last_used_at": None,
        "last_probe": None,
        "auth_snapshot": copy.deepcopy(auth_payload),
        "auth_mode": auth_payload.get("auth_mode") if isinstance(auth_payload.get("auth_mode"), str) else None,
        "official_id": official_id,
        "config_snapshot": normalize_config_snapshot(config_snapshot),
    }
    if source_path:
        profile["source_path"] = str(Path(source_path).expanduser())
    return profile


def build_profile_from_state(
    name: str,
    live_state: dict[str, Any],
    note: str = "",
    source_path: str | None = None,
) -> dict[str, Any]:
    state_type = live_state.get("type")
    if state_type == "relay":
        base_url = live_state.get("base_url")
        api_key = live_state.get("api_key")
        if not isinstance(base_url, str) or not isinstance(api_key, str):
            raise RelayError("Relay profile requires both base_url and OPENAI_API_KEY.")
        profile = make_profile(name, base_url, api_key, note)
    elif state_type == "official":
        auth_payload = live_state.get("auth_snapshot")
        if not isinstance(auth_payload, dict):
            raise RelayError("Official profile requires a valid auth.json snapshot.")
        profile = make_official_profile(
            name,
            auth_payload,
            normalize_config_snapshot(live_state.get("config_snapshot")),
            note,
            source_path=source_path,
        )
    else:
        raise RelayError("Current Codex config is neither a relay profile nor an official subscription.")
    if source_path:
        profile["source_path"] = str(Path(source_path).expanduser())
    return profile


def profile_type(profile: dict[str, Any]) -> str:
    raw = profile.get("type")
    if raw in {"relay", "official"}:
        return str(raw)
    if isinstance(profile.get("auth_snapshot"), dict):
        return "official"
    return "relay"


def profile_base_url(profile: dict[str, Any]) -> str | None:
    value = profile.get("base_url")
    if isinstance(value, str) and value.strip():
        return normalize_url(value)
    return None


def profile_api_key(profile: dict[str, Any]) -> str | None:
    value = profile.get("api_key")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def profile_auth_mode(profile: dict[str, Any]) -> str | None:
    value = profile.get("auth_mode")
    if isinstance(value, str) and value.strip():
        return value.strip()
    auth_snapshot = profile.get("auth_snapshot")
    if isinstance(auth_snapshot, dict):
        mode = auth_snapshot.get("auth_mode")
        if isinstance(mode, str) and mode.strip():
            return mode.strip()
    return None


def profile_official_id(profile: dict[str, Any]) -> str | None:
    value = profile.get("official_id")
    if isinstance(value, str) and value.strip():
        return value.strip()
    auth_snapshot = profile.get("auth_snapshot")
    if isinstance(auth_snapshot, dict):
        return official_identifier_from_auth(auth_snapshot)
    return None


def profile_supports_http(profile: dict[str, Any]) -> bool:
    return profile_type(profile) == "relay" and bool(profile_base_url(profile) and profile_api_key(profile))


def profile_signature(profile: dict[str, Any]) -> tuple[Any, ...]:
    kind = profile_type(profile)
    if kind == "official":
        return ("official", profile_official_id(profile))
    return ("relay", profile_base_url(profile) or "", profile_api_key(profile) or "")


def live_signature(live_state: dict[str, Any]) -> tuple[Any, ...]:
    if live_state.get("type") == "official":
        return ("official", live_state.get("official_id"))
    return ("relay", live_state.get("base_url") or "", live_state.get("api_key") or "")


def profile_exists(store: dict[str, Any], name: str) -> bool:
    return any(profile.get("name") == name for profile in store["profiles"])


def slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-").lower()


def suggest_name(store: dict[str, Any], url: str) -> str:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc or "profile"
    slug = slugify(host) or "profile"
    candidate = slug
    counter = 2
    while profile_exists(store, candidate):
        candidate = f"{slug}-{counter}"
        counter += 1
    return candidate


def suggest_official_name(
    store: dict[str, Any], official_id: str | None, source_path: str | Path | None = None
) -> str:
    base = None
    if source_path is not None:
        source_name = Path(source_path).expanduser().name
        cleaned = slugify(source_name)
        if cleaned and cleaned != "codex":
            base = cleaned
    if not base and official_id:
        cleaned_id = slugify(official_id)
        if cleaned_id:
            suffix = cleaned_id[-8:] if len(cleaned_id) > 8 else cleaned_id
            base = f"official-{suffix}"
    if not base:
        base = "official"
    candidate = base
    counter = 2
    while profile_exists(store, candidate):
        candidate = f"{base}-{counter}"
        counter += 1
    return candidate


def suggest_name_from_state(
    store: dict[str, Any], live_state: dict[str, Any], source_path: str | Path | None = None
) -> str:
    if live_state.get("type") == "relay" and isinstance(live_state.get("base_url"), str):
        return suggest_name(store, live_state["base_url"])
    return suggest_official_name(store, live_state.get("official_id"), source_path)


def normalize_profile_entry(profile: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(profile, dict):
        raise RelayError("Profile store contains a non-object entry.")
    normalized = copy.deepcopy(profile)
    changed = False
    kind = profile_type(normalized)
    if normalized.get("type") != kind:
        normalized["type"] = kind
        changed = True
    note = normalized.get("note")
    if not isinstance(note, str):
        normalized["note"] = "" if note is None else str(note)
        changed = True

    if kind == "relay":
        base_url = normalized.get("base_url")
        api_key = normalized.get("api_key")
        if not isinstance(base_url, str) or not base_url.strip():
            raise RelayError(f"Relay profile '{normalized.get('name')}' is missing base_url.")
        normalized_base_url = normalize_url(base_url)
        if normalized_base_url != base_url:
            normalized["base_url"] = normalized_base_url
            changed = True
        if not isinstance(api_key, str) or not api_key.strip():
            raise RelayError(f"Relay profile '{normalized.get('name')}' is missing api_key.")
        stripped_key = api_key.strip()
        if stripped_key != api_key:
            normalized["api_key"] = stripped_key
            changed = True
    else:
        auth_snapshot = normalized.get("auth_snapshot")
        if not isinstance(auth_snapshot, dict):
            raise RelayError(f"Official profile '{normalized.get('name')}' is missing auth_snapshot.")
        official_id = official_identifier_from_auth(auth_snapshot)
        if not official_id:
            raise RelayError(
                f"Official profile '{normalized.get('name')}' is missing account_id/refresh_token."
            )
        if normalized.get("official_id") != official_id:
            normalized["official_id"] = official_id
            changed = True
        auth_mode = auth_snapshot.get("auth_mode")
        normalized_auth_mode = auth_mode.strip() if isinstance(auth_mode, str) and auth_mode.strip() else None
        if normalized.get("auth_mode") != normalized_auth_mode:
            normalized["auth_mode"] = normalized_auth_mode
            changed = True
        config_snapshot = normalize_config_snapshot(normalized.get("config_snapshot"))
        if normalized.get("config_snapshot") != config_snapshot:
            normalized["config_snapshot"] = config_snapshot
            changed = True

    return normalized, changed


def normalize_store(store: Any) -> tuple[dict[str, Any], bool]:
    if not isinstance(store, dict):
        raise RelayError("Unsupported profile store format.")
    version = store.get("version")
    if version not in {1, STORE_VERSION} or not isinstance(store.get("profiles"), list):
        raise RelayError("Unsupported profile store format.")
    changed = version != STORE_VERSION
    normalized_profiles: list[dict[str, Any]] = []
    for profile in store["profiles"]:
        normalized_profile, profile_changed = normalize_profile_entry(profile)
        normalized_profiles.append(normalized_profile)
        changed = changed or profile_changed
    normalized_store = {"version": STORE_VERSION, "profiles": normalized_profiles}
    return normalized_store, changed


def load_store_unlocked(paths: Paths) -> tuple[dict[str, Any], str | None]:
    if paths.profiles_path.exists():
        try:
            store = json.loads(paths.profiles_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RelayError(f"Failed to parse {paths.profiles_path}: {exc}") from exc
        store, changed = normalize_store(store)
        if changed:
            write_store(paths, store)
        return store, None

    ensure_dir(paths.codex_home)
    store = {"version": STORE_VERSION, "profiles": []}
    created_message = None
    live_state = read_live_state(paths)
    if live_state.get("type") in {"relay", "official"}:
        imported_name = suggest_name_from_state(store, live_state, paths.codex_home)
        imported_profile = build_profile_from_state(
            imported_name,
            live_state,
            "Auto-imported from the current Codex config on first run.",
            source_path=str(paths.codex_home),
        )
        imported_profile["last_used_at"] = now_iso()
        store["profiles"].append(imported_profile)
        created_message = (
            f"Imported the current live Codex profile as '{imported_name}'."
        )
    write_store(paths, store)
    return store, created_message


def load_store(paths: Paths) -> tuple[dict[str, Any], str | None]:
    with store_lock(paths):
        return load_store_unlocked(paths)


def write_store(paths: Paths, store: dict[str, Any]) -> None:
    atomic_write_json(paths.profiles_path, store, mode=0o600)


def mask_key(key: str | None) -> str:
    if not key:
        return "(missing)"
    if len(key) <= 8:
        return key[:2] + "***"
    return f"{key[:6]}...{key[-4:]}"


def toml_value_literal(value: Any) -> str:
    if isinstance(value, str):
        return toml_quote(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    raise RelayError(f"Unsupported TOML value type: {type(value).__name__}")


def update_toml_root_key(text: str, key: str, new_value: str) -> str:
    key_pattern = re.compile(rf"(?m)^({re.escape(key)}\s*=\s*)([^#\n]*?)(\s*(?:#.*)?)$")
    if key_pattern.search(text):
        return key_pattern.sub(rf"\1{new_value}\3", text, count=1)
    first_section = re.search(r"(?m)^\[", text)
    insertion = f"{key} = {new_value}\n"
    if first_section:
        prefix = text[: first_section.start()]
        suffix = text[first_section.start() :]
        if prefix and not prefix.endswith("\n"):
            prefix += "\n"
        if prefix and not prefix.endswith("\n\n"):
            prefix += "\n"
        return prefix + insertion + suffix
    if text and not text.endswith("\n"):
        text += "\n"
    return text + insertion


def remove_toml_root_key(text: str, key: str) -> str:
    return re.sub(rf"(?m)^{re.escape(key)}\s*=\s*[^#\n]*\s*(?:#.*)?\n?", "", text)


def remove_toml_section(text: str, section_name: str) -> str:
    section_header = f"[{section_name}]"
    section_start = text.find(section_header)
    if section_start == -1:
        return text
    _, body_end = extract_body_start(text, section_start)
    updated = text[:section_start] + text[body_end:]
    return re.sub(r"\n{3,}", "\n\n", updated)


def replace_toml_section(text: str, section_name: str, values: dict[str, Any] | None) -> str:
    updated = remove_toml_section(text, section_name)
    if values is None:
        return updated
    if updated and not updated.endswith("\n"):
        updated += "\n"
    if updated and not updated.endswith("\n\n"):
        updated += "\n"
    lines = [f"[{section_name}]"]
    for key, value in values.items():
        if not isinstance(key, str):
            continue
        if value is None:
            continue
        lines.append(f"{key} = {toml_value_literal(value)}")
    return updated + "\n".join(lines) + "\n"


def apply_config_snapshot(
    config_text: str,
    current_provider_name: str,
    snapshot: dict[str, Any],
) -> str:
    normalized_snapshot = normalize_config_snapshot(snapshot)
    target_provider_name = normalized_snapshot.get("provider_name") or "OpenAI"
    model_provider = normalized_snapshot.get("model_provider")
    provider_section = normalized_snapshot.get("provider_section")
    if isinstance(model_provider, str) and model_provider:
        config_text = update_toml_root_key(config_text, "model_provider", toml_quote(model_provider))
    else:
        config_text = remove_toml_root_key(config_text, "model_provider")
    for provider_name in dict.fromkeys([current_provider_name, target_provider_name, "OpenAI"]):
        if provider_name:
            config_text = remove_toml_section(config_text, f"model_providers.{provider_name}")
    if provider_section is not None:
        config_text = replace_toml_section(
            config_text,
            f"model_providers.{target_provider_name}",
            provider_section,
        )
    return config_text


def relay_provider_config(profile: dict[str, Any]) -> dict[str, Any]:
    base_url = profile_base_url(profile)
    if not base_url:
        raise RelayError(f"Relay profile '{profile.get('name')}' is missing base_url.")
    return {
        "name": "OpenAI",
        "base_url": base_url,
        "wire_api": "responses",
        "requires_openai_auth": True,
    }


def profile_display_type(profile: dict[str, Any]) -> str:
    return profile_type(profile)


def profile_account_summary(profile: dict[str, Any]) -> str | None:
    if profile_type(profile) != "official":
        return None
    auth_snapshot = profile.get("auth_snapshot")
    if not isinstance(auth_snapshot, dict):
        return None
    tokens = auth_snapshot.get("tokens")
    if isinstance(tokens, dict):
        account_id = tokens.get("account_id")
        if isinstance(account_id, str) and account_id.strip():
            trimmed = account_id.strip()
            return f"account:{trimmed[-8:]}" if len(trimmed) > 8 else f"account:{trimmed}"
    official_id = profile_official_id(profile)
    if isinstance(official_id, str) and official_id.strip():
        return official_id
    return None


def profile_display_target(profile: dict[str, Any]) -> str:
    kind = profile_type(profile)
    if kind == "relay":
        return profile_base_url(profile) or "(missing)"
    source_path = profile.get("source_path")
    if isinstance(source_path, str) and source_path.strip():
        return source_path
    mode = profile_auth_mode(profile)
    if mode:
        return f"auth_mode={mode}"
    return "(official auth snapshot)"


def profile_display_secret(profile: dict[str, Any]) -> str:
    if profile_type(profile) == "relay":
        return mask_key(profile_api_key(profile))
    mode = profile_auth_mode(profile) or "(token-based auth)"
    summary = profile_account_summary(profile)
    if summary:
        return f"{mode} | {summary}"
    return mode


def format_probe(last_probe: dict[str, Any] | None) -> str:
    if not isinstance(last_probe, dict):
        return "never"
    methods = last_probe.get("methods")
    if isinstance(methods, dict):
        parts: list[str] = []
        for method in ("http", "codex"):
            entry = methods.get(method)
            if not isinstance(entry, dict):
                continue
            status = "ok" if entry.get("ok") else "fail"
            latency = entry.get("latency_ms")
            segment = f"{method}:{status}"
            if isinstance(latency, int):
                segment += f" {latency}ms"
            parts.append(segment)
        checked_at = last_probe.get("checked_at")
        if checked_at:
            parts.append(str(checked_at))
        return " | ".join(parts) if parts else "never"
    ok = last_probe.get("ok")
    status = "ok" if ok else "fail"
    method = last_probe.get("method") or "unknown"
    latency = last_probe.get("latency_ms")
    detail = last_probe.get("detail")
    checked_at = last_probe.get("checked_at")
    parts = [status, method]
    if isinstance(latency, int):
        parts.append(f"{latency}ms")
    if checked_at:
        parts.append(str(checked_at))
    if detail:
        parts.append(str(detail))
    return " | ".join(parts)


def find_profile_index(store: dict[str, Any], target: str) -> int:
    for index, profile in enumerate(store["profiles"]):
        if profile.get("name") == target:
            return index
    raise RelayError(f"Unknown profile: {target}")


def resolve_profile(
    store: dict[str, Any],
    target: str | None = None,
    index: int | None = None,
    interactive_label: str | None = None,
) -> tuple[int, dict[str, Any]]:
    if target is not None and index is not None:
        raise RelayError("Use either a profile name or --index, not both.")
    if target is not None:
        resolved_index = find_profile_index(store, target)
        return resolved_index, store["profiles"][resolved_index]
    if index is not None:
        resolved_index = index - 1
        if resolved_index < 0 or resolved_index >= len(store["profiles"]):
            raise RelayError(f"Invalid profile index: {index}")
        return resolved_index, store["profiles"][resolved_index]
    if not sys.stdin.isatty():
        raise RelayError("No profile selected. Pass a profile name or --index.")
    return choose_profile_interactively(store, interactive_label or "Select a profile")


def choose_profile_interactively(
    store: dict[str, Any], prompt: str
) -> tuple[int, dict[str, Any]]:
    if not store["profiles"]:
        raise RelayError("No saved profiles.")
    print_profile_table(store)
    answer = input(f"{prompt} [1-{len(store['profiles'])}]: ").strip()
    if not answer:
        raise RelayError("No profile selected.")
    if not answer.isdigit():
        raise RelayError("Please enter a numeric index.")
    return resolve_profile(store, index=int(answer))


def extract_body_start(text: str, start: int) -> tuple[int, int]:
    line_end = text.find("\n", start)
    if line_end == -1:
        return len(text), len(text)
    body_start = line_end + 1
    match = re.search(r"(?m)^\[", text[body_start:])
    if match is None:
        return body_start, len(text)
    return body_start, body_start + match.start()


def update_toml_key_in_section(
    text: str, section_name: str, key: str, new_value: str
) -> str:
    section_header = f"[{section_name}]"
    section_start = text.find(section_header)
    if section_start == -1:
        if not text.endswith("\n"):
            text += "\n"
        return text + f"\n{section_header}\n{key} = {new_value}\n"

    body_start, body_end = extract_body_start(text, section_start)
    body = text[body_start:body_end]
    key_pattern = re.compile(
        rf"(?m)^(\s*{re.escape(key)}\s*=\s*)([^#\n]*?)(\s*(?:#.*)?)$"
    )
    if key_pattern.search(body):
        body = key_pattern.sub(rf"\1{new_value}\3", body, count=1)
    else:
        if body and not body.endswith("\n"):
            body += "\n"
        body += f"{key} = {new_value}\n"
    return text[:body_start] + body + text[body_end:]


def backup_live_files(paths: Paths) -> None:
    ensure_dir(paths.backup_dir)
    if paths.config_path.exists():
        shutil.copy2(paths.config_path, paths.backup_dir / "config.toml.last.bak")
    if paths.auth_path.exists():
        shutil.copy2(paths.auth_path, paths.backup_dir / "auth.json.last.bak")


def apply_profile(paths: Paths, profile: dict[str, Any]) -> None:
    backup_live_files(paths)
    config_text, parsed = read_config(paths)
    provider_name = current_provider(parsed)
    kind = profile_type(profile)

    if kind == "relay":
        updated_config = apply_config_snapshot(
            config_text,
            provider_name,
            {
                "model_provider": "OpenAI",
                "provider_name": "OpenAI",
                "provider_section": relay_provider_config(profile),
            },
        )
        atomic_write_text(paths.config_path, updated_config)
        auth_payload = read_auth(paths)
        for key in ("auth_mode", "tokens", "last_refresh"):
            auth_payload.pop(key, None)
        auth_payload["OPENAI_API_KEY"] = profile_api_key(profile)
        atomic_write_json(paths.auth_path, auth_payload, mode=0o600)
        return

    auth_snapshot = profile.get("auth_snapshot")
    if not isinstance(auth_snapshot, dict):
        raise RelayError(f"Official profile '{profile.get('name')}' is missing auth_snapshot.")
    updated_config = apply_config_snapshot(
        config_text,
        provider_name,
        normalize_config_snapshot(profile.get("config_snapshot")),
    )
    atomic_write_text(paths.config_path, updated_config)
    atomic_write_json(paths.auth_path, copy.deepcopy(auth_snapshot), mode=0o600)


def print_profile_table(store: dict[str, Any], live_state: dict[str, Any] | None = None) -> None:
    active_signature = live_signature(live_state) if live_state else (None, None)
    if not store["profiles"]:
        print("No saved profiles.")
        return
    for offset, profile in enumerate(store["profiles"], start=1):
        marker = "*" if profile_signature(profile) == active_signature else " "
        print(f"{marker} [{offset}] {profile['name']}")
        print(f"    Type  : {profile_display_type(profile)}")
        print(f"    Target: {profile_display_target(profile)}")
        print(f"    Auth  : {profile_display_secret(profile)}")
        note = profile.get("note") or "-"
        print(f"    Note  : {note}")
        print(f"    Probe : {format_probe(profile.get('last_probe'))}")
        last_used = profile.get("last_used_at") or "never"
        print(f"    Used  : {last_used}")


def print_current(paths: Paths, store: dict[str, Any]) -> int:
    live_state = read_live_state(paths)
    current_type = live_state.get("type") or "unknown"
    print(f"Provider : {live_state.get('provider_name') or '(unknown)'}")
    print(f"Type     : {current_type}")
    print(f"Model    : {live_state.get('model') or '(unknown)'}")
    if current_type == "relay":
        print(f"Base URL : {live_state.get('base_url') or '(missing)'}")
        print(f"API key  : {mask_key(live_state.get('api_key'))}")
    elif current_type == "official":
        print(f"Auth mode: {live_state.get('auth_mode') or '(unknown)'}")
        live_summary = None
        auth_snapshot = live_state.get("auth_snapshot")
        if isinstance(auth_snapshot, dict):
            tokens = auth_snapshot.get("tokens")
            if isinstance(tokens, dict):
                account_id = tokens.get("account_id")
                if isinstance(account_id, str) and account_id.strip():
                    trimmed = account_id.strip()
                    live_summary = f"account:{trimmed[-8:]}" if len(trimmed) > 8 else f"account:{trimmed}"
        if live_summary:
            print(f"Account  : {live_summary}")
        print("Base URL : (managed by official subscription auth)")
    else:
        print(f"Base URL : {live_state.get('base_url') or '(missing)'}")
        print(f"API key  : {mask_key(live_state.get('api_key'))}")
    active_signature = live_signature(live_state)
    for offset, profile in enumerate(store["profiles"], start=1):
        if profile_signature(profile) == active_signature:
            print(f"Profile  : {profile['name']} [#{offset}]")
            note = profile.get("note")
            if note:
                print(f"Note     : {note}")
            return 0
    print("Profile  : unmanaged current config")
    print("Hint     : use `codex-relay save-current <name>` to save it.")
    return 1


def import_profile_from_directory(
    store: dict[str, Any],
    name: str,
    source_dir: str | Path,
    note: str = "",
) -> dict[str, Any]:
    source_root = Path(source_dir).expanduser().resolve()
    source_paths = build_paths(source_root)
    if not source_paths.config_path.exists():
        raise RelayError(f"Missing config.toml in {source_root}")
    if not source_paths.auth_path.exists():
        raise RelayError(f"Missing auth.json in {source_root}")
    live_state = read_live_state(source_paths)
    if live_state.get("type") not in {"relay", "official"}:
        raise RelayError(
            f"Could not detect a relay or official subscription profile in {source_root}"
        )
    if profile_exists(store, name):
        raise RelayError(f"Profile already exists: {name}")
    return build_profile_from_state(name, live_state, note, source_path=str(source_root))


def official_login_snapshot(paths: Paths) -> dict[str, Any]:
    _, parsed = read_config(paths)
    return {
        "model_provider": None,
        "provider_name": current_provider(parsed),
        "provider_section": None,
    }


def prepare_official_login_home(source_paths: Paths, target_codex_home: Path) -> None:
    ensure_dir(target_codex_home)
    config_text, parsed = read_config(source_paths)
    sanitized_config = apply_config_snapshot(
        config_text,
        current_provider(parsed),
        official_login_snapshot(source_paths),
    )
    atomic_write_text(target_codex_home / "config.toml", sanitized_config, mode=0o600)
    atomic_write_json(target_codex_home / "auth.json", {}, mode=0o600)


def build_official_profile_via_codex_login(paths: Paths, name: str, note: str = "") -> dict[str, Any]:
    if not shutil.which("codex"):
        raise RelayError("Could not find `codex` in PATH.")
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise RelayError("Official login requires an interactive terminal.")

    runtime_root = paths.codex_home / "relay_login_runtime"
    ensure_dir(runtime_root)
    with tempfile.TemporaryDirectory(prefix="official-login-", dir=runtime_root) as temp_root:
        temp_codex_home = Path(temp_root) / ".codex"
        prepare_official_login_home(paths, temp_codex_home)
        env = os.environ.copy()
        env["CODEX_HOME"] = str(temp_codex_home)
        completed = subprocess.run(
            ["codex", "login", "--device-auth"],
            env=env,
            check=False,
        )
        if completed.returncode != 0:
            raise RelayError("Official login was cancelled or failed.")

        status_check = subprocess.run(
            ["codex", "login", "status"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        if status_check.returncode != 0:
            detail = status_check.stdout.strip() or status_check.stderr.strip() or "unknown login status"
            raise RelayError(f"Official login did not complete cleanly: {excerpt(detail)}")

        temp_paths = build_paths(temp_codex_home)
        live_state = read_live_state(temp_paths)
        if live_state.get("type") != "official":
            raise RelayError(
                "Codex login finished, but no official subscription tokens were detected."
            )
        profile = build_profile_from_state(name, live_state, note)
        profile["login_method"] = "device_auth"
        return profile


def build_responses_url(base_url: str, suffix: str) -> str:
    base = normalize_url(base_url)
    parsed = urllib.parse.urlparse(base)
    path = parsed.path.rstrip("/")
    new_path = path + suffix
    return urllib.parse.urlunparse(
        (parsed.scheme, parsed.netloc, new_path, parsed.params, parsed.query, parsed.fragment)
    )


def responses_url_candidates(base_url: str) -> list[str]:
    base = normalize_url(base_url)
    parsed = urllib.parse.urlparse(base)
    path = parsed.path.rstrip("/")
    candidates = [build_responses_url(base, "/responses")]
    if not path.endswith("/v1"):
        candidates.append(build_responses_url(base, "/v1/responses"))
    deduped: list[str] = []
    for candidate in candidates:
        if candidate not in deduped:
            deduped.append(candidate)
    return deduped


def excerpt(value: str, limit: int = 120) -> str:
    compact = " ".join(value.split())
    return compact if len(compact) <= limit else compact[: limit - 3] + "..."


def clamp_text(value: str | None, limit: int = PROBE_TEXT_STORAGE_LIMIT) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text if len(text) <= limit else text[: limit - 3] + "..."


def build_probe_prompt(message: str) -> str:
    return message


def extract_response_text(payload: Any) -> str:
    if isinstance(payload, dict):
        output_text = payload.get("output_text")
        if isinstance(output_text, str) and output_text.strip():
            return output_text.strip()
        choices = payload.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str) and content.strip():
                        return content.strip()
        output = payload.get("output")
        if isinstance(output, list):
            parts: list[str] = []
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if not isinstance(content, list):
                    continue
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    text = block.get("text")
                    if isinstance(text, str) and text.strip():
                        parts.append(text.strip())
            if parts:
                return " ".join(parts)
        error = payload.get("error")
        if isinstance(error, dict):
            message = error.get("message")
            if isinstance(message, str):
                return message
    if isinstance(payload, str):
        return payload.strip()
    return ""


def parse_sse_events(raw: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    event_name: str | None = None
    data_lines: list[str] = []

    def flush() -> None:
        nonlocal event_name, data_lines
        if not data_lines:
            event_name = None
            return
        data_text = "\n".join(data_lines).strip()
        if data_text and data_text != "[DONE]":
            try:
                payload = json.loads(data_text)
            except json.JSONDecodeError:
                payload = data_text
            events.append({"event": event_name, "payload": payload})
        event_name = None
        data_lines = []

    for line in raw.splitlines():
        if not line.strip():
            flush()
            continue
        if line.startswith("event:"):
            event_name = line.partition(":")[2].strip()
        elif line.startswith("data:"):
            data_lines.append(line.partition(":")[2].strip())
    flush()
    return events


def extract_text_from_sse(raw: str) -> tuple[str, bool, str | None]:
    events = parse_sse_events(raw)
    deltas: list[str] = []
    final_text = ""
    completed = False
    error_message: str | None = None

    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        payload_type = payload.get("type")
        if payload_type == "response.output_text.delta":
            delta = payload.get("delta")
            if isinstance(delta, str):
                deltas.append(delta)
        elif payload_type == "response.output_text.done" and not deltas:
            text = payload.get("text")
            if isinstance(text, str):
                final_text = text
        elif payload_type == "response.completed":
            completed = True
            response = payload.get("response")
            if isinstance(response, dict):
                final_text = "".join(deltas).strip() or extract_response_text(response)
                error = response.get("error")
                if isinstance(error, dict):
                    message = error.get("message")
                    if isinstance(message, str) and message.strip():
                        error_message = message.strip()

    text = "".join(deltas).strip() or final_text.strip()
    return text, completed, error_message


def build_codex_http_probe_request(
    profile: dict[str, Any], model: str, message: str, stream: bool = True
) -> tuple[dict[str, str], dict[str, Any]]:
    body = {
        "model": model,
        "instructions": CODEX_PROBE_INSTRUCTIONS,
        "input": [
            {
                "type": "message",
                "role": "user",
                "content": [{"type": "input_text", "text": build_probe_prompt(message)}],
            }
        ],
        "tools": [],
        "tool_choice": "auto",
        "parallel_tool_calls": False,
        "reasoning": {"effort": "low", "summary": "auto"},
        "store": False,
        "stream": stream,
    }
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {profile['api_key']}",
        "openai-beta": "responses=experimental",
        "User-Agent": "Codex-CLI/1.0",
        "Accept": "text/event-stream, application/json",
    }
    return headers, body


def probe_via_http(
    profile: dict[str, Any], model: str, message: str, timeout: float, expect: str | None
) -> dict[str, Any]:
    last_error: dict[str, Any] | None = None
    headers, stream_body = build_codex_http_probe_request(profile, model, message, stream=True)
    for url in responses_url_candidates(profile["base_url"]):
        for current_body in (stream_body,):
            for attempt in range(1, DEFAULT_HTTP_RETRIES + 1):
                request = urllib.request.Request(
                    url,
                    data=json.dumps(current_body).encode("utf-8"),
                    headers=headers,
                    method="POST",
                )

                start = time.perf_counter()
                try:
                    with urllib.request.urlopen(request, timeout=timeout) as response:
                        raw = response.read().decode("utf-8", errors="replace")
                        latency_ms = int((time.perf_counter() - start) * 1000)

                        content_type = response.headers.get("Content-Type", "")
                        if "text/event-stream" in content_type or raw.lstrip().startswith("event:"):
                            content, completed, stream_error = extract_text_from_sse(raw)
                            detail = content or excerpt(raw)
                            ok = completed and not stream_error
                            if stream_error:
                                detail = stream_error
                        else:
                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                payload = raw
                            content = extract_response_text(payload) or raw
                            detail = content
                            ok = True

                        if expect and expect not in content:
                            ok = False
                            detail = f"missing expected text {expect!r}; got: {excerpt(content)}"

                        return {
                            "ok": ok,
                            "method": "http",
                            "status_code": getattr(response, "status", None),
                            "detail": clamp_text(detail),
                            "reply": clamp_text(content),
                            "latency_ms": latency_ms,
                        }
                except urllib.error.HTTPError as exc:
                    raw = exc.read().decode("utf-8", errors="replace")
                    latency_ms = int((time.perf_counter() - start) * 1000)
                    detail = raw
                    try:
                        payload = json.loads(raw)
                        detail = extract_response_text(payload) or raw
                    except json.JSONDecodeError:
                        pass
                    last_error = {
                        "ok": False,
                        "method": "http",
                        "status_code": exc.code,
                        "detail": clamp_text(detail),
                        "reply": None,
                        "latency_ms": latency_ms,
                    }
                    if exc.code != 404:
                        return last_error
                except (urllib.error.URLError, socket.timeout, TimeoutError, ssl.SSLError) as exc:
                    latency_ms = int((time.perf_counter() - start) * 1000)
                    reason = getattr(exc, "reason", exc)
                    reason_text = str(reason)
                    if (
                        "UNEXPECTED_EOF_WHILE_READING" in reason_text
                        and attempt < DEFAULT_HTTP_RETRIES
                    ):
                        continue
                    return {
                        "ok": False,
                        "method": "http",
                        "status_code": None,
                        "detail": clamp_text(reason_text),
                        "reply": None,
                        "latency_ms": latency_ms,
                    }

    if last_error is not None:
        return last_error
    return {
        "ok": False,
        "method": "http",
        "status_code": None,
        "detail": "no candidate response endpoint worked",
        "reply": None,
        "latency_ms": None,
    }


def write_probe_config(temp_codex_home: Path, source_paths: Paths, profile: dict[str, Any]) -> None:
    ensure_dir(temp_codex_home)
    shutil.copy2(source_paths.config_path, temp_codex_home / "config.toml")
    if source_paths.auth_path.exists():
        shutil.copy2(source_paths.auth_path, temp_codex_home / "auth.json")
    else:
        atomic_write_json(temp_codex_home / "auth.json", {}, mode=0o600)
    temp_paths = build_paths(temp_codex_home)
    apply_profile(temp_paths, profile)


def probe_via_codex(
    source_paths: Paths,
    profile: dict[str, Any],
    model: str | None,
    message: str,
    timeout: float,
    expect: str | None,
) -> dict[str, Any]:
    start = time.perf_counter()
    runtime_root = source_paths.codex_home / "relay_probe_runtime"
    if path_is_under_tmp(runtime_root):
        runtime_root = Path.home() / ".codex" / "relay_probe_runtime"
    ensure_dir(runtime_root)
    with tempfile.TemporaryDirectory(prefix="codex-relay-", dir=runtime_root) as temp_root:
        temp_root_path = Path(temp_root)
        temp_codex_home = temp_root_path / ".codex"
        write_probe_config(temp_codex_home, source_paths, profile)
        output_path = temp_root_path / "last_message.txt"
        temp_tmpdir = temp_root_path / "tmp"
        ensure_dir(temp_tmpdir)
        env = os.environ.copy()
        env["CODEX_HOME"] = str(temp_codex_home)
        if profile_type(profile) == "relay":
            api_key = profile_api_key(profile)
            if not api_key:
                raise RelayError(f"Relay profile '{profile.get('name')}' is missing api_key.")
            env["OPENAI_API_KEY"] = api_key
        else:
            env.pop("OPENAI_API_KEY", None)
        env["TMPDIR"] = str(temp_tmpdir)
        env["NO_COLOR"] = "1"
        cmd = [
            "codex",
            "exec",
            "--skip-git-repo-check",
            "--ephemeral",
            "--color",
            "never",
            "-C",
            "/tmp",
            "-o",
            str(output_path),
            build_probe_prompt(message),
        ]
        if model:
            cmd.extend(["-m", model])

        try:
            completed = subprocess.run(
                cmd,
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            latency_ms = int((time.perf_counter() - start) * 1000)
            return {
                "ok": False,
                "method": "codex",
                "status_code": None,
                "detail": f"timed out after {int(timeout)}s",
                "reply": None,
                "latency_ms": latency_ms,
            }

        latency_ms = int((time.perf_counter() - start) * 1000)
        output_text = ""
        if output_path.exists():
            output_text = output_path.read_text(encoding="utf-8", errors="replace").strip()

        detail = output_text or completed.stderr.strip() or completed.stdout.strip()
        reply = output_text.strip() or None
        ok = completed.returncode == 0
        if ok and expect:
            ok = expect in detail
            if not ok:
                detail = f"missing expected text {expect!r}; got: {excerpt(detail)}"
        return {
            "ok": ok,
            "method": "codex",
            "status_code": completed.returncode,
            "detail": clamp_text(detail or f"codex exited with {completed.returncode}"),
            "reply": clamp_text(reply),
            "latency_ms": latency_ms,
        }


def update_probe_metadata(
    profile: dict[str, Any], probe_results: dict[str, dict[str, Any]]
) -> None:
    profile["updated_at"] = now_iso()
    checked_at = now_iso()
    existing = profile.get("last_probe")
    existing_methods: dict[str, Any] = {}
    if isinstance(existing, dict) and isinstance(existing.get("methods"), dict):
        existing_methods = dict(existing["methods"])
    for method, probe_result in probe_results.items():
        existing_methods[method] = {
            "ok": bool(probe_result["ok"]),
            "status_code": probe_result.get("status_code"),
            "detail": probe_result.get("detail"),
            "reply": probe_result.get("reply"),
            "latency_ms": probe_result.get("latency_ms"),
            "checked_at": checked_at,
        }
    profile["last_probe"] = {
        "checked_at": checked_at,
        "methods": existing_methods,
    }


def probe_one(
    source_paths: Paths,
    profile: dict[str, Any],
    model: str,
    message: str,
    method: str,
    timeout: float,
    expect: str | None,
) -> dict[str, Any]:
    if method == "http":
        if not profile_supports_http(profile):
            return {
                "ok": False,
                "method": "http",
                "status_code": None,
                "detail": "HTTP probe is only available for relay profiles with base_url + api_key",
                "reply": None,
                "latency_ms": None,
            }
        return probe_via_http(profile, model, message, timeout, expect)
    return probe_via_codex(source_paths, profile, model, message, timeout, expect)


def collect_targets(
    store: dict[str, Any],
    targets: list[str],
    indexes: list[int],
    all_profiles: bool,
) -> list[tuple[int, dict[str, Any]]]:
    if all_profiles:
        return list(enumerate(store["profiles"]))
    resolved: list[tuple[int, dict[str, Any]]] = []
    seen: set[int] = set()
    for item in targets:
        idx = find_profile_index(store, item)
        if idx not in seen:
            resolved.append((idx, store["profiles"][idx]))
            seen.add(idx)
    for item in indexes:
        idx = item - 1
        if idx < 0 or idx >= len(store["profiles"]):
            raise RelayError(f"Invalid profile index: {item}")
        if idx not in seen:
            resolved.append((idx, store["profiles"][idx]))
            seen.add(idx)
    if resolved:
        return resolved
    if sys.stdin.isatty():
        chosen = choose_profile_interactively(store, "Select a profile to probe")
        return [chosen]
    raise RelayError("No probe target selected. Use --all, a profile name, or --index.")


def resolve_probe_methods(via: str) -> list[str]:
    if via == "both":
        return ["http", "codex"]
    return [via]


def effective_probe_methods(profile: dict[str, Any], requested_methods: list[str]) -> list[str]:
    methods = list(requested_methods)
    if "http" in methods and not profile_supports_http(profile):
        methods = [method for method in methods if method != "http"]
    return methods or list(requested_methods)


def primary_probe_method(methods: list[str]) -> str:
    if "codex" in methods:
        return "codex"
    return methods[0]


def summarize_probe_status(
    results_by_method: dict[str, dict[str, Any]], methods: list[str]
) -> tuple[str, bool]:
    primary = primary_probe_method(methods)
    primary_ok = bool(results_by_method.get(primary, {}).get("ok"))
    all_ok = all(bool(results_by_method.get(method, {}).get("ok")) for method in methods)
    if primary_ok and all_ok:
        return "OK ", True
    if primary_ok:
        return "MIX", True
    return "ERR", False


def print_probe_results(
    results: list[tuple[dict[str, Any], dict[str, dict[str, Any]], list[str]]]
) -> None:
    for profile, results_by_method, methods in results:
        status, _ = summarize_probe_status(results_by_method, methods)
        print(f"[{status}] {profile['name']}")
        print(f"    Type        : {profile_display_type(profile)}")
        print(f"    Target      : {profile_display_target(profile)}")
        for method in methods:
            result = results_by_method.get(method)
            label = method.capitalize()
            if result is None:
                print(f"    {label:<11}: missing")
                continue
            sub_status = "OK " if result["ok"] else "ERR"
            code = result.get("status_code")
            latency = result.get("latency_ms")
            code_text = code if code is not None else "-"
            time_text = f"{latency}ms" if latency is not None else "-"
            print(f"    {label:<11}: {sub_status} | {code_text} | {time_text}")
            reply = result.get("reply")
            if reply:
                print(f"    {label} Reply : {excerpt(reply, DEFAULT_REPLY_DISPLAY_LIMIT)}")
            detail = result.get("detail")
            if detail and (not result.get("ok")) and detail != reply:
                print(f"    {label} Detail: {excerpt(detail, DEFAULT_REPLY_DISPLAY_LIMIT)}")


def execute_probe(
    paths: Paths,
    targets: list[tuple[int, dict[str, Any]]],
    via: str,
    message: str,
    expect: str | None,
    model: str | None = None,
    timeout: float | None = None,
    workers: int | None = None,
) -> tuple[list[tuple[dict[str, Any], dict[str, dict[str, Any]], list[str]]], bool]:
    live_state = read_live_state(paths)
    resolved_model = model or live_state.get("model") or "gpt-5.4"
    requested_methods = resolve_probe_methods(via)

    if timeout is None:
        timeout = max(
            DEFAULT_HTTP_TIMEOUT if method == "http" else DEFAULT_CODEX_TIMEOUT
            for method in requested_methods
        )
    if workers is None:
        workers = sum(
            DEFAULT_HTTP_WORKERS if method == "http" else DEFAULT_CODEX_WORKERS
            for method in requested_methods
        )

    result_slots: dict[int, dict[str, Any]] = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, workers)) as executor:
        futures: dict[
            concurrent.futures.Future[dict[str, Any]], tuple[int, dict[str, Any], str]
        ] = {}
        for index, profile in targets:
            methods = effective_probe_methods(profile, requested_methods)
            result_slots[index] = {"profile": profile, "methods": {}, "requested_methods": methods}
            for method in methods:
                profile_copy = copy.deepcopy(profile)
                future = executor.submit(
                    probe_one,
                    paths,
                    profile_copy,
                    resolved_model,
                    message,
                    method,
                    float(timeout),
                    expect,
                )
                futures[future] = (index, profile, method)

        for future in concurrent.futures.as_completed(futures):
            index, profile, method = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                result = {
                    "ok": False,
                    "method": method,
                    "status_code": None,
                    "detail": clamp_text(f"{type(exc).__name__}: {exc}") or "unexpected probe error",
                    "reply": None,
                    "latency_ms": None,
                }
            slot = result_slots.setdefault(index, {"profile": profile, "methods": {}})
            slot["methods"][method] = result

    overall_ok = True
    ordered_results: list[tuple[dict[str, Any], dict[str, dict[str, Any]], list[str]]] = []
    for index in sorted(result_slots):
        slot = result_slots[index]
        methods_result = slot["methods"]
        methods = slot.get("requested_methods") or ["codex"]
        _, profile_ok = summarize_probe_status(methods_result, methods)
        overall_ok = overall_ok and profile_ok
        ordered_results.append((slot["profile"], methods_result, methods))

    with store_lock(paths):
        latest_store, _ = load_store_unlocked(paths)
        latest_by_name = {
            profile.get("name"): profile
            for profile in latest_store.get("profiles", [])
            if isinstance(profile, dict)
        }
        for profile, methods_result, _ in ordered_results:
            current = latest_by_name.get(profile.get("name"))
            if isinstance(current, dict):
                update_probe_metadata(current, methods_result)
        write_store(paths, latest_store)

    return ordered_results, overall_ok


def cmd_list(args: argparse.Namespace) -> int:
    paths = build_paths(args.codex_home)
    store, creation_message = load_store(paths)
    if creation_message:
        print(creation_message)
    live_state = read_live_state(paths)
    print_profile_table(store, live_state=live_state)
    return 0


def cmd_current(args: argparse.Namespace) -> int:
    paths = build_paths(args.codex_home)
    store, creation_message = load_store(paths)
    if creation_message:
        print(creation_message)
    return print_current(paths, store)


def cmd_add(args: argparse.Namespace) -> int:
    paths = build_paths(args.codex_home)
    creation_message = None
    with store_lock(paths):
        store, creation_message = load_store_unlocked(paths)
        if profile_exists(store, args.name):
            raise RelayError(f"Profile already exists: {args.name}")
        profile = make_profile(args.name, args.url, args.key, args.note or "")
        store["profiles"].append(profile)
        write_store(paths, store)
    print(f"Saved profile '{args.name}'.")
    if creation_message:
        print(creation_message)
    if args.activate:
        apply_profile(paths, profile)
        with store_lock(paths):
            store, _ = load_store_unlocked(paths)
            index = find_profile_index(store, args.name)
            store["profiles"][index]["last_used_at"] = now_iso()
            store["profiles"][index]["updated_at"] = now_iso()
            write_store(paths, store)
        print(f"Activated profile '{args.name}'.")
    return 0


def cmd_save_current(args: argparse.Namespace) -> int:
    paths = build_paths(args.codex_home)
    live_state = read_live_state(paths)
    if live_state.get("type") not in {"relay", "official"}:
        raise RelayError(
            "The current Codex config is neither a relay profile nor an official subscription."
        )
    with store_lock(paths):
        store, creation_message = load_store_unlocked(paths)
        if creation_message:
            print(creation_message)
        if profile_exists(store, args.name):
            raise RelayError(f"Profile already exists: {args.name}")
        profile = build_profile_from_state(
            args.name,
            live_state,
            args.note or "",
            source_path=str(paths.codex_home),
        )
        profile["last_used_at"] = now_iso()
        store["profiles"].append(profile)
        write_store(paths, store)
    print(
        f"Saved the current {profile_type(profile)} live config as profile '{args.name}'."
    )
    return 0


def cmd_import(args: argparse.Namespace) -> int:
    paths = build_paths(args.codex_home)
    source_dir = Path(args.source).expanduser().resolve()
    with store_lock(paths):
        store, creation_message = load_store_unlocked(paths)
        if creation_message:
            print(creation_message)
        name = args.name
        if not name:
            imported_state = read_live_state(build_paths(source_dir))
            if imported_state.get("type") == "relay" and isinstance(imported_state.get("base_url"), str):
                name = suggest_name(store, imported_state["base_url"])
            else:
                name = suggest_official_name(store, imported_state.get("official_id"), source_dir)
        profile = import_profile_from_directory(store, name, source_dir, args.note or "")
        store["profiles"].append(profile)
        write_store(paths, store)
    print(
        f"Imported {profile_type(profile)} profile '{profile['name']}' from {source_dir}."
    )
    if args.activate:
        apply_profile(paths, profile)
        with store_lock(paths):
            store, _ = load_store_unlocked(paths)
            index = find_profile_index(store, profile["name"])
            store["profiles"][index]["last_used_at"] = now_iso()
            store["profiles"][index]["updated_at"] = now_iso()
            write_store(paths, store)
        print(f"Activated profile '{profile['name']}'.")
    return 0


def cmd_login_official(args: argparse.Namespace) -> int:
    paths = build_paths(args.codex_home)
    with store_lock(paths):
        store, creation_message = load_store_unlocked(paths)
        if creation_message:
            print(creation_message)
        if profile_exists(store, args.name):
            raise RelayError(f"Profile already exists: {args.name}")

    profile = build_official_profile_via_codex_login(paths, args.name, args.note or "")

    with store_lock(paths):
        store, _ = load_store_unlocked(paths)
        if profile_exists(store, args.name):
            raise RelayError(f"Profile already exists: {args.name}")
        store["profiles"].append(profile)
        write_store(paths, store)

    print(f"Saved official profile '{args.name}' from native `codex login`.")
    if args.activate:
        apply_profile(paths, profile)
        with store_lock(paths):
            store, _ = load_store_unlocked(paths)
            index = find_profile_index(store, profile["name"])
            store["profiles"][index]["last_used_at"] = now_iso()
            store["profiles"][index]["updated_at"] = now_iso()
            write_store(paths, store)
        print(f"Activated profile '{profile['name']}'.")
    return 0


def cmd_use(args: argparse.Namespace) -> int:
    paths = build_paths(args.codex_home)
    with store_lock(paths):
        store, creation_message = load_store_unlocked(paths)
        if creation_message:
            print(creation_message)
        index, profile = resolve_profile(
            store,
            target=args.target,
            index=args.index,
            interactive_label="Select a profile to activate",
        )
        store["profiles"][index]["last_used_at"] = now_iso()
        store["profiles"][index]["updated_at"] = now_iso()
        write_store(paths, store)
    apply_profile(paths, profile)
    print(f"Activated profile '{profile['name']}'.")
    print(f"Type     -> {profile_display_type(profile)}")
    print(f"Target   -> {profile_display_target(profile)}")
    print(f"Auth     -> {profile_display_secret(profile)}")
    print(f"Backup   -> {paths.backup_dir / 'config.toml.last.bak'}")
    return 0


def cmd_remove(args: argparse.Namespace) -> int:
    paths = build_paths(args.codex_home)
    live_state = read_live_state(paths)
    active_signature = live_signature(live_state)
    with store_lock(paths):
        store, creation_message = load_store_unlocked(paths)
        if creation_message:
            print(creation_message)
        index, profile = resolve_profile(
            store,
            target=args.target,
            index=args.index,
            interactive_label="Select a profile to remove",
        )
        was_active = profile_signature(profile) == active_signature
        del store["profiles"][index]
        write_store(paths, store)
    print(f"Removed profile '{profile['name']}'.")
    if was_active:
        print("Note: the live Codex config still points to this endpoint until you switch again.")
    return 0


def cmd_edit(args: argparse.Namespace) -> int:
    paths = build_paths(args.codex_home)
    live_state = read_live_state(paths)
    apply_live = False
    with store_lock(paths):
        store, creation_message = load_store_unlocked(paths)
        if creation_message:
            print(creation_message)
        index, profile = resolve_profile(
            store,
            target=args.target,
            index=args.index,
            interactive_label="Select a profile to edit",
        )
        old_signature = profile_signature(profile)
        kind = profile_type(profile)

        if args.rename:
            if args.rename != profile["name"] and profile_exists(store, args.rename):
                raise RelayError(f"Profile already exists: {args.rename}")
            profile["name"] = args.rename
        if kind == "relay":
            if args.url:
                profile["base_url"] = normalize_url(args.url)
            if args.key:
                profile["api_key"] = args.key.strip()
        elif args.url or args.key:
            raise RelayError("Official profiles only support renaming and note updates.")
        if args.note is not None:
            profile["note"] = args.note.strip()
        profile["updated_at"] = now_iso()

        apply_live = old_signature == live_signature(live_state) and (
            kind == "relay" and (args.url is not None or args.key is not None)
        )
        write_store(paths, store)

    if apply_live:
        apply_profile(paths, profile)
        with store_lock(paths):
            store, _ = load_store_unlocked(paths)
            index = find_profile_index(store, profile["name"])
            store["profiles"][index]["last_used_at"] = now_iso()
            write_store(paths, store)
        print("Updated the live Codex config because this profile was active.")

    print(f"Updated profile '{profile['name']}'.")
    return 0


def cmd_probe_common(args: argparse.Namespace, all_profiles: bool) -> int:
    paths = build_paths(args.codex_home)
    store, creation_message = load_store(paths)
    if creation_message:
        print(creation_message)
    if not store["profiles"]:
        raise RelayError("No saved profiles.")

    targets = collect_targets(
        store,
        args.targets,
        args.indexes or [],
        all_profiles,
    )

    ordered_results, overall_ok = execute_probe(
        paths,
        targets,
        via=args.via,
        message=args.message,
        expect=args.expect,
        model=args.model,
        timeout=args.timeout,
        workers=args.workers,
    )
    print_probe_results(ordered_results)
    if overall_ok:
        return 0
    return 1


def cmd_probe(args: argparse.Namespace) -> int:
    return cmd_probe_common(args, all_profiles=False)


def cmd_probe_all(args: argparse.Namespace) -> int:
    return cmd_probe_common(args, all_profiles=True)


class RelayTUI:
    def __init__(self, paths: Paths) -> None:
        self.paths = paths
        self.selected = 0
        self.status = "Ready"
        self.store: dict[str, Any] = {"version": STORE_VERSION, "profiles": []}
        self.live_state: dict[str, Any] = {}
        self.probe_via = "both"
        self.probe_message = DEFAULT_MESSAGE
        self.probe_expect = ""
        self.filter_text = ""
        self.type_filter = "all"
        self.marked_profiles: set[str] = set()
        self.should_exit = False
        self.colors_ready = False
        self.color_map: dict[str, int] = {}

    def refresh(self) -> None:
        self.store, _ = load_store(self.paths)
        self.live_state = read_live_state(self.paths)
        valid_names = {
            profile.get("name")
            for profile in self.store.get("profiles", [])
            if isinstance(profile, dict) and isinstance(profile.get("name"), str)
        }
        self.marked_profiles.intersection_update(valid_names)
        profiles = self.visible_profiles()
        if not profiles:
            self.selected = 0
            return
        store_indexes = [index for index, _ in profiles]
        if self.selected not in store_indexes:
            self.selected = store_indexes[0]

    def visible_profiles(self) -> list[tuple[int, dict[str, Any]]]:
        profiles = self.store.get("profiles", [])
        needle = self.filter_text.casefold()
        visible: list[tuple[int, dict[str, Any]]] = []
        for index, profile in enumerate(profiles):
            if self.type_filter != "all" and profile_type(profile) != self.type_filter:
                continue
            haystack = " ".join(
                [
                    str(profile.get("name") or ""),
                    str(profile_display_target(profile)),
                    str(profile.get("note") or ""),
                    str(profile_type(profile)),
                    str(profile.get("source_path") or ""),
                ]
            ).casefold()
            if not needle or needle in haystack:
                visible.append((index, profile))
        return visible

    def current_profile_entry(self) -> tuple[int, dict[str, Any]] | None:
        profiles = self.visible_profiles()
        if not profiles:
            return None
        for index, profile in profiles:
            if index == self.selected:
                return index, profile
        self.selected = profiles[0][0]
        return profiles[0]

    def current_profile(self) -> dict[str, Any] | None:
        entry = self.current_profile_entry()
        return entry[1] if entry else None

    def marked_target_entries(self) -> list[tuple[int, dict[str, Any]]]:
        if not self.marked_profiles:
            return []
        return [
            (index, profile)
            for index, profile in enumerate(self.store.get("profiles", []))
            if profile.get("name") in self.marked_profiles
        ]

    def toggle_mark_current(self) -> None:
        entry = self.current_profile_entry()
        if not entry:
            self.status = "No profile selected."
            return
        name = entry[1]["name"]
        if name in self.marked_profiles:
            self.marked_profiles.remove(name)
            self.status = f"Unmarked '{name}'."
        else:
            self.marked_profiles.add(name)
            self.status = f"Marked '{name}' for probe."

    def toggle_mark_visible(self) -> None:
        profiles = self.visible_profiles()
        if not profiles:
            self.status = "No visible profiles to mark."
            return
        visible_names = {profile["name"] for _, profile in profiles}
        if visible_names.issubset(self.marked_profiles):
            self.marked_profiles.difference_update(visible_names)
            self.status = f"Unmarked {len(visible_names)} visible profile(s)."
        else:
            self.marked_profiles.update(visible_names)
            self.status = f"Marked {len(visible_names)} visible profile(s)."

    def clear_marked(self) -> None:
        count = len(self.marked_profiles)
        if count == 0:
            self.status = "No marked profiles."
            return
        self.marked_profiles.clear()
        self.status = f"Cleared {count} marked profile(s)."

    def active_store_index(self) -> int | None:
        active = live_signature(self.live_state)
        for index, profile in enumerate(self.store.get("profiles", [])):
            if profile_signature(profile) == active:
                return index
        return None

    def init_colors(self, curses_module: Any) -> None:
        if self.colors_ready:
            return
        if not curses_module.has_colors():
            self.colors_ready = True
            return
        curses_module.start_color()
        curses_module.use_default_colors()
        color_defs = [
            ("header", curses_module.COLOR_CYAN, -1),
            ("accent", curses_module.COLOR_BLUE, -1),
            ("ok", curses_module.COLOR_GREEN, -1),
            ("warn", curses_module.COLOR_YELLOW, -1),
            ("err", curses_module.COLOR_RED, -1),
            ("muted", curses_module.COLOR_BLACK, -1),
            ("status", curses_module.COLOR_WHITE, curses_module.COLOR_BLUE),
            ("selected", curses_module.COLOR_BLACK, curses_module.COLOR_CYAN),
            ("active", curses_module.COLOR_GREEN, -1),
            ("dialog", curses_module.COLOR_WHITE, curses_module.COLOR_BLACK),
        ]
        for idx, (_, fg, bg) in enumerate(color_defs, start=1):
            try:
                curses_module.init_pair(idx, fg, bg)
            except Exception:
                pass
        self.color_map = {name: idx for idx, (name, _, _) in enumerate(color_defs, start=1)}
        self.colors_ready = True

    def color(self, curses_module: Any, name: str, fallback: int = 0) -> int:
        if not getattr(self, "color_map", None):
            return fallback
        idx = self.color_map.get(name)
        if idx is None:
            return fallback
        try:
            return curses_module.color_pair(idx)
        except Exception:
            return fallback

    def status_attr(self, curses_module: Any) -> int:
        lowered = self.status.casefold()
        if "error" in lowered or "fail" in lowered or "unexpected" in lowered:
            return self.color(curses_module, "err")
        if "cancel" in lowered or "attention" in lowered:
            return self.color(curses_module, "warn")
        if "saved" in lowered or "updated" in lowered or "activated" in lowered or "finished" in lowered:
            return self.color(curses_module, "ok")
        return self.color(curses_module, "status")

    def move_selection(self, delta: int) -> None:
        profiles = self.visible_profiles()
        if not profiles:
            return
        positions = [index for index, _ in profiles]
        try:
            current_pos = positions.index(self.selected)
        except ValueError:
            current_pos = 0
        target_pos = max(0, min(len(positions) - 1, current_pos + delta))
        self.selected = positions[target_pos]

    def safe_add(self, win: Any, y: int, x: int, text: str, attr: int = 0) -> None:
        height, width = win.getmaxyx()
        if y < 0 or y >= height or x >= width:
            return
        try:
            win.addnstr(y, x, text, max(0, width - x - 1), attr)
        except Exception:
            pass

    def wrap_lines(self, text: str, width: int) -> list[str]:
        if width <= 4:
            return [text]
        source = text or ""
        lines: list[str] = []
        for part in source.splitlines() or [""]:
            wrapped = textwrap.wrap(part, width=width, break_long_words=True, break_on_hyphens=False)
            lines.extend(wrapped or [""])
        return lines or [""]

    def info_dialog(self, stdscr: Any, title: str, body: str) -> None:
        import curses

        max_width = max(20, min(stdscr.getmaxyx()[1] - 8, 120))
        wrapped_lines: list[str] = []
        for block in body.splitlines() or [""]:
            wrapped_lines.extend(self.wrap_lines(block, max_width - 4))
        if not wrapped_lines:
            wrapped_lines = [""]

        height = min(stdscr.getmaxyx()[0] - 4, max(8, min(len(wrapped_lines) + 4, stdscr.getmaxyx()[0] - 4)))
        width = min(
            stdscr.getmaxyx()[1] - 4,
            max(30, min(max((len(line) for line in wrapped_lines), default=10) + 4, max_width)),
        )
        top = max(1, (stdscr.getmaxyx()[0] - height) // 2)
        left = max(1, (stdscr.getmaxyx()[1] - width) // 2)
        win = curses.newwin(height, width, top, left)
        win.keypad(True)
        offset = 0
        page_size = max(1, height - 3)
        while True:
            win.erase()
            win.box()
            self.safe_add(win, 0, 2, f" {title} ", self.color(curses, "header", curses.A_BOLD))
            visible_lines = wrapped_lines[offset : offset + page_size]
            for idx, line in enumerate(visible_lines):
                self.safe_add(win, idx + 1, 2, line)
            footer = "Up/Down scroll | PgUp/PgDn page | Home/End | q/Esc close"
            if len(wrapped_lines) > page_size:
                footer = f"{offset + 1}-{min(offset + page_size, len(wrapped_lines))}/{len(wrapped_lines)} | {footer}"
            self.safe_add(win, height - 2, 2, excerpt(footer, width - 4), self.color(curses, "accent"))
            win.refresh()
            key = win.get_wch()
            if key in ("q", "Q", "\x1b", "\n", "\r", " "):
                return
            if key in (curses.KEY_UP, "k"):
                offset = max(0, offset - 1)
                continue
            if key in (curses.KEY_DOWN, "j"):
                offset = min(max(0, len(wrapped_lines) - page_size), offset + 1)
                continue
            if key == curses.KEY_PPAGE:
                offset = max(0, offset - page_size)
                continue
            if key == curses.KEY_NPAGE:
                offset = min(max(0, len(wrapped_lines) - page_size), offset + page_size)
                continue
            if key == curses.KEY_HOME:
                offset = 0
                continue
            if key == curses.KEY_END:
                offset = max(0, len(wrapped_lines) - page_size)
                continue

    def confirm_dialog(self, stdscr: Any, title: str, body: str) -> bool:
        import curses

        lines = self.wrap_lines(body, min(stdscr.getmaxyx()[1] - 10, 70))
        height = min(stdscr.getmaxyx()[0] - 4, max(7, len(lines) + 4))
        width = min(stdscr.getmaxyx()[1] - 4, max(30, max((len(line) for line in lines), default=10) + 4))
        top = max(1, (stdscr.getmaxyx()[0] - height) // 2)
        left = max(1, (stdscr.getmaxyx()[1] - width) // 2)
        win = curses.newwin(height, width, top, left)
        win.keypad(True)
        while True:
            win.erase()
            win.box()
            self.safe_add(win, 0, 2, f" {title} ", self.color(curses, "header", curses.A_BOLD))
            for idx, line in enumerate(lines[: height - 3]):
                self.safe_add(win, idx + 1, 2, line)
            self.safe_add(win, height - 2, 2, "y = yes | n = no | Esc = cancel", self.color(curses, "accent"))
            win.refresh()
            key = win.get_wch()
            if key in ("y", "Y"):
                return True
            if key in ("n", "N", "\x1b"):
                return False

    def input_dialog(
        self,
        stdscr: Any,
        title: str,
        prompt: str,
        initial: str = "",
        allow_empty: bool = True,
        secret: bool = False,
    ) -> str | None:
        import curses

        max_y, max_x = stdscr.getmaxyx()
        width = min(max_x - 4, max(50, len(prompt) + 10))
        height = 8
        top = max(1, (max_y - height) // 2)
        left = max(1, (max_x - width) // 2)
        win = curses.newwin(height, width, top, left)
        win.keypad(True)
        value = list(initial)
        pos = len(value)
        curses.curs_set(1)
        try:
            while True:
                win.erase()
                win.box()
                self.safe_add(win, 0, 2, f" {title} ", self.color(curses, "header", curses.A_BOLD))
                for idx, line in enumerate(self.wrap_lines(prompt, width - 4)[:2]):
                    self.safe_add(win, 1 + idx, 2, line)
                display = "".join("*" if secret else ch for ch in value)
                visible_width = width - 4
                start = max(0, pos - visible_width + 1)
                shown = display[start : start + visible_width]
                self.safe_add(win, 4, 2, shown)
                cursor_x = min(width - 3, 2 + pos - start)
                win.move(4, cursor_x)
                self.safe_add(win, height - 2, 2, "Enter = save | Esc = cancel", self.color(curses, "accent"))
                win.refresh()
                key = win.get_wch()
                if key in ("\n", "\r"):
                    result = "".join(value)
                    if result or allow_empty:
                        return result
                    curses.beep()
                    continue
                if key == "\x1b":
                    return None
                if key in (curses.KEY_BACKSPACE, "\b", "\x7f", "\x08"):
                    if pos > 0:
                        pos -= 1
                        del value[pos]
                    else:
                        curses.beep()
                    continue
                if key == curses.KEY_DC:
                    if pos < len(value):
                        del value[pos]
                    else:
                        curses.beep()
                    continue
                if key == curses.KEY_LEFT:
                    pos = max(0, pos - 1)
                    continue
                if key == curses.KEY_RIGHT:
                    pos = min(len(value), pos + 1)
                    continue
                if key == curses.KEY_HOME:
                    pos = 0
                    continue
                if key == curses.KEY_END:
                    pos = len(value)
                    continue
                if isinstance(key, str) and key.isprintable():
                    value.insert(pos, key)
                    pos += 1
        finally:
            curses.curs_set(0)

    def select_active(self) -> None:
        active_index = self.active_store_index()
        if active_index is not None:
            self.selected = active_index

    def action_use(self) -> None:
        profile = self.current_profile()
        if not profile:
            self.status = "No profile selected."
            return
        with store_lock(self.paths):
            store, _ = load_store_unlocked(self.paths)
            index = find_profile_index(store, profile["name"])
            target = store["profiles"][index]
            store["profiles"][index]["last_used_at"] = now_iso()
            store["profiles"][index]["updated_at"] = now_iso()
            write_store(self.paths, store)
        apply_profile(self.paths, target)
        self.status = f"Activated '{target['name']}'."
        self.refresh()
        self.select_active()

    def action_add(self, stdscr: Any) -> None:
        name = self.input_dialog(stdscr, "Add Profile", "Profile name:", allow_empty=False)
        if name is None:
            self.status = "Add cancelled."
            return
        url = self.input_dialog(stdscr, "Add Profile", "Relay base URL:", allow_empty=False)
        if url is None:
            self.status = "Add cancelled."
            return
        key = self.input_dialog(stdscr, "Add Profile", "API key:", allow_empty=False, secret=True)
        if key is None:
            self.status = "Add cancelled."
            return
        note = self.input_dialog(stdscr, "Add Profile", "Note:", allow_empty=True) or ""
        activate = self.confirm_dialog(stdscr, "Activate Now?", f"Activate '{name}' right after saving?")
        with store_lock(self.paths):
            store, _ = load_store_unlocked(self.paths)
            if profile_exists(store, name):
                raise RelayError(f"Profile already exists: {name}")
            profile = make_profile(name, url, key, note)
            store["profiles"].append(profile)
            write_store(self.paths, store)
        if activate:
            apply_profile(self.paths, profile)
            with store_lock(self.paths):
                store, _ = load_store_unlocked(self.paths)
                index = find_profile_index(store, name)
                store["profiles"][index]["last_used_at"] = now_iso()
                store["profiles"][index]["updated_at"] = now_iso()
                write_store(self.paths, store)
            self.status = f"Saved and activated '{name}'."
        else:
            self.status = f"Saved profile '{name}'."
        self.refresh()
        for idx, profile in enumerate(self.store.get("profiles", [])):
            if profile.get("name") == name:
                self.selected = idx
                break

    def run_with_terminal_handoff(
        self,
        stdscr: Any,
        title: str,
        message_lines: list[str],
        runner: Any,
    ) -> Any:
        import curses

        self.status = title
        self.draw(stdscr)
        stdscr.refresh()
        curses.def_prog_mode()
        curses.endwin()
        printed_notice = False
        try:
            notice = "\n".join(line for line in message_lines if line)
            if notice:
                print(notice)
                print("")
                printed_notice = True
            return runner()
        finally:
            if printed_notice:
                try:
                    print("")
                    input("Press Enter to return to codex-relay...")
                except EOFError:
                    pass
            curses.reset_prog_mode()
            try:
                curses.curs_set(0)
            except Exception:
                pass
            stdscr.erase()
            stdscr.refresh()

    def action_login_official(self, stdscr: Any) -> None:
        name = self.input_dialog(
            stdscr,
            "Official Login",
            "Profile name for the new official subscription:",
            allow_empty=False,
        )
        if name is None:
            self.status = "Official login cancelled."
            return
        note = self.input_dialog(stdscr, "Official Login", "Note:", allow_empty=True) or ""
        activate = self.confirm_dialog(
            stdscr,
            "Activate Now?",
            "Activate this official profile right after login succeeds?",
        )
        with store_lock(self.paths):
            store, _ = load_store_unlocked(self.paths)
            if profile_exists(store, name):
                raise RelayError(f"Profile already exists: {name}")
        profile = self.run_with_terminal_handoff(
            stdscr,
            "Launching native `codex login --device-auth` in an isolated profile...",
            [
                "Native Codex login is taking over this terminal temporarily.",
                "If the browser opens `auth.openai.com/log-in` first, that is expected.",
                "Sign in there first, then continue the Codex device authorization flow.",
                "Complete the official device login in your browser.",
                "This runs in an isolated CODEX_HOME and will only be saved if validation succeeds.",
            ],
            lambda: build_official_profile_via_codex_login(self.paths, name, note),
        )
        with store_lock(self.paths):
            store, _ = load_store_unlocked(self.paths)
            if profile_exists(store, name):
                raise RelayError(f"Profile already exists: {name}")
            store["profiles"].append(profile)
            write_store(self.paths, store)
        if activate:
            apply_profile(self.paths, profile)
            with store_lock(self.paths):
                store, _ = load_store_unlocked(self.paths)
                index = find_profile_index(store, profile["name"])
                store["profiles"][index]["last_used_at"] = now_iso()
                store["profiles"][index]["updated_at"] = now_iso()
                write_store(self.paths, store)
            self.status = f"Logged in and activated '{profile['name']}'."
        else:
            self.status = f"Logged in and saved official profile '{profile['name']}'."
        self.refresh()
        for idx, item in enumerate(self.store.get("profiles", [])):
            if item.get("name") == profile["name"]:
                self.selected = idx
                break

    def action_import(self, stdscr: Any) -> None:
        source = self.input_dialog(
            stdscr,
            "Import Profile",
            "Directory containing config.toml and auth.json:",
            allow_empty=False,
        )
        if source is None:
            self.status = "Import cancelled."
            return
        name = self.input_dialog(
            stdscr,
            "Import Profile",
            "Profile name (leave empty to auto-suggest):",
            allow_empty=True,
        )
        if name is None:
            self.status = "Import cancelled."
            return
        note = self.input_dialog(stdscr, "Import Profile", "Note:", allow_empty=True) or ""
        activate = self.confirm_dialog(stdscr, "Activate Now?", "Activate this imported profile now?")
        source_root = Path(source).expanduser().resolve()
        with store_lock(self.paths):
            store, _ = load_store_unlocked(self.paths)
            target_name = name.strip()
            if not target_name:
                imported_state = read_live_state(build_paths(source_root))
                target_name = suggest_name_from_state(store, imported_state, source_root)
            profile = import_profile_from_directory(store, target_name, source_root, note)
            store["profiles"].append(profile)
            write_store(self.paths, store)
        if activate:
            apply_profile(self.paths, profile)
            with store_lock(self.paths):
                store, _ = load_store_unlocked(self.paths)
                index = find_profile_index(store, profile["name"])
                store["profiles"][index]["last_used_at"] = now_iso()
                store["profiles"][index]["updated_at"] = now_iso()
                write_store(self.paths, store)
            self.status = f"Imported and activated '{profile['name']}'."
        else:
            self.status = f"Imported {profile_type(profile)} profile '{profile['name']}'."
        self.refresh()
        for idx, item in enumerate(self.store.get("profiles", [])):
            if item.get("name") == profile["name"]:
                self.selected = idx
                break

    def action_edit(self, stdscr: Any) -> None:
        profile = self.current_profile()
        if not profile:
            self.status = "No profile selected."
            return
        original_name = profile["name"]
        name = self.input_dialog(stdscr, "Edit Profile", "Profile name:", profile["name"], allow_empty=False)
        if name is None:
            self.status = "Edit cancelled."
            return
        kind = profile_type(profile)
        url = None
        key = None
        if kind == "relay":
            url = self.input_dialog(
                stdscr,
                "Edit Profile",
                "Relay base URL:",
                profile.get("base_url", ""),
                allow_empty=False,
            )
            if url is None:
                self.status = "Edit cancelled."
                return
            key = self.input_dialog(
                stdscr,
                "Edit Profile",
                "API key:",
                profile.get("api_key", ""),
                allow_empty=False,
                secret=True,
            )
            if key is None:
                self.status = "Edit cancelled."
                return
        note = self.input_dialog(stdscr, "Edit Profile", "Note:", profile.get("note", ""), allow_empty=True)
        live_state = read_live_state(self.paths)
        apply_live = False
        updated_profile: dict[str, Any] | None = None
        with store_lock(self.paths):
            store, _ = load_store_unlocked(self.paths)
            index = find_profile_index(store, original_name)
            target = store["profiles"][index]
            old_signature = profile_signature(target)
            if name != original_name and profile_exists(store, name):
                raise RelayError(f"Profile already exists: {name}")
            target["name"] = name
            if kind == "relay":
                target["base_url"] = normalize_url(url or "")
                target["api_key"] = (key or "").strip()
            target["note"] = (note or "").strip()
            target["updated_at"] = now_iso()
            updated_profile = dict(target)
            apply_live = (
                kind == "relay"
                and old_signature == live_signature(live_state)
                and (
                    target.get("base_url") != profile.get("base_url")
                    or target.get("api_key") != profile.get("api_key")
                )
            )
            write_store(self.paths, store)
        if apply_live and updated_profile is not None:
            apply_profile(self.paths, updated_profile)
            with store_lock(self.paths):
                store, _ = load_store_unlocked(self.paths)
                idx = find_profile_index(store, name)
                store["profiles"][idx]["last_used_at"] = now_iso()
                write_store(self.paths, store)
            self.status = f"Updated '{name}' and live config."
        else:
            self.status = f"Updated '{name}'."
        self.refresh()
        for idx, item in enumerate(self.store.get("profiles", [])):
            if item.get("name") == name:
                self.selected = idx
                break

    def action_remove(self, stdscr: Any) -> None:
        profile = self.current_profile()
        if not profile:
            self.status = "No profile selected."
            return
        if not self.confirm_dialog(stdscr, "Delete Profile", f"Delete '{profile['name']}' from the saved list?"):
            self.status = "Delete cancelled."
            return
        live_state = read_live_state(self.paths)
        active_signature = live_signature(live_state)
        with store_lock(self.paths):
            store, _ = load_store_unlocked(self.paths)
            index = find_profile_index(store, profile["name"])
            target = store["profiles"][index]
            was_active = profile_signature(target) == active_signature
            del store["profiles"][index]
            write_store(self.paths, store)
        self.refresh()
        self.status = f"Removed '{profile['name']}'."
        if was_active:
            self.status += " Live config still points there until you switch."

    def action_save_current(self, stdscr: Any) -> None:
        live_state = read_live_state(self.paths)
        if live_state.get("type") not in {"relay", "official"}:
            raise RelayError(
                "Current live config is neither a relay profile nor an official subscription."
            )
        name = self.input_dialog(stdscr, "Save Current", "Profile name for current live config:", allow_empty=False)
        if name is None:
            self.status = "Save-current cancelled."
            return
        note = self.input_dialog(stdscr, "Save Current", "Note:", allow_empty=True) or ""
        with store_lock(self.paths):
            store, _ = load_store_unlocked(self.paths)
            if profile_exists(store, name):
                raise RelayError(f"Profile already exists: {name}")
            profile = build_profile_from_state(name, live_state, note, source_path=str(self.paths.codex_home))
            profile["last_used_at"] = now_iso()
            store["profiles"].append(profile)
            write_store(self.paths, store)
        self.refresh()
        for idx, item in enumerate(self.store.get("profiles", [])):
            if item.get("name") == name:
                self.selected = idx
                break
        self.status = f"Saved current {profile_type(profile)} config as '{name}'."

    def action_probe(self, stdscr: Any, all_profiles: bool) -> None:
        profiles = self.visible_profiles()
        if not profiles:
            self.status = "No profiles to probe."
            return
        if all_profiles:
            targets = profiles
            label = "all visible profiles" if self.filter_text else "all profiles"
        else:
            marked = self.marked_target_entries()
            if marked:
                targets = marked
                label = f"{len(marked)} marked profile(s)"
            else:
                current = self.current_profile_entry()
                if not current:
                    self.status = "No profile selected."
                    return
                targets = [current]
                label = current[1]["name"]
        self.status = f"Probing {label} via {self.probe_via}..."
        self.draw(stdscr)
        stdscr.refresh()
        results, overall_ok = execute_probe(
            self.paths,
            targets,
            via=self.probe_via,
            message=self.probe_message,
            expect=self.probe_expect or None,
        )
        self.refresh()
        status_label = "OK" if overall_ok else "Needs Attention"
        lines = [f"Probe target: {label}", f"Mode: {self.probe_via}", ""]
        for profile, methods_result, methods in results:
            badge, _ = summarize_probe_status(methods_result, methods)
            lines.append(f"[{badge}] {profile['name']}")
            for method in methods:
                result = methods_result.get(method)
                if not result:
                    continue
                code = result.get("status_code")
                latency = result.get("latency_ms")
                reply = result.get("reply") or "-"
                lines.append(f"  {method}: {'ok' if result['ok'] else 'fail'} | code={code if code is not None else '-'} | time={latency}ms")
                lines.extend(f"    {line}" for line in self.wrap_lines(f"reply: {reply}", 100))
                if not result["ok"] and result.get("detail"):
                    lines.extend(f"    {line}" for line in self.wrap_lines(f"detail: {result['detail']}", 100))
            lines.append("")
        self.info_dialog(stdscr, f"Probe Results - {status_label}", "\n".join(lines).strip())
        self.status = f"Probe finished for {label}."

    def action_message(self, stdscr: Any) -> None:
        result = self.input_dialog(
            stdscr,
            "Probe Message",
            "Message sent to the model during probe:",
            self.probe_message,
            allow_empty=True,
        )
        if result is not None:
            self.probe_message = result
            self.status = "Updated probe message."
        else:
            self.status = "Probe message unchanged."

    def action_expect(self, stdscr: Any) -> None:
        result = self.input_dialog(
            stdscr,
            "Probe Expect",
            "Optional expected substring. Empty disables it:",
            self.probe_expect,
            allow_empty=True,
        )
        if result is not None:
            self.probe_expect = result
            self.status = "Updated expected substring."
        else:
            self.status = "Expected substring unchanged."

    def action_search(self, stdscr: Any) -> None:
        result = self.input_dialog(
            stdscr,
            "Search Profiles",
            "Filter by name, URL, or note. Empty clears the filter:",
            self.filter_text,
            allow_empty=True,
        )
        if result is None:
            self.status = "Search unchanged."
            return
        self.filter_text = result.strip()
        self.refresh()
        if self.filter_text:
            count = len(self.visible_profiles())
            self.status = f"Filter applied: {count} match(es)."
        else:
            self.status = "Filter cleared."

    def cycle_type_filter(self) -> None:
        order = ["all", "relay", "official"]
        self.type_filter = order[(order.index(self.type_filter) + 1) % len(order)]
        self.refresh()
        self.status = f"Type filter set to {self.type_filter}."

    def set_type_filter(self, value: str) -> None:
        if value not in {"all", "relay", "official"}:
            return
        self.type_filter = value
        self.refresh()
        self.status = f"Type filter set to {self.type_filter}."

    def clear_search(self) -> None:
        self.filter_text = ""
        self.refresh()
        self.status = "Filter cleared."

    def action_details(self, stdscr: Any) -> None:
        profile = self.current_profile()
        if not profile:
            self.status = "No profile selected."
            return
        lines = [
            f"Name: {profile.get('name')}",
            f"Type: {profile_type(profile)}",
            f"Target: {profile_display_target(profile)}",
            f"Auth: {profile_display_secret(profile)}",
            f"Note: {profile.get('note') or '-'}",
            f"Created: {profile.get('created_at') or '-'}",
            f"Updated: {profile.get('updated_at') or '-'}",
            f"Last used: {profile.get('last_used_at') or '-'}",
        ]
        if profile_type(profile) == "official" and profile.get("source_path"):
            lines.append(f"Source: {profile.get('source_path')}")
        last_probe = profile.get("last_probe")
        methods = last_probe.get("methods") if isinstance(last_probe, dict) else None
        if isinstance(methods, dict):
            lines.append("")
            lines.append("Last probe:")
            for method in ("http", "codex"):
                entry = methods.get(method)
                if not isinstance(entry, dict):
                    continue
                lines.append(
                    f"  {method}: {'ok' if entry.get('ok') else 'fail'} | "
                    f"code={entry.get('status_code') if entry.get('status_code') is not None else '-'} | "
                    f"time={entry.get('latency_ms') if entry.get('latency_ms') is not None else '-'}ms"
                )
                if entry.get("reply"):
                    lines.extend(f"    {line}" for line in self.wrap_lines(f"reply: {entry['reply']}", 70))
                if entry.get("detail") and entry.get("detail") != entry.get("reply"):
                    lines.extend(f"    {line}" for line in self.wrap_lines(f"detail: {entry['detail']}", 70))
        self.info_dialog(stdscr, f"Profile Details - {profile['name']}", "\n".join(lines))
        self.status = f"Viewed details for '{profile['name']}'."

    def cycle_via(self) -> None:
        order = ["both", "http", "codex"]
        self.probe_via = order[(order.index(self.probe_via) + 1) % len(order)]
        self.status = f"Probe mode set to {self.probe_via}."

    def help_text(self) -> str:
        return "\n".join(
            [
                "Up/Down or j/k : move selection",
                "PgUp/PgDn      : jump faster through the list",
                "Home/End       : jump to the first/last visible profile",
                "Enter or u     : activate selected profile",
                "a              : add relay profile",
                "o              : add official profile via native codex login",
                "I              : import a profile snapshot from a directory",
                "e              : edit selected profile",
                "d              : delete selected profile",
                "s              : save current live config as profile",
                "Space          : mark/unmark selected profile for probe",
                "A              : mark or unmark all visible profiles",
                "C              : clear all probe marks",
                "p              : probe marked profiles, or current if none marked",
                "P              : probe all profiles",
                "v              : cycle probe mode (both/http/codex)",
                "t              : cycle type filter (all/relay/official)",
                "Tab/Shift-Tab   : switch the top type tab",
                "1/2/3          : jump to All/Relay/Official tab",
                "m              : edit probe message",
                "x              : edit expected substring",
                "/              : search/filter profiles",
                "c              : clear filter text",
                "i              : open full details for selected profile",
                "g              : jump to active profile",
                "r              : refresh",
                "h or ?         : help",
                "q              : quit",
            ]
        )

    def draw(self, stdscr: Any) -> None:
        import curses

        self.init_colors(curses)
        stdscr.erase()
        height, width = stdscr.getmaxyx()
        if height < 18 or width < 76:
            header_attr = self.color(curses, "status", curses.A_REVERSE | curses.A_BOLD)
            self.safe_add(stdscr, 0, 0, " " * max(1, width - 1), header_attr)
            self.safe_add(stdscr, 0, 1, "codex-relay", header_attr | curses.A_BOLD)
            self.safe_add(stdscr, 2, 0, "Terminal too small for the full TUI. Resize to at least 76x18.")
            self.safe_add(stdscr, 3, 0, "You can still use q to quit, or run list/current/probe from the CLI.")
            stdscr.refresh()
            return

        left_width = max(30, min(42, width // 3))
        right_x = left_width + 2
        content_top = 5
        footer_top = height - 3
        content_height = max(1, footer_top - content_top - 1)
        active_sig = live_signature(self.live_state)
        profiles = self.visible_profiles()
        all_profiles = self.store.get("profiles", [])
        total_profiles = len(all_profiles)
        active_index = self.active_store_index()
        active_profile = all_profiles[active_index] if active_index is not None and active_index < total_profiles else None
        active_name = active_profile.get("name") if active_profile else "-"

        header_attr = self.color(curses, "status", curses.A_REVERSE | curses.A_BOLD)
        section_attr = self.color(curses, "header", curses.A_BOLD)
        accent_attr = self.color(curses, "accent", curses.A_BOLD)
        muted_attr = curses.A_DIM
        selected_attr = self.color(curses, "selected", curses.A_REVERSE)
        active_attr = self.color(curses, "active", curses.A_BOLD)

        self.safe_add(stdscr, 0, 0, " " * max(1, width - 1), header_attr)
        self.safe_add(stdscr, 0, 1, "codex-relay", header_attr | curses.A_BOLD)
        header_summary = f"{len(profiles)}/{total_profiles} visible"
        if self.filter_text:
            header_summary += " | filtered"
        if self.type_filter != "all":
            header_summary += f" | type={self.type_filter}"
        self.safe_add(
            stdscr,
            0,
            max(1, width - len(header_summary) - 2),
            header_summary,
            header_attr,
        )

        title = (
            f"Active: {active_name} | Provider: {self.live_state.get('provider_name') or '-'} | "
            f"Model: {self.live_state.get('model') or '-'}"
        )
        self.safe_add(stdscr, 1, 0, excerpt(title, width - 1), section_attr)

        probe_cfg = f"Probe via {self.probe_via} | message={self.probe_message or '(empty)'}"
        if self.probe_expect:
            probe_cfg += f" | expect={self.probe_expect}"
        self.safe_add(stdscr, 2, 0, excerpt(probe_cfg, width - 1), accent_attr)

        tabs = [("all", "All"), ("relay", "Relay"), ("official", "Official")]
        tab_x = 0
        for value, label in tabs:
            is_active_tab = self.type_filter == value
            tab_text = f" {label} "
            tab_attr = selected_attr | curses.A_BOLD if is_active_tab else section_attr
            self.safe_add(stdscr, 3, tab_x, tab_text, tab_attr)
            tab_x += len(tab_text) + 1
        tabs_hint = "Tab/Shift-Tab or 1/2/3"
        self.safe_add(stdscr, 3, max(tab_x + 1, width - len(tabs_hint) - 1), tabs_hint, muted_attr)

        stdscr.hline(4, 0, curses.ACS_HLINE, max(1, width - 1))
        stdscr.hline(height - 4, 0, curses.ACS_HLINE, max(1, width - 1))
        stdscr.vline(content_top, left_width, curses.ACS_VLINE, max(1, height - 9))

        profiles_title = f"Profiles ({len(profiles)})"
        if self.filter_text:
            profiles_title += f" | / {excerpt(self.filter_text, max(10, left_width - 18))}"
        self.safe_add(stdscr, 4, 0, excerpt(profiles_title, left_width - 1), section_attr)

        detail_header = "Details"
        current_entry = self.current_profile_entry()
        if current_entry:
            detail_header = f"Details | {current_entry[1].get('name')}"
        self.safe_add(stdscr, 4, right_x, excerpt(detail_header, width - right_x - 1), section_attr)

        list_top = content_top
        list_height = content_height
        if not profiles:
            empty_text = "No matching profiles."
            if total_profiles and self.filter_text:
                empty_text = "No matches. Press 'c' to clear the filter."
            elif not total_profiles:
                empty_text = "No profiles yet. Press 'a' to add your first one."
            self.safe_add(stdscr, list_top, 0, excerpt(empty_text, left_width - 1), muted_attr)
        else:
            positions = [index for index, _ in profiles]
            try:
                selected_pos = positions.index(self.selected)
            except ValueError:
                selected_pos = 0
                self.selected = positions[0]
            start = 0
            if selected_pos >= list_height:
                start = selected_pos - list_height + 1
            visible = profiles[start : start + list_height]
            for offset, (store_index, profile) in enumerate(visible):
                row = list_top + offset
                marker = "*" if profile_signature(profile) == active_sig else " "
                marked = "+" if profile.get("name") in self.marked_profiles else " "
                kind = profile_type(profile)[0].upper()
                line = f"{marker}{marked} [{store_index + 1:>2}] [{kind}] {profile['name']}"
                display_target = profile_display_target(profile)
                url_excerpt = (
                    urllib.parse.urlparse(display_target).netloc
                    if isinstance(display_target, str) and display_target.startswith("http")
                    else display_target
                ) or "-"
                remaining = max(8, left_width - len(line) - 4)
                line = f"{line}  {excerpt(url_excerpt, remaining)}"
                attr = 0
                if store_index == self.selected:
                    attr = selected_attr | curses.A_BOLD
                elif marker == "*":
                    attr = active_attr
                self.safe_add(stdscr, row, 0, excerpt(line, left_width - 1), attr)

        profile = self.current_profile()
        detail_y = content_top
        detail_width = width - right_x - 1
        if profile:
            is_active = profile_signature(profile) == active_sig
            is_marked = profile.get("name") in self.marked_profiles
            detail_lines = [
                f"Name   : {profile.get('name')}",
                f"Type   : {profile_type(profile)}",
                f"Target : {profile_display_target(profile)}",
                f"Auth   : {profile_display_secret(profile)}",
                f"Active : {'yes' if is_active else 'no'}",
                f"Marked : {'yes' if is_marked else 'no'}",
                f"Note   : {profile.get('note') or '-'}",
                f"Created: {profile.get('created_at') or '-'}",
                f"Updated: {profile.get('updated_at') or '-'}",
                f"Used   : {profile.get('last_used_at') or 'never'}",
                f"Probe  : {format_probe(profile.get('last_probe'))}",
            ]
            if profile_type(profile) == "official" and profile.get("source_path"):
                detail_lines.append(f"Source : {profile.get('source_path')}")
            last_probe = profile.get("last_probe")
            methods = last_probe.get("methods") if isinstance(last_probe, dict) else None
            if isinstance(methods, dict):
                detail_lines.append("")
                detail_lines.append("Last replies:")
                for method in ("http", "codex"):
                    entry = methods.get(method)
                    if not isinstance(entry, dict):
                        continue
                    code = entry.get("status_code")
                    latency = entry.get("latency_ms")
                    detail_lines.append(
                        f"{method.upper():<5}: {'ok' if entry.get('ok') else 'fail'} | "
                        f"code={code if code is not None else '-'} | "
                        f"time={latency if latency is not None else '-'}ms"
                    )
                    reply = entry.get("reply")
                    if reply:
                        detail_lines.extend(
                            self.wrap_lines(
                                f"Reply : {excerpt(reply, RIGHT_PANEL_TEXT_LIMIT)}",
                                max(20, detail_width),
                            )
                        )
                    detail = entry.get("detail")
                    if detail and detail != reply:
                        detail_lines.extend(
                            self.wrap_lines(
                                f"Detail: {excerpt(detail, RIGHT_PANEL_TEXT_LIMIT)}",
                                max(20, detail_width),
                            )
                        )
            detail_lines.extend(
                [
                    "",
                    f"Visible filter : {self.filter_text or '(none)'}",
                    f"Type filter    : {self.type_filter}",
                    f"Marked count   : {len(self.marked_profiles)}",
                    "Shortcuts      : Space mark | p probe marked/current | P probe-all | i full details",
                ]
            )
            row = detail_y
            for line in detail_lines:
                wrapped = self.wrap_lines(line, max(20, detail_width))
                for piece in wrapped:
                    if row >= height - 4:
                        break
                    self.safe_add(stdscr, row, right_x, piece)
                    row += 1
                if row >= height - 4:
                    break
        else:
            self.safe_add(
                stdscr,
                detail_y,
                right_x,
                "Select a profile on the left to view more details.",
                muted_attr,
            )

        footer_primary = "Enter use | Space mark | A mark-all-visible | C clear-marks | p probe marked/current | P probe-all | v mode | t type"
        footer_secondary = "a relay | o official-login | I import | e edit | d delete | s save-current | m message | x expect | g active | r refresh | h help | q quit"
        self.safe_add(stdscr, height - 3, 0, excerpt(footer_primary, width - 1), accent_attr)
        self.safe_add(stdscr, height - 2, 0, excerpt(footer_secondary, width - 1), muted_attr)
        status_attr = self.status_attr(curses) | curses.A_BOLD
        self.safe_add(stdscr, height - 1, 0, " " * max(1, width - 1), status_attr)
        self.safe_add(stdscr, height - 1, 0, excerpt(f"Status: {self.status}", width - 1), status_attr)
        stdscr.refresh()

    def handle_key(self, stdscr: Any, key: Any) -> None:
        import curses

        profiles = self.visible_profiles()
        if key in ("q", "Q"):
            self.should_exit = True
            return
        if key in ("KEY_RESIZE", curses.KEY_RESIZE):
            return
        if key in (curses.KEY_UP, "k"):
            self.move_selection(-1)
            return
        if key in (curses.KEY_DOWN, "j"):
            self.move_selection(1)
            return
        if key == curses.KEY_PPAGE:
            self.move_selection(-10)
            return
        if key == curses.KEY_NPAGE:
            self.move_selection(10)
            return
        if key == curses.KEY_HOME:
            if profiles:
                self.selected = profiles[0][0]
            return
        if key == curses.KEY_END:
            if profiles:
                self.selected = profiles[-1][0]
            return
        if key == "\t":
            self.cycle_type_filter()
            return
        if key == curses.KEY_BTAB:
            order = ["all", "relay", "official"]
            current_pos = order.index(self.type_filter)
            self.set_type_filter(order[(current_pos - 1) % len(order)])
            return
        if key == "1":
            self.set_type_filter("all")
            return
        if key == "2":
            self.set_type_filter("relay")
            return
        if key == "3":
            self.set_type_filter("official")
            return
        if key in ("\n", "\r", curses.KEY_ENTER, "u"):
            self.action_use()
            return
        if key == " ":
            self.toggle_mark_current()
            return
        if key == "A":
            self.toggle_mark_visible()
            return
        if key == "a":
            self.action_add(stdscr)
            return
        if key in ("o", "O"):
            self.action_login_official(stdscr)
            return
        if key == "I":
            self.action_import(stdscr)
            return
        if key == "e":
            self.action_edit(stdscr)
            return
        if key == "d":
            self.action_remove(stdscr)
            return
        if key == "s":
            self.action_save_current(stdscr)
            return
        if key == "p":
            self.action_probe(stdscr, all_profiles=False)
            return
        if key == "P":
            self.action_probe(stdscr, all_profiles=True)
            return
        if key == "v":
            self.cycle_via()
            return
        if key == "t":
            self.cycle_type_filter()
            return
        if key == "m":
            self.action_message(stdscr)
            return
        if key == "x":
            self.action_expect(stdscr)
            return
        if key == "/":
            self.action_search(stdscr)
            return
        if key in ("c", "C"):
            if key == "C":
                self.clear_marked()
            else:
                self.clear_search()
            return
        if key in ("i", "I"):
            self.action_details(stdscr)
            return
        if key == "g":
            self.select_active()
            self.status = "Jumped to active profile."
            return
        if key in ("r",):
            self.refresh()
            self.status = "Refreshed."
            return
        if key in ("h", "H", "?"):
            self.info_dialog(stdscr, "Help", self.help_text())
            self.status = "Help closed."
            return

    def run(self, stdscr: Any) -> int:
        import curses

        curses.curs_set(0)
        stdscr.keypad(True)
        self.init_colors(curses)
        self.refresh()
        self.select_active()
        while not self.should_exit:
            self.draw(stdscr)
            try:
                key = stdscr.get_wch()
            except KeyboardInterrupt:
                self.should_exit = True
                break
            except Exception:
                continue
            try:
                self.handle_key(stdscr, key)
            except RelayError as exc:
                self.status = str(exc)
            except Exception as exc:
                self.status = f"Unexpected error: {exc}"
        return 0


def cmd_tui(args: argparse.Namespace) -> int:
    import curses

    app = RelayTUI(build_paths(args.codex_home))
    return curses.wrapper(app.run)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="codex-relay",
        description="Manage multiple Codex relay and official subscription profiles from the command line.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              codex-relay list
              codex-relay add relay-a --url https://relay.example.com --key sk-example --note "primary relay"
              codex-relay login-official official-main
              codex-relay import ~/.codex-backup/example-codex --name official-backup
              codex-relay use relay-a
              codex-relay edit relay-a --note "faster today"
              codex-relay probe-all
              codex-relay probe relay-a relay-b --via codex
            """
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {VERSION}",
    )
    parser.add_argument(
        "--codex-home",
        default="~/.codex",
        help="Override the Codex home directory. Default: ~/.codex",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="List saved relay and official profiles.")
    list_parser.set_defaults(func=cmd_list)

    current_parser = subparsers.add_parser("current", help="Show the currently active live Codex profile.")
    current_parser.set_defaults(func=cmd_current)

    tui_parser = subparsers.add_parser("tui", aliases=["ui"], help="Launch the interactive profile manager.")
    tui_parser.set_defaults(func=cmd_tui)

    add_parser = subparsers.add_parser("add", help="Add a new relay profile.")
    add_parser.add_argument("name", help="Profile name.")
    add_parser.add_argument("--url", required=True, help="Relay base URL.")
    add_parser.add_argument("--key", required=True, help="API key for this relay.")
    add_parser.add_argument("--note", default="", help="Optional note.")
    add_parser.add_argument(
        "--activate",
        action="store_true",
        help="Activate the profile immediately after saving it.",
    )
    add_parser.set_defaults(func=cmd_add)

    save_current_parser = subparsers.add_parser(
        "save-current",
        help="Save the current live relay or official Codex config as a named profile.",
    )
    save_current_parser.add_argument("name", help="Profile name.")
    save_current_parser.add_argument("--note", default="", help="Optional note.")
    save_current_parser.set_defaults(func=cmd_save_current)

    login_official_parser = subparsers.add_parser(
        "login-official",
        help="Create an official profile via native `codex login --device-auth` in an isolated directory.",
    )
    login_official_parser.add_argument("name", help="Profile name.")
    login_official_parser.add_argument("--note", default="", help="Optional note.")
    login_official_parser.add_argument(
        "--activate",
        action="store_true",
        help="Activate the official profile immediately after login succeeds.",
    )
    login_official_parser.set_defaults(func=cmd_login_official)

    import_parser = subparsers.add_parser(
        "import",
        help="Import a relay or official profile snapshot from another Codex directory.",
    )
    import_parser.add_argument("source", help="Directory containing config.toml and auth.json.")
    import_parser.add_argument("--name", help="Profile name. Defaults to an auto-generated name.")
    import_parser.add_argument("--note", default="", help="Optional note.")
    import_parser.add_argument(
        "--activate",
        action="store_true",
        help="Activate the imported profile immediately.",
    )
    import_parser.set_defaults(func=cmd_import)

    use_parser = subparsers.add_parser("use", help="Activate a saved profile.")
    use_parser.add_argument("target", nargs="?", help="Profile name.")
    use_parser.add_argument("--index", type=int, help="Profile index from `list`.")
    use_parser.set_defaults(func=cmd_use)

    remove_parser = subparsers.add_parser("remove", help="Delete a saved profile.")
    remove_parser.add_argument("target", nargs="?", help="Profile name.")
    remove_parser.add_argument("--index", type=int, help="Profile index from `list`.")
    remove_parser.set_defaults(func=cmd_remove)

    edit_parser = subparsers.add_parser("edit", help="Edit a saved profile.")
    edit_parser.add_argument("target", nargs="?", help="Profile name.")
    edit_parser.add_argument("--index", type=int, help="Profile index from `list`.")
    edit_parser.add_argument("--rename", help="Rename the profile.")
    edit_parser.add_argument("--url", help="Update the relay base URL. Relay profiles only.")
    edit_parser.add_argument("--key", help="Update the API key. Relay profiles only.")
    edit_parser.add_argument("--note", help="Replace the note. Pass an empty string to clear it.")
    edit_parser.set_defaults(func=cmd_edit)

    probe_parent = argparse.ArgumentParser(add_help=False)
    probe_parent.add_argument("targets", nargs="*", help="Profile names to probe.")
    probe_parent.add_argument(
        "--index",
        dest="indexes",
        action="append",
        type=int,
        help="Profile index from `list`. Repeatable.",
    )
    probe_parent.add_argument(
        "--via",
        choices=["http", "codex", "both"],
        default="both",
        help="Probe via HTTP, via `codex exec`, or both. Default: both.",
    )
    probe_parent.add_argument(
        "--message",
        default=DEFAULT_MESSAGE,
        help=f"Prompt used for probe requests. Default: {DEFAULT_MESSAGE!r}",
    )
    probe_parent.add_argument(
        "--expect",
        help="Optional substring that must appear in the response for the probe to count as healthy.",
    )
    probe_parent.add_argument(
        "--model",
        help="Override the model used for the probe. Defaults to the live Codex model.",
    )
    probe_parent.add_argument(
        "--timeout",
        type=float,
        help="Timeout in seconds. Defaults to 20 for HTTP and 90 for Codex mode.",
    )
    probe_parent.add_argument(
        "--workers",
        type=int,
        help="Number of concurrent workers. Defaults to 8 for HTTP and 3 for Codex mode.",
    )

    probe_parser = subparsers.add_parser(
        "probe",
        parents=[probe_parent],
        help="Probe one or more saved profiles.",
    )
    probe_parser.set_defaults(func=cmd_probe)

    probe_all_parser = subparsers.add_parser(
        "probe-all",
        parents=[probe_parent],
        help="Probe all saved profiles concurrently.",
    )
    probe_all_parser.set_defaults(func=cmd_probe_all)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except RelayError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
