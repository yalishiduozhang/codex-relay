<div align="center">
  <img src="assets/logo.svg" alt="codex-relay logo" width="600" />
</div>

# codex-relay

`codex-relay` is a relay management tool built for Codex users. It provides a zero-dependency command-line interface and terminal UI for managing multiple Codex-compatible relay endpoints safely and clearly, without repeatedly editing `~/.codex/config.toml` and `~/.codex/auth.json` by hand.

Chinese documentation: [README.zh-CN.md](README.zh-CN.md)

## Overview

For users who switch between multiple relays, manual Codex configuration changes are usually both inefficient and error-prone. A typical workflow often looks like this:

1. Open `~/.codex/config.toml`
2. Replace `base_url`
3. Open `~/.codex/auth.json`
4. Replace `OPENAI_API_KEY`
5. Try to remember which previous configuration still worked

`codex-relay` turns that repetitive sequence into a maintainable daily workflow:

- save multiple relay profiles
- attach notes to each relay
- switch the active endpoint safely
- probe relays in batches and store the results
- manage everything from either the CLI or the TUI

## Typical use cases

This tool is especially useful if you:

- maintain multiple relays or public/community endpoints
- need to switch between different relay services frequently
- want to keep notes and usage history for each endpoint
- want a quick way to tell which relay is currently available or more stable
- do not want experiments to overwrite your working Codex configuration

## Core features

- Zero third-party runtime dependencies
- Works directly with the existing `~/.codex` configuration
- Stores multiple relays in a dedicated profile database
- Automatically backs up the current live config before switching
- Updates only the required fields and leaves the rest of the Codex config untouched
- Supports add, edit, rename, remove, and current-config inspection workflows
- Auto-imports the current live relay on first run
- Supports two probe paths:
  - an HTTP probe shaped like real Codex traffic
  - a real `codex exec` probe
- Runs `http + codex` together by default
- Includes a built-in TUI with filtering, details, probes, and fast switching
- Protects profile storage with file locking to avoid concurrent write corruption

## Requirements

- Python 3.11 or newer
- Codex CLI installed locally if you want to use `--via codex`
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

### 1. Inspect the current state

```bash
codex-relay current
codex-relay list
```

### 2. Add a relay

```bash
codex-relay add relay-a \
  --url https://relay.example.com \
  --key <API_KEY> \
  --note "Primary relay"
```

### 3. Switch to that relay

```bash
codex-relay use relay-a
```

### 4. Run a probe

```bash
codex-relay probe relay-a
```

### 5. Open the interactive UI

```bash
codex-relay tui
```

## Detailed usage

### List all saved profiles

```bash
codex-relay list
```

The output includes:

- saved profile names
- the currently active entry
- masked API keys
- notes
- the latest probe summary
- the last-used timestamp

### Show the current live Codex configuration

```bash
codex-relay current
```

This command shows:

- the current provider
- the current model
- the live `base_url`
- the masked API key
- whether the live configuration matches a saved profile

### Add a profile

```bash
codex-relay add relay-a \
  --url https://relay.example.com \
  --key <API_KEY> \
  --note "Primary relay"
```

If you want to activate it immediately:

```bash
codex-relay add relay-a \
  --url https://relay.example.com \
  --key <API_KEY> \
  --note "Primary relay" \
  --activate
```

### Save the current live configuration

```bash
codex-relay save-current snapshot-1 --note "Saved from a working live setup"
```

This is useful when you have already adjusted the live Codex configuration manually and want to preserve it as a reusable profile.

### Switch to a saved profile

```bash
codex-relay use relay-a
```

You can also switch by index:

```bash
codex-relay use --index 2
```

When switching profiles, `codex-relay` will:

1. back up the current `config.toml`
2. back up the current `auth.json`
3. update only the active provider `base_url`
4. update only `OPENAI_API_KEY`
5. preserve the rest of the Codex configuration

### Edit a profile

Rename a profile:

```bash
codex-relay edit relay-a --rename relay-main
```

Update the URL:

```bash
codex-relay edit relay-a --url https://new-relay.example.com
```

Update the API key:

```bash
codex-relay edit relay-a --key <NEW_API_KEY>
```

Update the note:

```bash
codex-relay edit relay-a --note "More stable on weekday evenings"
```

If the edited profile is currently active and you change its URL or key, the tool will also update the live Codex configuration.

### Remove a profile

```bash
codex-relay remove relay-a
```

This removes the saved entry. If the current live configuration still points to it, the tool warns you but does not rewrite your live configuration automatically.

## Probe workflows

### Probe a single relay

```bash
codex-relay probe relay-a
```

By default, this runs two probe methods:

- an HTTP probe
- a Codex probe

### Probe all relays

```bash
codex-relay probe-all
```

### Run only the HTTP probe

```bash
codex-relay probe relay-a --via http
```

### Run only the real Codex probe

```bash
codex-relay probe relay-a --via codex
```

### Probe with a custom message

```bash
codex-relay probe relay-a --message "Hello, who are you?"
```

### Use an expected substring for a functional check

```bash
codex-relay probe relay-a \
  --message "Reply with exactly 42" \
  --expect 42
```

This is more suitable for a functional health check than a simple connectivity check.

### What probe output includes

For each profile and each probe method, the tool attempts to display:

- whether the probe succeeded
- the returned status code
- latency
- the model reply
- error details when the probe fails

The meaning of the default `both` mode is practical:

- `http` is lighter and better for quick protocol-level checks
- `codex` is closer to real usage

## TUI usage

Launch the TUI with:

```bash
codex-relay tui
```

The main TUI screen shows:

- the current active profile
- the profile list on the left
- details and recent replies on the right
- the current probe configuration
- status information at the bottom

### Common TUI keys

- `h` or `?`: open help
- `Enter` or `u`: switch to the selected entry
- `a`: add a profile
- `e`: edit the selected profile
- `d`: delete the selected profile
- `s`: save the current live configuration as a profile
- `p`: probe the selected profile
- `P`: probe all currently visible entries
- `v`: switch probe mode between `both / http / codex`
- `m`: edit the probe message
- `x`: edit the expected substring
- `/`: search by name, URL, or note
- `c`: clear the current filter
- `i`: open the full details dialog
- `g`: jump to the active entry
- `PgUp` or `PgDn`: move quickly through long lists
- `Home` or `End`: jump to the first or last visible entry
- `q`: quit

## Implementation details

### Profile storage

By default, profiles are stored in:

```text
~/.codex/relay_profiles.json
```

Each profile includes:

- `name`
- `base_url`
- `api_key`
- `note`
- `created_at`
- `updated_at`
- `last_used_at`
- `last_probe`

### How the HTTP probe works

The HTTP probe:

- tries `.../responses` first
- falls back to `.../v1/responses` when necessary
- builds a request body close to real Codex `responses` traffic
- parses streamed SSE output
- extracts the final model reply

### How the Codex probe works

The Codex probe:

- creates an isolated temporary `CODEX_HOME`
- injects the selected relay URL and key
- invokes a real `codex exec`
- reads the final reply from the output file

## Development and testing

Run the test suite:

```bash
PYTHONPATH=src python -m unittest discover -s tests -v
```

Run the tool directly from source:

```bash
PYTHONPATH=src python -m codex_relay --help
```

## Project layout

```text
codex-relay/
├── .github/workflows/ci.yml
├── LICENSE
├── README.md
├── README.zh-CN.md
├── pyproject.toml
├── src/codex_relay/
│   ├── __init__.py
│   ├── __main__.py
│   └── cli.py
└── tests/
    ├── helpers.py
    ├── test_cli_workflows.py
    ├── test_probe_http.py
    └── test_tui_and_hygiene.py
```

## License

MIT
