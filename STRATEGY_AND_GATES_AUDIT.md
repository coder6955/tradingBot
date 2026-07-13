# Strategy And Gates Audit

## Strategy Summary

The live scanner is a multi-factor BankNifty directional option-buying system. It uses underlying trend/momentum, BankNifty-specific context, option premium participation, option quality/Greeks, liquidity, volatility edge, entry timing, and risk controls.

Primary path: `app/services/scanner_service.py:35 ScannerService.scan_with_diagnostics`.

## Strategy Components

| Component | File/Class | Live scanner | Backtest | Purpose |
|---|---|---:|---:|---|
| Raw technical score | `indicator_scoring_service.py:7 IndicatorScoringService` | Yes | Partly | RSI/ADX/MACD/EMA/VWAP/volume/context |
| Weighted decision score | `decision_engine_service.py:11 DecisionEngineService` | Yes | Yes through parity mode | Combines components with caps |
| Contract selection | `trade_setup_service.py:28 TradeSetupService` | Yes | Yes | Nearest expiry, strike, liquidity scoring |
| Market regime | `market_regime_service.py` | Yes | Yes/approx | Broad market suitability |
| Price action | `price_action_service.py` | Yes | Yes/approx | Structure, room, trend quality |
| Option chain | `option_chain_service.py` | Yes | Yes/approx | OI/chain context |
| Option quality | `option_quality_service.py:16 OptionQualityService` | Yes | Yes | Delta/theta/IV/DTE/spread |
| Premium confirmation | `option_premium_confirmation_service.py:18` | Yes | Yes if option candles exist | Option premium breakout/VWAP/volume |
| BankNifty intelligence | `banknifty_intelligence_service.py:27` | Yes | Partial | Top banks, relative strength, ORB, zones |
| Regime filter | `banknifty_regime_filter_service.py:22` | Yes | Partial | Extra BankNifty condition filter |
| Entry timing | `entry_timing_service.py:20` | Yes | Limited | Enter now, armed, too late |
| Volatility edge | `volatility_edge_service.py:23` | Yes | Partial | IV/RV, IV crush, expected move |
| Strategy edge | `strategy_edge_service.py` | Optional | Yes | Uses stored validations |
| Outcome learning | `outcome_learning_service.py` | Optional | Analytics | Guards repeated weak setups |

## Weighted Score

Defined in `DecisionEngineService.score_breakdown`.

Weights:

- trend_momentum: 0.14
- market_regime: 0.11
- price_action: 0.19
- option_chain_context: 0.07
- liquidity: 0.13
- option_quality: 0.16
- banknifty_intelligence: 0.20

Caps:

- `trend_momentum` is capped at `MAX_TREND_MOMENTUM_SCORE`.

Threshold:

- `MIN_SIGNAL_SCORE`.

Audit note: score is explainable but not calibrated by local evidence.

## Hard Gates

| Gate | File | Condition | Stored? | Strictness |
|---|---|---|---|---|
| Real market data | `scanner_service.py` | Kite required and snapshot not real | Yes | Correct for live |
| Contract available | `trade_setup_service.py` | no expiry/CE/PE contract | Yes | Correct |
| Freshness live | `data_freshness_service.py` | quote/candle/chain/option stale | Yes via scanner | Correct |
| Option quality | `option_quality_service.py` | score below min | Yes | Good, depends on Greek estimate |
| Premium confirmation | `option_premium_confirmation_service.py` | no fresh premium candles or poor breakout | Yes | Strong; may overblock early |
| BankNifty top bank alignment | `banknifty_intelligence_service.py` | mixed/against top banks | Yes | Useful, data dependent |
| Opening range | `banknifty_intelligence_service.py` | before OR complete / inside OR | Yes | Good late/false-entry filter |
| Major zone trap | `banknifty_intelligence_service.py` | near round-number trap | Yes | Useful, may overblock |
| Range/choppy day | `banknifty_intelligence_service.py` | choppy/range day | Yes | Good |
| Expected move | `banknifty_intelligence_service.py` | move coverage below threshold | Yes | Good |
| Entry timing | `entry_timing_service.py` | too late/reward compressed/spread widened | Yes | Very important |
| Liquidity/spread/OI/volume | `trade_setup_service.py` | below thresholds | Yes | Necessary |
| Expiry day block | `trade_setup_service.py`, `order_service.py` | expiry <= today if enabled | Yes/order error | Conservative |
| Order validation | `order_service.py` | not BankNifty option buy, missing metadata | N/A | Strong live safety |
| Risk guard | `risk_management_service.py` | daily loss/trades/open exposure/cooldown | N/A | Strong |

## Missed Trade Risks

- Premium candle warmup may reject good early moves.
- Top-bank data unavailable reduces confidence but may not always hard block.
- Opening range logic may miss first impulse trades by design.
- Event/news handling is static-date based.

## Low Quality Trade Risks

- Paper mode can use fallback/stored data more permissively.
- Market order execution may degrade edge.
- Strategy score has no current local calibration.

