"""Historical Evidence Layer - offline builder (research only, 2026-09-30).

    python -m crypto_trading.entry_research.evidence_builder

The ONLY writer of data/historical_evidence.db. The bot never imports this
module; it reads the database read-only through crypto_trading/evidence/.

Source: the exit-lab checkpoints (2.4 M historical entries: 17 event
families + random BASELINE, point-in-time regimes, outcomes after costs for
the FIXED exit and 15m / 30m / 1h / 2h / 4h holding times).

Temporal contract: a snapshot with `as_of` includes an outcome only if its
EXIT time is < as_of (an entry still open at as_of is invisible). Snapshots
are built at every month start from 2026-02-01 (the end of TRAIN) and at
the end of the data, so a replayed decision can read exactly what was known
then.

Statistics per (signal type, side, regime, exit model, period): n, mean R,
standard error clustered per UTC entry day (streamed - rows arrive in time
order, so each cell keeps only running sums), 95 % CI, one-sided p, win
rate, MFE (24 h) / MAE, holding time, USDT per trade at 1000 notional, and
the difference to the random BASELINE in the same side/regime/exit/period.
"""

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import sys
from datetime import UTC, datetime
from pathlib import Path

from crypto_trading.entry_research import exit_lab as xl
from crypto_trading.entry_research import regime_lab as rl
from crypto_trading.entry_research.stats import t_sf
from crypto_trading.evidence import store, verdict

DB_PATH = store.DEFAULT_PATH
DAY = 86_400
EXITS = store.EXIT_MODELS  # FIXED gets the regime breakdown, the TIME models only ALL
OOS_PERIODS = ("VALID", "TEST", "HOLDOUT")
SURVIVORSHIP = (
    "Universe = symbols listed on 2026-09-30 (survivorship bias: coins delisted during "
    "the window are missing); Binance prices/funding as proxy for BingX; no AI step replayed."
)


def as_of_dates() -> list[datetime]:
    out = [datetime(2026, m, 1, tzinfo=UTC) for m in range(2, 10)]
    out.append(datetime(2026, 9, 30, tzinfo=UTC))  # end of the archive data
    return out


def period_days_until(as_of_ts: int) -> dict[str, float]:
    """Days of each period that lie before as_of (the trades/day denominator)."""
    days = {}
    for name, a, b in rl.PERIODS:
        days[name] = max(0.0, (min(b, as_of_ts) - a) / DAY)
    days["OOS"] = sum(days[p] for p in OOS_PERIODS)
    return days


def pooled_flags(tab: xl.ExitTable) -> bytearray:
    """ANY = all event types pooled, one per symbol/side/hour (as in the labs)."""
    flags = bytearray(len(tab))
    last: dict[tuple, int] = {}
    base = rl.TYPE_IDX["BASELINE"]
    for i in sorted(range(len(tab)), key=lambda j: tab.T[j]):
        if tab.typ[i] == base:
            continue
        key = (tab.sym[i], tab.side[i])
        if tab.T[i] - last.get(key, -(10**12)) >= rl.THIN_S:
            last[key] = tab.T[i]
            flags[i] = 1
    return flags


# accumulator layout
N, S, W, MFE, MAE, H, U, DAYC, DS, DN, A, B, C, G = range(14)


def _acc() -> list:
    return [0, 0.0, 0, 0.0, 0.0, 0.0, 0.0, None, 0.0, 0, 0.0, 0.0, 0.0, 0]


def _flush(a: list) -> None:
    if a[DAYC] is not None and a[DN]:
        a[A] += a[DS] * a[DS]
        a[B] += a[DS] * a[DN]
        a[C] += a[DN] * a[DN]
        a[G] += 1
    a[DS], a[DN] = 0.0, 0


def _add(a: list, day: int, r: float, mfe: float, mae: float, hold: float, usdt: float) -> None:
    if a[DAYC] != day:
        _flush(a)
        a[DAYC] = day
    a[N] += 1
    a[S] += r
    a[W] += r > 0
    a[MFE] += mfe
    a[MAE] += mae
    a[H] += hold
    a[U] += usdt
    a[DS] += r
    a[DN] += 1


def _finish(a: list, days: float) -> dict:
    _flush(a)
    n = a[N]
    m = a[S] / n
    se = None
    if a[G] > 1:
        var = (a[A] - 2 * m * a[B] + m * m * a[C]) / (n * n) * a[G] / (a[G] - 1)
        se = math.sqrt(max(var, 0.0))
    p = t_sf(m / se, a[G] - 1) if se else None
    return {
        "n": n,
        "trades_per_day": round(n / days, 3) if days > 0 else None,
        "mean_r": m,
        "se_r": se,
        "ci_low": m - 1.96 * se if se is not None else None,
        "ci_high": m + 1.96 * se if se is not None else None,
        "p_pos": p,
        "win_rate": a[W] / n,
        "mfe_r": a[MFE] / n,
        "mae_r": a[MAE] / n,
        "hold_min": a[H] / n,
        "net_usdt_per_trade": a[U] / n,
    }


def build_snapshot(
    tab: xl.ExitTable, as_of: datetime, pooled: bytearray, accepted: set[tuple]
) -> tuple[list[tuple], list[tuple]]:
    """Cell rows and verdict rows of one snapshot. Pure w.r.t. I/O."""
    as_of_ts = int(as_of.timestamp())
    vidx = {name: xl.VNAMES.index(name) for name in EXITS}
    base = rl.TYPE_IDX["BASELINE"]
    acc: dict[tuple, list] = {}
    for i in sorted(range(len(tab)), key=lambda j: tab.T[j]):
        pi = tab.per[i]
        if pi == rl.NONE:
            continue
        pname = rl.PERIOD_NAMES[pi]
        periods = (pname, "OOS") if pname in OOS_PERIODS else (pname,)
        side = rl.SIDES[tab.side[i]]
        types = [rl.TYPES[tab.typ[i]]]
        if tab.typ[i] != base and pooled[i]:
            types.append("ANY")
        regs = [("ALL", "ALL")] + [
            (dim, rl.DIMS[dim][tab.reg[di][i]])
            for di, dim in enumerate(rl.DIM_NAMES)
            if tab.reg[di][i] != rl.NONE
        ]
        day = tab.T[i] // DAY
        risk_usdt = xl.STOP_ATR * tab.stop[i] / 100 * rl.NOTIONAL
        for em, vi in vidx.items():
            r = tab.r[vi][i]
            if r != r:
                continue
            hold = tab.hold[vi][i]
            if tab.entry[i] + hold * xl.BAR_S >= as_of_ts:
                continue  # not yet exited at as_of -> unknown at that time
            mae = tab.mae[vi][i]
            usdt = r * risk_usdt
            for typ in types:
                for dim, val in regs if em == "FIXED" else regs[:1]:
                    for p in periods:
                        key = (typ, side, dim, val, em, p)
                        a = acc.get(key)
                        if a is None:
                            a = acc[key] = _acc()
                        _add(a, day, r, tab.mfe24[i], mae, hold * 5.0, usdt)
    days = period_days_until(as_of_ts)
    stats = {k: _finish(a, days[k[5]]) for k, a in acc.items()}
    for k, s in stats.items():  # difference to the random baseline
        b = stats.get(("BASELINE", *k[1:]))
        if k[0] == "BASELINE" or b is None or s["se_r"] is None or b["se_r"] is None:
            s["baseline_mean_r"] = b["mean_r"] if b else None
            s["diff_vs_baseline"] = s["p_diff"] = None
            continue
        diff = s["mean_r"] - b["mean_r"]
        se = math.sqrt(s["se_r"] ** 2 + b["se_r"] ** 2)
        s["baseline_mean_r"] = b["mean_r"]
        s["diff_vs_baseline"] = diff
        s["p_diff"] = math.erfc(abs(diff / se) / math.sqrt(2)) if se > 0 else None
    cell_rows = [(*k, *[s[c] for c in CELL_STAT_COLS]) for k, s in stats.items()]
    verdict_rows = []
    groups: dict[tuple, dict] = {}
    for k, s in stats.items():
        if k[4] == "FIXED":
            groups.setdefault(k[:4], {})[k[5]] = s
    for g, periods_ in groups.items():
        v = verdict.verdict(periods_, protocol_accepted=g in accepted)
        verdict_rows.append(
            (
                *g,
                v["oos_status"],
                v["strength"],
                v["vs_baseline"],
                int(v["train_only_positive"]),
                int(v["protocol_accepted"]),
                v["headline"],
            )
        )
    return cell_rows, verdict_rows


CELL_STAT_COLS = (
    "n",
    "trades_per_day",
    "mean_r",
    "se_r",
    "ci_low",
    "ci_high",
    "p_pos",
    "win_rate",
    "mfe_r",
    "mae_r",
    "hold_min",
    "net_usdt_per_trade",
    "baseline_mean_r",
    "diff_vs_baseline",
    "p_diff",
)


def accepted_cells() -> tuple[set[tuple], str]:
    """Cells the pre-registered protocol accepted (frozen selection AND passed
    TEST + HOLDOUT). Both labs accepted nothing, so this is empty today."""
    out: set[tuple] = set()
    h = hashlib.sha256()
    rj = xl.OUT / "regime_lab.json"
    if rj.exists():
        r = json.loads(rj.read_text(encoding="utf-8"))
        g = r["geometries"].get(rl.PRIMARY, {})
        h.update(json.dumps(g.get("selection"), sort_keys=True).encode())
        if g.get("accepted"):
            out |= {tuple(k.split("|")) for k in g["selection"]["survivors"]}
    ej = xl.OUT / "exit_lab_selection.json"
    if ej.exists():
        e = json.loads(ej.read_text(encoding="utf-8"))
        h.update(e["sha256"].encode())
    xr = xl.OUT / "exit_lab.json"
    if xr.exists() and json.loads(xr.read_text(encoding="utf-8")).get("accepted"):
        sel = json.loads(ej.read_text(encoding="utf-8"))["selection"]["survivors"]
        out |= {tuple(s["cell"].split("|")) for s in sel if s["variant"] == "FIXED"}
    return out, h.hexdigest()


def write_snapshot(conn: sqlite3.Connection, meta: dict, cells: list, verdicts: list) -> None:
    sid = meta["snapshot_id"]
    for t in ("evidence_cell", "evidence_verdict", "evidence_snapshot"):
        conn.execute(f"DELETE FROM {t} WHERE snapshot_id = ?", (sid,))  # noqa: S608
    conn.execute(
        "INSERT INTO evidence_snapshot VALUES (?,?,?,?,?,?,?,?,?)",
        (
            sid,
            meta["as_of"],
            meta["built_at"],
            meta["code_hash"],
            meta["source"],
            meta["data_start"],
            meta["symbols"],
            meta["survivorship_note"],
            meta["selection_sha"],
        ),
    )
    conn.executemany(
        f"INSERT INTO evidence_cell VALUES ({','.join('?' * 22)})", [(sid, *c) for c in cells]
    )
    conn.executemany(
        f"INSERT INTO evidence_verdict VALUES ({','.join('?' * 11)})", [(sid, *v) for v in verdicts]
    )
    conn.commit()


def load_table() -> xl.ExitTable:
    conn = sqlite3.connect(f"file:{rl.ARCHIVE_DB}?mode=ro", uri=True)
    symbols = [s for (s,) in conn.execute("SELECT DISTINCT symbol FROM klines5m ORDER BY symbol")]
    btc = rl.load_derivs(conn, [rl.REF]).oi.get(rl.REF, ([], []))
    conn.close()
    code = xl.code_hash()
    tab = xl.ExitTable()
    by_t: dict[int, list] = {}
    for sym in symbols:
        cp = xl.load_checkpoint(sym, code)
        if cp is None:
            raise RuntimeError(f"missing exit-lab checkpoint for {sym} (run exit_lab first)")
        ch, xs = cp
        tab.extend(ch)
        rl.add_cross_section(by_t, xs)
    tab.attach_market(rl.market_regimes(by_t, btc))
    return tab


def main() -> None:
    tab = load_table()
    print(f"{len(tab)} entries loaded", flush=True)
    pooled = pooled_flags(tab)
    accepted, sel_sha = accepted_cells()
    code = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()[:16] + "/" + xl.code_hash()[:16]
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.executescript(store.SCHEMA)
    for as_of in as_of_dates():
        cells, verdicts = build_snapshot(tab, as_of, pooled, accepted)
        meta = {
            "snapshot_id": f"{as_of.isoformat()}#{code[:8]}",
            "as_of": as_of.isoformat(),
            "built_at": datetime.now(UTC).isoformat(),
            "code_hash": code,
            "source": "exit_lab checkpoints (regime_lab entries, 22 exits) 2025-08..2026-09",
            "data_start": datetime.fromtimestamp(rl.PERIODS[0][1], UTC).isoformat(),
            "symbols": len(tab.syms),
            "survivorship_note": SURVIVORSHIP,
            "selection_sha": sel_sha,
        }
        write_snapshot(conn, meta, cells, verdicts)
        print(f"snapshot {as_of.date()}: {len(cells)} cells, {len(verdicts)} verdicts", flush=True)
    conn.close()


if __name__ == "__main__":
    main()
    sys.exit(0)
