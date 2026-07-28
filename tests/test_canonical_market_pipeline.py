import os
import tempfile
import unittest
import json
from datetime import datetime, timedelta

from app.config import settings
from app.providers.kite_feed import KiteFeed
from app.providers.kite_provider import KiteProvider
from app.services.database import Candle, RawTickRecord, get_session, init_db
from app.services.data_ingestion_service import DataIngestionService
from app.services.kite_websocket_price_feed import WebSocketTick
from app.services.latency_metrics_service import LatencyMetricsService
from app.services.raw_tick_capture_service import RawTickCaptureService
from app.services.underlying_candle_service import UnderlyingCandleService
from app.services.tick_replay_service import TickReplayService


class CanonicalMarketPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_db = tempfile.NamedTemporaryFile(suffix=".db", delete=False)
        self.temp_db.close()
        init_db(f"sqlite:///{self.temp_db.name}")
        self.original_candle_pipeline = settings.enable_underlying_candle_pipeline
        self.original_raw_capture = settings.enable_raw_tick_capture
        object.__setattr__(settings, "enable_underlying_candle_pipeline", True)
        object.__setattr__(settings, "enable_raw_tick_capture", True)

    def tearDown(self) -> None:
        object.__setattr__(settings, "enable_underlying_candle_pipeline", self.original_candle_pipeline)
        object.__setattr__(settings, "enable_raw_tick_capture", self.original_raw_capture)
        try:
            os.remove(self.temp_db.name)
        except PermissionError:
            pass

    def _tick(self, minute: int, price: float, *, token: int = 260105, volume: float = 1000) -> WebSocketTick:
        stamp = datetime(2026, 7, 3, 9, minute, 5)
        return WebSocketTick(
            instrument_token=token,
            price=price,
            timestamp=stamp,
            receive_timestamp=stamp + timedelta(milliseconds=25),
            timestamp_source="exchange_timestamp",
            packet_type="full",
            volume=volume,
        )

    def test_exchange_ticks_build_completed_one_and_five_minute_candles(self) -> None:
        service = UnderlyingCandleService()
        service.set_underlying_token(260105)
        service.start()
        for offset, minute in enumerate(range(15, 22)):
            service.on_tick(self._tick(minute, 58000 + offset, volume=1000 + (offset * 10)))
        service.stop()

        session = get_session()
        try:
            one_minute = session.query(Candle).filter(Candle.symbol == "BANKNIFTY", Candle.timeframe == "1minute").order_by(Candle.timestamp).all()
            five_minute = session.query(Candle).filter(Candle.symbol == "BANKNIFTY", Candle.timeframe == "5minute").order_by(Candle.timestamp).all()
        finally:
            session.close()
        self.assertEqual(len(one_minute), 6)
        self.assertEqual(len(five_minute), 1)
        self.assertEqual(five_minute[0].timestamp, datetime(2026, 7, 3, 9, 15))
        self.assertEqual(five_minute[0].open_price, 58000)
        self.assertEqual(five_minute[0].close_price, 58004)
        self.assertEqual(five_minute[0].timestamp_source, "exchange_timestamp")

    def test_missing_minutes_are_generated_only_inside_session(self) -> None:
        service = UnderlyingCandleService()
        service.set_underlying_token(260105)
        service.start()
        service.on_tick(self._tick(15, 58000))
        service.on_tick(self._tick(18, 58030))
        service.stop()

        session = get_session()
        try:
            rows = session.query(Candle).filter(Candle.timeframe == "1minute").order_by(Candle.timestamp).all()
        finally:
            session.close()
        self.assertEqual([row.timestamp.minute for row in rows], [15, 16, 17])
        self.assertEqual([bool(row.is_generated) for row in rows], [False, True, True])

    def test_local_receive_timestamp_is_rejected_for_canonical_candles(self) -> None:
        service = UnderlyingCandleService()
        service.set_underlying_token(260105)
        tick = self._tick(15, 58000)
        local_tick = WebSocketTick(**{**tick.__dict__, "timestamp_source": "local_receive_time"})
        result = service.on_tick(local_tick)
        self.assertFalse(result["accepted"])
        self.assertEqual(result["reason"], "exchange_timestamp_provenance_required")

    def test_broad_context_quotes_do_not_populate_analysis_cache(self) -> None:
        class Client:
            def quote(self, instruments):
                stamp = datetime(2026, 7, 3, 10, 30)
                return {item: {"last_price": 58000, "exchange_timestamp": stamp} for item in instruments}

        feed = KiteFeed()
        feed.client = Client()
        result = feed.get_snapshots(["BANKNIFTY", "NIFTY"])
        self.assertTrue(result["BANKNIFTY"]["context_quote_only"])
        self.assertEqual(feed._snapshot_cache, {})
        self.assertIn("BANKNIFTY", feed._context_snapshot_cache)

    def test_raw_tick_queue_evicts_warm_for_risk_sensitive_tick(self) -> None:
        class AliveWorker:
            def is_alive(self):
                return True

        service = RawTickCaptureService(queue_size=1)
        service._worker = AliveWorker()  # type: ignore[assignment]
        warm = service.capture(self._tick(15, 100, token=1), symbol="WARM", owners=["banknifty_prewarm"])
        risk = service.capture(self._tick(15, 101, token=2), symbol="RISK", owners=["active_trade"])
        self.assertTrue(warm["captured"])
        self.assertTrue(risk["captured"])
        self.assertEqual(service.evicted_warm_count, 1)
        self.assertEqual(service.dropped_risk_count, 0)
        self.assertTrue(service._queue.get_nowait()["risk_sensitive"])

    def test_raw_tick_capture_preserves_five_level_depth(self) -> None:
        service = RawTickCaptureService(queue_size=10)
        service.start()
        tick = WebSocketTick(
            **{
                **self._tick(15, 100, token=123).__dict__,
                "bid": 99.5,
                "ask": 100.5,
                "buy_depth": ({"price": 99.5, "quantity": 15}, {"price": 99.0, "quantity": 30}),
                "sell_depth": ({"price": 100.5, "quantity": 15},),
            }
        )
        service.capture(tick, symbol="BANKNIFTY26JUL58000CE", owners=["active_trade"])
        service.stop()

        session = get_session()
        try:
            row = session.query(RawTickRecord).one()
            depth = json.loads(row.depth_json)
        finally:
            session.close()
        self.assertEqual(depth["buy"][1]["quantity"], 30)
        self.assertEqual(depth["sell"][0]["price"], 100.5)

    def test_canonical_historical_bootstrap_filters_after_hours_and_is_idempotent(self) -> None:
        class Provider:
            def instruments(self, exchange=None):
                return [{"tradingsymbol": "NIFTY BANK", "name": "NIFTY BANK", "instrument_token": 260105}]

            def historical_data(self, token, from_dt, to_dt, timeframe):
                minute = 1 if timeframe == "1minute" else 5
                return [
                    {"date": datetime(2026, 7, 3, 8, 59), "open": 57900, "high": 57910, "low": 57890, "close": 57900, "volume": 10},
                    {"date": datetime(2026, 7, 3, 9, 15), "open": 58000, "high": 58020, "low": 57990, "close": 58010, "volume": 100},
                    {"date": datetime(2026, 7, 3, 9, 15) + timedelta(minutes=minute), "open": 58010, "high": 58030, "low": 58000, "close": 58020, "volume": 120},
                    {"date": datetime(2026, 7, 3, 15, 30), "open": 58100, "high": 58110, "low": 58090, "close": 58100, "volume": 50},
                ]

        service = DataIngestionService(kite_provider_factory=Provider)
        kwargs = {"from_date": "2026-07-03 09:15:00", "to_date": "2026-07-03 15:31:00", "use_checkpoint": False}
        first = service.bootstrap_canonical_banknifty(**kwargs)
        second = service.bootstrap_canonical_banknifty(**kwargs)

        session = get_session()
        try:
            rows = session.query(Candle).filter(Candle.symbol == "BANKNIFTY").order_by(Candle.timeframe, Candle.timestamp).all()
        finally:
            session.close()
        self.assertEqual(first["status"], "ok")
        self.assertEqual(second["status"], "ok")
        self.assertEqual(len(rows), 5)
        self.assertEqual({row.timeframe for row in rows}, {"1minute", "5minute"})
        self.assertTrue(all(row.timestamp_source == "broker_exchange_timestamp" for row in rows))
        self.assertTrue(all(row.data_quality == "broker_historical_complete" for row in rows))
        self.assertTrue(all(row.timestamp.time() >= datetime(2026, 7, 3, 9, 15).time() for row in rows))

    def test_one_minute_bootstrap_chunks_ranges_below_broker_limit(self) -> None:
        calls = []

        class Provider:
            def instruments(self, exchange=None):
                return [{"tradingsymbol": "NIFTY BANK", "name": "NIFTY BANK", "instrument_token": 260105}]

            def historical_data(self, token, from_dt, to_dt, timeframe):
                calls.append((from_dt, to_dt, timeframe))
                if (to_dt - from_dt).days > 60:
                    raise AssertionError("broker interval limit exceeded")
                return []

        service = DataIngestionService(kite_provider_factory=Provider)
        result = service.ingest_candles(
            symbols=["BANKNIFTY"],
            timeframe="1minute",
            from_date="2026-03-01 09:15:00",
            to_date="2026-07-01 15:30:00",
            use_checkpoint=False,
        )

        self.assertEqual(result["results"][0]["status"], "ok")
        self.assertEqual(result["results"][0]["historical_calls"], 3)
        self.assertEqual(len(calls), 3)
        self.assertTrue(all((end - start).days <= 55 for start, end, _ in calls))

    def test_latency_report_has_tail_percentiles(self) -> None:
        service = LatencyMetricsService(sample_limit=20)
        for value in range(1, 11):
            service.record("tick_to_decision", value)
        metric = service.report()["metrics"]["tick_to_decision"]
        self.assertEqual(metric["sample_size"], 10)
        self.assertEqual(metric["p50_ms"], 5.0)
        self.assertEqual(metric["p95_ms"], 10.0)
        self.assertEqual(metric["p99_ms"], 10.0)
        self.assertEqual(metric["max_ms"], 10.0)

    def test_latency_report_exposes_all_required_metrics_and_missing_samples(self) -> None:
        service = LatencyMetricsService(sample_limit=20)
        service.record_missing("exchange_tick_to_application_receive", detail={"reason": "exchange_timestamp_unavailable"})

        report = service.report()

        self.assertTrue(set(LatencyMetricsService.REQUIRED_METRICS).issubset(report["metrics"]))
        self.assertEqual(report["metrics"]["exchange_tick_to_application_receive"]["sample_count"], 0)
        self.assertEqual(report["metrics"]["exchange_tick_to_application_receive"]["missing_sample_count"], 1)
        self.assertIsNone(report["metrics"]["exit_trigger_to_fill"]["p99_ms"])

    def test_persisted_tick_replay_preserves_capture_sequence(self) -> None:
        stamp = datetime(2026, 7, 3, 10, 0)
        session = get_session()
        try:
            for sequence, price, receive_offset in [(30, 103.0, 1), (10, 101.0, 3), (20, 102.0, 2)]:
                session.add(
                    RawTickRecord(
                        session_date="2026-07-03",
                        sequence=sequence,
                        instrument_token=260105,
                        symbol="BANKNIFTY",
                        last_price=price,
                        exchange_timestamp=stamp + timedelta(seconds=sequence),
                        receive_timestamp=stamp + timedelta(seconds=receive_offset),
                        timestamp_source="exchange_timestamp",
                        packet_type="full",
                        capture_context="warm_market",
                        strategy_version="v2-test",
                        config_hash="hash-a",
                    )
                )
            session.commit()
        finally:
            session.close()
        seen = []
        result = TickReplayService(lambda tick: seen.append(tick.price)).replay_persisted(session_date="2026-07-03")
        self.assertEqual(seen, [101.0, 102.0, 103.0])
        self.assertFalse(result["mixed_lineage"])

    def test_kite_interval_translation(self) -> None:
        from app.providers.kite_auth_state import kite_auth_state

        kite_auth_state.clear()

        class Client:
            def __init__(self):
                self.interval = None

            def historical_data(self, token, from_dt, to_dt, interval):
                self.interval = interval
                return []

        provider = KiteProvider()
        client = Client()
        provider.client = client
        provider.access_token = "mock-token"
        provider.historical_data(260105, datetime(2026, 7, 3, 9, 15), datetime(2026, 7, 3, 10, 15), "1minute")
        self.assertEqual(client.interval, "minute")
        self.assertEqual(provider._broker_interval("1minute"), "minute")
        self.assertEqual(provider._broker_interval("5minute"), "5minute")
        with self.assertRaises(ValueError):
            provider._broker_interval("2minute")


if __name__ == "__main__":
    unittest.main()
