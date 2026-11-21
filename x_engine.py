"""IS IT COOKED? — zero-cost X editorial engine.

Discovers selective on-chain signals, writes concise English posts and stores a
reviewable outbox. It never publishes to X without an official API connection.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests

DEX_BOOSTS = "https://api.dexscreener.com/token-boosts/latest/v1"
DEX_TOKEN = "https://api.dexscreener.com/latest/dex/tokens/{address}"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{address}/report/summary"
GOPLUS_BASE = "https://api.gopluslabs.io/api/v1/token_security/8453"
CHAINS = {"solana", "base"}
STATE_PATH = Path("data/state.json")
OUTBOX_PATH = Path("outbox/latest.json")
TIMEOUT = 15
MAX_CANDIDATES = 35

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("isitcooked-x")


@dataclass
class Signal:
    chain: str
    address: str
    name: str
    symbol: str
    url: str
    age_minutes: float
    liquidity: float
    volume_h1: float
    change_m5: float
    change_h1: float
    buys_h1: int
    sells_h1: int
    fdv: float
    score: int


def num(value: Any) -> float:
    try:
        return float(value or 0)
    except (TypeError, ValueError):
        return 0.0


def load_state() -> dict[str, Any]:
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        if isinstance(state, dict):
            return state
    except (OSError, json.JSONDecodeError):
        pass
    return {"version": 1, "seen_tokens": {}, "recent_hooks": [], "last_run": None}


def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    temporary.replace(path)


def get_json(session: requests.Session, url: str) -> Any:
    response = session.get(url, timeout=TIMEOUT, headers={"User-Agent": "isitcooked-x/1.0"})
    response.raise_for_status()
    return response.json()


def score_pair(pair: dict[str, Any], age: float) -> int:
    liquidity = num(pair.get("liquidity", {}).get("usd"))
    volume = num(pair.get("volume", {}).get("h1"))
    txns = pair.get("txns", {}).get("h1", {})
    buys, sells = int(txns.get("buys", 0)), int(txns.get("sells", 0))
    change_h1 = num(pair.get("priceChange", {}).get("h1"))
    score = 45
    score += min(15, int(liquidity / 10_000) * 3)
    score += min(15, int(volume / 25_000) * 3)
    score += min(10, max(0, buys - sells) // 10)
    score += 8 if 5 <= age <= 360 else 4
    score += 7 if 5 <= change_h1 <= 80 else 0
    return min(100, score)


def pair_to_signal(pair: dict[str, Any], now_ms: int) -> Signal | None:
    chain = str(pair.get("chainId", "")).lower()
    if chain not in CHAINS:
        return None
    created = int(num(pair.get("pairCreatedAt")))
    age = (now_ms - created) / 60_000 if created else 99_999
    liquidity = num(pair.get("liquidity", {}).get("usd"))
    volume = num(pair.get("volume", {}).get("h1"))
    txns = pair.get("txns", {}).get("h1", {})
    buys, sells = int(txns.get("buys", 0)), int(txns.get("sells", 0))
    change_h1 = num(pair.get("priceChange", {}).get("h1"))
    # Selectivity is the product: no signal is better than a low-quality signal.
    if not (5 <= age <= 1_440 and liquidity >= 20_000 and volume >= 30_000):
        return None
    if buys + sells < 35 or buys <= sells or change_h1 <= 0 or change_h1 > 180:
        return None
    base = pair.get("baseToken", {})
    address = str(base.get("address", "")).strip()
    if not address:
        return None
    signal = Signal(
        chain=chain, address=address, name=str(base.get("name", "Unknown"))[:40],
        symbol=re.sub(r"[^A-Za-z0-9_]", "", str(base.get("symbol", "TOKEN")))[:15],
        url=str(pair.get("url", "")), age_minutes=round(age, 1), liquidity=liquidity,
        volume_h1=volume, change_m5=num(pair.get("priceChange", {}).get("m5")),
        change_h1=change_h1, buys_h1=buys, sells_h1=sells,
        fdv=num(pair.get("marketCap") or pair.get("fdv")), score=score_pair(pair, age),
    )
    return signal if signal.score >= 70 else None


def passes_security(session: requests.Session, signal: Signal) -> bool:
    """Fail closed when the free chain-specific safety service is unavailable."""
    try:
        if signal.chain == "solana":
            report = get_json(session, RUGCHECK.format(address=signal.address))
            risks = report.get("risks") or []
            critical_words = {"danger", "critical", "rug", "honeypot"}
            for risk in risks:
                level = str(risk.get("level", "")).lower()
                name = str(risk.get("name", "")).lower()
                if level in {"danger", "critical"} or any(word in name for word in critical_words):
                    return False
            return "score" in report or "score_normalised" in report
        response = session.get(
            GOPLUS_BASE, params={"contract_addresses": signal.address.lower()},
            timeout=TIMEOUT, headers={"User-Agent": "isitcooked-x/1.0"},
        )
        response.raise_for_status()
        result = response.json().get("result", {}).get(signal.address.lower())
        if not isinstance(result, dict):
            return False
        red_flags = (
            "is_honeypot", "cannot_sell_all", "hidden_owner", "owner_change_balance",
            "slippage_modifiable", "transfer_pausable", "selfdestruct",
        )
        return not any(str(result.get(flag, "0")) == "1" for flag in red_flags)
    except (requests.RequestException, ValueError, TypeError) as exc:
        log.warning("Security check failed closed for %s: %s", signal.address, exc)
        return False


def discover(session: requests.Session) -> list[Signal]:
    boosts = get_json(session, DEX_BOOSTS)
    addresses: list[str] = []
    for item in boosts if isinstance(boosts, list) else []:
        if str(item.get("chainId", "")).lower() in CHAINS and item.get("tokenAddress"):
            addresses.append(str(item["tokenAddress"]))
    unique = list(dict.fromkeys(addresses))[:MAX_CANDIDATES]
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    signals: dict[str, Signal] = {}
    for address in unique:
        try:
            data = get_json(session, DEX_TOKEN.format(address=address))
            for pair in data.get("pairs") or []:
                signal = pair_to_signal(pair, now_ms)
                if signal and (signal.address not in signals or signal.score > signals[signal.address].score):
                    signals[signal.address] = signal
        except (requests.RequestException, ValueError, TypeError) as exc:
            log.warning("Candidate lookup failed: %s", exc)
    ranked = sorted(signals.values(), key=lambda s: (s.score, s.volume_h1), reverse=True)
    audited: list[Signal] = []
    for signal in ranked[:10]:
        if passes_security(session, signal):
            audited.append(signal)
    return audited


def money(value: float) -> str:
    if value >= 1_000_000:
        return f"${value / 1_000_000:.2f}M"
    if value >= 1_000:
        return f"${value / 1_000:.1f}K"
    return f"${value:.0f}"


def choose(seed: str, options: list[str]) -> str:
    return random.Random(int(hashlib.sha256(seed.encode()).hexdigest()[:12], 16)).choice(options)


def write_signal(signal: Signal) -> dict[str, Any]:
    hook = choose(signal.address, [
        "Volume arrived. Now we check whether structure can hold it.",
        "The candle caught attention. The order flow earned a closer look.",
        "Early momentum is forming — but momentum is not proof.",
        "This moved onto the radar for structure, not hype.",
    ])
    caution = choose(signal.address + "risk", [
        "Promising, not proven. Liquidity and holder behavior still matter.",
        "A radar signal is a reason to investigate — never a reason to chase.",
        "Fast markets reverse fast. Verify the contract and size risk accordingly.",
    ])
    first = (
        f"COOKED RADAR · {signal.chain.upper()}\n\n"
        f"${signal.symbol} entered the radar.\n{hook}\n\n"
        f"1H volume: {money(signal.volume_h1)}\n"
        f"Liquidity: {money(signal.liquidity)}\n"
        f"Buy/sell: {signal.buys_h1}/{signal.sells_h1}\n"
        f"1H move: {signal.change_h1:+.1f}%\n"
        f"Cooked Score: {signal.score}/100"
    )
    second = (
        f"${signal.symbol} · RISK CHECK\n\n{caution}\n\n"
        f"CA: {signal.address}\n{signal.url}\n\n"
        "Signal before noise. NFA."
    )
    thread = [first, second]
    if any(len(post) > 280 for post in thread):
        raise ValueError("Generated X post exceeds 280 characters")
    return {
        "type": "signal", "priority": signal.score, "text": "\n\n--- THREAD ---\n\n".join(thread),
        "thread": thread, "signal": asdict(signal),
    }


def write_no_signal() -> dict[str, Any]:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    text = choose(day, [
        "RADAR STATUS: NO CLEAN SIGNAL.\n\nPlenty of movement. Not enough confirmation.\n\nPatience is part of the edge. Signal before noise.",
        "The radar is active. The standards are higher than the current market.\n\nNo forced calls. No filler. We wait for structure.",
        "Not every green candle deserves an alert.\n\nToday the radar sees activity — not conviction. We stay selective.",
    ])
    return {"type": "market_status", "priority": 30, "text": text}


def notify_telegram(items: list[dict[str, Any]]) -> None:
    token, chat_id = os.getenv("EDITOR_TELEGRAM_TOKEN"), os.getenv("EDITOR_CHAT_ID")
    if not token or not chat_id:
        log.info("Editorial Telegram secrets absent; outbox only")
        return
    for item in items:
        posts = item.get("thread") or [item["text"]]
        numbered = "\n\n".join(f"[{index}/{len(posts)}]\n{post}" for index, post in enumerate(posts, 1))
        body = "X DRAFT — REVIEW BEFORE POSTING\n\n" + numbered
        response = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": body, "disable_web_page_preview": True}, timeout=TIMEOUT,
        )
        response.raise_for_status()


def run(dry_run: bool = False) -> list[dict[str, Any]]:
    state = load_state()
    seen = state.setdefault("seen_tokens", {})
    with requests.Session() as session:
        signals = discover(session)
    fresh = [signal for signal in signals if signal.address not in seen]
    items = [write_signal(fresh[0])] if fresh else [write_no_signal()]
    now = datetime.now(timezone.utc).isoformat()
    payload = {"generated_at": now, "account": "@isitcooked1", "requires_human_review": True, "items": items}
    save_json(OUTBOX_PATH, payload)
    if not dry_run:
        if fresh:
            seen[fresh[0].address] = {"first_seen": now, "score": fresh[0].score, "symbol": fresh[0].symbol}
        # Keep state bounded for permanent operation.
        if len(seen) > 2_000:
            oldest = sorted(seen, key=lambda key: seen[key].get("first_seen", ""))
            for key in oldest[:-2_000]:
                del seen[key]
        state["last_run"] = now
        save_json(STATE_PATH, state)
        try:
            notify_telegram(items)
        except requests.RequestException as exc:
            log.warning("Telegram editorial delivery failed: %s", exc)
    log.info("Generated %s editorial item(s); %s clean signal(s) found", len(items), len(signals))
    return items


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)

