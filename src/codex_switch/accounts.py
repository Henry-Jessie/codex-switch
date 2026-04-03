from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

DATA_DIR_ENV = "CODEX_SWITCH_DATA_DIR"
CODEX_HOME_ENV = "CODEX_SWITCH_CODEX_HOME"

DEFAULT_DATA_DIR = Path.home() / ".codex-switch"
DEFAULT_CODEX_HOME = Path.home() / ".codex"
CURRENT_FILENAME = ".current"


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


def state_path() -> Path:
    return data_dir() / CURRENT_FILENAME


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
    if read_current_name() == old_name:
        write_current_name(new_name)
    return dst


def save_current(name: str) -> Path:
    src = current_auth_path()
    if not src.exists():
        raise AccountError(f"Current auth file does not exist: {src}")
    raw = load_auth_file(src)
    _validate_auth_shape(raw, src)
    dst = account_path(name)
    dst.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
    write_current_name(dst.stem)
    return dst


def add_account(source: Path, name: str) -> Path:
    source = source.expanduser().resolve()
    raw = load_auth_file(source)
    _validate_auth_shape(raw, source)
    dst = account_path(name)
    dst.write_text(json.dumps(raw, indent=2, sort_keys=True) + "\n")
    return dst


def _backup_dir() -> Path:
    root = data_dir() / "backups"
    root.mkdir(parents=True, exist_ok=True)
    return root


def backup_current() -> Path | None:
    src = current_auth_path()
    if not src.exists():
        return None
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dst = _backup_dir() / f"auth-{timestamp}.json"
    shutil.copy2(src, dst)
    return dst


def switch_account(name: str) -> tuple[Path | None, Path]:
    acct = get_account(name)
    backup = backup_current()
    dst_dir = codex_home_dir()
    dst_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(acct.path, current_auth_path())
    write_current_name(acct.name)
    return backup, current_auth_path()


def read_current_name() -> str | None:
    path = state_path()
    if not path.exists():
        return None
    text = path.read_text().strip()
    return text or None


def write_current_name(name: str) -> None:
    state_path().write_text(f"{_normalize_name(name)}\n")


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
    for account in list_accounts():
        if _sha256(account.path) == current_hash:
            return account.name
    return None


def current_account_display_name() -> str | None:
    matched = identify_current_account()
    if matched:
        return matched
    stored = read_current_name()
    if stored and account_path(stored).exists():
        return stored
    return None
