# Marinade SAM Monitor

Public-good, read-only Telegram monitor for Solana validators participating in Marinade SAM / Validator Bonds.

The monitor watches public Marinade APIs for one validator vote account and sends actionable alerts when bond reserve, protected events, SAM scoring, or validator eligibility signals need attention.

## Why this exists

Validator operators need a simple way to notice Marinade SAM issues before they become expensive or reputation-impacting. This project is intended to be reusable by any operator:

- no private keys;
- no Solana transaction signing;
- no automatic bond top-ups;
- no validator-specific hardcoded defaults;
- one Telegram bot token can be reused for many validators either through a `validators.json` file or one monitor instance per vote account.

## What it checks

| Source | Used for |
| --- | --- |
| `https://validator-bonds-api.marinade.finance/bonds` | bond config, `cpmpe`, `max_stake_wanted`, settlement claim |
| `https://validator-bonds-api.marinade.finance/protected-events` | new protected events for the configured vote account |
| `https://scoring.marinade.finance/api/v1/scores/sam` | SAM target, effective bid, bond balance, bond risk, penalties, blacklist flags |
| `https://validators-api.marinade.finance/validators?limit=2000` | validator warnings, commission, version, recent uptime stats |

## Alert types

- `CRITICAL` — bond reserve below calculated minimum, bid-too-low penalty, blacklist signal.
- `ALERT` — protected event, bond risk fee, validator warning, repeated API failure, missing API row.
- `WARN` — bond reserve below ideal runway but still above minimum.
- `INFO` — bond/scoring config changed.
- `RECOVERED` — API, reserve, validator warning, or missing-row condition cleared.
- `STATUS` — snapshot of current SAM/bond state.

## Operating rhythm

Recommended cadence for production operators:

1. Run the monitor every **30 minutes** with systemd timer or cron.
2. On the first run, it initializes state and sends one `STATUS` message. Historical protected events are recorded but not spammed as alerts.
3. On every later run, it alerts only on new or changed conditions.
4. Reserve warnings repeat after `ALERT_REPEAT_SEC` (default: 12 hours) while still active.
5. API outage alerts fire after `API_FAILURE_THRESHOLD` consecutive failures (default: 2 runs), then send `RECOVERED` after APIs work again.
6. Optional heartbeat/status messages can be enabled with `STATUS_EVERY_SEC=86400` for a daily status.

The state file is important. It stores seen protected events and previous condition levels for deduplication and recovery alerts.

## Quick start

```bash
git clone https://github.com/web3validator/marinade-SAM-monitor.git
cd marinade-SAM-monitor
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
cp .env.example .env
```

Edit `.env`:

```dotenv
VOTE_ACCOUNT=YOUR_SOLANA_VOTE_ACCOUNT_HERE
MONITOR_NAME=My Validator
TELEGRAM_TOKEN=REPLACE_WITH_TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=REPLACE_WITH_TELEGRAM_CHAT_ID
STATE_FILE=state/marinade-sam-monitor.json
```

Dry-run without Telegram:

```bash
marinade-sam-monitor --print-only --status
```

Send a real Telegram status:

```bash
marinade-sam-monitor --status
```

## Configuration

All settings can be supplied via `.env`, environment variables, or CLI flags where available.

| Variable | Default | Description |
| --- | --- | --- |
| `VOTE_ACCOUNT` | required | Solana validator vote account to monitor. |
| `MONITOR_NAME` | vote account | Human-readable label in alerts. |
| `TELEGRAM_TOKEN` / `TG_BOT_TOKEN` | required unless `--print-only` | Telegram bot token from BotFather. |
| `TELEGRAM_CHAT_ID` / `TG_CHAT_ID` | required unless `--print-only` | Telegram chat/channel/group id. |
| `TELEGRAM_THREAD_ID` | empty | Optional Telegram forum topic id. |
| `STATE_FILE` | `state/marinade-sam-monitor.json` | Persistent JSON state for deduplication. |
| `MIN_BOND_EPOCHS` | `4` | Minimum bid runway included in reserve calculation. |
| `IDEAL_BOND_EPOCHS` | `12` | Ideal bid runway included in reserve calculation. |
| `ALERT_REPEAT_SEC` | `43200` | Repeat active reserve warnings after this many seconds. |
| `API_FAILURE_THRESHOLD` | `2` | Consecutive failed API runs before outage alert. |
| `API_TIMEOUT_SEC` | `25` | Timeout per Marinade API request. |
| `STATUS_EVERY_SEC` | `0` | Optional periodic status cadence; `86400` = daily. |
| `ALERT_BIDDING_EVENTS` | `0` | Set `1` to alert on protected events with reason `Bidding`. |
| `MAX_COMMISSION_PCT` | `7` | Commission threshold for validator API warning. |
| `BAD_UPTIME_THRESHOLD` | `0.8` | Recent epoch uptime threshold used for warning. |

CLI flags:

```bash
marinade-sam-monitor --help
marinade-sam-monitor --vote-account <VOTE_ACCOUNT> --name "My Validator" --print-only --status
```

## Monitoring multiple validators with one bot token

Use `validators.example.json` when one scheduled command should monitor multiple vote accounts with the same Telegram bot token:

```bash
cp validators.example.json validators.json
editor validators.json
marinade-sam-monitor --validators-file validators.json --print-only --status
marinade-sam-monitor --validators-file validators.json
```

Each validator entry needs a unique `vote_account`, `name`, and `state_file`. Optional per-validator `telegram_chat_id` and `telegram_thread_id` override the shared Telegram destination.

For larger operations, this mode keeps config in one file; systemd template instances remain useful when each validator should have separate service logs and independent retry state.

## Reserve calculation

The monitor estimates reserve runway from Marinade scoring fields:

```text
epoch_bid_cost = marinadeActivatedStakeSol * effectiveBidPmpe / 1000
base_cover     = max(minBondBalanceSol, marinadeActivatedStakeSol / 10000)
min_required   = settlement_claim + base_cover + MIN_BOND_EPOCHS * epoch_bid_cost
ideal_required = settlement_claim + base_cover + IDEAL_BOND_EPOCHS * epoch_bid_cost
```

This is a monitoring heuristic, not financial advice and not a replacement for Marinade documentation or operator judgment.

## Running with systemd

Single validator example:

```bash
sudo cp deploy/systemd/marinade-sam-monitor.service /etc/systemd/system/
sudo cp deploy/systemd/marinade-sam-monitor.timer /etc/systemd/system/
sudo cp deploy/systemd/marinade-sam-monitor.env.example /etc/marinade-sam-monitor.env
sudo editor /etc/marinade-sam-monitor.env
sudo systemctl daemon-reload
sudo systemctl enable --now marinade-sam-monitor.timer
```

Check logs:

```bash
journalctl -u marinade-sam-monitor.service -n 100 --no-pager
```

Multi-validator pattern:

- use the same Telegram bot token;
- install the templated systemd files from `deploy/systemd/marinade-sam-monitor@.service` and `deploy/systemd/marinade-sam-monitor@.timer`;
- create one env file per validator, for example `/etc/marinade-sam-monitor/mainnet-a.env` and `/etc/marinade-sam-monitor/mainnet-b.env`;
- use `deploy/systemd/marinade-sam-monitor.instance.env.example` as a template;
- set a unique `VOTE_ACCOUNT`, `MONITOR_NAME`, and `STATE_FILE` in each file;
- enable instances such as `marinade-sam-monitor@mainnet-a.timer`.

## Cron example

```cron
*/30 * * * * cd /opt/marinade-SAM-monitor && . .venv/bin/activate && marinade-sam-monitor >> /var/log/marinade-sam-monitor.log 2>&1
```

## Development

```bash
python -m pip install -e .
python -m unittest discover -s tests -v
python -m compileall src tests
```

## Security model

This monitor is designed to be safe to run on a non-validator host:

- it only performs HTTPS GET requests to public Marinade APIs;
- it only sends HTTPS POST requests to Telegram Bot API;
- it does not connect to Solana RPC;
- it does not read keypairs, seed phrases, identity files, vote-account keys, or withdraw-authority keys;
- it does not sign, simulate, submit, or build transactions;
- it does not top up bonds automatically.

Keep Telegram tokens in `.env`, systemd `EnvironmentFile`, or your secret manager. Never commit real tokens.

## Public-goods proposal

A short contribution proposal for Marinade review is included at [`docs/marinade-contribution-proposal.md`](docs/marinade-contribution-proposal.md).

## Contributing

Issues and pull requests are welcome, especially for:

- additional notification sinks;
- better SAM reserve heuristics;
- better docs for new Marinade operators;
- test fixtures for API response shape changes;
- deployment examples for Docker, Kubernetes, and hosted cron.

## License

MIT. See [`LICENSE`](LICENSE).
