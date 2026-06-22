# Security Policy

## Supported versions

The project is currently pre-1.0 operational tooling. Security fixes should target the default branch unless releases are later formalized.

## Reporting a vulnerability

Please open a private security advisory on GitHub if available, or contact the maintainers through the repository owner.

Do not include real Telegram tokens, validator identities, keypair paths, seed phrases, or private infrastructure details in public issues.

## Security model

`marinade-sam-monitor` is designed as read-only monitoring software. It should only:

- read public Marinade HTTPS APIs;
- write a local JSON state file;
- send Telegram Bot API messages.

It must not read validator keys, sign transactions, broadcast transactions, or automate bond top-ups.
