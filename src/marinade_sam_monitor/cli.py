#!/usr/bin/env python3
"""
Read-only Marinade SAM / PSR monitor for Solana validators.

The monitor checks public Marinade APIs and sends Telegram alerts for:
- new protected events for the configured vote account
- bond balance below calculated minimum / ideal reserve runway
- SAM scoring penalties, blacklisting, or bond-risk signals
- validator API warnings and eligibility-adjacent issues
- Marinade API outages and recovery

It never reads validator keys, signs transactions, broadcasts transactions, or
performs automatic bond top-ups.
"""

from __future__ import annotations

import argparse
import html
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import __version__

BONDS_URL = "https://validator-bonds-api.marinade.finance/bonds"
EVENTS_URL = "https://validator-bonds-api.marinade.finance/protected-events"
SCORES_URL = "https://scoring.marinade.finance/api/v1/scores/sam"
VALIDATORS_URL = "https://validators-api.marinade.finance/validators?limit=2000"

USER_AGENT = (
    "marinade-sam-monitor/1.0 (+https://github.com/web3validator/marinade-SAM-monitor)"
)
LAMPORTS_PER_SOL = 1_000_000_000
DEFAULT_STATE_FILE = "state/marinade-sam-monitor.json"


@dataclass(frozen=True)
class Config:
    vote_account: str
    monitor_name: str
    state_file: Path
    telegram_token: str | None
    telegram_chat_id: str | None
    telegram_thread_id: str | None
    min_bond_epochs: float = 4.0
    ideal_bond_epochs: float = 12.0
    alert_repeat_sec: int = 12 * 3600
    api_failure_threshold: int = 2
    alert_bidding_events: bool = False
    status_every_sec: int = 0
    max_commission_pct: float = 7.0
    bad_uptime_threshold: float = 0.8
    api_timeout_sec: int = 25


@dataclass(frozen=True)
class ReserveEstimate:
    min_required: float
    ideal_required: float
    epoch_bid_cost: float
    settlement_sol: float


class ConfigurationError(ValueError):
    """Raised when required runtime configuration is missing or invalid."""


def env_float(name: str, default: float) -> float:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return float(value)


def env_int(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return int(value)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def load_env_file(path: Path | None) -> None:
    """Load a simple KEY=VALUE env file without shell evaluation.

    Existing environment variables win over values from the file. This keeps
    systemd/Kubernetes secrets able to override a local .env file.
    """

    if path is None or not path.exists():
        return

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


def fetch_json(url: str, timeout: int = 25) -> Any:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def sol_from_lamports(value: Any) -> float:
    try:
        return float(value) / LAMPORTS_PER_SOL
    except (TypeError, ValueError):
        return 0.0


def get_list(data: Any, key: str | None) -> list[dict[str, Any]]:
    if isinstance(data, list):
        return [x for x in data if isinstance(x, dict)]
    if isinstance(data, dict) and key:
        value = data.get(key, [])
        if isinstance(value, list):
            return [x for x in value if isinstance(x, dict)]
    return []


def find_by_vote(rows: list[dict[str, Any]], vote: str) -> list[dict[str, Any]]:
    return [
        row
        for row in rows
        if row.get("vote_account") == vote or row.get("voteAccount") == vote
    ]


def latest_score(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    return max(
        rows,
        key=lambda r: (
            int(r.get("epoch") or 0),
            int(r.get("scoringRunId") or 0),
        ),
    )


def read_state(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def h(value: Any) -> str:
    return html.escape(str(value), quote=False)


def fmt_sol(value: float) -> str:
    if value >= 1000:
        return f"{value:,.0f} SOL"
    if value >= 10:
        return f"{value:,.2f} SOL"
    return f"{value:,.4f} SOL"


def event_key(event: dict[str, Any]) -> str:
    return "|".join(
        [
            str(event.get("epoch", "")),
            str(event.get("reason", "")),
            str(event.get("amount", "")),
            str(event.get("vote_account", "") or event.get("voteAccount", "")),
            str(event.get("signature", "") or event.get("transaction_signature", "")),
        ]
    )


def settlement_claim_sol(bond: dict[str, Any] | None) -> float:
    if not bond:
        return 0.0
    return sol_from_lamports(
        bond.get("remaining_settlement_claim_amount")
        or bond.get("remainining_settlement_claim_amount")
    )


def calculate_reserve(
    score: dict[str, Any] | None,
    bond: dict[str, Any] | None,
    min_epochs: float,
    ideal_epochs: float,
) -> ReserveEstimate:
    if not score:
        return ReserveEstimate(0.0, 0.0, 0.0, settlement_claim_sol(bond))

    values = score.get("values", {}) or {}
    rev = score.get("revShare", {}) or {}
    config = (score.get("metadata") or {}).get("scoringConfig") or {}

    marinade_stake = float(values.get("marinadeActivatedStakeSol") or 0.0)
    effective_bid = float(
        score.get("effectiveBid") or rev.get("auctionEffectiveBidPmpe") or 0.0
    )
    bid_pmpe = float(rev.get("bidPmpe") or 0.0)
    bid_for_cost = effective_bid or bid_pmpe
    epoch_bid_cost = marinade_stake * bid_for_cost / 1000.0

    # Marinade validator bonds cover protected-stake incidents. This heuristic
    # keeps at least protocol minimum, plus a small downtime cover and bid runway.
    downtime_cover = marinade_stake / 10_000.0
    protocol_min = float(config.get("minBondBalanceSol") or 7.0)
    base_cover = max(protocol_min, downtime_cover)
    settlement_sol = settlement_claim_sol(bond)

    return ReserveEstimate(
        min_required=settlement_sol + base_cover + min_epochs * epoch_bid_cost,
        ideal_required=settlement_sol + base_cover + ideal_epochs * epoch_bid_cost,
        epoch_bid_cost=epoch_bid_cost,
        settlement_sol=settlement_sol,
    )


def send_telegram(text: str, config: Config) -> None:
    if not config.telegram_token or not config.telegram_chat_id:
        raise RuntimeError("Telegram token/chat id is not configured")

    data = {
        "chat_id": config.telegram_chat_id,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true",
        "text": text,
    }
    if config.telegram_thread_id:
        data["message_thread_id"] = config.telegram_thread_id

    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{config.telegram_token}/sendMessage",
        data=body,
        headers={"User-Agent": USER_AGENT},
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        payload = json.loads(resp.read().decode("utf-8"))
    if not payload.get("ok"):
        raise RuntimeError(f"Telegram API rejected message: {payload}")


def alert_title(level: str, name: str) -> str:
    return f"{level} <b>Marinade SAM monitor</b> — {h(name)}"


def build_status(
    name: str,
    vote: str,
    bond: dict[str, Any] | None,
    score: dict[str, Any] | None,
    validator: dict[str, Any] | None,
    reserve: ReserveEstimate,
) -> str:
    lines = [alert_title("STATUS", name), f"vote: <code>{h(vote)}</code>"]
    if score:
        values = score.get("values", {}) or {}
        rev = score.get("revShare", {}) or {}
        effective_bid = float(
            score.get("effectiveBid") or rev.get("auctionEffectiveBidPmpe") or 0
        )
        bid_pmpe = float(rev.get("bidPmpe") or 0)
        lines += [
            f"epoch: <code>{h(score.get('epoch'))}</code>",
            f"Marinade active: <b>{fmt_sol(float(values.get('marinadeActivatedStakeSol') or 0))}</b>",
            f"SAM target: <b>{fmt_sol(float(score.get('marinadeSamTargetSol') or 0))}</b>",
            f"effective bid: <code>{effective_bid:.6f} PMPE</code>",
            f"bid in bond: <code>{bid_pmpe:.6f} PMPE</code>",
            f"bond balance: <b>{fmt_sol(float(values.get('bondBalanceSol') or 0))}</b>",
            f"estimated bid drain/epoch: <b>{fmt_sol(reserve.epoch_bid_cost)}</b>",
            f"settlement claim: <b>{fmt_sol(reserve.settlement_sol)}</b>",
            f"reserve min/ideal: <b>{fmt_sol(reserve.min_required)}</b> / <b>{fmt_sol(reserve.ideal_required)}</b>",
        ]
    if bond:
        lines += [
            f"bond cpmpe: <code>{int(float(bond.get('cpmpe') or 0))}</code>",
            f"maxStakeWanted API: <code>{sol_from_lamports(bond.get('max_stake_wanted')):,.0f} SOL</code>",
        ]
    if validator:
        lines += [
            f"version: <code>{h(validator.get('version'))}</code>",
            f"commission: <code>{h(validator.get('commission_advertised'))}%</code>",
            f"warnings: <code>{h(', '.join(validator.get('warnings') or []) or 'none')}</code>",
        ]
    return "\n".join(lines)


def append_missing_row_alerts(
    alerts: list[str],
    state: dict[str, Any],
    name: str,
    vote: str,
    bond: dict[str, Any] | None,
    score: dict[str, Any] | None,
    validator: dict[str, Any] | None,
) -> None:
    current = {
        "bond": bond is None,
        "score": score is None,
        "validator": validator is None,
    }
    previous = state.get("missing_rows") or {}
    if current == previous:
        return

    missing = [key for key, is_missing in current.items() if is_missing]
    previously_missing = [key for key, was_missing in previous.items() if was_missing]
    if missing:
        alerts.append(
            "\n".join(
                [
                    alert_title("ALERT", name),
                    f"Marinade API row(s) missing for vote account <code>{h(vote)}</code>.",
                    f"missing: <code>{h(', '.join(missing))}</code>",
                ]
            )
        )
    elif previously_missing:
        alerts.append(
            "\n".join(
                [
                    alert_title("RECOVERED", name),
                    "All expected Marinade API rows are present again.",
                ]
            )
        )
    state["missing_rows"] = current


def append_event_alerts(
    alerts: list[str],
    state: dict[str, Any],
    name: str,
    events: list[dict[str, Any]],
    alert_bidding_events: bool,
) -> bool:
    seen_events: set[str] = set(state.get("seen_events", []))
    current_event_keys = {event_key(e) for e in events}
    first_run = "seen_events" not in state
    new_events = [e for e in events if event_key(e) not in seen_events]

    if not first_run:
        important_events = []
        for event in sorted(
            new_events,
            key=lambda e: (int(e.get("epoch") or 0), str(e.get("reason", ""))),
        ):
            reason = str(event.get("reason", ""))
            if reason == "Bidding" and not alert_bidding_events:
                continue
            important_events.append(event)
        if important_events:
            lines = [alert_title("ALERT", name), "New protected event(s):"]
            for event in important_events[:8]:
                lines.append(
                    f"- epoch <code>{h(event.get('epoch'))}</code> "
                    f"<b>{h(event.get('reason'))}</b>: "
                    f"<b>{fmt_sol(sol_from_lamports(event.get('amount')))}</b>"
                )
            if len(important_events) > 8:
                lines.append(f"...and <code>{len(important_events) - 8}</code> more")
            alerts.append("\n".join(lines))

    state["seen_events"] = sorted(current_event_keys)[-500:]
    return first_run


def append_score_alerts(
    alerts: list[str],
    state: dict[str, Any],
    config: Config,
    bond: dict[str, Any] | None,
    score: dict[str, Any] | None,
    reserve: ReserveEstimate,
    now: int,
) -> None:
    if not score:
        return

    values = score.get("values", {}) or {}
    rev = score.get("revShare", {}) or {}
    bond_balance = float(values.get("bondBalanceSol") or 0.0)
    bond_risk_fee = float(values.get("bondRiskFeeSol") or 0.0)
    bid_too_low = float(rev.get("bidTooLowPenaltyPmpe") or 0.0)
    blacklist_penalty = float(rev.get("blacklistPenaltyPmpe") or 0.0)
    sam_blacklisted = bool(values.get("samBlacklisted"))

    if bond_risk_fee > 0:
        alerts.append(
            "\n".join(
                [
                    alert_title("ALERT", config.monitor_name),
                    f"Scoring predicts/records bond risk fee: <b>{fmt_sol(bond_risk_fee)}</b>.",
                    f"bond balance: <b>{fmt_sol(bond_balance)}</b>",
                    f"min reserve: <b>{fmt_sol(reserve.min_required)}</b>",
                ]
            )
        )

    if bid_too_low > 0 or blacklist_penalty > 0 or sam_blacklisted:
        alerts.append(
            "\n".join(
                [
                    alert_title("CRITICAL", config.monitor_name),
                    "Penalty / blacklist signal in SAM scoring.",
                    f"bidTooLowPenaltyPmpe: <code>{bid_too_low:.6f}</code>",
                    f"blacklistPenaltyPmpe: <code>{blacklist_penalty:.6f}</code>",
                    f"samBlacklisted: <code>{sam_blacklisted}</code>",
                ]
            )
        )

    reserve_level = "ok"
    if bond_balance < reserve.min_required:
        reserve_level = "critical"
    elif bond_balance < reserve.ideal_required:
        reserve_level = "warning"

    last_reserve_alert = int(state.get("last_reserve_alert", 0))
    prev_level = state.get("reserve_level")
    should_repeat = now - last_reserve_alert > config.alert_repeat_sec
    if reserve_level == "critical" and (prev_level != reserve_level or should_repeat):
        state["last_reserve_alert"] = now
        alerts.append(
            "\n".join(
                [
                    alert_title("CRITICAL", config.monitor_name),
                    "Bond reserve is below calculated minimum. Top-up should be prepared.",
                    f"bond balance: <b>{fmt_sol(bond_balance)}</b>",
                    f"min reserve: <b>{fmt_sol(reserve.min_required)}</b>",
                    f"ideal reserve: <b>{fmt_sol(reserve.ideal_required)}</b>",
                    f"estimated bid drain/epoch: <b>{fmt_sol(reserve.epoch_bid_cost)}</b>",
                ]
            )
        )
    elif reserve_level == "warning" and (prev_level != reserve_level or should_repeat):
        state["last_reserve_alert"] = now
        alerts.append(
            "\n".join(
                [
                    alert_title("WARN", config.monitor_name),
                    "Bond reserve is below ideal runway. Not urgent, but monitor/top-up planning is reasonable.",
                    f"bond balance: <b>{fmt_sol(bond_balance)}</b>",
                    f"min reserve: <b>{fmt_sol(reserve.min_required)}</b>",
                    f"ideal reserve: <b>{fmt_sol(reserve.ideal_required)}</b>",
                    f"estimated bid drain/epoch: <b>{fmt_sol(reserve.epoch_bid_cost)}</b>",
                ]
            )
        )
    elif reserve_level == "ok" and prev_level in ("critical", "warning"):
        alerts.append(
            "\n".join(
                [
                    alert_title("RECOVERED", config.monitor_name),
                    "Bond reserve is back above ideal threshold.",
                    f"bond balance: <b>{fmt_sol(bond_balance)}</b>",
                    f"ideal reserve: <b>{fmt_sol(reserve.ideal_required)}</b>",
                ]
            )
        )
    state["reserve_level"] = reserve_level

    current_config = {
        "cpmpe": int(float((bond or {}).get("cpmpe") or 0)),
        "maxStakeWantedSol": round(
            sol_from_lamports((bond or {}).get("max_stake_wanted")), 6
        ),
        "scoreMaxStakeWantedSol": float(score.get("maxStakeWanted") or 0),
        "bidPmpe": float(rev.get("bidPmpe") or 0.0),
        "effectiveBid": float(
            score.get("effectiveBid") or rev.get("auctionEffectiveBidPmpe") or 0.0
        ),
    }
    previous_config = state.get("last_config")
    if previous_config and previous_config != current_config:
        alerts.append(
            "\n".join(
                [
                    alert_title("INFO", config.monitor_name),
                    "Marinade bond/scoring config changed.",
                    f"previous: <code>{h(json.dumps(previous_config, sort_keys=True))}</code>",
                    f"current: <code>{h(json.dumps(current_config, sort_keys=True))}</code>",
                ]
            )
        )
    state["last_config"] = current_config


def append_validator_alerts(
    alerts: list[str],
    state: dict[str, Any],
    config: Config,
    validator: dict[str, Any] | None,
) -> None:
    if not validator:
        return

    warnings = validator.get("warnings") or []
    rugged = bool(validator.get("rugged_commission"))
    commission = float(validator.get("commission_advertised") or 0.0)
    recent_stats = validator.get("epoch_stats") or []
    bad_uptime = [
        s
        for s in recent_stats[:3]
        if s.get("uptime_pct") is not None
        and float(s.get("uptime_pct") or 0) < config.bad_uptime_threshold
    ]
    val_status = {
        "warnings": warnings,
        "rugged_commission": rugged,
        "commission": commission,
        "bad_uptime_epochs": [s.get("epoch") for s in bad_uptime],
    }
    previous = state.get("validator_status")
    has_issue = bool(
        warnings or rugged or commission > config.max_commission_pct or bad_uptime
    )
    had_issue = bool(
        previous
        and (
            previous.get("warnings")
            or previous.get("rugged_commission")
            or float(previous.get("commission") or 0) > config.max_commission_pct
            or previous.get("bad_uptime_epochs")
        )
    )

    if val_status != previous and has_issue:
        alerts.append(
            "\n".join(
                [
                    alert_title("ALERT", config.monitor_name),
                    "Validator API shows eligibility-adjacent issue.",
                    f"warnings: <code>{h(', '.join(warnings) or 'none')}</code>",
                    f"rugged_commission: <code>{rugged}</code>",
                    f"commission: <code>{commission:g}%</code>",
                    f"bad uptime epochs: <code>{h(', '.join(map(str, val_status['bad_uptime_epochs'])) or 'none')}</code>",
                ]
            )
        )
    elif val_status != previous and had_issue and not has_issue:
        alerts.append(
            "\n".join(
                [
                    alert_title("RECOVERED", config.monitor_name),
                    "Validator API warning state is clear again.",
                ]
            )
        )
    state["validator_status"] = val_status


def fetch_marinade_rows(
    config: Config,
) -> tuple[
    dict[str, Any] | None,
    list[dict[str, Any]],
    dict[str, Any] | None,
    dict[str, Any] | None,
]:
    bonds_data = fetch_json(BONDS_URL, config.api_timeout_sec)
    events_data = fetch_json(EVENTS_URL, config.api_timeout_sec)
    scores_data = fetch_json(SCORES_URL, config.api_timeout_sec)
    validators_data = fetch_json(VALIDATORS_URL, config.api_timeout_sec)

    bonds = find_by_vote(get_list(bonds_data, "bonds"), config.vote_account)
    events = find_by_vote(
        get_list(events_data, "protected_events"), config.vote_account
    )
    scores = find_by_vote(get_list(scores_data, None), config.vote_account)
    validators = find_by_vote(
        get_list(validators_data, "validators"), config.vote_account
    )

    return (
        bonds[0] if bonds else None,
        events,
        latest_score(scores),
        validators[0] if validators else None,
    )


def deliver_alerts(alerts: list[str], config: Config, print_only: bool) -> None:
    for msg in alerts:
        if print_only:
            print(msg)
            print()
        else:
            send_telegram(msg, config)


def run(
    config: Config, status_requested: bool = False, print_only: bool = False
) -> int:
    if not print_only and (not config.telegram_token or not config.telegram_chat_id):
        raise ConfigurationError(
            "Telegram token and chat id are required unless --print-only is used"
        )

    state = read_state(config.state_file)
    now = int(time.time())
    alerts: list[str] = []

    try:
        bond, events, score, validator = fetch_marinade_rows(config)
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        failures = int(state.get("api_failures", 0)) + 1
        state["api_failures"] = failures
        if failures >= config.api_failure_threshold and not state.get(
            "api_alert_active"
        ):
            state["api_alert_active"] = True
            alerts.append(
                "\n".join(
                    [
                        alert_title("ALERT", config.monitor_name),
                        "Marinade API checks failed repeatedly.",
                        f"failures: <code>{failures}</code>",
                        f"error: <code>{h(exc)}</code>",
                    ]
                )
            )
        write_state(config.state_file, state)
        deliver_alerts(alerts, config, print_only)
        return 2

    if state.get("api_alert_active"):
        alerts.append(
            "\n".join(
                [
                    alert_title("RECOVERED", config.monitor_name),
                    "Marinade API checks are working again.",
                ]
            )
        )
    state["api_failures"] = 0
    state["api_alert_active"] = False

    append_missing_row_alerts(
        alerts, state, config.monitor_name, config.vote_account, bond, score, validator
    )
    first_run = append_event_alerts(
        alerts, state, config.monitor_name, events, config.alert_bidding_events
    )

    reserve = calculate_reserve(
        score, bond, config.min_bond_epochs, config.ideal_bond_epochs
    )
    append_score_alerts(alerts, state, config, bond, score, reserve, now)
    append_validator_alerts(alerts, state, config, validator)

    should_send_periodic_status = (
        config.status_every_sec > 0
        and now - int(state.get("last_status_at", 0)) >= config.status_every_sec
    )
    if first_run:
        state["initialized_at"] = now
        status_requested = True

    if status_requested or should_send_periodic_status:
        alerts.append(
            build_status(
                config.monitor_name,
                config.vote_account,
                bond,
                score,
                validator,
                reserve,
            )
        )
        state["last_status_at"] = now

    state["last_ok_at"] = now
    write_state(config.state_file, state)
    deliver_alerts(alerts, config, print_only)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="marinade-sam-monitor",
        description="Read-only Telegram monitor for Marinade SAM validator state.",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}"
    )
    parser.add_argument(
        "--env-file",
        default=os.getenv("ENV_FILE", ".env"),
        help="path to KEY=VALUE config file; default: .env",
    )
    parser.add_argument(
        "--vote-account", help="Solana validator vote account; env: VOTE_ACCOUNT"
    )
    parser.add_argument("--name", help="display name used in alerts; env: MONITOR_NAME")
    parser.add_argument(
        "--validators-file",
        default=os.getenv("VALIDATORS_FILE"),
        help="JSON file with multiple validators to monitor; env: VALIDATORS_FILE",
    )
    parser.add_argument(
        "--state-file",
        help=f"state JSON path; env: STATE_FILE; default: {DEFAULT_STATE_FILE}",
    )
    parser.add_argument(
        "--status", action="store_true", help="send/print a status message on this run"
    )
    parser.add_argument(
        "--print-only",
        action="store_true",
        help="print alerts instead of sending Telegram messages",
    )
    parser.add_argument(
        "--telegram-token",
        help="Telegram bot token; prefer env TELEGRAM_TOKEN or TG_BOT_TOKEN",
    )
    parser.add_argument(
        "--telegram-chat-id",
        help="Telegram chat id; prefer env TELEGRAM_CHAT_ID or TG_CHAT_ID",
    )
    parser.add_argument(
        "--telegram-thread-id",
        help="optional Telegram forum topic id; env TELEGRAM_THREAD_ID",
    )
    return parser


def first_non_empty(*values: str | None) -> str | None:
    for value in values:
        if value is not None and value != "":
            return value
    return None


def mapping_value(mapping: dict[str, Any], *names: str) -> str | None:
    for name in names:
        value = mapping.get(name)
        if value is not None and value != "":
            return str(value)
    return None


def mapping_float(
    mapping: dict[str, Any], env_name: str, default: float, *names: str
) -> float:
    value = mapping_value(mapping, *names, env_name)
    if value is not None:
        return float(value)
    return env_float(env_name, default)


def mapping_int(
    mapping: dict[str, Any], env_name: str, default: int, *names: str
) -> int:
    value = mapping_value(mapping, *names, env_name)
    if value is not None:
        return int(value)
    return env_int(env_name, default)


def mapping_bool(
    mapping: dict[str, Any], env_name: str, default: bool, *names: str
) -> bool:
    value = mapping_value(mapping, *names, env_name)
    if value is None:
        return env_bool(env_name, default)
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def safe_state_stem(value: str) -> str:
    stem = "".join(ch if ch.isalnum() or ch in "-_." else "-" for ch in value)
    return stem.strip("-.")[:80] or "validator"


def state_path_from_value(value: str, base_dir: Path) -> Path:
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    return base_dir / path


def config_from_mapping(
    mapping: dict[str, Any], base_dir: Path, args: argparse.Namespace | None = None
) -> Config:
    vote = mapping_value(mapping, "vote_account", "voteAccount", "VOTE_ACCOUNT")
    if not vote:
        raise ConfigurationError("Every validator entry must include vote_account")

    name = mapping_value(mapping, "monitor_name", "name", "MONITOR_NAME") or vote
    state_value = (
        mapping_value(mapping, "state_file", "STATE_FILE")
        or f"state/{safe_state_stem(name)}.json"
    )
    state_file = state_path_from_value(state_value, base_dir)

    token = first_non_empty(
        getattr(args, "telegram_token", None) if args else None,
        mapping_value(
            mapping, "telegram_token", "bot_token", "TELEGRAM_TOKEN", "BOT_TOKEN"
        ),
        os.getenv("TELEGRAM_TOKEN"),
        os.getenv("BOT_TOKEN"),
        os.getenv("TG_BOT_TOKEN"),
        os.getenv("TG_API"),
    )
    chat_id = first_non_empty(
        getattr(args, "telegram_chat_id", None) if args else None,
        mapping_value(
            mapping, "telegram_chat_id", "chat_id", "TELEGRAM_CHAT_ID", "CHAT_ID"
        ),
        os.getenv("TELEGRAM_CHAT_ID"),
        os.getenv("CHAT_ID"),
        os.getenv("TG_CHAT_ID"),
        os.getenv("TG_CHAT"),
    )
    thread_id = first_non_empty(
        getattr(args, "telegram_thread_id", None) if args else None,
        mapping_value(mapping, "telegram_thread_id", "thread_id", "TELEGRAM_THREAD_ID"),
        os.getenv("TELEGRAM_THREAD_ID"),
        os.getenv("TG_THREAD_ID"),
    )

    return Config(
        vote_account=vote,
        monitor_name=name,
        state_file=state_file,
        telegram_token=token,
        telegram_chat_id=chat_id,
        telegram_thread_id=thread_id,
        min_bond_epochs=mapping_float(
            mapping, "MIN_BOND_EPOCHS", 4.0, "min_bond_epochs"
        ),
        ideal_bond_epochs=mapping_float(
            mapping, "IDEAL_BOND_EPOCHS", 12.0, "ideal_bond_epochs"
        ),
        alert_repeat_sec=mapping_int(
            mapping, "ALERT_REPEAT_SEC", 12 * 3600, "alert_repeat_sec"
        ),
        api_failure_threshold=mapping_int(
            mapping, "API_FAILURE_THRESHOLD", 2, "api_failure_threshold"
        ),
        alert_bidding_events=mapping_bool(
            mapping, "ALERT_BIDDING_EVENTS", False, "alert_bidding_events"
        ),
        status_every_sec=mapping_int(
            mapping, "STATUS_EVERY_SEC", 0, "status_every_sec"
        ),
        max_commission_pct=mapping_float(
            mapping, "MAX_COMMISSION_PCT", 7.0, "max_commission_pct"
        ),
        bad_uptime_threshold=mapping_float(
            mapping, "BAD_UPTIME_THRESHOLD", 0.8, "bad_uptime_threshold"
        ),
        api_timeout_sec=mapping_int(mapping, "API_TIMEOUT_SEC", 25, "api_timeout_sec"),
    )


def configs_from_file(
    path: Path, args: argparse.Namespace | None = None
) -> list[Config]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ConfigurationError("Validators config must be a JSON object")

    validators = data.get("validators")
    if not isinstance(validators, list) or not validators:
        raise ConfigurationError(
            "Validators config must include a non-empty validators list"
        )

    defaults = data.get("defaults", {})
    if defaults is None:
        defaults = {}
    if not isinstance(defaults, dict):
        raise ConfigurationError("validators config defaults must be a JSON object")

    shared = {
        key: value
        for key, value in data.items()
        if key not in {"validators", "defaults"}
    }
    configs: list[Config] = []
    for index, validator in enumerate(validators, start=1):
        if not isinstance(validator, dict):
            raise ConfigurationError(f"validator entry #{index} must be a JSON object")
        configs.append(
            config_from_mapping({**shared, **defaults, **validator}, path.parent, args)
        )
    return configs


def run_many(
    configs: list[Config], status_requested: bool = False, print_only: bool = False
) -> int:
    exit_code = 0
    for config in configs:
        code = run(config, status_requested=status_requested, print_only=print_only)
        if code != 0 and exit_code == 0:
            exit_code = code
    return exit_code


def config_from_args(args: argparse.Namespace) -> Config:
    vote = first_non_empty(args.vote_account, os.getenv("VOTE_ACCOUNT"))
    if not vote:
        raise ConfigurationError(
            "VOTE_ACCOUNT is required; set it in .env or pass --vote-account"
        )

    name = first_non_empty(args.name, os.getenv("MONITOR_NAME"), vote)
    state_file = Path(
        first_non_empty(args.state_file, os.getenv("STATE_FILE"), DEFAULT_STATE_FILE)
        or DEFAULT_STATE_FILE
    ).expanduser()

    token = first_non_empty(
        args.telegram_token,
        os.getenv("TELEGRAM_TOKEN"),
        os.getenv("BOT_TOKEN"),
        os.getenv("TG_BOT_TOKEN"),
        os.getenv("TG_API"),
    )
    chat_id = first_non_empty(
        args.telegram_chat_id,
        os.getenv("TELEGRAM_CHAT_ID"),
        os.getenv("CHAT_ID"),
        os.getenv("TG_CHAT_ID"),
        os.getenv("TG_CHAT"),
    )
    thread_id = first_non_empty(
        args.telegram_thread_id,
        os.getenv("TELEGRAM_THREAD_ID"),
        os.getenv("TG_THREAD_ID"),
    )

    return Config(
        vote_account=vote,
        monitor_name=name or vote,
        state_file=state_file,
        telegram_token=token,
        telegram_chat_id=chat_id,
        telegram_thread_id=thread_id,
        min_bond_epochs=env_float("MIN_BOND_EPOCHS", 4.0),
        ideal_bond_epochs=env_float("IDEAL_BOND_EPOCHS", 12.0),
        alert_repeat_sec=env_int("ALERT_REPEAT_SEC", 12 * 3600),
        api_failure_threshold=env_int("API_FAILURE_THRESHOLD", 2),
        alert_bidding_events=env_bool("ALERT_BIDDING_EVENTS", False),
        status_every_sec=env_int("STATUS_EVERY_SEC", 0),
        max_commission_pct=env_float("MAX_COMMISSION_PCT", 7.0),
        bad_uptime_threshold=env_float("BAD_UPTIME_THRESHOLD", 0.8),
        api_timeout_sec=env_int("API_TIMEOUT_SEC", 25),
    )


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    env_file = Path(args.env_file).expanduser() if args.env_file else None
    load_env_file(env_file)

    try:
        if args.validators_file:
            configs = configs_from_file(Path(args.validators_file).expanduser(), args)
            return run_many(
                configs, status_requested=args.status, print_only=args.print_only
            )

        config = config_from_args(args)
        return run(config, status_requested=args.status, print_only=args.print_only)
    except ConfigurationError as exc:
        parser.error(str(exc))
        return 2
    except Exception as exc:
        print(f"marinade-sam-monitor failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
