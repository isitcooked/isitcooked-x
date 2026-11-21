from datetime import datetime, timezone
import unittest

from x_engine import money, pair_to_signal, passes_security, write_signal


def sample_pair():
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    return {
        "chainId": "solana", "pairCreatedAt": now_ms - 60 * 60_000,
        "liquidity": {"usd": 55000}, "volume": {"h1": 150000},
        "txns": {"h1": {"buys": 180, "sells": 90}},
        "priceChange": {"m5": 8.2, "h1": 34.5},
        "baseToken": {"address": "ABC123", "name": "Test Coin", "symbol": "TEST"},
        "url": "https://dexscreener.com/solana/example", "fdv": 800000,
    }, now_ms


class EngineTests(unittest.TestCase):
    def test_strong_pair_becomes_signal(self):
        pair, now_ms = sample_pair()
        signal = pair_to_signal(pair, now_ms)
        self.assertIsNotNone(signal)
        self.assertGreaterEqual(signal.score, 70)
        self.assertEqual(signal.address, "ABC123")

    def test_weak_liquidity_is_rejected(self):
        pair, now_ms = sample_pair()
        pair["liquidity"]["usd"] = 9000
        self.assertIsNone(pair_to_signal(pair, now_ms))

    def test_post_contains_risk_and_contract(self):
        pair, now_ms = sample_pair()
        post = write_signal(pair_to_signal(pair, now_ms))["text"]
        self.assertIn("CA: ABC123", post)
        self.assertIn("Not financial advice", post)
        self.assertIn("Cooked Score", post)

    def test_money_format(self):
        self.assertEqual(money(1_250_000), "$1.25M")
        self.assertEqual(money(42_000), "$42.0K")

    def test_rugcheck_danger_is_rejected(self):
        pair, now_ms = sample_pair()
        signal = pair_to_signal(pair, now_ms)

        class Response:
            def raise_for_status(self):
                return None

            def json(self):
                return {"score": 9000, "risks": [{"level": "danger", "name": "Honeypot"}]}

        class Session:
            def get(self, *args, **kwargs):
                return Response()

        self.assertFalse(passes_security(Session(), signal))

