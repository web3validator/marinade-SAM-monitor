# Public Goods Contribution Proposal: Marinade SAM Monitor

## Summary

`marinade-sam-monitor` is a read-only Telegram monitor for Solana validators participating in Marinade SAM / Validator Bonds. It helps operators detect bond reserve risk, protected events, scoring penalties, validator API warnings, and API outages early enough to react manually.

The goal is to provide a small, auditable, operator-friendly public good that any validator can run with only a vote account and Telegram bot credentials.

## Problem

Marinade SAM participants need to track several public data sources to stay healthy:

- Validator Bonds API for bond parameters and settlement claims;
- Protected Events API for chargeable events;
- SAM scoring API for effective bid, target stake, bond balance, penalties, and blacklist signals;
- Validators API for warnings, commission, version, and uptime-adjacent signals.

Without a simple monitor, operators may notice issues late, especially when bond reserve runway falls below safe levels or scoring penalties appear.

## Solution

This repository packages an existing operational monitor into a reusable public project:

- Python CLI with no third-party runtime dependencies;
- single-validator `.env` mode;
- multi-validator `validators.json` mode for one bot token monitoring many vote accounts;
- Telegram alerts with clear severity labels;
- persistent JSON state for deduplication and recovery alerts;
- systemd service/timer examples;
- CI and unit tests;
- explicit security model: no keys, no signing, no transactions, no automated top-ups.

## Suggested operating rhythm

- Run every 30 minutes.
- First run initializes state and sends a status snapshot.
- Later runs alert only on new or changed conditions.
- Reserve warnings repeat every 12 hours by default while still active.
- API outage alerts require two consecutive failed runs by default.
- Optional daily status can be enabled with `STATUS_EVERY_SEC=86400`.

## Safety guarantees

The monitor is designed to be safe on a non-validator host:

- only reads public Marinade HTTPS APIs;
- only writes a local JSON state file;
- only sends Telegram Bot API messages;
- never reads validator keys, seed phrases, identities, vote keys, or withdraw authority keys;
- never signs, simulates, builds, or broadcasts transactions;
- never performs automatic bond top-ups.

## Public-good value for Marinade

- Reduces operational mistakes for SAM participants.
- Makes Validator Bonds risk more visible to smaller validators.
- Provides an auditable reference implementation for community alerting.
- Encourages standardized, actionable alert wording.
- Can be extended by the community with additional notification sinks and better scoring heuristics.

## Future improvements

- Docker and Kubernetes examples.
- Prometheus metrics exporter mode.
- Additional notification sinks such as Discord, Slack, Matrix, and webhooks.
- API response fixtures collected from real-world Marinade shape changes.
- Optional public dashboard mode for read-only status pages.
- More precise reserve heuristic if Marinade publishes an official formula.

## Review request

Feedback requested from Marinade and validator operators:

1. Are the reserve thresholds and alert severities aligned with current SAM expectations?
2. Are any public API fields missing from the alert model?
3. Should an official API schema or sample fixtures be added?
4. Would Marinade prefer any wording changes for protected-event or bond-risk alerts?
