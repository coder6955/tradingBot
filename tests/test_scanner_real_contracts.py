import unittest

from app.services.scanner_service import ScannerService


class HighScoreFeedWithoutOptions:
    def get_snapshot(self, symbol):  # type: ignore[no-untyped-def]
        return {
            "symbol": symbol,
            "source": "kite",
            "is_real_data": True,
            "price": 1000.0,
            "rsi": 58,
            "adx": 25,
            "macd_positive": True,
            "ema_alignment": True,
            "vwap_above_price": True,
            "volume_confirmed": True,
            "trend_bullish": True,
            "market_context": "strong",
        }

    def get_instruments(self, exchange):  # type: ignore[no-untyped-def]
        return []


class ScannerRealContractTests(unittest.TestCase):
    def test_high_score_without_option_contract_does_not_emit_placeholder_signal(self) -> None:
        scanner = ScannerService(feed=HighScoreFeedWithoutOptions())  # type: ignore[arg-type]

        opportunities = scanner.scan_symbols(symbols=["TEST"], side="BUY")

        self.assertEqual(opportunities, [])

    def test_legacy_indicators_do_not_change_active_technical_score(self) -> None:
        scanner = ScannerService(feed=HighScoreFeedWithoutOptions())  # type: ignore[arg-type]
        snapshot = {
            "rsi": 44,
            "adx": 24,
            "macd_positive": False,
            "ema_alignment": False,
            "vwap_above_price": False,
            "volume_confirmed": True,
            "trend_bullish": False,
            "market_context": "strong",
        }

        aligned_score = scanner._technical_score(snapshot, "bearish")
        snapshot.update(
            {
                "rsi": 90,
                "adx": 2,
                "macd_positive": True,
                "ema_alignment": True,
                "vwap_above_price": True,
                "volume_confirmed": False,
                "trend_bullish": True,
            }
        )
        opposed_score = scanner._technical_score(snapshot, "bearish")

        self.assertEqual(aligned_score, 50)
        self.assertEqual(opposed_score, 50)


if __name__ == "__main__":
    unittest.main()
