from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[1]
SCRIPT = ROOT / "scripts" / "diag_live_payout.py"


@pytest.mark.parametrize(
    ("argv", "expected_reason"),
    [
        ([], "receiver is required"),
        (
            ["--receiver", "not-allowlisted@example.com"],
            "receiver is not present in config/policy.json merchant_allowlist",
        ),
    ],
)
def test_diag_receiver_validation_refuses_without_payout_or_decision(
    argv, expected_reason, tmp_path
):
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT / "src")
    environment["LEASH_DB_PATH"] = str(tmp_path / "leash.db")
    environment["PAYOUTS_DB_PATH"] = str(tmp_path / "payouts.db")
    result = subprocess.run(
        [sys.executable, str(SCRIPT), *argv],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode != 0
    assert expected_reason in result.stderr
    assert not (tmp_path / "leash.db").exists()
    assert not (tmp_path / "payouts.db").exists()
    assert "receiver=" not in result.stdout
