# Deployment Runbook

This runbook documents the production-style deployment used for `marinade-sam-monitor`.

The monitor is read-only. It needs only:

- outbound HTTPS access to Marinade public APIs;
- outbound HTTPS access to Telegram Bot API;
- a Solana validator vote account;
- Telegram bot token and chat id.

## Recommended layout

| Path | Purpose |
| --- | --- |
| `/opt/marinade-SAM-monitor` | Git checkout and Python virtualenv. |
| `/opt/marinade-SAM-monitor/.venv` | Python virtualenv. |
| `/etc/marinade-sam-monitor.env` | Root-owned production config and Telegram secrets. |
| `/var/lib/marinade-sam-monitor/state.json` | Runtime state for deduplication and recovery alerts. |
| `marinade-sam-monitor.timer` | systemd timer, every 30 minutes. |
| `marinade-sam-monitor.service` | one-shot monitor run. |

For the hardened systemd unit, keep `STATE_FILE` under `/var/lib/marinade-sam-monitor/`. The unit uses `ProtectSystem=strict` and can only write under `/var/lib/marinade-sam-monitor`.

## Fresh install

```bash
sudo install -d -o "$USER" -g "$USER" /opt/marinade-SAM-monitor
git clone https://github.com/web3validator/marinade-SAM-monitor.git /opt/marinade-SAM-monitor
python3 -m venv /opt/marinade-SAM-monitor/.venv
/opt/marinade-SAM-monitor/.venv/bin/python -m pip install --upgrade pip
/opt/marinade-SAM-monitor/.venv/bin/python -m pip install -e /opt/marinade-SAM-monitor
/opt/marinade-SAM-monitor/.venv/bin/marinade-sam-monitor --version
```

Create production config:

```bash
sudo install -m 600 -o root -g root \
  /opt/marinade-SAM-monitor/deploy/systemd/marinade-sam-monitor.env.example \
  /etc/marinade-sam-monitor.env
sudo editor /etc/marinade-sam-monitor.env
```

Minimum required values:

```dotenv
VOTE_ACCOUNT=YOUR_SOLANA_VOTE_ACCOUNT_HERE
MONITOR_NAME=My Validator
TELEGRAM_TOKEN=REPLACE_WITH_TELEGRAM_BOT_TOKEN
TELEGRAM_CHAT_ID=REPLACE_WITH_TELEGRAM_CHAT_ID
STATE_FILE=/var/lib/marinade-sam-monitor/state.json
```

## Validate before enabling Telegram delivery

Run a read-only smoke test with a temporary state file and no Telegram delivery:

```bash
sudo /opt/marinade-SAM-monitor/.venv/bin/marinade-sam-monitor \
  --env-file /etc/marinade-sam-monitor.env \
  --state-file /tmp/marinade-sam-monitor-smoke-state.json \
  --print-only \
  --status
sudo rm -f /tmp/marinade-sam-monitor-smoke-state.json
```

Expected result: the command prints a `STATUS` message and exits `0`. If the validator is already below reserve thresholds, it may also print `WARN` or `CRITICAL` messages.

## Enable systemd timer

Install and verify units:

```bash
sudo cp /opt/marinade-SAM-monitor/deploy/systemd/marinade-sam-monitor.service /etc/systemd/system/
sudo cp /opt/marinade-SAM-monitor/deploy/systemd/marinade-sam-monitor.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemd-analyze verify \
  /etc/systemd/system/marinade-sam-monitor.service \
  /etc/systemd/system/marinade-sam-monitor.timer
```

Start once, then enable the timer:

```bash
sudo systemctl start marinade-sam-monitor.service
sudo systemctl enable --now marinade-sam-monitor.timer
```

Validate the one-shot run and timer:

```bash
sudo systemctl show marinade-sam-monitor.service \
  --property=Result \
  --property=ExecMainStatus \
  --property=ActiveState
sudo systemctl list-timers marinade-sam-monitor.timer --all --no-pager
sudo journalctl -u marinade-sam-monitor.service -n 80 --no-pager
sudo ls -la /var/lib/marinade-sam-monitor/
```

For a successful one-shot service, `ActiveState=inactive` is normal. Check `Result=success` and `ExecMainStatus=0`.

## Migrating from an existing cron monitor

If replacing an older monitor, avoid double alerts by disabling the old schedule before enabling the systemd timer.

1. Back up the current crontab:

```bash
crontab -l > "$HOME/marinade-sam-monitor-crontab-backup.txt"
```

2. Remove the old monitor line from crontab, keeping unrelated cron jobs intact.

3. If the previous monitor used a compatible JSON state file, optionally copy it before the first systemd run:

```bash
sudo install -d -m 755 /var/lib/marinade-sam-monitor
sudo cp /path/to/old-state.json /var/lib/marinade-sam-monitor/state.json
sudo chown root:root /var/lib/marinade-sam-monitor/state.json
```

With `DynamicUser=yes`, systemd may later show numeric ownership for files inside `/var/lib/marinade-sam-monitor`; that is normal.

4. Start the new service once and check `Result=success` before enabling the timer.

## Multi-validator deployment

Two supported patterns:

### One service instance per validator

Use the templated units:

```bash
sudo cp /opt/marinade-SAM-monitor/deploy/systemd/marinade-sam-monitor@.service /etc/systemd/system/
sudo cp /opt/marinade-SAM-monitor/deploy/systemd/marinade-sam-monitor@.timer /etc/systemd/system/
sudo install -d -m 755 /etc/marinade-sam-monitor
sudo cp /opt/marinade-SAM-monitor/deploy/systemd/marinade-sam-monitor.instance.env.example \
  /etc/marinade-sam-monitor/mainnet-a.env
sudo editor /etc/marinade-sam-monitor/mainnet-a.env
sudo systemctl daemon-reload
sudo systemctl enable --now marinade-sam-monitor@mainnet-a.timer
```

In each instance env file, replace `INSTANCE_NAME` in `STATE_FILE` with the instance name, for example:

```dotenv
STATE_FILE=/var/lib/marinade-sam-monitor/mainnet-a/state.json
```

### One scheduled command for many validators

Use `validators.json`:

```bash
cp /opt/marinade-SAM-monitor/validators.example.json /opt/marinade-SAM-monitor/validators.json
editor /opt/marinade-SAM-monitor/validators.json
/opt/marinade-SAM-monitor/.venv/bin/marinade-sam-monitor \
  --validators-file /opt/marinade-SAM-monitor/validators.json \
  --print-only \
  --status
```

This is convenient for shared Telegram credentials. Use separate `state_file` values for each validator.

## Upgrade

```bash
git -C /opt/marinade-SAM-monitor pull --ff-only
/opt/marinade-SAM-monitor/.venv/bin/python -m pip install -e /opt/marinade-SAM-monitor
sudo systemctl start marinade-sam-monitor.service
sudo systemctl show marinade-sam-monitor.service --property=Result --property=ExecMainStatus
```

## Rollback

If a new version fails:

```bash
git -C /opt/marinade-SAM-monitor log --oneline -n 5
git -C /opt/marinade-SAM-monitor checkout <known-good-commit>
/opt/marinade-SAM-monitor/.venv/bin/python -m pip install -e /opt/marinade-SAM-monitor
sudo systemctl start marinade-sam-monitor.service
```

If migrating from cron and rollback needs the previous schedule, restore the saved crontab backup:

```bash
crontab "$HOME/marinade-sam-monitor-crontab-backup.txt"
sudo systemctl disable --now marinade-sam-monitor.timer
```
