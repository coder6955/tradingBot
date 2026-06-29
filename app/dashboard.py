import sys
from pathlib import Path

import streamlit as st

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.services.chart_service import ChartService
from app.services.historical_candles_service import HistoricalCandlesService
from app.services.market_analysis_service import MarketAnalysisService
from app.services.market_service import MarketService
from app.services.paper_trading_service import PaperTradingService
from app.services.scanner_service import ScannerService
from app.services.signal_repository import SignalRepository

st.set_page_config(page_title="AI Option Trader", layout="wide")
st.title("AI Option Trader")

market_service = MarketService()
scanner_service = ScannerService()
market_analysis_service = MarketAnalysisService()
paper_trading_service = PaperTradingService()
signal_repository = SignalRepository()
historical_candles_service = HistoricalCandlesService()
chart_service = ChartService()

market_status = market_service.get_market_status()
st.subheader("Market Status")
st.json(market_status)

snapshot = {"nifty": 22150, "banknifty": 47200, "vix": 14.3}
market_summary = market_analysis_service.analyze_market(snapshot)
st.subheader("Market Regime")
st.json(market_summary)

st.subheader("Top Opportunities")
recommendations = scanner_service.scan_symbols(
    symbols=["NIFTY", "BANKNIFTY", "RELIANCE", "HDFCBANK"],
    side="BUY",
)
for signal in recommendations:
    st.metric(signal.symbol, f"Score {signal.score}", f"{signal.probability * 100:.0f}% probability")
    st.write(
        f"{signal.action} {signal.tradingsymbol or ''} | Entry: {signal.entry_price} | "
        f"Stop: {signal.stop_loss} | Qty: {signal.quantity} | RR: {signal.risk_reward}"
    )

st.subheader("Recent Signals")
for record in signal_repository.list_signals()[:5]:
    st.write(f"{record.symbol} | {record.action} | Score {record.score} | Confidence {record.confidence:.0%}")

st.subheader("Historical Candles")
candles = historical_candles_service.generate_candles("NIFTY", 10, 22000.0)
closes = [candle["close"] for candle in candles]
chart_series = chart_service.build_series(closes, "close")
st.line_chart([point["y"] for point in chart_series])
for candle in candles[:5]:
    st.write(f"{candle['timestamp']} | O {candle['open']} | H {candle['high']} | L {candle['low']} | C {candle['close']} | V {candle['volume']}")

st.subheader("Paper Trading")
summary = paper_trading_service.get_summary()
st.json(summary)
