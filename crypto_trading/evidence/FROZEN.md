# FROZEN / RESEARCH-ONLY (since 2026-10-01, P1)

This package makes no LIVE entry, exit, size or stop decision, and the
2026-10-01 reviews found no validated effect from it. It is frozen:

- no functional changes;
- the code is kept in git, nothing is deleted;
- its threads/hooks do not run in the trading process
  (`crypto_trading/config/frozen_layers.yaml`).

It is switched back on only for a concrete, pre-registered hypothesis, with
the measurement written up in docs/superpowers/reports/.
See docs/superpowers/ARCHITECTURE.md.
