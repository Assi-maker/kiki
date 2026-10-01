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

Exception: Guardian Authority (guardian/authority*.py) is part of the SAFETY
path and keeps running unchanged (it can only tighten a stop or close early,
never add risk). Only its learning (the strategist self-improvement tick) is
frozen.
