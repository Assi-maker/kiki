"""Polymarket liquidity-rewards PAPER test.

Virtual two-sided maker quotes on reward-eligible markets. No wallet, no
orders: every minute the public order books are read, our virtual quotes are
scored with Polymarket's own reward formula against the competitors in the
book, and fills are simulated from the public trade tape. Measures one thing:
daily rewards minus losses from being filled (adverse selection).

Pre-registration: docs/superpowers/reports/2026-10-02-polymarket-rewards-paper-test.md (v1),
docs/superpowers/reports/2026-10-19-polymarket-rewards-paper-test-v2.md (v2, withdrawn),
docs/superpowers/reports/2026-10-19-polymarket-rewards-paper-test-v3-final.md (v3, this version)
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

CLOB = "https://clob.polymarket.com"
DATA = "https://data-api.polymarket.com"
HEADERS = {"User-Agent": "Mozilla/5.0", "Content-Type": "application/json"}
BUDGET = 100.0
TICK_SECONDS = 60
GAP_SECONDS = 180          # longer gap -> virtual orders count as cancelled
RESELECT_SECONDS = 3600
C_SINGLE = 3.0
SHORTLIST = 40             # per variant, markets whose competitiveness is fetched each hour
# name, distance from mid in cents, minimum hours to market end
VARIANTS = [
    ("d2_all", 2.0, 0),    # PRIMARY
    ("d1_all", 1.0, 0),
    ("d3_all", 3.0, 0),
    ("d1_long", 1.0, 48),
    ("d2_long", 2.0, 48),
    ("d3_long", 3.0, 48),
]
EPS = 1e-9

log = logging.getLogger("polymarket_paper")


# ---------------------------------------------------------------- formulas

def score(v: float, s_cents: float) -> float:
    """Polymarket S(v, s) = ((v - s) / v)^2, spreads in cents."""
    if s_cents < 0 or s_cents >= v:
        return 0.0
    return ((v - s_cents) / v) ** 2


def q_min(q1: float, q2: float, mid: float) -> float:
    if 0.10 <= mid <= 0.90:
        return max(min(q1, q2), max(q1, q2) / C_SINGLE)
    return min(q1, q2)


def _cutoff_price(levels, min_size, best_first_key):
    cum = 0.0
    for p, sz in sorted(levels, key=best_first_key):
        cum += sz
        if cum >= min_size:
            return p
    return None


def adjusted_mid(bids, asks, min_size):
    """Size-cutoff midpoint: best prices once cumulative size reaches min_size."""
    b = _cutoff_price(bids, min_size, lambda x: -x[0])
    a = _cutoff_price(asks, min_size, lambda x: x[0])
    if b is None or a is None or a <= b:
        return None
    return (a + b) / 2


def competitor_q(bids, asks, mid, v):
    """Upper bound on the summed Q_min of all makers already in the book.

    Per maker Q_min <= (q_one + q_two) / 2, so the sum over makers is bounded by
    half the book's two-sided total. Overstating competitors makes our share
    conservative. Level sizes are aggregated across orders, so no per-order
    min-size filter is applied (also conservative).
    """
    q1 = sum(sz * score(v, abs(mid - p) * 100) for p, sz in bids)
    q2 = sum(sz * score(v, abs(p - mid) * 100) for p, sz in asks)
    return (q1 + q2) / 2


def honest_mid(mid, bb, ba, ltp, v):
    """True when the size-cutoff mid agrees with the visible book and the last trade.

    v1 lesson: with dust below min_size, the adjusted mid can sit in an empty gap far
    from where the market trades; quotes there scored alone and took the whole pool.
    A visible spread wider than twice the reward band means there is no price to quote around.
    """
    if (ba - bb) * 100 > 2 * v or abs(mid - (bb + ba) / 2) * 100 > v / 2:
        return False
    return ltp is None or abs(mid - ltp) * 100 <= v


def quote_prices(mid, d_cents, tick, best_bid, best_ask):
    pb = math.floor((mid - d_cents / 100) / tick + EPS) * tick
    pa = math.ceil((mid + d_cents / 100) / tick - EPS) * tick
    pb = min(pb, best_ask - tick)      # stay a maker
    pa = max(pa, best_bid + tick)
    return round(pb, 6), round(pa, 6)


def our_qmin(size, pb, pa, mid, v, bid_on, ask_on):
    q1 = size * score(v, (mid - pb) * 100) if bid_on else 0.0
    q2 = size * score(v, (pa - mid) * 100) if ask_on else 0.0
    return q_min(q1, q2, mid)


def share(ours, comp):
    if ours <= 0:
        return 0.0
    return ours / (ours + comp)


def _yes_prices(trades):
    for t in trades:
        p = float(t["price"])
        yield p if int(t["outcomeIndex"]) == 0 else 1.0 - p


def bid_filled(pb, trades, best_ask, last_trade=None):
    """Filled if a trade printed at/through our bid, the book moved through it,
    or the last traded price sits at/below it (pessimistic: in a thin book an
    informed seller would hit a bid the market already values lower)."""
    if any(p <= pb + EPS for p in _yes_prices(trades)):
        return "tape"
    if best_ask is not None and best_ask <= pb + EPS:
        return "book"
    if last_trade is not None and last_trade <= pb + EPS:
        return "ltp"
    return ""


def ask_filled(pa, trades, best_bid, last_trade=None):
    if any(p >= pa - EPS for p in _yes_prices(trades)):
        return "tape"
    if best_bid is not None and best_bid >= pa - EPS:
        return "book"
    if last_trade is not None and last_trade >= pa - EPS:
        return "ltp"
    return ""


@dataclass
class Position:
    cid: str
    size: float
    yes: float = 0.0
    no: float = 0.0
    yes_cost: float = 0.0
    no_cost: float = 0.0
    realized: float = 0.0

    def fill_bid(self, pb):
        self.yes += self.size
        self.yes_cost += self.size * pb
        self._merge()

    def fill_ask(self, pa):
        # an ask on Yes without holding Yes is a bid on No at 1 - pa
        self.no += self.size
        self.no_cost += self.size * (1.0 - pa)
        self._merge()

    def _merge(self):
        k = min(self.yes, self.no)
        if k <= 0:
            return
        ay, an = self.yes_cost / self.yes, self.no_cost / self.no
        self.realized += k * (1.0 - ay - an)
        self.yes_cost -= k * ay
        self.no_cost -= k * an
        self.yes -= k
        self.no -= k

    def may_bid(self):
        return self.yes - self.no < self.size - EPS

    def may_ask(self):
        return self.no - self.yes < self.size - EPS

    def inventory_cost(self):
        return self.yes_cost + self.no_cost

    def value(self, yes_price):
        return (self.realized + self.yes * yes_price - self.yes_cost
                + self.no * (1.0 - yes_price) - self.no_cost)


def capital(n, pb, pa):
    return n * (pb + 1.0 - pa)


def rank(cands, d, now, min_hours):
    """(expected reward per dollar of capital, capital, candidate), best first."""
    ranked = []
    for c in cands:
        if c["rate"] <= 0:
            continue
        if c["end"] is not None and c["end"] - now < min_hours * 3600:
            continue
        if not 0.10 <= c["mid"] <= 0.90 or not c.get("honest", True):
            continue
        pb, pa = quote_prices(c["mid"], d, c["tick"], c["bb"], c["ba"])
        ltp = c.get("ltp")
        if ltp is not None and not pb < ltp < pa:
            continue                      # the market already trades outside our quotes
        s = min(score(c["v"], (c["mid"] - pb) * 100), score(c["v"], (pa - c["mid"]) * 100))
        if s <= 0:
            continue
        q = c["n"] * s
        cap = capital(c["n"], pb, pa)
        ranked.append((c["rate"] * q / (q + c["comp"]) / cap, cap, c))
    ranked.sort(key=lambda x: -x[0])
    return ranked


def shortlist(cands, params, now, k):
    """Union of every variant's top k, the markets worth asking Polymarket about."""
    return {c["cid"] for d, min_hours in params for _, _, c in rank(cands, d, now, min_hours)[:k]}


def select(cands, d, budget, now, min_hours):
    """Greedy pick by expected reward per dollar of capital."""
    picked, left = [], budget
    for _, cap, c in rank(cands, d, now, min_hours):
        if cap <= left:
            picked.append(c)
            left -= cap
    return picked


# ---------------------------------------------------------------- network

def _get(url, timeout=30):
    req = urllib.request.Request(url, headers=HEADERS)
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())


def _post(url, body, timeout=60):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=HEADERS)
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode())


def _retry(fn, *a, tries=3):
    for i in range(tries):
        try:
            return fn(*a)
        except Exception as e:  # noqa: BLE001 - network, logged and retried
            if i == tries - 1:
                raise
            log.warning("retry %s: %s", getattr(fn, "__name__", fn), e)
            time.sleep(2 * (i + 1))


def fetch_universe():
    out, cur = [], ""
    while True:
        d = _retry(_get, f"{CLOB}/sampling-markets?next_cursor={cur}")
        out += d["data"]
        cur = d.get("next_cursor")
        if not cur or cur == "LTE=" or not d["data"]:
            break
    uni = {}
    for m in out:
        r = m.get("rewards") or {}
        rate = sum(x["rewards_daily_rate"] for x in r.get("rates") or [])
        if (rate <= 0 or not m.get("active") or m.get("closed") or not m.get("accepting_orders")
                or len(m.get("tokens") or []) != 2 or not r.get("max_spread")):
            continue
        end = m.get("end_date_iso")
        uni[m["condition_id"]] = dict(
            cid=m["condition_id"], question=m["question"], yes=m["tokens"][0]["token_id"],
            rate=float(rate), v=float(r["max_spread"]),
            n=float(max(r.get("min_size") or 0, m.get("minimum_order_size") or 5, 5)),
            tick=float(m["minimum_tick_size"]),
            end=datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp() if end else None)
    return uni


def fetch_books(token_ids):
    """token_id -> (bids, asks) with (price, size) floats. Missing on error."""
    books = {}
    chunks = [token_ids[i:i + 400] for i in range(0, len(token_ids), 400)]

    def one(chunk):
        try:
            return _retry(_post, f"{CLOB}/books", [{"token_id": t} for t in chunk])
        except Exception as e:  # noqa: BLE001
            log.warning("books chunk failed: %s", e)
            return []

    with ThreadPoolExecutor(8) as ex:
        for res in ex.map(one, chunks):
            for b in res or []:
                ltp = b.get("last_trade_price")
                books[b["asset_id"]] = (
                    [(float(o["price"]), float(o["size"])) for o in b.get("bids") or []],
                    [(float(o["price"]), float(o["size"])) for o in b.get("asks") or []],
                    float(ltp) if ltp not in (None, "", "0", 0) else None)
    return books


def confirmed(m, mc, rate_pm):
    """Market metadata after the hourly check against /rewards/markets: competitors floored at
    Polymarket's competitiveness, rate the lower of both Polymarket sources."""
    return dict(m, mc=mc, rate=min(m["rate"], rate_pm))


def fetch_competitiveness(cids):
    """cid -> (market_competitiveness, rate per day) from /rewards/markets; failed cids are missing.
    A market no longer listed there comes back with rate 0."""
    def one(cid):
        try:
            d = _retry(_get, f"{CLOB}/rewards/markets/{cid}")["data"]
            if not d:
                return cid, (0.0, 0.0)
            rate = sum(float(x.get("rate_per_day") or 0) for x in d[0].get("rewards_config") or [])
            return cid, (float(d[0].get("market_competitiveness") or 0.0), rate)
        except Exception as e:  # noqa: BLE001
            log.warning("competitiveness %s failed: %s", cid[:10], e)
            return cid, None

    with ThreadPoolExecutor(8) as ex:
        return {cid: mc for cid, mc in ex.map(one, cids) if mc is not None}


def fetch_trades(cids, since, until):
    """cid -> trades in (since, until]; cids missing from the result failed."""
    def one(cid):
        try:
            t = _retry(_get, f"{DATA}/trades?market={cid}&limit=500")
            return cid, [x for x in t if since < int(x["timestamp"]) <= until]
        except Exception as e:  # noqa: BLE001
            log.warning("trades %s failed: %s", cid[:10], e)
            return cid, None

    out = {}
    with ThreadPoolExecutor(8) as ex:
        for cid, t in ex.map(one, cids):
            if t is not None:
                out[cid] = t
    return out


# ---------------------------------------------------------------- simulation

SCHEMA = """
CREATE TABLE IF NOT EXISTS markets(cid TEXT PRIMARY KEY, question TEXT, yes TEXT, end_ts REAL,
    rate REAL, v REAL, n REAL, tick REAL);
CREATE TABLE IF NOT EXISTS samples(variant TEXT, ts INTEGER, cid TEXT, mid REAL, pb REAL, pa REAL,
    bid_on INTEGER, ask_on INTEGER, our_q REAL, comp REAL, share REAL, rate REAL, reward REAL,
    ltp REAL);
CREATE TABLE IF NOT EXISTS fills(variant TEXT, ts INTEGER, cid TEXT, side TEXT, price REAL,
    size REAL, mid REAL, reason TEXT);
CREATE TABLE IF NOT EXISTS ticks(ts INTEGER, gap INTEGER, n_books INTEGER, trades_missing INTEGER,
    seconds REAL, max_capital REAL, max_net_inv_ratio REAL);
CREATE TABLE IF NOT EXISTS state(variant TEXT PRIMARY KEY, json TEXT);
CREATE INDEX IF NOT EXISTS samples_v_ts ON samples(variant, ts);
"""


class Variant:
    def __init__(self, name, d, min_hours, budget=BUDGET):
        self.name, self.d, self.min_hours, self.budget = name, d, min_hours, budget
        self.positions: dict[str, Position] = {}
        self.orders: dict[str, tuple] = {}      # cid -> (pb, pa, bid_on, ask_on)
        self.selected: list[str] = []

    def to_json(self):
        return json.dumps(dict(positions={k: asdict(p) for k, p in self.positions.items()},
                               orders=self.orders, selected=self.selected))

    def load(self, s):
        d = json.loads(s)
        self.positions = {k: Position(**p) for k, p in d["positions"].items()}
        self.orders = {k: tuple(v) for k, v in d["orders"].items()}
        self.selected = d["selected"]

    def held(self):
        return [c for c, p in self.positions.items() if p.yes > EPS or p.no > EPS]

    def inventory_cost(self):
        return sum(p.inventory_cost() for p in self.positions.values())


def market_view(m, book):
    if book is None:
        return None
    bids, asks, ltp = book
    if not bids or not asks:
        return None
    mid = adjusted_mid(bids, asks, m["n"])
    if mid is None:
        return None
    bb, ba = max(p for p, _ in bids), min(p for p, _ in asks)
    # competitors: our book bound, never below Polymarket's own competitiveness figure
    return dict(mid=mid, bb=bb, ba=ba, ltp=ltp, honest=honest_mid(mid, bb, ba, ltp, m["v"]),
                comp=max(competitor_q(bids, asks, mid, m["v"]), m.get("mc", 0.0)))


def step_variant(var, uni, views, trades, now, gap, db, missing=frozenset()):
    # 1) fills of the orders that rested since the previous tick
    if not gap:
        for cid, (pb, pa, bon, aon) in var.orders.items():
            view = views.get(cid)
            tr = trades.get(cid, [])
            pos = var.positions.setdefault(cid, Position(cid=cid, size=uni[cid]["n"] if cid in uni else 0))
            mid = view["mid"] if view else None
            ltp = view["ltp"] if view else None
            why = bon and bid_filled(pb, tr, view["ba"] if view else None, ltp)
            if why:
                pos.fill_bid(pb)
                db.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?,?)",
                           (var.name, now, cid, "bid", pb, pos.size, mid, why))
            why = aon and ask_filled(pa, tr, view["bb"] if view else None, ltp)
            if why:
                pos.fill_ask(pa)
                db.execute("INSERT INTO fills VALUES(?,?,?,?,?,?,?,?)",
                           (var.name, now, cid, "ask", pa, pos.size, mid, why))

    # 2) new quotes: selected markets first, then unwinding of held inventory
    var.orders = {}
    used = var.inventory_cost()
    for cid in list(dict.fromkeys(var.selected + var.held())):
        m, view = uni.get(cid), views.get(cid)
        if m is None or view is None:
            continue
        pos = var.positions.setdefault(cid, Position(cid=cid, size=m["n"]))
        pb, pa = quote_prices(view["mid"], var.d, m["tick"], view["bb"], view["ba"])
        if not (0 < pb < pa < 1):
            continue
        sel = cid in var.selected
        ltp = view["ltp"]
        bon = pos.may_bid() and (sel or pos.no > pos.yes + EPS) and (ltp is None or ltp > pb + EPS)
        aon = pos.may_ask() and (sel or pos.yes > pos.no + EPS) and (ltp is None or ltp < pa - EPS)
        need = (pos.size * pb if bon else 0.0) + (pos.size * (1 - pa) if aon else 0.0)
        if used + need > var.budget + EPS:
            continue
        used += need
        var.orders[cid] = (pb, pa, bon, aon)
        ours = our_qmin(pos.size, pb, pa, view["mid"], m["v"], bon, aon)
        sh = share(ours, view["comp"])
        # no reward while the mid is not honest, or when this minute's fills could not be checked
        earns = view.get("honest", True) and cid not in missing
        reward = m["rate"] * sh / 1440 if earns else 0.0
        db.execute("INSERT INTO samples VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                   (var.name, now, cid, view["mid"], pb, pa, int(bon), int(aon), ours, view["comp"],
                    sh, m["rate"], reward, view["ltp"]))
    net_ratio = max((abs(p.yes - p.no) / p.size for p in var.positions.values() if p.size), default=0.0)
    return used, net_ratio


def merge_universe(old, fresh, held):
    """Fresh reward list plus held markets that left it, kept at rate 0 so they can be
    unwound without earning (v1 credited them at their last, stale rate). The last known
    competitiveness is carried over until it is fetched again."""
    kept = {c: dict(m, rate=0.0) for c, m in old.items() if c in held and c not in fresh}
    fresh = {c: dict(m, mc=old[c]["mc"]) if "mc" in old.get(c, {}) else m for c, m in fresh.items()}
    return {**kept, **fresh}


def finish_if_done(db, out_path, now, resolve=True):
    """After the last valid day has ended, evaluate once and freeze the verdict in out_path."""
    from crypto_trading.polymarket import evaluate as E
    if Path(out_path).exists():
        return None
    days, _ = E.valid_days(db)
    if len(days) < E.N_DAYS or E.day_of(now) <= days[-1]:
        return None
    res = dict(E.evaluate(db, resolve=resolve), evaluated_at=now)
    Path(out_path).write_text(json.dumps(res, indent=1, ensure_ascii=False), encoding="utf-8")
    return res


def run(db_path: Path, stop_at: float | None = None):
    result_path = db_path.with_name(db_path.stem + "_result.json")
    if result_path.exists():                 # a restart after the end must not extend the test
        log.info("test already complete: %s", result_path)
        return
    db = sqlite3.connect(db_path)
    db.executescript(SCHEMA)
    variants = [Variant(*v) for v in VARIANTS]
    for v in variants:
        row = db.execute("SELECT json FROM state WHERE variant=?", (v.name,)).fetchone()
        if row:
            v.load(row[0])
    uni, last_select, prev = {}, 0.0, None
    while stop_at is None or time.time() < stop_at:
        t0 = time.time()
        now = int(t0)
        try:
            reselect = t0 - last_select >= RESELECT_SECONDS or not uni
            if reselect:
                fresh = fetch_universe()
                uni = merge_universe(uni, fresh, {c for v in variants for c in v.held()})
                db.executemany("INSERT OR REPLACE INTO markets VALUES(?,?,?,?,?,?,?,?)",
                               [(m["cid"], m["question"], m["yes"], m["end"], m["rate"], m["v"], m["n"], m["tick"])
                                for m in fresh.values()])
                need = list(uni)
            else:
                need = list({c for v in variants for c in v.selected + v.held()})
            books = fetch_books([uni[c]["yes"] for c in need if c in uni])
            views = {}
            for c in need:
                if c in uni:
                    vw = market_view(uni[c], books.get(uni[c]["yes"]))
                    if vw:
                        views[c] = vw
            if reselect:
                result = finish_if_done(db, result_path, now)
                if result:
                    log.info("test complete, verdict %s %s", result["verdict"], result["passing_variants"])
                    return
                cands = [dict(uni[c], **views[c]) for c in views if c in fresh]
                short = shortlist(cands, [(v.d, v.min_hours) for v in variants], now, SHORTLIST)
                mcs = fetch_competitiveness(sorted(short))
                for c, (mc, rate_pm) in mcs.items():
                    uni[c] = confirmed(uni[c], mc, rate_pm)
                    vw = market_view(uni[c], books.get(uni[c]["yes"]))
                    if vw:
                        views[c] = vw
                # only markets whose competitiveness Polymarket confirmed this hour can be selected
                cands = [dict(uni[c], **views[c]) for c in mcs if c in views]
                for v in variants:
                    v.selected = [c["cid"] for c in select(cands, v.d, v.budget - v.inventory_cost(), now, v.min_hours)]
                last_select = t0
            gap = prev is None or now - prev > GAP_SECONDS
            resting = list({c for v in variants for c in v.orders})
            trades = fetch_trades(resting, prev, now) if (resting and not gap) else {}
            unread = set() if gap else set(resting) - set(trades)
            missing = len(unread)
            max_cap, max_ratio = 0.0, 0.0
            for v in variants:
                cap, ratio = step_variant(v, uni, views, trades, now, gap, db, unread)
                max_cap, max_ratio = max(max_cap, cap), max(max_ratio, ratio)
                db.execute("INSERT OR REPLACE INTO state VALUES(?,?)", (v.name, v.to_json()))
            db.execute("INSERT INTO ticks VALUES(?,?,?,?,?,?,?)",
                       (now, int(gap), len(views), missing, time.time() - t0, max_cap, max_ratio))
            db.commit()
            prev = now
            log.info("tick %s views=%d resting=%d missing=%d %.1fs sel=%s", now, len(views), len(resting),
                     missing, time.time() - t0, {v.name: len(v.selected) for v in variants})
        except Exception:  # noqa: BLE001 - one bad tick must not stop a 7-day run
            log.exception("tick failed")
            db.rollback()
        time.sleep(max(1.0, TICK_SECONDS - (time.time() - t0)))


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default="data/polymarket_paper/paper_v3.db")
    ap.add_argument("--minutes", type=float, default=None)
    a = ap.parse_args()
    Path(a.db).parent.mkdir(parents=True, exist_ok=True)
    Path("logs").mkdir(exist_ok=True)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                        handlers=[logging.FileHandler("logs/polymarket_paper.log"), logging.StreamHandler()])
    from crypto_trading.single_instance import InstanceLock, lock_path_for
    lock = InstanceLock(lock_path_for(Path(a.db)))
    lock.acquire()                     # never two collectors on one database
    run(Path(a.db), time.time() + a.minutes * 60 if a.minutes else None)


if __name__ == "__main__":
    main()
