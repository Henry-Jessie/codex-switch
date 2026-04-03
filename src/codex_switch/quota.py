from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

class QuotaError(RuntimeError):
    pass


@dataclass(frozen=True)
class RateLimitWindow:
    used_percent: int | None
    window_duration_mins: int | None
    resets_at: int | None


@dataclass(frozen=True)
class CreditsSnapshot:
    has_credits: bool
    unlimited: bool
    balance: str | None


@dataclass(frozen=True)
class RateLimitSnapshot:
    limit_id: str | None
    limit_name: str | None
    primary: RateLimitWindow | None
    secondary: RateLimitWindow | None
    credits: CreditsSnapshot | None
    plan_type: str | None


@dataclass(frozen=True)
class AccountSnapshot:
    auth_path: Path
    email: str | None
    plan_type: str | None
    auth_method: str | None
    requires_openai_auth: bool | None
    default_rate_limit: RateLimitSnapshot | None
    rate_limits_by_id: dict[str, RateLimitSnapshot]
    raw: dict[str, Any]


def query_account_snapshot(
    auth_path: Path,
    *,
    timeout_sec: float = 10.0,
    limit_id: str = "codex",
) -> AccountSnapshot:
    auth_path = auth_path.expanduser().resolve()
    if not auth_path.exists():
        raise QuotaError(f"Auth file not found: {auth_path}")

    codex = shutil.which("codex")
    if codex is None:
        raise QuotaError("codex is not installed or not on PATH")

    try:
        from websockets.sync.client import connect
    except ImportError as exc:
        raise QuotaError(
            "Python package 'websockets' is required for quota checks"
        ) from exc

    with TemporaryDirectory(prefix="codex-switch-") as tempdir:
        codex_home = Path(tempdir) / ".codex"
        codex_home.mkdir(parents=True, exist_ok=True)
        shutil.copy2(auth_path, codex_home / "auth.json")

        port = _find_free_port()
        env = dict(os.environ)
        env["CODEX_HOME"] = str(codex_home)
        proc = subprocess.Popen(
            [codex, "app-server", "--listen", f"ws://127.0.0.1:{port}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )

        try:
            _wait_for_port(port, timeout_sec=timeout_sec)
            with connect(
                f"ws://127.0.0.1:{port}",
                open_timeout=timeout_sec,
                close_timeout=1,
            ) as ws:
                _send(ws, 0, "initialize", {"clientInfo": {"name": "codex-switch", "version": "0.1"}})
                init_resp = _recv(ws)
                _raise_for_rpc_error(init_resp)

                _send(ws, 1, "account/read", {})
                account_resp = _recv(ws)
                _raise_for_rpc_error(account_resp)

                _send(ws, 2, "getAuthStatus", {})
                auth_resp = _recv(ws)
                _raise_for_rpc_error(auth_resp)

                _send(ws, 3, "account/rateLimits/read", {})
                rate_resp = _recv(ws)
                _raise_for_rpc_error(rate_resp)
        except Exception as exc:
            _terminate(proc)
            stderr = _collect_stderr(proc)
            raise QuotaError(
                f"Quota query failed for {auth_path.name}: {exc}\n{stderr}".strip()
            ) from exc

        _terminate(proc)

    account_result = account_resp.get("result") or {}
    auth_result = auth_resp.get("result") or {}
    rate_result = rate_resp.get("result") or {}

    default_snapshot = _parse_rate_limit_snapshot(rate_result.get("rateLimits"))
    by_id_raw = rate_result.get("rateLimitsByLimitId") or {}
    if not isinstance(by_id_raw, dict):
        by_id_raw = {}
    by_id: dict[str, RateLimitSnapshot] = {}
    for key, value in by_id_raw.items():
        parsed = _parse_rate_limit_snapshot(value)
        if parsed is not None:
            by_id[key] = parsed
    if default_snapshot is None and limit_id in by_id:
        default_snapshot = by_id[limit_id]

    account_payload = account_result.get("account") or {}
    if not isinstance(account_payload, dict):
        account_payload = {}

    plan_type = _first_str(
        account_payload.get("planType"),
        default_snapshot.plan_type if default_snapshot else None,
    )

    return AccountSnapshot(
        auth_path=auth_path,
        email=_first_str(account_payload.get("email")),
        plan_type=plan_type,
        auth_method=_first_str(auth_result.get("authMethod")),
        requires_openai_auth=_as_bool(account_result.get("requiresOpenaiAuth")),
        default_rate_limit=default_snapshot,
        rate_limits_by_id=by_id,
        raw={
            "account": account_resp,
            "auth": auth_resp,
            "rate": rate_resp,
        },
    )


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_port(port: int, *, timeout_sec: float) -> None:
    deadline = time.time() + timeout_sec
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.settimeout(0.2)
            if sock.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    raise TimeoutError(f"Timed out waiting for codex app-server on port {port}")


def _send(ws: Any, request_id: int, method: str, params: dict[str, Any]) -> None:
    ws.send(
        json.dumps(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )
    )


def _recv(ws: Any) -> dict[str, Any]:
    raw = ws.recv()
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise QuotaError("Unexpected non-object JSON-RPC response")
    return data


def _raise_for_rpc_error(resp: dict[str, Any]) -> None:
    if "error" in resp:
        raise QuotaError(f"JSON-RPC error: {resp['error']}")


def _collect_stderr(proc: subprocess.Popen[str]) -> str:
    if proc.stderr is None:
        return ""
    if proc.poll() is None:
        return ""
    try:
        output = proc.stderr.read()
    except Exception:
        return ""
    return output.strip()


def _terminate(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()


def _parse_rate_limit_snapshot(raw: Any) -> RateLimitSnapshot | None:
    if not isinstance(raw, dict):
        return None
    return RateLimitSnapshot(
        limit_id=_first_str(raw.get("limitId")),
        limit_name=_first_str(raw.get("limitName")),
        primary=_parse_window(raw.get("primary")),
        secondary=_parse_window(raw.get("secondary")),
        credits=_parse_credits(raw.get("credits")),
        plan_type=_first_str(raw.get("planType")),
    )


def _parse_window(raw: Any) -> RateLimitWindow | None:
    if not isinstance(raw, dict):
        return None
    return RateLimitWindow(
        used_percent=_as_int(raw.get("usedPercent")),
        window_duration_mins=_as_int(raw.get("windowDurationMins")),
        resets_at=_as_int(raw.get("resetsAt")),
    )


def _parse_credits(raw: Any) -> CreditsSnapshot | None:
    if not isinstance(raw, dict):
        return None
    return CreditsSnapshot(
        has_credits=bool(raw.get("hasCredits")),
        unlimited=bool(raw.get("unlimited")),
        balance=_first_str(raw.get("balance")),
    )


def _first_str(*values: Any) -> str | None:
    for value in values:
        if isinstance(value, str) and value:
            return value
    return None


def _as_int(value: Any) -> int | None:
    return value if isinstance(value, int) else None


def _as_bool(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None
