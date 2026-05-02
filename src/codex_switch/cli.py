from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import __version__
from .accounts import (
    AccountError,
    add_account,
    account_path,
    current_account_display_name,
    current_auth_path,
    ensure_storage,
    get_account,
    identify_current_account,
    list_accounts,
    remove_account,
    refresh_account,
    rename_account,
    save_current,
    switch_account,
)
from .auth import TokenInfo, format_epoch, summarize_auth_data, summarize_auth_file
from .quota import AccountSnapshot, QuotaError, probe_account_usage, query_account_snapshot

RESET = "\033[0m"
BOLD = "\033[1m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        ensure_storage()

        command = getattr(args, "command", None)
        if command is None:
            parser.print_help()
            return 1

        if command == "list":
            return cmd_list()
        if command == "switch":
            return cmd_switch(args.name)
        if command == "current":
            return cmd_current()
        if command == "quota":
            return cmd_quota(args.name)
        if command == "refresh":
            return cmd_refresh(args.name)
        if command == "probe":
            return cmd_probe(args.name, args.model)
        if command == "validate":
            return cmd_validate(args.name)
        if command == "save":
            return cmd_save(args.name)
        if command == "add":
            return cmd_add(args.path, args.name)
        if command in ("remove", "rm"):
            return cmd_remove(args.name)
        if command in ("rename", "mv"):
            return cmd_rename(args.old, args.new)
    except (AccountError, QuotaError) as exc:
        print(_color(f"Error: {exc}", RED), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print(_color("Interrupted", YELLOW), file=sys.stderr)
        return 130

    parser.print_help()
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="codex-switch", description="Manage multiple Codex CLI accounts")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("list", help="List saved accounts with quota and token status")

    switch_parser = subparsers.add_parser("switch", help="Switch ~/.codex/auth.json to a saved account")
    switch_parser.add_argument("name", help="Account name to switch to")

    subparsers.add_parser("current", help="Show the currently active account")

    quota_parser = subparsers.add_parser("quota", help="Check real-time quota for the current or a named account")
    quota_parser.add_argument("name", nargs="?", help="Account name (default: current)")

    refresh_parser = subparsers.add_parser("refresh", help="Refresh tokens for the current or a named account")
    refresh_parser.add_argument("name", nargs="?", help="Account name (default: current)")

    probe_parser = subparsers.add_parser("probe", help="Run a tiny Codex request to start the quota timer")
    probe_parser.add_argument("name", nargs="?", help="Account name (default: current)")
    probe_parser.add_argument("--model", help="Model to probe with (default: Codex CLI default)")

    validate_parser = subparsers.add_parser("validate", help="Validate one account or all saved accounts")
    validate_parser.add_argument("name", nargs="?", help="Account name (default: all)")

    save_parser = subparsers.add_parser("save", help="Save the current ~/.codex/auth.json as a named account")
    save_parser.add_argument("name", help="Name for the saved account")

    add_parser = subparsers.add_parser("add", help="Import an auth.json file as a named account")
    add_parser.add_argument("path", help="Path to auth.json file")
    add_parser.add_argument("name", help="Name for the imported account")

    remove_parser = subparsers.add_parser("remove", aliases=["rm"], help="Remove a saved account")
    remove_parser.add_argument("name", help="Account name to remove")

    rename_parser = subparsers.add_parser("rename", aliases=["mv"], help="Rename a saved account")
    rename_parser.add_argument("old", help="Current account name")
    rename_parser.add_argument("new", help="New account name")

    return parser


def cmd_list() -> int:
    accounts = list_accounts()
    if not accounts:
        print("No saved accounts found.")
        return 0

    active = identify_current_account()

    # Pass 1: collect static info (offline JWT parsing, fast) to compute column widths
    static_rows: list[list[str]] = []
    for account in accounts:
        info = _safe_auth_summary(account.path)
        marker = "*" if active == account.name else " "
        static_rows.append(
            [
                marker,
                account.name,
                _auth_email(info),
                _auth_plan(info),
                "",  # quota placeholder
                _strip_ansi(_auth_access_exp_cell(info)),
                "",  # live placeholder
            ]
        )

    headers = [" ", "name", "email", "plan", "quota", "access_exp", "live"]
    min_widths = {"quota": 43, "live": 4}  # "5h:xx% (MM-DD HH:MM) / wk:xx% (MM-DD HH:MM)"
    widths = [max(len(h), min_widths.get(h, 0)) for h in headers]
    for row in static_rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    print(f"{BOLD}Saved Codex accounts{RESET}")
    header_line = "  ".join(headers[idx].ljust(widths[idx]) for idx in range(len(headers)))
    print(header_line)
    print("  ".join("-" * w for w in widths))

    # Pass 2: query quota per account and print each row immediately
    for account, static in zip(accounts, static_rows):
        info = _safe_auth_summary(account.path)
        live = _safe_quota(account.path)
        marker = _color("*", GREEN) if active == account.name else " "
        row = [
            marker,
            account.name,
            _auth_email(info, live),
            _live_plan(live) or _auth_plan(info),
            _quota_brief(live),
            _auth_access_exp_cell(info),
            _live_cell(live),
        ]
        _print_row(row, widths)

    return 0


def cmd_switch(name: str) -> int:
    dst = switch_account(name)
    print(_color(f"Switched to account '{name}'", GREEN))
    print(f"Active auth: {dst}")
    return 0


def cmd_current() -> int:
    auth_path = current_auth_path()
    if not auth_path.exists():
        raise AccountError(f"Current auth file does not exist: {auth_path}")

    info = summarize_auth_file(auth_path)
    name = current_account_display_name()
    print(f"{BOLD}Current Codex account{RESET}")
    print(f"name:  {name or _color('untracked', YELLOW)}")
    print(f"path:  {auth_path}")
    print(f"email: {info.email or '-'}")
    print(f"plan:  {info.plan_type or '-'}")
    print(f"access token: {_expiry_text(info.access_expired, info.access_exp)}")
    print(f"id token:     {_expiry_text(info.id_expired, info.id_exp)}")
    print(f"refresh:      {'present' if info.refresh_token_present else 'missing'}")
    return 0


def cmd_quota(name: str | None) -> int:
    target_name, auth_path = _resolve_target(name)
    snapshot = query_account_snapshot(auth_path)

    print(f"{BOLD}Quota for {target_name}{RESET}")
    print(f"email: {snapshot.email or '-'}")
    print(f"plan:  {snapshot.plan_type or '-'}")
    print(f"auth:  {snapshot.auth_method or '-'}")
    print("")

    rows: list[list[str]] = []
    for bucket_name, quota in sorted(snapshot.rate_limits_by_id.items()):
        rows.append(
            [
                bucket_name,
                quota.limit_name or "-",
                _window_brief(quota.primary),
                _window_brief(quota.secondary),
                _credits_brief(quota),
                quota.plan_type or "-",
            ]
        )

    if rows:
        _print_table(["bucket", "name", "5h", "week", "credits", "plan"], rows)
    else:
        print("No rate-limit data returned.")
    return 0


def cmd_refresh(name: str | None) -> int:
    updated_paths, refreshed_data = refresh_account(name)
    info = summarize_auth_data(refreshed_data)
    label = name or current_account_display_name() or "current"

    print(f"{BOLD}Refreshed {label}{RESET}")
    print(f"email: {info.email or '-'}")
    print(f"plan:  {info.plan_type or '-'}")
    print(f"access token: {_expiry_text(info.access_expired, info.access_exp)}")
    print(f"id token:     {_expiry_text(info.id_expired, info.id_exp)}")
    print("")
    print("Updated files:")
    for path in updated_paths:
        print(f"  {path}")
    return 0


def cmd_probe(name: str | None, model: str | None) -> int:
    target_name, auth_path = _resolve_target(name)
    print(f"{BOLD}Probing {target_name}{RESET}")
    print(f"auth:  {auth_path}")
    print(f"model: {model or 'Codex CLI default'}")
    result = probe_account_usage(auth_path, model=model)
    print(_color("Probe completed", GREEN))
    if result.stdout:
        print(f"reply: {result.stdout}")

    live = _safe_quota(auth_path)
    if isinstance(live, Exception):
        print(f"quota: {_color(str(live), RED)}")
    else:
        print(f"email: {_live_email(live) or '-'}")
        print(f"plan:  {_live_plan(live) or '-'}")
        print(f"quota: {_quota_brief(live)}")
    return 0


def cmd_validate(name: str | None) -> int:
    if name:
        targets = [get_account(name)]
    else:
        targets = list_accounts()
        if not targets:
            print("No saved accounts found.")
            return 0

    for index, account in enumerate(targets):
        info = _safe_auth_summary(account.path)
        live = _safe_quota(account.path)
        if index:
            print("")
        print(f"{BOLD}{account.name}{RESET}")
        if isinstance(info, Exception):
            print(f"  auth:    {_color(str(info), RED)}")
            if isinstance(live, Exception):
                print(f"  live:    {_color(str(live), RED)}")
            else:
                print(f"  email:   {_live_email(live) or '-'}")
                print(f"  plan:    {_live_plan(live) or '-'}")
                print(f"  live:    {_color('OK', GREEN)} {_quota_brief(live)}")
            continue
        print(f"  email:   {info.email or _live_email(live) or '-'}")
        print(f"  plan:    {_live_plan(live) or info.plan_type or '-'}")
        print(f"  access:  {_expiry_text(info.access_expired, info.access_exp)}")
        print(f"  id:      {_expiry_text(info.id_expired, info.id_exp)}")
        print(f"  refresh: {'present' if info.refresh_token_present else _color('missing', RED)}")
        if isinstance(live, Exception):
            print(f"  live:    {_color(str(live), RED)}")
        else:
            print(f"  live:    {_color('OK', GREEN)} {_quota_brief(live)}")
    return 0


def cmd_save(name: str) -> int:
    will_overwrite = account_path(name).exists()
    dst = save_current(name)
    if will_overwrite:
        print(_color(f"Warning: overwrote existing account '{dst.stem}'", YELLOW))
    print(_color(f"Saved current auth as '{dst.stem}'", GREEN))
    print(dst)
    return 0


def cmd_add(path: str, name: str) -> int:
    will_overwrite = account_path(name).exists()
    dst = add_account(Path(path), name)
    if will_overwrite:
        print(_color(f"Warning: overwrote existing account '{dst.stem}'", YELLOW))
    print(_color(f"Imported account '{dst.stem}'", GREEN))
    print(dst)
    return 0


def cmd_remove(name: str) -> int:
    path = remove_account(name)
    print(_color(f"Removed account '{name}'", GREEN))
    print(path)
    return 0


def cmd_rename(old: str, new: str) -> int:
    dst = rename_account(old, new)
    print(_color(f"Renamed '{old}' → '{new}'", GREEN))
    print(dst)
    return 0


def _resolve_target(name: str | None) -> tuple[str, Path]:
    if name:
        account = get_account(name)
        return account.name, account.path

    path = current_auth_path()
    if not path.exists():
        raise AccountError(f"Current auth file does not exist: {path}")
    return current_account_display_name() or "current", path


def _safe_auth_summary(path: Path) -> TokenInfo | Exception:
    try:
        return summarize_auth_file(path)
    except Exception as exc:
        return exc


def _safe_quota(path: Path) -> AccountSnapshot | Exception:
    try:
        return query_account_snapshot(path)
    except Exception as exc:
        return exc


def _live_email(snapshot: AccountSnapshot | Exception) -> str | None:
    return snapshot.email if isinstance(snapshot, AccountSnapshot) else None


def _live_plan(snapshot: AccountSnapshot | Exception) -> str | None:
    return snapshot.plan_type if isinstance(snapshot, AccountSnapshot) else None


def _auth_email(info: TokenInfo | Exception, live: AccountSnapshot | Exception | None = None) -> str:
    if isinstance(info, TokenInfo):
        return info.email or _live_email(live) or "-"
    return _live_email(live) or "-"


def _auth_plan(info: TokenInfo | Exception) -> str:
    if isinstance(info, TokenInfo):
        return info.plan_type or "-"
    return "-"


def _auth_access_exp_cell(info: TokenInfo | Exception) -> str:
    if isinstance(info, TokenInfo):
        return _access_exp_cell(info.access_expired, info.access_exp)
    return "-"


def _quota_brief(snapshot: AccountSnapshot | Exception) -> str:
    if not isinstance(snapshot, AccountSnapshot):
        return _color("ERR", RED)
    quota = snapshot.default_rate_limit
    if quota is None:
        return "-"
    return f"5h:{_window_brief(quota.primary)} / wk:{_window_brief(quota.secondary)}"


def _window_pct(window: Any) -> str:
    used = getattr(window, "used_percent", None)
    return f"{used}%" if isinstance(used, int) else "-"


def _window_brief(window: Any) -> str:
    used = getattr(window, "used_percent", None)
    resets_at = getattr(window, "resets_at", None)
    if not isinstance(used, int):
        return "-"
    return f"{used}% ({_format_reset(resets_at)})"


def _credits_brief(quota: Any) -> str:
    credits = getattr(quota, "credits", None)
    if credits is None:
        return "-"
    balance = credits.balance if credits.balance is not None else "-"
    if credits.unlimited:
        return "unlimited"
    if credits.has_credits:
        return balance
    return balance


def _format_reset(epoch: int | None) -> str:
    if epoch is None:
        return "-"
    return datetime.fromtimestamp(epoch).strftime("%m-%d %H:%M")


def _token_cell(expired: bool | None) -> str:
    if expired is None:
        return "-"
    return _color("expired", RED) if expired else _color("ok", GREEN)


def _access_exp_cell(expired: bool | None, epoch: int | None) -> str:
    if epoch is None:
        return "-"
    label = datetime.fromtimestamp(epoch).strftime("%m-%d %H:%M")
    if expired:
        return _color(label, RED)
    return _color(label, GREEN)


def _live_cell(snapshot: AccountSnapshot | Exception) -> str:
    return _color("ok", GREEN) if isinstance(snapshot, AccountSnapshot) else _color("fail", RED)


def _expiry_text(expired: bool | None, epoch: int | None) -> str:
    label = format_epoch(epoch)
    if expired is None:
        return label
    color = RED if expired else GREEN
    prefix = "expired" if expired else "ok"
    return f"{_color(prefix, color)} ({label})"


def _color(text: str, color: str) -> str:
    return f"{color}{text}{RESET}"


def _print_row(row: list[str], widths: list[int]) -> None:
    plain = [_strip_ansi(cell) for cell in row]
    print("  ".join(
        row[idx].ljust(widths[idx] + (len(row[idx]) - len(plain[idx])))
        for idx in range(len(row))
    ), flush=True)


def _print_table(headers: list[str], rows: list[list[str]]) -> None:
    widths = [len(header) for header in headers]
    plain_rows = [[_strip_ansi(cell) for cell in row] for row in rows]
    for row in plain_rows:
        for idx, cell in enumerate(row):
            widths[idx] = max(widths[idx], len(cell))

    header_line = "  ".join(header.ljust(widths[idx]) for idx, header in enumerate(headers))
    print(header_line)
    print("  ".join("-" * width for width in widths))
    for row, plain_row in zip(rows, plain_rows):
        print("  ".join(row[idx].ljust(widths[idx] + (len(row[idx]) - len(plain_row[idx]))) for idx in range(len(row))))


def _strip_ansi(value: str) -> str:
    result = []
    in_escape = False
    for char in value:
        if char == "\033":
            in_escape = True
            continue
        if in_escape:
            if char == "m":
                in_escape = False
            continue
        result.append(char)
    return "".join(result)
