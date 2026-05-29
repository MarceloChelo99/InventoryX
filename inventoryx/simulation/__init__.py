"""
Layer 3 — daily discrete-event simulation harness.

This package exists ONLY to validate engine behavior across regimes and
supply shocks. It is NOT a production forecasting validator — for real-world
accuracy you need real backtest data. See the truth/forecaster separation:
truth.py and demand_service.py deliberately share no helpers so the test
cannot be tautological.
"""
