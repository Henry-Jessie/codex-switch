from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_switch import accounts, cli
from codex_switch import quota
from codex_switch.quota import AccountSnapshot, ProbeResult, RateLimitSnapshot, RateLimitWindow


def auth_data(refresh_token: str) -> dict[str, object]:
    return {
        "tokens": {
            "access_token": "access",
            "id_token": "id",
            "refresh_token": refresh_token,
        }
    }


def quota_snapshot(path: Path) -> AccountSnapshot:
    default_limit = RateLimitSnapshot(
        limit_id="codex",
        limit_name=None,
        primary=RateLimitWindow(used_percent=12, window_duration_mins=300, resets_at=1_900_000_000),
        secondary=RateLimitWindow(used_percent=34, window_duration_mins=10080, resets_at=1_900_086_400),
        credits=None,
        plan_type="pro",
    )
    return AccountSnapshot(
        auth_path=path,
        email="live@example.com",
        plan_type="pro",
        auth_method="chatgpt",
        requires_openai_auth=False,
        default_rate_limit=default_limit,
        rate_limits_by_id={"codex": default_limit},
        raw={},
    )


class AccountStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data_dir = root / "data"
        self.home_dir = root / "home"
        self.input_dir = root / "input"
        self.data_dir.mkdir()
        self.home_dir.mkdir()
        self.input_dir.mkdir()
        self.env = mock.patch.dict(
            os.environ,
            {
                accounts.DATA_DIR_ENV: str(self.data_dir),
                accounts.CODEX_HOME_ENV: str(self.home_dir),
            },
        )
        self.env.start()

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def write_auth(self, path: Path, refresh_token: str) -> None:
        path.write_text(json.dumps(auth_data(refresh_token)) + "\n")

    def test_add_account_overwrites_duplicate_name_with_cli_warning(self) -> None:
        first = self.input_dir / "first.json"
        second = self.input_dir / "second.json"
        self.write_auth(first, "first-refresh")
        self.write_auth(second, "second-refresh")

        accounts.add_account(first, "work")

        output = io.StringIO()
        with redirect_stdout(output):
            result = cli.cmd_add(str(second), "work")

        self.assertEqual(result, 0)
        self.assertIn("overwrote existing account 'work'", output.getvalue())
        stored = accounts.load_auth_file(accounts.account_path("work"))
        self.assertEqual(stored["tokens"]["refresh_token"], "second-refresh")

    def test_save_current_overwrites_duplicate_name_with_cli_warning(self) -> None:
        current = accounts.current_auth_path()
        self.write_auth(current, "first-refresh")
        accounts.save_current("work")
        self.write_auth(current, "second-refresh")

        output = io.StringIO()
        with redirect_stdout(output):
            result = cli.cmd_save("work")

        self.assertEqual(result, 0)
        self.assertIn("overwrote existing account 'work'", output.getvalue())
        stored = accounts.load_auth_file(accounts.account_path("work"))
        self.assertEqual(stored["tokens"]["refresh_token"], "second-refresh")

    def test_rm_alias_removes_account(self) -> None:
        source = self.input_dir / "source.json"
        self.write_auth(source, "refresh")
        accounts.add_account(source, "old")

        output = io.StringIO()
        with redirect_stdout(output):
            result = cli.main(["rm", "old"])

        self.assertEqual(result, 0)
        self.assertIn("Removed account 'old'", output.getvalue())
        self.assertFalse(accounts.account_path("old").exists())

    def test_mv_alias_renames_account(self) -> None:
        source = self.input_dir / "source.json"
        self.write_auth(source, "refresh")
        accounts.add_account(source, "old")

        output = io.StringIO()
        with redirect_stdout(output):
            result = cli.main(["mv", "old", "new"])

        self.assertEqual(result, 0)
        self.assertIn("Renamed 'old'", output.getvalue())
        self.assertFalse(accounts.account_path("old").exists())
        self.assertTrue(accounts.account_path("new").exists())

    def test_probe_runs_named_account_with_model(self) -> None:
        source = self.input_dir / "source.json"
        self.write_auth(source, "refresh")
        accounts.add_account(source, "work")
        expected_path = accounts.account_path("work").resolve()

        probe_result = ProbeResult(
            auth_path=expected_path,
            model="gpt-5.1-codex",
            stdout="OK",
            stderr="",
        )
        with (
            mock.patch.object(cli, "probe_account_usage", return_value=probe_result) as probe,
            mock.patch.object(cli, "query_account_snapshot", return_value=quota_snapshot(expected_path)),
        ):
            output = io.StringIO()
            with redirect_stdout(output):
                result = cli.main(["probe", "work", "--model", "gpt-5.1-codex"])

        self.assertEqual(result, 0)
        probe.assert_called_once_with(expected_path, model="gpt-5.1-codex")
        text = output.getvalue()
        self.assertIn("Probing work", text)
        self.assertIn("Probe completed", text)
        self.assertIn("reply: OK", text)
        self.assertIn("quota: 5h:12% (", text)


class CliBadAccountTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.data_dir = root / "data"
        self.home_dir = root / "home"
        self.data_dir.mkdir()
        self.home_dir.mkdir()
        self.env = mock.patch.dict(
            os.environ,
            {
                accounts.DATA_DIR_ENV: str(self.data_dir),
                accounts.CODEX_HOME_ENV: str(self.home_dir),
            },
        )
        self.env.start()
        (self.data_dir / "good.json").write_text(json.dumps(auth_data("refresh")) + "\n")
        (self.data_dir / "bad.json").write_text("not json\n")

    def tearDown(self) -> None:
        self.env.stop()
        self.tmp.cleanup()

    def test_list_keeps_showing_accounts_when_one_file_is_bad(self) -> None:
        with mock.patch.object(cli, "query_account_snapshot", side_effect=RuntimeError("quota disabled")) as query:
            output = io.StringIO()
            with redirect_stdout(output):
                result = cli.cmd_list()

        self.assertEqual(result, 0)
        text = output.getvalue()
        self.assertIn("bad", text)
        self.assertIn("good", text)
        self.assertIn("fail", text)
        self.assertEqual(query.call_count, 2)

    def test_list_uses_live_quota_when_offline_auth_summary_fails(self) -> None:
        bad_path = self.data_dir / "bad.json"
        with mock.patch.object(cli, "query_account_snapshot", return_value=quota_snapshot(bad_path)) as query:
            output = io.StringIO()
            with redirect_stdout(output):
                result = cli.cmd_list()

        self.assertEqual(result, 0)
        text = output.getvalue()
        self.assertIn("bad", text)
        self.assertIn("live@example.com", text)
        self.assertIn("5h:12% (", text)
        self.assertIn(") / wk:34% (", text)
        self.assertIn("ok", text)
        self.assertEqual(query.call_count, 2)

    def test_validate_reports_bad_file_and_continues(self) -> None:
        with mock.patch.object(cli, "query_account_snapshot", side_effect=RuntimeError("quota disabled")) as query:
            output = io.StringIO()
            with redirect_stdout(output):
                result = cli.cmd_validate(None)

        self.assertEqual(result, 0)
        text = output.getvalue()
        self.assertIn("bad", text)
        self.assertIn("Invalid JSON", text)
        self.assertIn("good", text)
        self.assertEqual(query.call_count, 2)


class ProbeCommandTests(unittest.TestCase):
    def test_probe_account_usage_runs_codex_exec_with_temporary_home(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            auth_path = Path(tmp) / "auth.json"
            auth_path.write_text(json.dumps(auth_data("refresh")) + "\n")
            completed = subprocess.CompletedProcess(
                args=[],
                returncode=0,
                stdout="OK\n",
                stderr="",
            )

            with (
                mock.patch.object(quota.shutil, "which", return_value="/usr/bin/codex"),
                mock.patch.object(quota.subprocess, "run", return_value=completed) as run,
            ):
                result = quota.probe_account_usage(auth_path, model="gpt-5.1-codex")

        self.assertEqual(result.stdout, "OK")
        command = run.call_args.args[0]
        self.assertEqual(command[:2], ["/usr/bin/codex", "exec"])
        self.assertIn("--skip-git-repo-check", command)
        self.assertIn("--ephemeral", command)
        self.assertIn("--ignore-user-config", command)
        self.assertIn("--ignore-rules", command)
        self.assertNotIn("--ask-for-approval", command)
        self.assertIn("--model", command)
        self.assertIn("gpt-5.1-codex", command)
        self.assertEqual(command[-1], "Reply with exactly OK.")
        self.assertEqual(run.call_args.kwargs["stdin"], subprocess.DEVNULL)
        self.assertIn("CODEX_HOME", run.call_args.kwargs["env"])


if __name__ == "__main__":
    unittest.main()
