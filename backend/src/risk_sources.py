"""
External risk & intelligence sources

Lightweight wrappers around public APIs:
- Pump.fun (token info)
- Birdeye token_security
- TokenSniffer
- RugCheck
- GoPlus
- RugDoc

These helpers are best-effort and should fail soft (return None) to keep the
pipeline resilient if any provider is down or rate-limited.
"""

from __future__ import annotations

import os
import requests
from typing import Optional, Dict, Any, List

HELIUS_BASE = "https://api.helius.xyz"


def _get(url: str, headers: Optional[Dict[str, str]] = None, params: Optional[Dict[str, Any]] = None, timeout: int = 10):
    try:
        resp = requests.get(url, headers=headers or {}, params=params or {}, timeout=timeout)
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


def pumpfun_token(token_address: str) -> Optional[Dict[str, Any]]:
    base = os.getenv("PUMPFUN_API_URL", "https://frontend-api.pump.fun")
    url = f"{base}/coins/{token_address}"
    return _get(url)


def birdeye_security(token_address: str) -> Optional[Dict[str, Any]]:
    api_key = os.getenv("BIRDEYE_API_KEY", "")
    if not api_key:
        return None
    url = os.getenv("BIRDEYE_API_URL", "https://public-api.birdeye.so") + "/defi/token_security"
    headers = {"X-API-KEY": api_key}
    params = {"address": token_address}
    return _get(url, headers=headers, params=params)


def tokensniffer_report(token_address: str) -> Optional[Dict[str, Any]]:
    if os.getenv("TOKEN_SNIFFER_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        return None
    url = os.getenv("TOKEN_SNIFFER_API_URL", "https://tokensniffer.com/api/v2/tokens/solana") + f"/{token_address}"
    return _get(url, timeout=int(os.getenv("TOKEN_SNIFFER_TIMEOUT", "8")))


def rugcheck_report(token_address: str) -> Optional[Dict[str, Any]]:
    if os.getenv("RUGCHECK_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        return None
    url = os.getenv("RUGCHECK_API_URL", "https://api.rugcheck.xyz/v1") + f"/tokens/{token_address}"
    return _get(url, timeout=int(os.getenv("RUGCHECK_TIMEOUT", "8")))


def goplus_security(token_address: str) -> Optional[Dict[str, Any]]:
    if os.getenv("GOPLUS_ENABLED", "true").lower() not in {"1", "true", "yes", "on"}:
        return None
    base = os.getenv("GOPLUS_API_URL", "https://api.gopluslabs.io/api/v1")
    url = base + "/token_security/solana"
    params = {"contract_addresses": token_address}
    return _get(url, params=params, timeout=int(os.getenv("GOPLUS_TIMEOUT", "8")))


def rugdoc_report(token_address: str) -> Optional[Dict[str, Any]]:
    if os.getenv("RUGDOC_ENABLED", "false").lower() not in {"1", "true", "yes", "on"}:
        return None
    url = os.getenv("RUGDOC_API_URL", "https://api.rugdoc.io/v1") + f"/scan/{token_address}"
    return _get(url, timeout=int(os.getenv("RUGDOC_TIMEOUT", "8")))


def helius_latest_tx_age_minutes(address: str) -> Optional[float]:
    """
    Get age in minutes of the latest transaction involving this address via Helius.
    Returns None if unavailable.
    """
    api_key = os.getenv("HELIUS_API_KEY", "")
    if not api_key:
        return None
    url = f"{HELIUS_BASE}/v0/addresses/{address}/transactions"
    params = {"api-key": api_key, "limit": 1}
    data = _get(url, params=params, timeout=8)
    if not data or not isinstance(data, list) or not data:
        return None
    tx = data[0]
    ts = tx.get("timestamp")
    if not ts:
        return None
    import time
    return max(0.0, (time.time() - ts) / 60.0)


def evaluate_token(token_address: str) -> Dict[str, Any]:
    """
    Aggregate multiple sources into a simple risk view.
    risk_level: LOW | MEDIUM | HIGH | CRITICAL
    """
    findings: List[str] = []
    risk = "LOW"

    # Pump.fun presence (not a risk flag, but availability)
    pf = pumpfun_token(token_address)
    if pf and pf.get("error"):
        findings.append("pumpfun_error")

    # Birdeye security
    be = birdeye_security(token_address)
    if be and be.get("data"):
        data = be["data"]
        if data.get("isFreezeAuthorityEnabled"):
            findings.append("freeze_authority")
            risk = max_risk(risk, "HIGH")
        if data.get("isMintAuthorityEnabled"):
            findings.append("mint_authority")
            risk = max_risk(risk, "HIGH")
        top10 = data.get("top10HolderPercent", 0) or 0
        if top10 > 50:
            findings.append("top10>50%")
            risk = max_risk(risk, "MEDIUM")

    # TokenSniffer
    ts = tokensniffer_report(token_address)
    if ts:
        score = ts.get("score", 100)
        if score is not None and score < 60:
            findings.append(f"tokensniffer_score_{score}")
            risk = max_risk(risk, "HIGH")

    # RugCheck
    rc = rugcheck_report(token_address)
    if rc:
        status = rc.get("status", "").upper()
        if status in {"RUG", "SCAM"}:
            findings.append(f"rugcheck_{status.lower()}")
            risk = max_risk(risk, "CRITICAL")

    # GoPlus
    gp = goplus_security(token_address)
    if gp and gp.get("result"):
        # GoPlus returns dict keyed by address
        res = next(iter(gp["result"].values())) if isinstance(gp["result"], dict) else None
        if res:
            if res.get("is_honeypot") == "1":
                findings.append("goplus_honeypot")
                risk = max_risk(risk, "CRITICAL")
            if res.get("trading_halted") == "1":
                findings.append("goplus_trading_halted")
                risk = max_risk(risk, "HIGH")

    # RugDoc
    rd = rugdoc_report(token_address)
    if rd and isinstance(rd, dict):
        if rd.get("status") in {"RUG", "SCAM"}:
            findings.append(f"rugdoc_{rd.get('status').lower()}")
            risk = max_risk(risk, "CRITICAL")

    return {
        "risk_level": risk,
        "findings": findings,
        "sources": {
            "pumpfun": bool(pf),
            "birdeye": bool(be),
            "tokensniffer": bool(ts),
            "rugcheck": bool(rc),
            "goplus": bool(gp),
            "rugdoc": bool(rd),
            "helius": True if helius_latest_tx_age_minutes(token_address) is not None else False,
        },
    }


def max_risk(current: str, incoming: str) -> str:
    order = ["LOW", "MEDIUM", "HIGH", "CRITICAL"]
    return order[max(order.index(current), order.index(incoming))]


