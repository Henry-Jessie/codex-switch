from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from codex_switch import accounts, cli
from codex_switch import quota
from codex_switch.auth import format_epoch
from codex_switch.quota import AccountSnapshot, ProbeResult, RateLimitSnapshot, RateLimitWindow


def auth_data(refresh_token: str, account_id: str | None = None) -> dict[str, object]:
    tokens: dict[str, object] = {
        "access_token": "access",
        "id_token": "id",
        "refresh_token": refresh_token,
    }
    if account_id is not None:
        tokens["account_id"] = account_id
    return {"tokens": tokens}


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

    def test_switch_syncs_current_live_auth_to_saved_profile(self) -> None:
        (self.data_dir / "alpha.json").write_text(
            json.dumps(auth_data("refresh-a", account_id="acc-1")) + "\n"
        )
        (self.data_dir / "beta.json").write_text(
            json.dumps(auth_data("refresh-b", account_id="acc-2")) + "\n"
        )

        accounts.switch_account("alpha")

        live = accounts.current_auth_path()
        live.write_text(
            json.dumps(auth_data("refresh-a-rotated", account_id="acc-1")) + "\n"
        )

        accounts.switch_account("beta")

        alpha_saved = accounts.load_auth_file(accounts.account_path("alpha"))
        self.assertEqual(alpha_saved["tokens"]["refresh_token"], "refresh-a-rotated")

        live_data = accounts.load_auth_file(live)
        self.assertEqual(live_data["tokens"]["refresh_token"], "refresh-b")

    def test_switch_to_same_account_does_not_corrupt_saved_profile(self) -> None:
        (self.data_dir / "alpha.json").write_text(
            json.dumps(auth_data("refresh-a", account_id="acc-1")) + "\n"
        )
        accounts.switch_account("alpha")

        live = accounts.current_auth_path()
        live.write_text(
            json.dumps(auth_data("refresh-a-rotated", account_id="acc-1")) + "\n"
        )

        accounts.switch_account("alpha")

        live_data = accounts.load_auth_file(live)
        self.assertEqual(live_data["tokens"]["refresh_token"], "refresh-a-rotated")

    def test_list_uses_live_file_for_active_account(self) -> None:
        (self.data_dir / "work.json").write_text(
            json.dumps(auth_data("saved-refresh", account_id="acc-1")) + "\n"
        )

        live = accounts.current_auth_path()
        live.write_text(
            json.dumps(auth_data("live-refresh", account_id="acc-1")) + "\n"
        )

        with mock.patch.object(
            cli, "query_account_snapshot", side_effect=lambda p: quota_snapshot(p)
        ) as query:
            output = io.StringIO()
            with redirect_stdout(output):
                result = cli.cmd_list()

        self.assertEqual(result, 0)
        called_paths = [call.args[0].resolve() for call in query.call_args_list]
        self.assertIn(live.resolve(), called_paths)

    def _fake_codex_login(self, refresh_token: str, account_id: str = "acc-new"):
        """Return a side_effect that writes auth.json to CODEX_HOME."""
        def _impl(args, env, check):
            codex_home = Path(env["CODEX_HOME"])
            (codex_home / "auth.json").write_text(
                json.dumps(auth_data(refresh_token, account_id=account_id)) + "\n"
            )
            return subprocess.CompletedProcess(args=args, returncode=0)
        return _impl

    def test_login_saves_profile(self) -> None:
        with (
            mock.patch.object(accounts.shutil, "which", return_value="/usr/bin/codex"),
            mock.patch.object(accounts.subprocess, "run", side_effect=self._fake_codex_login("new-rt")),
        ):
            dst, overwritten = accounts.login_account("work")

        self.assertFalse(overwritten)
        saved = accounts.load_auth_file(dst)
        self.assertEqual(saved["tokens"]["refresh_token"], "new-rt")

    def test_login_with_device_auth_passes_flag(self) -> None:
        with (
            mock.patch.object(accounts.shutil, "which", return_value="/usr/bin/codex"),
            mock.patch.object(
                accounts.subprocess, "run", side_effect=self._fake_codex_login("dev-rt")
            ) as run,
        ):
            accounts.login_account("dev", device_auth=True)

        command = run.call_args.args[0]
        self.assertIn("--device-auth", command)

    def test_login_switch_activates_account(self) -> None:
        with (
            mock.patch.object(accounts.shutil, "which", return_value="/usr/bin/codex"),
            mock.patch.object(
                accounts.subprocess, "run", side_effect=self._fake_codex_login("sw-rt", "acc-sw")
            ),
        ):
            accounts.login_account("newbie", switch=True)

        live = accounts.load_auth_file(accounts.current_auth_path())
        self.assertEqual(live["tokens"]["refresh_token"], "sw-rt")
        self.assertEqual(accounts.identify_current_account(), "newbie")

    def test_login_overwrites_existing_profile(self) -> None:
        (self.data_dir / "work.json").write_text(
            json.dumps(auth_data("old-rt", account_id="acc-old")) + "\n"
        )
        with (
            mock.patch.object(accounts.shutil, "which", return_value="/usr/bin/codex"),
            mock.patch.object(
                accounts.subprocess, "run", side_effect=self._fake_codex_login("new-rt", "acc-new")
            ),
        ):
            dst, overwritten = accounts.login_account("work")

        self.assertTrue(overwritten)
        saved = accounts.load_auth_file(dst)
        self.assertEqual(saved["tokens"]["refresh_token"], "new-rt")

    def test_login_does_not_disturb_current_active_account(self) -> None:
        live = accounts.current_auth_path()
        self.write_auth(live, "current-rt")
        with (
            mock.patch.object(accounts.shutil, "which", return_value="/usr/bin/codex"),
            mock.patch.object(
                accounts.subprocess, "run", side_effect=self._fake_codex_login("new-rt", "acc-new")
            ),
        ):
            accounts.login_account("extra")

        live_data = accounts.load_auth_file(live)
        self.assertEqual(live_data["tokens"]["refresh_token"], "current-rt")

    def test_login_fails_when_codex_not_installed(self) -> None:
        with mock.patch.object(accounts.shutil, "which", return_value=None):
            with self.assertRaises(accounts.AccountError):
                accounts.login_account("work")

    def test_login_fails_when_codex_login_exits_nonzero(self) -> None:
        def fail(args, env, check):
            raise subprocess.CalledProcessError(1, args)

        with (
            mock.patch.object(accounts.shutil, "which", return_value="/usr/bin/codex"),
            mock.patch.object(accounts.subprocess, "run", side_effect=fail),
        ):
            with self.assertRaises(accounts.AccountError):
                accounts.login_account("work")

    def test_login_without_name_uses_current_account_name(self) -> None:
        (self.data_dir / "default.json").write_text(
            json.dumps(auth_data("old-rt", account_id="acc-1")) + "\n"
        )
        live = accounts.current_auth_path()
        self.write_auth(live, "old-rt")

        with (
            mock.patch.object(accounts.shutil, "which", return_value="/usr/bin/codex"),
            mock.patch.object(
                accounts.subprocess, "run", side_effect=self._fake_codex_login("new-rt", "acc-1")
            ),
        ):
            dst, overwritten = accounts.login_account()

        self.assertEqual(dst.stem, "default")
        self.assertTrue(overwritten)
        saved = accounts.load_auth_file(dst)
        self.assertEqual(saved["tokens"]["refresh_token"], "new-rt")

    def test_login_without_name_defaults_to_default_when_no_active(self) -> None:
        with (
            mock.patch.object(accounts.shutil, "which", return_value="/usr/bin/codex"),
            mock.patch.object(
                accounts.subprocess, "run", side_effect=self._fake_codex_login("fresh-rt", "acc-x")
            ),
        ):
            dst, overwritten = accounts.login_account()

        self.assertEqual(dst.stem, "default")
        self.assertFalse(overwritten)

    def test_login_without_name_switch_replaces_current(self) -> None:
        (self.data_dir / "default.json").write_text(
            json.dumps(auth_data("old-rt", account_id="acc-1")) + "\n"
        )
        live = accounts.current_auth_path()
        self.write_auth(live, "old-rt")

        with (
            mock.patch.object(accounts.shutil, "which", return_value="/usr/bin/codex"),
            mock.patch.object(
                accounts.subprocess, "run", side_effect=self._fake_codex_login("new-rt", "acc-1")
            ),
        ):
            accounts.login_account(switch=True)

        live_data = accounts.load_auth_file(live)
        self.assertEqual(live_data["tokens"]["refresh_token"], "new-rt")

    def test_login_includes_file_credential_config(self) -> None:
        with (
            mock.patch.object(accounts.shutil, "which", return_value="/usr/bin/codex"),
            mock.patch.object(
                accounts.subprocess, "run", side_effect=self._fake_codex_login("rt", "acc-1")
            ) as run,
        ):
            accounts.login_account("work")

        command = run.call_args.args[0]
        self.assertIn("-c", command)
        idx = command.index("-c")
        self.assertEqual(command[idx + 1], 'cli_auth_credentials_store="file"')

    def test_login_normalizes_name_before_switch_comparison(self) -> None:
        (self.data_dir / "work.json").write_text(
            json.dumps(auth_data("old-rt", account_id="acc-1")) + "\n"
        )
        live = accounts.current_auth_path()
        self.write_auth(live, "old-rt")

        with (
            mock.patch.object(accounts.shutil, "which", return_value="/usr/bin/codex"),
            mock.patch.object(
                accounts.subprocess, "run", side_effect=self._fake_codex_login("new-rt", "acc-1")
            ),
        ):
            accounts.login_account(" work ", switch=True)

        live_data = accounts.load_auth_file(live)
        self.assertEqual(live_data["tokens"]["refresh_token"], "new-rt")


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

    def test_validate_named_active_account_uses_live_file(self) -> None:
        live = accounts.current_auth_path()
        saved_data = auth_data("saved-refresh", account_id="acc-1")
        (self.data_dir / "good.json").write_text(json.dumps(saved_data) + "\n")
        live_data = auth_data("live-refresh", account_id="acc-1")
        live.write_text(json.dumps(live_data) + "\n")

        with mock.patch.object(
            cli, "query_account_snapshot", side_effect=lambda p: quota_snapshot(p)
        ) as query:
            output = io.StringIO()
            with redirect_stdout(output):
                result = cli.cmd_validate("good")

        self.assertEqual(result, 0)
        called_path = query.call_args.args[0].resolve()
        self.assertEqual(called_path, live.resolve())


class TimestampFormatTests(unittest.TestCase):
    def test_access_exp_cell_uses_utc(self) -> None:
        epoch = 1783494110
        expected = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%m-%d %H:%M")
        result = cli._strip_ansi(cli._access_exp_cell(False, epoch))
        self.assertEqual(result, expected)

    def test_format_reset_uses_utc(self) -> None:
        epoch = 1783494110
        expected = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%m-%d %H:%M")
        self.assertEqual(cli._format_reset(epoch), expected)

    def test_timestamps_consistent_with_format_epoch(self) -> None:
        epoch = 1783494110
        utc_str = datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%m-%d %H:%M")
        self.assertIn(utc_str, format_epoch(epoch))
        self.assertEqual(cli._strip_ansi(cli._access_exp_cell(False, epoch)), utc_str)
        self.assertEqual(cli._format_reset(epoch), utc_str)


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
