from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
VENV_SITE_PACKAGES = ROOT / ".venv" / "Lib" / "site-packages"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if VENV_SITE_PACKAGES.exists() and str(VENV_SITE_PACKAGES) not in sys.path:
    sys.path.append(str(VENV_SITE_PACKAGES))

from app.providers.kite_provider import KiteProvider
from app.providers.token_store import load_access_token
from app.services.database import (
    Candle,
    OpportunityRecord,
    OptionQuoteSnapshot,
    SignalRecord,
    StrategyValidationRecord,
    TradeRecord,
    get_session,
    init_db,
)
from app.services.data_ingestion_service import DataIngestionService
from app.services.greeks_service import GreeksService
from app.services.market_data_service import MarketDataService
from app.services.option_history_repository import OptionHistoryRepository
from app.services.time_utils import ist_now_naive


TABLES = {
    "candles": Candle,
    "option_quote_snapshots": OptionQuoteSnapshot,
    "signals": SignalRecord,
    "opportunities": OpportunityRecord,
    "trades": TradeRecord,
    "strategy_validations": StrategyValidationRecord,
}


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Reset Bank Nifty research data and ingest fresh Kite candles."
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required to delete existing research/trade history.",
    )
    parser.add_argument(
        "--days", type=int, default=180, help="Underlying Bank Nifty candle lookback."
    )
    parser.add_argument(
        "--option-days", type=int, default=45, help="Current-option candle lookback."
    )
    parser.add_argument("--timeframe", default="5minute")
    parser.add_argument("--strike-window-pct", type=float, default=3.0)
    parser.add_argument("--max-option-contracts", type=int, default=40)
    args = parser.parse_args()

    init_db()
    before = counts()
    if not args.confirm:
        print(
            {
                "status": "dry_run",
                "before": before,
                "message": "Run with --confirm to delete and rebuild.",
            }
        )
        return

    reset_tables()
    after_reset = counts()

    ingestion = DataIngestionService(
        kite_provider_factory=kite_provider,
        market_data_service=MarketDataService(),
        option_history_repository=OptionHistoryRepository(),
        greeks_service=GreeksService(),
    )
    underlying = ingest_underlying_in_chunks(
        ingestion, timeframe=args.timeframe, days=args.days
    )
    option_candles = ingestion.ingest_option_candles(
        symbols=["BANKNIFTY"],
        timeframe=args.timeframe,
        days=args.option_days,
        strike_window_pct=args.strike_window_pct,
        max_contracts_per_symbol=args.max_option_contracts,
    )
    option_snapshots = ingestion.capture_option_snapshots(
        symbols=["BANKNIFTY"],
        strike_window_pct=max(args.strike_window_pct, 4.0),
        max_contracts_per_symbol=120,
    )
    print(
        {
            "status": "ok",
            "before": before,
            "after_reset": after_reset,
            "underlying_candles": underlying,
            "option_candles": option_candles,
            "option_snapshots": option_snapshots,
            "after_ingest": counts(),
        }
    )


def counts() -> dict[str, int]:
    session = get_session()
    try:
        return {
            name: int(session.query(model).count()) for name, model in TABLES.items()
        }
    finally:
        session.close()


def reset_tables() -> None:
    session = get_session()
    try:
        for model in (
            TradeRecord,
            OpportunityRecord,
            SignalRecord,
            StrategyValidationRecord,
            OptionQuoteSnapshot,
            Candle,
        ):
            session.query(model).delete(synchronize_session=False)
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def ingest_underlying_in_chunks(
    ingestion: DataIngestionService, *, timeframe: str, days: int
) -> dict[str, Any]:
    to_dt = ist_now_naive()
    from_dt = to_dt - timedelta(days=days)
    chunk_days = 90
    results: list[dict[str, Any]] = []
    cursor = from_dt
    while cursor < to_dt:
        chunk_to = min(cursor + timedelta(days=chunk_days), to_dt)
        result = ingestion.ingest_candles(
            symbols=["BANKNIFTY"],
            timeframe=timeframe,
            from_date=cursor.isoformat(sep=" "),
            to_date=chunk_to.isoformat(sep=" "),
            use_checkpoint=False,
        )
        results.append(result)
        cursor = chunk_to
    return {
        "status": "ok",
        "timeframe": timeframe,
        "from": from_dt.isoformat(sep=" "),
        "to": to_dt.isoformat(sep=" "),
        "chunk_days": chunk_days,
        "chunks": results,
    }


def kite_provider() -> KiteProvider:
    provider = KiteProvider()
    token = load_access_token()
    if token:
        provider.set_access_token(token)
    return provider


if __name__ == "__main__":
    main()
