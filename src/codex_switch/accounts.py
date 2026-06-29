from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TYPE_CHECKING, Any

from .quota import refresh_account_tokens

if TYPE_CHECKING:
    from .auth import TokenInfo

DATA_DIR_ENV = "CODEX_SWITCH_DATA_DIR"
CODEX_HOME_ENV = "CODEX_SWITCH_CODEX_HOME"

DEFAULT_DATA_DIR = Path.home() / ".codex-switch"
DEFAULT_CODEX_HOME = Path.home() / ".codex"


class AccountError(RuntimeError):
    pass


@dataclass(frozen=True)
class StoredAccount:
    name: str
    path: Path


def data_dir() -> Path:
    return Path(os.environ.get(DATA_DIR_ENV, str(DEFAULT_DATA_DIR))).expanduser()


def codex_home_dir() -> Path:
    return Path(os.environ.get(CODEX_HOME_ENV, str(DEFAULT_CODEX_HOME))).expanduser()


def current_auth_path() -> Path:
    return codex_home_dir() / "auth.json"


def _normalize_name(name: str) -> str:
    normalized = name.strip()
    if not normalized:
        raise AccountError("Account name cannot be empty")
    if not re.fullmatch(r"[A-Za-z0-9._-]+", normalized):
        raise AccountError(
            "Account name must match [A-Za-z0-9._-]+ to avoid ambiguous file paths"
        )
    return normalized


def ensure_storage() -> None:
    data_dir().mkdir(parents=True, exist_ok=True)


def list_accounts() -> list[StoredAccount]:
    ensure_storage()
    accounts: list[StoredAccount] = []
    for path in sorted(data_dir().glob("*.json")):
        accounts.append(StoredAccount(name=path.stem, path=path))
    return accounts


def account_path(name: str) -> Path:
    return data_dir() / f"{_normalize_name(name)}.json"


def get_account(name: str) -> StoredAccount:
    path = account_path(name)
    if not path.exists():
        raise AccountError(f"No saved account named '{name}'")
    return StoredAccount(name=path.stem, path=path)


def load_auth_file(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text())
    except FileNotFoundError as exc:
        raise AccountError(f"Missing auth file: {path}") from exc
    except json.JSONDecodeError as exc:
        raise AccountError(f"Invalid JSON in auth file {path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise AccountError(f"Auth file {path} does not contain a JSON object")
    return raw


def _validate_auth_shape(raw: dict[str, Any], source: Path) -> None:
    if "tokens" not in raw or not isinstance(raw["tokens"], dict):
        raise AccountError(f"{source} is missing a tokens object")
    for field in ("access_token", "id_token", "refresh_token"):
        if field not in raw["tokens"]:
            raise AccountError(f"{source} is missing tokens.{field}")


def remove_account(name: str) -> Path:
    acct = get_account(name)
    current = identify_current_account()
    if current == name:
        raise AccountError(f"Cannot remove '{name}' — it is the currently active account. Switch first.")
    acct.path.unlink()
    return acct.path


def rename_account(old_name: str, new_name: str) -> Path:
    acct = get_account(old_name)
    dst = account_path(new_name)
    if dst.exists():
        raise AccountError(f"Account '{new_name}' already exists")
    acct.path.rename(dst)
    return dst


def save_current(name: str) -> Path:
    src = current_auth_path()
    if not src.exists():
        raise AccountError(f"Current auth file does not exist: {src}")
    raw = load_auth_file(src)
    _validate_auth_shape(raw, src)
    dst = account_path(name)
    shutil.copy2(src, dst)
    return dst


def add_account(source: Path, name: str) -> Path:
    source = source.expanduser().resolve()
    raw = load_auth_file(source)
    _validate_auth_shape(raw, source)
    dst = account_path(name)
    dst.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
    return dst


def login_account(
    name: str | None = None, *, device_auth: bool = False, switch: bool = False
) -> tuple[Path, bool]:
    """Log in via Codex CLI OAuth and save as a named account.

    Runs ``codex login`` inside a temporary ``CODEX_HOME`` so the current
    active account in ``~/.codex/auth.json`` is not disturbed.  The resulting
    ``auth.json`` is validated and saved to the profile store.

    If *name* is ``None`` the current account's display name is used (or
    ``"default"`` when no active account is identified), making
    ``codex-switch login`` a convenient re-login for the current account.

    Returns ``(destination_path, overwritten)``.
    """
    codex = shutil.which("codex")
    if codex is None:
        raise AccountError("codex is not installed or not on PATH")

    with TemporaryDirectory(prefix="codex-switch-login-") as tempdir:
        codex_home = Path(tempdir) / ".codex"
        codex_home.mkdir(parents=True, exist_ok=True)

        env = dict(os.environ)
        env["CODEX_HOME"] = str(codex_home)

        command = [codex, "login", "-c", 'cli_auth_credentials_store="file"']
        if device_auth:
            command.append("--device-auth")

        try:
            subprocess.run(command, env=env, check=True)
        except subprocess.CalledProcessError as exc:
            raise AccountError(
                f"codex login failed with exit code {exc.returncode}"
            ) from exc

        temp_auth = codex_home / "auth.json"
        if not temp_auth.exists():
            raise AccountError("Login did not produce an auth.json file")

        raw = load_auth_file(temp_auth)
        _validate_auth_shape(raw, temp_auth)

        if name is None:
            name = current_account_display_name() or "default"
        else:
            name = _normalize_name(name)

        will_overwrite = account_path(name).exists()
        dst = account_path(name)
        shutil.copy2(temp_auth, dst)

    if switch:
        current = current_account_display_name()
        if current is not None and current != name:
            _sync_current_to_saved()
        live_path = current_auth_path()
        live_path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(dst, live_path)

    return dst, will_overwrite


def switch_account(name: str) -> Path:
    acct = get_account(name)
    dst_dir = codex_home_dir()
    dst_dir.mkdir(parents=True, exist_ok=True)
    _sync_current_to_saved()
    shutil.copy2(acct.path, current_auth_path())
    return current_auth_path()


def _sync_current_to_saved() -> None:
    """Sync the live auth.json back to its matching saved profile.

    Codex CLI auto-refreshes tokens in ~/.codex/auth.json.  Without this
    sync, those refreshed tokens are lost when switching to another account
    because the saved profile still holds the old snapshot.
    """
    live_path = current_auth_path()
    if not live_path.exists():
        return
    current_name = identify_current_account()
    if current_name is None:
        return
    saved_path = account_path(current_name)
    if saved_path.exists():
        shutil.copy2(live_path, saved_path)


def refresh_account(name: str | None) -> tuple[list[Path], dict[str, Any]]:
    from .auth import summarize_auth_data, summarize_auth_file

    updated_paths: list[Path] = []

    if name is not None:
        saved = get_account(name)
        refreshed_data = refresh_account_tokens(saved.path)
        refreshed_info = summarize_auth_data(refreshed_data)

        _write_auth_data(saved.path, refreshed_data)
        updated_paths.append(saved.path)

        live_path = current_auth_path()
        if live_path.exists():
            live_info = summarize_auth_file(live_path)
            if _same_logical_account(refreshed_info, live_info):
                _write_auth_data(live_path, refreshed_data)
                updated_paths.append(live_path)

        return updated_paths, refreshed_data

    live_path = current_auth_path()
    if not live_path.exists():
        raise AccountError(f"Current auth file does not exist: {live_path}")

    refreshed_data = refresh_account_tokens(live_path)
    refreshed_info = summarize_auth_data(refreshed_data)

    _write_auth_data(live_path, refreshed_data)
    updated_paths.append(live_path)

    matching_accounts: list[Path] = []
    for account in list_accounts():
        try:
            account_info = summarize_auth_file(account.path)
        except AccountError:
            continue
        if _same_logical_account(refreshed_info, account_info):
            matching_accounts.append(account.path)

    if len(matching_accounts) == 1:
        _write_auth_data(matching_accounts[0], refreshed_data)
        updated_paths.append(matching_accounts[0])

    return updated_paths, refreshed_data


def _sha256(path: Path) -> str | None:
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    return hashlib.sha256(data).hexdigest()


def identify_current_account() -> str | None:
    current_hash = _sha256(current_auth_path())
    if current_hash is None:
        return None

    accounts = list_accounts()
    for account in accounts:
        if _sha256(account.path) == current_hash:
            return account.name

    from .auth import summarize_auth_file

    try:
        current_info = summarize_auth_file(current_auth_path())
    except AccountError:
        return None

    if current_info.account_id:
        account_id_matches: list[str] = []
        for account in accounts:
            try:
                info = summarize_auth_file(account.path)
            except AccountError:
                continue
            if info.account_id == current_info.account_id:
                account_id_matches.append(account.name)
        if len(account_id_matches) == 1:
            return account_id_matches[0]

    if current_info.email:
        email_matches: list[str] = []
        for account in accounts:
            try:
                info = summarize_auth_file(account.path)
            except AccountError:
                continue
            if info.email == current_info.email:
                email_matches.append(account.name)
        if len(email_matches) == 1:
            return email_matches[0]

    return None


def current_account_display_name() -> str | None:
    return identify_current_account()


def _same_logical_account(info_a: TokenInfo, info_b: TokenInfo) -> bool:
    if info_a.account_id and info_b.account_id:
        return info_a.account_id == info_b.account_id
    if info_a.email and info_b.email:
        return info_a.email == info_b.email
    return False


def _write_auth_data(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
