import json
import os
import tempfile
import unittest
from pathlib import Path

from marinade_sam_monitor.cli import (
    build_parser,
    calculate_reserve,
    configs_from_file,
    event_key,
    find_by_vote,
    fmt_sol,
    latest_score,
    load_env_file,
    sol_from_lamports,
)


class FormattingTests(unittest.TestCase):
    def test_fmt_sol(self) -> None:
        self.assertEqual(fmt_sol(2.34567), "2.3457 SOL")
        self.assertEqual(fmt_sol(12.34567), "12.35 SOL")
        self.assertEqual(fmt_sol(1234.5), "1,234 SOL")

    def test_sol_from_lamports(self) -> None:
        self.assertEqual(sol_from_lamports(1_500_000_000), 1.5)
        self.assertEqual(sol_from_lamports(None), 0.0)


class ApiShapeTests(unittest.TestCase):
    def test_find_by_vote_supports_snake_and_camel_case(self) -> None:
        rows = [
            {"vote_account": "vote-a", "value": 1},
            {"voteAccount": "vote-b", "value": 2},
            {"vote_account": "vote-c", "value": 3},
        ]
        self.assertEqual(
            find_by_vote(rows, "vote-a"), [{"vote_account": "vote-a", "value": 1}]
        )
        self.assertEqual(
            find_by_vote(rows, "vote-b"), [{"voteAccount": "vote-b", "value": 2}]
        )

    def test_latest_score_prefers_latest_epoch_then_scoring_run(self) -> None:
        rows = [
            {"epoch": 10, "scoringRunId": 2},
            {"epoch": 11, "scoringRunId": 1},
            {"epoch": 11, "scoringRunId": 3},
        ]
        self.assertEqual(latest_score(rows), {"epoch": 11, "scoringRunId": 3})

    def test_event_key_includes_common_unique_fields(self) -> None:
        first = event_key(
            {
                "epoch": 1,
                "reason": "Test",
                "amount": 10,
                "vote_account": "vote",
                "signature": "sig-a",
            }
        )
        second = event_key(
            {
                "epoch": 1,
                "reason": "Test",
                "amount": 10,
                "vote_account": "vote",
                "signature": "sig-b",
            }
        )
        self.assertNotEqual(first, second)


class ReserveCalculationTests(unittest.TestCase):
    def test_calculate_reserve_uses_bid_runway_and_settlement(self) -> None:
        score = {
            "effectiveBid": 0.1,
            "values": {
                "marinadeActivatedStakeSol": 10_000,
                "bondBalanceSol": 100,
            },
            "metadata": {"scoringConfig": {"minBondBalanceSol": 7}},
        }
        bond = {"remaining_settlement_claim_amount": 3_000_000_000}
        reserve = calculate_reserve(score, bond, min_epochs=4, ideal_epochs=12)
        self.assertEqual(reserve.epoch_bid_cost, 1.0)
        self.assertEqual(reserve.settlement_sol, 3.0)
        self.assertEqual(reserve.min_required, 14.0)
        self.assertEqual(reserve.ideal_required, 22.0)

    def test_calculate_reserve_supports_legacy_settlement_typo(self) -> None:
        reserve = calculate_reserve(
            None, {"remainining_settlement_claim_amount": 2_000_000_000}, 4, 12
        )
        self.assertEqual(reserve.settlement_sol, 2.0)


class MultiValidatorConfigTests(unittest.TestCase):
    def test_configs_from_file_merges_shared_defaults_and_validator_overrides(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "validators.json"
            path.write_text(
                json.dumps(
                    {
                        "telegram_token": "token",
                        "telegram_chat_id": "chat",
                        "defaults": {"status_every_sec": 86400, "min_bond_epochs": 5},
                        "validators": [
                            {
                                "name": "Validator A",
                                "vote_account": "vote-a",
                                "state_file": "state/a.json",
                            },
                            {
                                "name": "Validator B",
                                "vote_account": "vote-b",
                                "state_file": "state/b.json",
                                "telegram_thread_id": "42",
                                "min_bond_epochs": 7,
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )
            args = build_parser().parse_args(
                ["--validators-file", str(path), "--print-only"]
            )
            configs = configs_from_file(path, args)
            self.assertEqual(len(configs), 2)
            self.assertEqual(configs[0].vote_account, "vote-a")
            self.assertEqual(configs[0].telegram_token, "token")
            self.assertEqual(configs[0].status_every_sec, 86400)
            self.assertEqual(configs[0].min_bond_epochs, 5)
            self.assertEqual(configs[0].state_file, Path(tmp) / "state/a.json")
            self.assertEqual(configs[1].telegram_thread_id, "42")
            self.assertEqual(configs[1].min_bond_epochs, 7)


class EnvFileTests(unittest.TestCase):
    def test_load_env_file_does_not_override_existing_env(self) -> None:
        key = "MARINADE_SAM_MONITOR_TEST_VALUE"
        os.environ[key] = "already-set"
        try:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / ".env"
                path.write_text(
                    f"{key}=from-file\nNEW_TEST_VALUE='created'\n", encoding="utf-8"
                )
                os.environ.pop("NEW_TEST_VALUE", None)
                load_env_file(path)
                self.assertEqual(os.environ[key], "already-set")
                self.assertEqual(os.environ["NEW_TEST_VALUE"], "created")
        finally:
            os.environ.pop(key, None)
            os.environ.pop("NEW_TEST_VALUE", None)


if __name__ == "__main__":
    unittest.main()
