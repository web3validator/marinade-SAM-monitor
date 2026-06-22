# Troubleshooting

## The systemd service is `inactive (dead)`

`marinade-sam-monitor.service` is a one-shot service, so `inactive (dead)` is normal after a successful run.

Use:

```bash
sudo systemctl show marinade-sam-monitor.service \
  --property=Result \
  --property=ExecMainStatus \
  --property=ActiveState
```

Healthy output should include:

```text
Result=success
ExecMainStatus=0
ActiveState=inactive
```

Check timer scheduling with:

```bash
sudo systemctl list-timers marinade-sam-monitor.timer --all --no-pager
```

## `Telegram token/chat id is not configured`

For local CLI runs, pass `--env-file` or export env variables:

```bash
marinade-sam-monitor --env-file .env --status
```

For systemd, the unit reads `/etc/marinade-sam-monitor.env` via `EnvironmentFile=`. Do not make that file world-readable. Recommended permissions:

```bash
sudo chown root:root /etc/marinade-sam-monitor.env
sudo chmod 600 /etc/marinade-sam-monitor.env
```

## State file permission errors under systemd

The bundled systemd unit is hardened with:

- `DynamicUser=yes`
- `ProtectSystem=strict`
- `StateDirectory=marinade-sam-monitor`
- `ReadWritePaths=/var/lib/marinade-sam-monitor`

Set:

```dotenv
STATE_FILE=/var/lib/marinade-sam-monitor/state.json
```

Do not use a relative `state/...` path in the systemd env file unless you also adjust the unit sandbox.

Numeric file ownership under `/var/lib/marinade-sam-monitor` is normal with `DynamicUser=yes`.

## Dry-run works, systemd fails

Common causes:

1. `STATE_FILE` is not under `/var/lib/marinade-sam-monitor/`.
2. `/opt/marinade-SAM-monitor/.venv/bin/marinade-sam-monitor` does not exist or is not executable.
3. `/etc/marinade-sam-monitor.env` has malformed `KEY=VALUE` lines.
4. Network egress to Marinade APIs or Telegram is blocked.

Useful commands:

```bash
sudo journalctl -u marinade-sam-monitor.service -n 120 --no-pager
sudo systemd-analyze verify \
  /etc/systemd/system/marinade-sam-monitor.service \
  /etc/systemd/system/marinade-sam-monitor.timer
sudo /opt/marinade-SAM-monitor/.venv/bin/marinade-sam-monitor \
  --env-file /etc/marinade-sam-monitor.env \
  --state-file /tmp/marinade-sam-monitor-smoke-state.json \
  --print-only \
  --status
```

## First run sends alerts for existing conditions

On first run, the monitor initializes state and sends a status snapshot. It can also alert on currently active reserve/scoring conditions.

When migrating from an existing compatible monitor, copy the old JSON state before the first production run if you want to preserve deduplication state.

## How to get Telegram `chat_id` and `thread_id`

1. Create a bot with BotFather.
2. Add the bot to a chat, group, supergroup, or channel.
3. Send a test message in the target chat/topic.
4. Call Telegram `getUpdates` for the bot and inspect `chat.id`.
5. For forum topics, use `message_thread_id` from the update as `TELEGRAM_THREAD_ID`.

Keep the bot token private and never commit it to git.
