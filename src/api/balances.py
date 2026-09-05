"""Live provider balances for the console's spend page and depth gating.

Two providers fund a run and they fail differently, so they are reported
separately rather than as one number:

  OpenRouter  pays for LLM calls. Its /credits endpoint is authoritative and
              needs no special scope, so this is always a real balance.

  Bright Data pays for discovery records. Its balance endpoint needs a
              key scoped Admin or Finance (a Bright Data API key is one of
              five scopes: Admin, Finance, Ops, Limit, User); the collector
              keys this harness discovers with are deliberately scoped
              Ops/User and get a 403. When that happens the number is
              DERIVED -- a configured starting balance minus the record
              ledger the harness itself keeps -- and is labelled as such, so
              the console never presents an estimate as a live reading.

Every result carries `source` ("live" | "derived" | "unavailable") and the
UI is expected to show it. A wrong balance silently presented as live is
how a run gets launched against money that is not there.
"""

from __future__ import annotations

import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

from src.config import get_config

_TIMEOUT = 12.0
_CACHE_TTL_SECONDS = 60.0
_cache: dict[str, tuple[float, dict[str, Any]]] = {}


@dataclass
class Balance:
    provider: str
    available_usd: float | None
    source: str                    # live | derived | unavailable
    detail: str = ""
    spent_usd: float | None = None
    limit_usd: float | None = None
    # Only set when source == "derived"; tells the UI what to tell the user
    # so they can upgrade the reading to a live one.
    remediation: str = ""


def _cached(key: str) -> dict[str, Any] | None:
    hit = _cache.get(key)
    if hit and (time.monotonic() - hit[0]) < _CACHE_TTL_SECONDS:
        return hit[1]
    return None


def _store(key: str, value: dict[str, Any]) -> dict[str, Any]:
    _cache[key] = (time.monotonic(), value)
    return value


def openrouter_balance(force: bool = False) -> dict[str, Any]:
    """Credits purchased minus credits used, from OpenRouter's own ledger."""
    if not force:
        hit = _cached("openrouter")
        if hit:
            return hit

    key = (get_config().openrouter.api_key or "").strip()
    if not key:
        return _store("openrouter", asdict(Balance(
            provider="openrouter", available_usd=None, source="unavailable",
            detail="OPENROUTER_API_KEY is not set.",
        )))

    try:
        resp = httpx.get(
            "https://openrouter.ai/api/v1/credits",
            headers={"Authorization": f"Bearer {key}"},
            timeout=_TIMEOUT,
        )
        resp.raise_for_status()
        data = (resp.json() or {}).get("data") or {}
        granted = float(data.get("total_credits") or 0.0)
        used = float(data.get("total_usage") or 0.0)
        return _store("openrouter", asdict(Balance(
            provider="openrouter",
            available_usd=round(granted - used, 4),
            source="live",
            detail="OpenRouter /credits",
            spent_usd=round(used, 4),
            limit_usd=round(granted, 4),
        )))
    except Exception as exc:
        return _store("openrouter", asdict(Balance(
            provider="openrouter", available_usd=None, source="unavailable",
            detail=f"{type(exc).__name__}: {exc}",
        )))


def brightdata_balance(force: bool = False) -> dict[str, Any]:
    """Live where the token allows it; otherwise derived from the ledger."""
    if not force:
        hit = _cached("brightdata")
        if hit:
            return hit

    cfg = get_config()
    key = (cfg.brightdata.api_key or "").strip()
    if not key:
        return _store("brightdata", asdict(Balance(
            provider="brightdata", available_usd=None, source="unavailable",
            detail="BRIGHTDATA_API_KEY is not set.",
        )))

    try:
        resp = httpx.get(
            "https://api.brightdata.com/customer/balance",
            headers={"Authorization": f"Bearer {key}"},
            timeout=_TIMEOUT,
        )
        if resp.status_code == 200:
            data = resp.json() or {}
            # Bright Data returns the spendable figure under `balance`;
            # pending_costs is work billed but not yet settled, so it is
            # subtracted to give what can actually still be spent.
            balance = float(data.get("balance") or 0.0)
            pending = float(data.get("pending_costs") or 0.0)
            return _store("brightdata", asdict(Balance(
                provider="brightdata",
                available_usd=round(balance - pending, 4),
                source="live",
                detail="Bright Data /customer/balance",
            )))
        detail = f"HTTP {resp.status_code}"
    except Exception as exc:
        detail = f"{type(exc).__name__}: {exc}"

    return _store("brightdata", _derived_brightdata(detail))


def _derived_brightdata(why: str) -> dict[str, Any]:
    """Starting balance minus the harness's own record ledger.

    Honest but approximate: it counts what THIS harness billed and cannot
    see spend from anything else on the account, so it drifts if the account
    is shared. Labelled `derived` for exactly that reason.
    """
    from src.observability.logging_config import total_records_spent

    cfg = get_config().harness
    try:
        records = int(total_records_spent())
    except Exception:
        records = 0

    spent = round(records * float(cfg.brightdata_cost_per_record_usd), 4)
    start = float(getattr(cfg, "brightdata_starting_balance_usd", 0.0) or 0.0)
    available = round(start - spent, 4) if start > 0 else None

    return asdict(Balance(
        provider="brightdata",
        available_usd=available,
        source="derived",
        detail=(
            f"{records:,} records billed x ${cfg.brightdata_cost_per_record_usd}"
            f" = ${spent:,.2f} spent by this harness"
        ),
        spent_usd=spent,
        limit_usd=start or None,
        remediation=(
            f"Live balance unavailable ({why}). A Bright Data API key has one "
            "of five permission scopes -- Admin, Finance, Ops, Limit, User -- "
            "and only Admin or Finance can read billing; the collector key "
            "this harness discovers with is scoped Ops/User on purpose, so it "
            "can never do this itself. An account admin can generate a "
            "Finance-scoped key at brightdata.com/cp/setting/users (only "
            "admins can generate keys at all) and paste it into Connect "
            "below, or set BRIGHTDATA_STARTING_BALANCE_USD in .env to track "
            "against a known starting figure instead."
        ),
    ))


ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def connect_brightdata(api_key: str) -> dict[str, Any]:
    """Swap in a Bright Data token with billing permission, without a restart.

    Bright Data has no OAuth handshake to "connect" -- the only way a token
    gets billing access is an account admin generating one scoped Admin or
    Finance (of the five scopes a key can have -- Admin, Finance, Ops, Limit,
    User -- only those two can read balance), and the only way this harness
    learns of it is being given it. So this validates the token LIVE before
    accepting it: a typo, or a token that is still scoped Ops/User, returns
    422 and changes nothing, rather than persisting a key that will just
    fail the same way next time.
    """
    key = (api_key or "").strip()
    if not key:
        raise ValueError("API key is empty.")

    resp = httpx.get(
        "https://api.brightdata.com/customer/balance",
        headers={"Authorization": f"Bearer {key}"},
        timeout=_TIMEOUT,
    )
    if resp.status_code != 200:
        detail = resp.text.strip() or f"HTTP {resp.status_code}"
        raise ValueError(f"Bright Data rejected this token: {detail}")

    data = resp.json() or {}
    balance = float(data.get("balance") or 0.0)
    pending = float(data.get("pending_costs") or 0.0)

    get_config().brightdata.api_key = key
    _persist_env_key("BRIGHTDATA_API_KEY", key)
    _cache.pop("brightdata", None)

    return _store("brightdata", asdict(Balance(
        provider="brightdata",
        available_usd=round(balance - pending, 4),
        source="live",
        detail="Bright Data /customer/balance",
    )))


def _persist_env_key(name: str, value: str) -> None:
    """Rewrite one KEY=value line in .env, in place, touching nothing else.

    Best-effort: a run launched from this key still works even if .env is
    read-only or missing, since the in-memory config was already updated.
    """
    try:
        if not ENV_PATH.exists():
            return
        lines = ENV_PATH.read_text().splitlines(keepends=True)
        prefix = f"{name}="
        for i, line in enumerate(lines):
            if line.startswith(prefix):
                lines[i] = f"{prefix}{value}\n"
                ENV_PATH.write_text("".join(lines))
                return
        with ENV_PATH.open("a") as f:
            f.write(f"{prefix}{value}\n")
    except OSError:
        pass


def all_balances(force: bool = False) -> dict[str, Any]:
    openrouter = openrouter_balance(force=force)
    brightdata = brightdata_balance(force=force)
    return {
        "providers": [openrouter, brightdata],
        "checked_at": time.time(),
    }
