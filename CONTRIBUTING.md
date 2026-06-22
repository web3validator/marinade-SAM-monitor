# Contributing

Thanks for helping make Marinade SAM monitoring easier for validator operators.

## Good first contributions

- Add sample API fixtures and regression tests.
- Improve deployment docs for your environment.
- Add notification backends while keeping Telegram as the default path.
- Improve wording of alerts so they are more actionable for operators.
- Document Marinade API shape changes.

## Development workflow

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
python -m unittest discover -s tests -v
python -m compileall src tests
```

## Safety requirements

Pull requests must preserve the monitor-only safety model:

- no private key reads;
- no seed phrase handling;
- no transaction signing;
- no transaction broadcasting;
- no automatic bond top-ups;
- no committed Telegram tokens or operator secrets.

If a future feature needs write access to any external service, make it explicitly opt-in and document the risk.
