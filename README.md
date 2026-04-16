<div align="center">
  <img src="assets/logo.svg" alt="codex-relay logo" width="600" />
</div>

# codex-relay

`codex-relay` is a zero-dependency CLI and TUI for managing multiple Codex profiles.
It supports both relay-style API-key endpoints and official Codex subscriptions, so you can switch safely without hand-editing `~/.codex/config.toml` and `~/.codex/auth.json`.

Chinese documentation: [README.zh-CN.md](README.zh-CN.md)

## Overview

Many Codex users switch between:

- relay endpoints that require `base_url + OPENAI_API_KEY`
- official Codex subscriptions that use `auth_mode + tokens`

Those two states are not stored in the same shape, so manual switching is error-prone. `codex-relay` turns that into a repeatable workflow:

- save multiple relay and official profiles
- switch between them safely
- preserve notes and usage history
- probe profiles through real Codex-compatible paths
- manage everything from either the CLI or a built-in TUI

## Core features

- Zero third-party runtime dependencies
- Works directly with the existing `~/.codex` directory
- Supports two profile types: `relay` and `official`
- Automatically migrates legacy relay-only stores
- Switches profiles by updating only the relevant config and auth fields
- Preserves official auth snapshots for safe round-trips
- Supports importing snapshots from another Codex directory
- Supports creating official profiles via native `codex login --device-auth`
- Backs up the live config before switching
- Supports both HTTP probes and real `codex exec` probes
- Skips HTTP probing automatically for official profiles
- Keeps probe failures isolated so one broken profile does not abort a batch
- Includes a TUI with type tabs, filters, multi-select probe targets, and scrollable result dialogs

## Requirements

- Python 3.11 or newer
- Codex CLI installed locally if you want to use:
  - `login-official`
  - `--via codex`
  - TUI official-login flow
- A Linux, macOS, or other `curses`-capable environment for the TUI

## Installation

### Option 1: run from source

```bash
git clone https://github.com/yalishiduozhang/codex-relay.git
cd codex-relay
PYTHONPATH=src python -m codex_relay --help
```

### Option 2: install as a local command

```bash
git clone https://github.com/yalishiduozhang/codex-relay.git
cd codex-relay
python -m pip install -e .
codex-relay --help
```

## Quick start

### Inspect the current state

```bash
codex-relay current
codex-relay list
```

### Add a relay profile

```bash
codex-relay add relay-a \
  --url https://relay.example.com \
  --key sk-example \
  --note "Primary relay"
```

### Save the current live official or relay config

```bash
codex-relay save-current snapshot-1 --note "Known good state"
```

### Create an official profile via native login

```bash
codex-relay login-official official-main
```

### Import a profile snapshot from another Codex directory

```bash
codex-relay import ~/.codex-backup/example-codex --name official-backup
```

### Activate a saved profile

```bash
codex-relay use official-main
codex-relay use relay-a
```

### Probe profiles

```bash
codex-relay probe relay-a
codex-relay probe-all
```

### Open the interactive UI

```bash
codex-relay tui
```

## Profile types

### Relay profiles

Relay profiles store:

- `base_url`
- `api_key`
- notes
- probe history

### Official profiles

Official profiles store:

- `auth_snapshot` copied from `auth.json`
- a normalized config snapshot for provider-related state
- `auth_mode`
- an official account identifier summary when available
- notes
- probe history

## Official login flow

To create a new official profile, `codex-relay` can launch native `codex login --device-auth` in an isolated `CODEX_HOME`.

That means:

- your live `~/.codex` is not overwritten during login
- browser login is still handled by native Codex
- the profile is saved only if validation succeeds

In the TUI, the official login flow temporarily hands control back to the terminal, runs native Codex login, and then returns to the TUI cleanly.

## Probe behavior

### Available probe modes

- `http`
- `codex`
- `both`

### Relay probes

Relay profiles can use both probe modes:

- HTTP probe against Responses-style endpoints
- real `codex exec` probe in an isolated runtime

### Official probes

Official profiles automatically use only the `codex` probe path.
They do not require an API key and do not attempt the relay-style HTTP probe.

### Probe robustness

- probe batches continue even if one profile fails unexpectedly
- failures are stored per profile and per method
- replies and details are kept longer for later inspection

## TUI

Launch with:

```bash
codex-relay tui
```

### Main TUI capabilities

- top tabs for `All / Relay / Official`
- search and type filtering
- add relay profiles
- create official profiles through native login
- import snapshot profiles
- save current live config
- switch, edit, and delete profiles
- multi-select probe targets
- scrollable detail and probe-result dialogs

### Useful keys

- `Enter` or `u`: activate selected profile
- `a`: add relay profile
- `o`: create official profile through native Codex login
- `I`: import a profile snapshot directory
- `e`: edit selected profile
- `d`: delete selected profile
- `s`: save the current live config
- `Space`: mark or unmark selected profile for probing
- `A`: mark or unmark all visible profiles
- `C`: clear marked probe targets
- `p`: probe marked profiles, or current if nothing is marked
- `P`: probe all visible profiles
- `v`: cycle probe mode
- `t`: cycle profile type tab
- `Tab` or `Shift-Tab`: switch type tabs
- `/`: search
- `c`: clear search filter
- `i`: open full details
- `g`: jump to active profile
- `h` or `?`: help
- `q`: quit

Result dialogs support scrolling with:

- `Up` or `Down`
- `PgUp` or `PgDn`
- `Home` or `End`

## Development and testing

Run the test suite:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Run the tool directly from source:

```bash
PYTHONPATH=src python -m codex_relay --help
```

## License

MIT
