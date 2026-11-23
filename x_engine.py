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
from io import BytesIO
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

DEX_BOOSTS = "https://api.dexscreener.com/token-boosts/latest/v1"
DEX_TOKEN = "https://api.dexscreener.com/latest/dex/tokens/{address}"
RUGCHECK = "https://api.rugcheck.xyz/v1/tokens/{address}/report/summary"
GOPLUS_BASE = "https://api.gopluslabs.io/api/v1/token_security/8453"
CHAINS = {"solana", "base"}
STATE_PATH = Path("data/state.json")
OUTBOX_PATH = Path("outbox/latest.json")
CARD_PATH = Path("outbox/latest-card.png")
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
    image_url: str = ""


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
    change_m5 = num(pair.get("priceChange", {}).get("m5"))
    change_h1 = num(pair.get("priceChange", {}).get("h1"))
    # Selectivity is the product: no signal is better than a low-quality signal.
    if not (5 <= age <= 1_440 and liquidity >= 20_000 and volume >= 30_000):
        return None
    # Activity alone is not momentum. Demand a meaningful move in at least one
    # short window and enough turnover relative to available liquidity.
    has_momentum = change_h1 > 0 and (change_h1 >= 5 or change_m5 >= 3)
    turnover = volume / liquidity if liquidity else 0
    if buys + sells < 35 or buys <= sells or not has_momentum or change_h1 > 180:
        return None
    if turnover < 0.40:
        return None
    base = pair.get("baseToken", {})
    address = str(base.get("address", "")).strip()
    if not address:
        return None
    signal = Signal(
        chain=chain, address=address, name=str(base.get("name", "Unknown"))[:40],
        symbol=re.sub(r"[^A-Za-z0-9_]", "", str(base.get("symbol", "TOKEN")))[:15],
        url=str(pair.get("url", "")), age_minutes=round(age, 1), liquidity=liquidity,
        volume_h1=volume, change_m5=change_m5,
        change_h1=change_h1, buys_h1=buys, sells_h1=sells,
        fdv=num(pair.get("marketCap") or pair.get("fdv")), score=score_pair(pair, age),
        image_url=str(pair.get("info", {}).get("imageUrl", "")),
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


def font(size: int, bold: bool = False) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for path in (f"/usr/share/fonts/truetype/dejavu/{name}", name):
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    return ImageFont.load_default()


def fetch_logo(session: requests.Session, url: str) -> Image.Image | None:
    if not url.startswith("https://"):
        return None
    try:
        response = session.get(url, timeout=TIMEOUT, headers={"User-Agent": "isitcooked-x/1.0"})
        response.raise_for_status()
        if len(response.content) > 5_000_000:
            return None
        image = Image.open(BytesIO(response.content))
        image.load()
        if image.width < 32 or image.height < 32 or image.width * image.height > 16_000_000:
            return None
        return image.convert("RGBA")
    except (requests.RequestException, OSError, ValueError):
        return None


def draw_chain_symbol(draw: ImageDraw.ImageDraw, chain: str, x: int, y: int) -> None:
    """Draw crisp native-looking chain marks without external brand assets."""
    if chain == "solana":
        colors = ((73, 255, 189, 255), (92, 172, 255, 255), (168, 75, 255, 255))
        for index, color in enumerate(colors):
            top = y + index * 13
            draw.polygon([(x+7, top), (x+48, top), (x+41, top+9), (x, top+9)], fill=color)
    else:  # Base
        draw.ellipse((x, y-2, x+46, y+44), fill=(0, 82, 255, 255))
        draw.rounded_rectangle((x+10, y+15, x+37, y+23), radius=4, fill=(255, 255, 255, 255))
def make_card(signal: Signal, session: requests.Session, path: Path = CARD_PATH) -> Path:
    """Render a premium 16:9 X card with a real token logo when available."""
    width, height = 1600, 900
    image = Image.new("RGB", (width, height), "#040710")
    draw = ImageDraw.Draw(image, "RGBA")
    # Atmospheric gradient and radar geometry.
    for y in range(height):
        ratio = y / height
        draw.line((0, y, width, y), fill=(3 + int(8 * ratio), 7, 20 + int(16 * ratio), 255))
    for radius, alpha in ((620, 55), (470, 70), (320, 90), (180, 110)):
        box = (90-radius, 450-radius, 90+radius, 450+radius)
        draw.ellipse(box, outline=(0, 220, 255, alpha), width=3)
    draw.polygon([(90, 450), (680, 110), (570, 520)], fill=(0, 220, 255, 34))
    for x in range(950, 1600, 130):
        draw.line((x, 0, x-420, 900), fill=(131, 69, 255, 22), width=2)

    # Header.
    draw.text((90, 62), "IS IT COOKED?  /  COOKED RADAR", font=font(30, True), fill="#F5F7FF")
    draw.text((90, 112), "SIGNAL BEFORE NOISE", font=font(20, True), fill="#53E7FF")
    chain_name = signal.chain.upper()
    badge_box = (1260, 62, 1510, 128)
    draw.rounded_rectangle(badge_box, radius=20, fill=(22, 25, 49, 230), outline=(156, 92, 255, 220), width=2)
    draw_chain_symbol(draw, signal.chain, 1284, 77)
    draw.text((1348, 80), chain_name, font=font(24, True), fill="#FFFFFF")

    # Token logo, integrated rather than pasted as a square.
    logo = fetch_logo(session, signal.image_url)
    logo_size = 250
    logo_box = (105, 245, 105+logo_size, 245+logo_size)
    draw.ellipse((70, 210, 390, 530), fill=(0, 219, 255, 25), outline=(0, 219, 255, 40), width=12)
    draw.ellipse((82, 222, 378, 518), fill=(135, 72, 255, 32), outline=(135, 72, 255, 75), width=7)
    draw.ellipse((88, 228, 372, 512), fill=(8, 15, 32, 245), outline=(55, 230, 255, 235), width=5)
    if logo:
        logo.thumbnail((logo_size, logo_size), Image.Resampling.LANCZOS)
        canvas = Image.new("RGBA", (logo_size, logo_size), (0, 0, 0, 0))
        canvas.alpha_composite(logo, ((logo_size-logo.width)//2, (logo_size-logo.height)//2))
        mask = Image.new("L", (logo_size, logo_size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, logo_size, logo_size), fill=255)
        canvas.putalpha(mask)
        image.paste(canvas, (105, 245), canvas)
    else:
        initials = signal.symbol[:3]
        box = draw.textbbox((0, 0), initials, font=font(64, True))
        draw.text((230-(box[2]-box[0])/2, 370-(box[3]-box[1])/2), initials, font=font(64, True), fill="#FFFFFF")

    # Identity and score.
    draw.text((420, 225), signal.name.upper()[:28], font=font(32, True), fill="#AAB5CA")
    draw.text((420, 270), f"${signal.symbol}", font=font(82, True), fill="#FFFFFF")
    draw.rounded_rectangle((420, 382, 690, 506), radius=24, fill=(8, 16, 34, 235), outline=(48, 224, 255, 175), width=2)
    draw.text((445, 400), "COOKED SCORE", font=font(19, True), fill="#7F91AC")
    draw.text((445, 428), f"{signal.score}", font=font(61, True), fill="#53E7FF")
    draw.text((540, 462), "/100", font=font(22, True), fill="#9D6CFF")
    draw.rounded_rectangle((420, 525, 720, 575), radius=18, fill=(18, 55, 52, 220), outline=(101, 244, 196, 165), width=2)
    draw.ellipse((443, 544, 455, 556), fill=(101, 244, 196, 255))
    draw.text((472, 538), "MOMENTUM CONFIRMED", font=font(15, True), fill="#65F4C4")

    metrics = [
        ("1H VOLUME", money(signal.volume_h1)),
        ("LIQUIDITY", money(signal.liquidity)),
        ("1H MOVE", f"{signal.change_h1:+.1f}%"),
        ("BUY / SELL", f"{signal.buys_h1:,} / {signal.sells_h1:,}"),
    ]
    start_x, start_y = 760, 250
    for index, (label, value) in enumerate(metrics):
        col, row = index % 2, index // 2
        x, y = start_x + col * 400, start_y + row * 180
        draw.rounded_rectangle((x, y, x+350, y+135), radius=18, fill=(12, 18, 38, 225), outline=(52, 74, 112, 200), width=2)
        draw.text((x+24, y+22), label, font=font(19, True), fill="#7F91AC")
        color = "#65F4C4" if label == "1H MOVE" and signal.change_h1 >= 0 else "#FFFFFF"
        draw.text((x+24, y+58), value, font=font(40, True), fill=color)

    ca = signal.address
    short_ca = f"{ca[:8]}...{ca[-8:]}"
    draw.rounded_rectangle((90, 742, 1510, 825), radius=18, fill=(7, 11, 25, 235), outline=(52, 74, 112, 200), width=2)
    draw.text((120, 765), "CONTRACT", font=font(18, True), fill="#7F91AC")
    draw.text((285, 754), short_ca, font=font(31, True), fill="#FFFFFF")
    draw.text((1190, 765), "VERIFY · NFA", font=font(18, True), fill="#9D6CFF")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG", optimize=True)
    return path


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


# Evergreen posts give a new account reasons to follow even when no token earns
# a signal. They are deliberately opinionated, useful and short enough to quote.
EDITORIAL_LIBRARY = [
    {
        "pillar": "contrarian", "headline": "VIRAL IS NOT LIQUID", "subhead": "Attention can enter faster than you can exit.",
        "text": "A memecoin can be viral and still be untradeable.\n\nFollowers measure attention. Liquidity measures the exit door.\n\nNever confuse the two."
    },
    {
        "pillar": "education", "headline": "MARKET CAP CAN LIE", "subhead": "The exit door is called liquidity.",
        "text": "The fastest 10-second token check:\n\n1. Verify the contract\n2. Check liquidity\n3. Compare volume to liquidity\n4. Read buys vs sells\n5. Inspect holder concentration\n\nPrice comes last."
    },
    {
        "pillar": "conversation", "headline": "WHAT MAKES YOU SKIP?", "subhead": "One red flag. No second chances.",
        "text": "You get one reason to reject a new memecoin instantly.\n\nLow liquidity?\nConcentrated holders?\nUnlocked LP?\nAnonymous team?\nFake engagement?\n\nWhat is your non-negotiable red flag?"
    },
    {
        "pillar": "culture", "headline": "THE CHART: +400%", "subhead": "MY ENTRY: the exact local top.",
        "text": "The chart before I buy: +400%\n\nThe chart after I buy: a forensic investigation.\n\nChasing is not a strategy."
    },
    {
        "pillar": "contrarian", "headline": "NO SIGNAL IS A SIGNAL", "subhead": "Selectivity is the product.",
        "text": "If your alpha account has a new ‘gem’ every hour, the product is not research.\n\nThe product is your attention.\n\nA serious radar must be allowed to say nothing."
    },
    {
        "pillar": "education", "headline": "VOLUME NEEDS CONTEXT", "subhead": "Big numbers can still be empty.",
        "text": "Volume alone proves activity — not conviction.\n\nAsk three questions:\n• Is liquidity deep enough?\n• Are transactions broad or bot-driven?\n• Does buy pressure persist beyond one candle?"
    },
    {
        "pillar": "conversation", "headline": "BUILD THE PERFECT RADAR", "subhead": "Choose only three metrics.",
        "text": "You can keep only three metrics on a memecoin radar.\n\nLiquidity. Volume. Holder concentration. Buy pressure. Pair age. Social velocity.\n\nWhich three survive?"
    },
    {
        "pillar": "culture", "headline": "DYOR, THEY SAID", "subhead": "So I opened 37 tabs.",
        "text": "Crypto research starter pack:\n\n12 scanners. 8 dashboards. 37 tabs. One contract copied from the wrong account.\n\nVerify the CA first. Everything else comes second."
    },
    {
        "pillar": "authority", "headline": "PUBLIC RECEIPTS > HYPE", "subhead": "Wins and losses stay visible.",
        "text": "Anyone can screenshot a winner.\n\nAuthority starts when every signal gets a public receipt — including the ugly ones.\n\nNo deleted misses. No invented certainty."
    },
    {
        "pillar": "education", "headline": "BUY PRESSURE CAN FAKE YOU", "subhead": "Count quality, not only quantity.",
        "text": "1,000 buys and 400 sells looks bullish.\n\nUntil 980 buys are dust-sized bot transactions.\n\nTransaction count needs trade-size and wallet context."
    },
    {
        "pillar": "conversation", "headline": "HONEST ANSWERS ONLY", "subhead": "What was your most expensive lesson?",
        "text": "What cost you more in crypto?\n\nA rug.\nA bad entry.\nRefusing to take profit.\nOr trusting someone else’s conviction?\n\nHonest answers only."
    },
    {
        "pillar": "culture", "headline": "ME: I WILL WAIT", "subhead": "Also me: buys candle number nine.",
        "text": "Me: I’ll wait for confirmation.\n\nAlso me after nine green candles: this entry looks early.\n\nThe market charges tuition for impatience."
    },
]


def write_editorial(now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(timezone.utc)
    # Each four-hour window gets a stable draft, while consecutive runs rotate.
    slot = now.toordinal() * 6 + now.hour // 4
    item = EDITORIAL_LIBRARY[slot % len(EDITORIAL_LIBRARY)]
    text = item["text"] + "\n\nSignal before noise."
    if len(text) > 280:
        raise ValueError("Generated editorial post exceeds 280 characters")
    return {**item, "type": "editorial", "priority": 50, "text": text}


def make_editorial_card(item: dict[str, Any], path: Path = CARD_PATH) -> Path:
    """Render an original shareable brand card instead of recycled meme art."""
    width, height = 1600, 900
    image = Image.new("RGB", (width, height), "#040710")
    draw = ImageDraw.Draw(image, "RGBA")
    for y in range(height):
        draw.line((0, y, width, y), fill=(4, 7 + y // 140, 17 + y // 45, 255))
    for radius, alpha in ((620, 40), (450, 55), (290, 75), (140, 100)):
        draw.ellipse((800-radius, 450-radius, 800+radius, 450+radius), outline=(42, 222, 255, alpha), width=3)
    draw.polygon([(800, 450), (1530, 120), (1440, 690)], fill=(142, 74, 255, 28))
    draw.text((85, 65), "IS IT COOKED?", font=font(31, True), fill="#FFFFFF")
    draw.text((85, 112), "SIGNAL BEFORE NOISE", font=font(19, True), fill="#53E7FF")
    badge = str(item.get("pillar", "editorial")).upper()
    draw.rounded_rectangle((1260, 65, 1510, 120), radius=18, fill=(22, 25, 49, 230), outline=(156, 92, 255, 210), width=2)
    draw.text((1285, 80), badge[:18], font=font(18, True), fill="#FFFFFF")
    headline = str(item["headline"])
    lines = headline.split("\n")
    y = 285 if len(lines) == 1 else 235
    for line in lines:
        box = draw.textbbox((0, 0), line, font=font(72, True))
        draw.text(((width - (box[2]-box[0]))/2, y), line, font=font(72, True), fill="#FFFFFF")
        y += 92
    subhead = str(item["subhead"])
    box = draw.textbbox((0, 0), subhead, font=font(30, False))
    draw.text(((width - (box[2]-box[0]))/2, y+42), subhead, font=font(30, False), fill="#AAB5CA")
    draw.rounded_rectangle((470, 660, 1130, 730), radius=24, fill=(7, 15, 32, 220), outline=(45, 219, 255, 155), width=2)
    footer = "ON-CHAIN INTELLIGENCE  /  PUBLIC RECEIPTS"
    box = draw.textbbox((0, 0), footer, font=font(20, True))
    draw.text(((width - (box[2]-box[0]))/2, 682), footer, font=font(20, True), fill="#65F4C4")
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, "PNG", optimize=True)
    return path


def notify_telegram(items: list[dict[str, Any]], card_path: Path | None = None) -> None:
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
        if card_path and card_path.exists():
            with card_path.open("rb") as card:
                photo = requests.post(
                    f"https://api.telegram.org/bot{token}/sendPhoto",
                    data={"chat_id": chat_id, "caption": "X VISUAL — attach this image to the draft above"},
                    files={"photo": (card_path.name, card, "image/png")}, timeout=30,
                )
            photo.raise_for_status()


def run(dry_run: bool = False) -> list[dict[str, Any]]:
    state = load_state()
    seen = state.setdefault("seen_tokens", {})
    with requests.Session() as session:
        signals = discover(session)
    fresh = [signal for signal in signals if signal.address not in seen]
    items = [write_signal(fresh[0])] if fresh else [write_editorial()]
    now = datetime.now(timezone.utc).isoformat()
    payload = {"generated_at": now, "account": "@isitcooked1", "requires_human_review": True, "items": items}
    save_json(OUTBOX_PATH, payload)
    card_path = None
    if fresh:
        with requests.Session() as media_session:
            card_path = make_card(fresh[0], media_session)
    else:
        card_path = make_editorial_card(items[0])
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
            notify_telegram(items, card_path)
        except requests.RequestException as exc:
            log.warning("Telegram editorial delivery failed: %s", exc)
    log.info("Generated %s editorial item(s); %s clean signal(s) found", len(items), len(signals))
    return items


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    run(dry_run=args.dry_run)

