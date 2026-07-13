import os
import tempfile
import unittest
from datetime import datetime, timedelta

from app.services.data_ingestion_service import DataIngestionService
from app.services.database import Candle, RejectedOpportunityRecord, get_session, init_db
from app.services.rejected_opportunity_repository import RejectedOpportunityRepository
from app.services.trade_setup_service import OptionContract


class FakeKiteProvider:
    def __init__(self) -> None:
        self.historical_calls: list[tuple[int, datetime, datetime, str]] = []

    def historical_data(self, token, from_dt, to_dt, timeframe):  # type: ignore[no-untyped-def]
        self.historical_calls.append((int(token), from_dt, to_dt, str(timeframe)))
        step = 1 if str(timeframe) == "1minute" else 5
        rows = []
        current = from_dt.replace(second=0, microsecond=0)
        while current <= to_dt.replace(second=0, microsecond=0):
            rows.append(
                {
                    "date": current,
                    "open": 100,
                    "high": 105,
                    "low": 95,
                    "close": 101,
                    "volume": 1000,
                }
            )
            current += timedelta(minutes=step)
        return rows

    def instruments(self, exchange):  # type: ignore[no-untyped-def]
        return [
            {
                "tradingsymbol": "BANKNIFTY26JUL58000CE",
                "instrument_token": 580001,
                "name": "BANKNIFTY",
                "instrument_type": "CE",
                "strike": 58000,
                "expiry": "2026-07-26",
            }
        ]


class TargetedOptionCandleBackfillTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")
        self.provider = FakeKiteProvider()

    def tearDown(self) -> None:
        try:
            if os.path.exists(self.temp_db.name):
                os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def test_relevant_contract_backfill_uses_rejected_contract_token_and_reports_coverage(self) -> None:
        rejected_repo = RejectedOpportunityRepository()
        contract = OptionContract(
            tradingsymbol="BANKNIFTY26JUL58000CE",
            exchange="NFO",
            instrument_token=580001,
            name="BANKNIFTY",
            expiry="2026-07-26",
            strike=58000,
            option_type="CE",
            lot_size=15,
            last_price=100,
            open_interest=50000,
            volume=10000,
            bid=99,
            ask=101,
        )
        rejected = rejected_repo.save_rejection(
            symbol="BANKNIFTY",
            side="BUY",
            action="BUY_CE",
            score=78,
            reasons=["premium confirmation failed"],
            contract=contract,
            factor_scores={"prices": {"entry_price": 100, "stop_loss": 90, "target_1": 120}},
            market_session="REGULAR_MARKET",
            learning_eligible=True,
        )
        self._set_rejection_created_at(rejected.id, datetime(2026, 7, 13, 9, 30))
        service = DataIngestionService(kite_provider_factory=lambda: self.provider)

        result = service.backfill_relevant_option_candles(
            symbols=["BANKNIFTY"],
            trading_date="2026-07-13",
            timeframes=["1minute", "5minute"],
            batch_limit=10,
            delay_seconds=0,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["contracts_found"], 1)
        self.assertEqual(result["contracts_attempted"], 1)
        self.assertEqual({call[3] for call in self.provider.historical_calls}, {"1minute", "5minute"})
        self.assertGreater(result["inserted"], 0)
        one_minute = result["coverage_after"]["by_timeframe"]["1minute"]
        self.assertEqual(one_minute["data_quality"], "high_confidence")
        self.assertEqual(one_minute["missing_candles"], 0)

    def test_coverage_combines_symbol_and_websocket_token_candles(self) -> None:
        rejected_repo = RejectedOpportunityRepository()
        contract = OptionContract(
            tradingsymbol="BANKNIFTY26JUL58000CE",
            exchange="NFO",
            instrument_token=580001,
            name="BANKNIFTY",
            expiry="2026-07-26",
            strike=58000,
            option_type="CE",
            lot_size=15,
            last_price=100,
            open_interest=50000,
            volume=10000,
            bid=99,
            ask=101,
        )
        rejected = rejected_repo.save_rejection(
            symbol="BANKNIFTY",
            side="BUY",
            action="BUY_CE",
            score=78,
            reasons=["premium confirmation failed"],
            contract=contract,
            factor_scores={"prices": {"entry_price": 100, "stop_loss": 90, "target_1": 120}},
            market_session="REGULAR_MARKET",
            learning_eligible=True,
        )
        self._set_rejection_created_at(rejected.id, datetime(2026, 7, 13, 15, 7))
        self._save_candle("BANKNIFTY26JUL58000CE", "1minute", datetime(2026, 7, 13, 15, 7))
        self._save_candle("WS_TOKEN:580001", "1minute", datetime(2026, 7, 13, 15, 8))
        self._save_candle("WS_TOKEN:580001", "1minute", datetime(2026, 7, 13, 15, 9))
        self._save_candle("WS_TOKEN:580001", "1minute", datetime(2026, 7, 13, 15, 10))
        service = DataIngestionService(kite_provider_factory=lambda: self.provider)

        result = service.option_candle_coverage_report(
            symbols=["BANKNIFTY"],
            trading_date="2026-07-13",
            timeframes=["1minute"],
        )

        row = result["rows"][0]
        self.assertEqual(row["actual_candles"], 4)
        self.assertEqual(row["coverage_pct"], 100.0)
        self.assertEqual(row["data_quality"], "high_confidence")
        self.assertEqual(row["stored_symbol_rows"], 1)
        self.assertEqual(row["websocket_token_rows"], 3)

    def test_live_relevant_backfill_repairs_missing_closed_intraday_option_candles(self) -> None:
        rejected_repo = RejectedOpportunityRepository()
        contract = OptionContract(
            tradingsymbol="BANKNIFTY26JUL58000CE",
            exchange="NFO",
            instrument_token=580001,
            name="BANKNIFTY",
            expiry="2026-07-26",
            strike=58000,
            option_type="CE",
            lot_size=15,
            last_price=100,
            open_interest=50000,
            volume=10000,
            bid=99,
            ask=101,
        )
        rejected = rejected_repo.save_rejection(
            symbol="BANKNIFTY",
            side="BUY",
            action="BUY_CE",
            score=78,
            reasons=["premium confirmation failed"],
            contract=contract,
            factor_scores={"prices": {"entry_price": 100, "stop_loss": 90, "target_1": 120}},
            market_session="REGULAR_MARKET",
            learning_eligible=True,
        )
        self._set_rejection_created_at(rejected.id, datetime(2026, 7, 13, 10, 50))
        self._save_candle("BANKNIFTY26JUL58000CE", "1minute", datetime(2026, 7, 13, 11, 0))
        service = DataIngestionService(kite_provider_factory=lambda: self.provider)

        result = service.backfill_live_relevant_option_candle_gaps(
            symbols=["BANKNIFTY"],
            now=datetime(2026, 7, 13, 11, 10),
            timeframes=["1minute"],
            max_contracts=5,
            lookback_minutes=30,
            batch_limit=5,
            delay_seconds=0,
        )

        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["contracts_found"], 1)
        self.assertEqual(result["historical_calls"], 1)
        self.assertGreater(result["inserted"], 0)
        token, from_dt, to_dt, timeframe = self.provider.historical_calls[-1]
        self.assertEqual(token, 580001)
        self.assertEqual(timeframe, "1minute")
        self.assertEqual(from_dt, datetime(2026, 7, 13, 11, 1))
        self.assertEqual(to_dt, datetime(2026, 7, 13, 11, 9))

    def _set_rejection_created_at(self, rejection_id: int, value: datetime) -> None:
        session = get_session()
        try:
            row = session.get(RejectedOpportunityRecord, rejection_id)
            row.created_at = value
            session.commit()
        finally:
            session.close()

    def _save_candle(self, symbol: str, timeframe: str, timestamp: datetime) -> None:
        session = get_session()
        try:
            session.add(
                Candle(
                    symbol=symbol.upper(),
                    timeframe=timeframe,
                    timestamp=timestamp,
                    open_price=100,
                    high_price=105,
                    low_price=95,
                    close_price=101,
                    volume=1000,
                )
            )
            session.commit()
        finally:
            session.close()


if __name__ == "__main__":
    unittest.main()
