"""Evidence backfill: the local point-in-time source obeys the same cut-off
contract as the public API, merges the research files without look-ahead,
and the A/B pieces read nothing from after the observation."""

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from crypto_trading.evidence_shadow import local_data as ld
from crypto_trading.evidence_shadow import service as sv

T = int(datetime(2026, 9, 20, 12, 0, tzinfo=UTC).timestamp())


def _files(tmp_path: Path) -> ld.LocalSource:
    a = sqlite3.connect(tmp_path / "archive.db")
    a.executescript(
        "CREATE TABLE klines5m (symbol TEXT, t INTEGER, o REAL, h REAL, l REAL, c REAL,"
        " v REAL, qv REAL, n INTEGER, tbv REAL, PRIMARY KEY (symbol, t));"
        "CREATE TABLE funding (symbol TEXT, t INTEGER, rate REAL, PRIMARY KEY (symbol, t));"
        "CREATE TABLE metrics (symbol TEXT, t INTEGER, oi REAL, oi_value REAL, top_ls_acct REAL,"
        " top_ls_pos REAL, ls_acct REAL, taker_ls_vol REAL, PRIMARY KEY (symbol, t));"
    )
    for t in (T - 900, T - 600, T - 300, T):  # the bar opening at T closes after T
        a.execute("INSERT INTO klines5m VALUES ('XUSDT',?,1,2,0.5,1.5,10,0,0,0)", (t * 1000,))
    a.execute("INSERT INTO funding VALUES ('XUSDT', ?, 0.0001)", ((T - 3600) * 1000 + 1,))
    a.execute("INSERT INTO funding VALUES ('XUSDT', ?, 0.9)", ((T + 3600) * 1000 + 1,))
    a.execute("INSERT INTO metrics VALUES ('XUSDT', ?, 0, 5, 0, 0, 0, 0)", ((T - 300) * 1000,))
    a.execute("INSERT INTO metrics VALUES ('XUSDT', ?, 0, 999, 0, 0, 0, 0)", ((T + 300) * 1000,))
    a.commit()
    d = sqlite3.connect(tmp_path / "derivs.db")
    d.executescript(
        "CREATE TABLE funding (symbol TEXT, t INTEGER, rate REAL);"
        "CREATE TABLE oi (symbol TEXT, t INTEGER, oi_value REAL);"
    )
    d.execute("INSERT INTO funding VALUES ('XUSDT', ?, 0.0002)", (T - 60,))
    d.execute("INSERT INTO funding VALUES ('XUSDT', ?, 0.8)", (T + 60,))
    d.execute("INSERT INTO oi VALUES ('XUSDT', ?, 6)", (T,))
    d.execute("INSERT INTO oi VALUES ('XUSDT', ?, 888)", (T + 1,))
    d.commit()
    g = ld.open_gap(str(tmp_path / "gap.db"))
    g.execute("INSERT INTO klines5m VALUES ('XUSDT', ?, 7, 7, 7, 7, 7)", (T + 300,))
    g.commit()
    return ld.LocalSource(
        str(tmp_path / "archive.db"), str(tmp_path / "derivs.db"), str(tmp_path / "gap.db")
    )


def test_local_source_returns_nothing_known_only_after_the_cutoff(tmp_path):
    src = _files(tmp_path)
    bars = src.klines_5m("XUSDT", T - 3600, T)
    assert [b[0] for b in bars] == [T - 900, T - 600, T - 300]  # closed <= T only
    assert src.funding("XUSDT", T - 7200, T) == [(T - 3600, 0.0001), (T - 60, 0.0002)]
    assert src.open_interest("XUSDT", T - 3600, T) == [(T - 300, 5.0), (T, 6.0)]
    d = src.fetch_symbol("XUSDT", T)
    assert max(b[0] + 300 for b in d.bars) <= T
    assert max(t for t, _ in d.funding) <= T and max(t for t, _ in d.oi) <= T


def test_local_source_matches_the_api_fetchers_on_the_same_rows(tmp_path):
    """Same rows in, same answer out: the API path over identical data."""
    src = _files(tmp_path)

    def get(url):
        if "klines" in url:
            return [
                [t * 1000, "1", "2", "0.5", "1.5", "10"] for t in (T - 900, T - 600, T - 300, T)
            ]
        if "fundingRate" in url:
            return [
                {"fundingTime": (T - 3600) * 1000, "fundingRate": "0.0001"},
                {"fundingTime": (T - 60) * 1000, "fundingRate": "0.0002"},
                {"fundingTime": (T + 60) * 1000, "fundingRate": "0.8"},
            ]
        return [
            {"timestamp": (T - 300) * 1000, "sumOpenInterestValue": "5"},
            {"timestamp": T * 1000, "sumOpenInterestValue": "6"},
            {"timestamp": (T + 300) * 1000, "sumOpenInterestValue": "999"},
        ]

    from crypto_trading.evidence_shadow import binance_data as bd

    assert src.klines_5m("XUSDT", T - 3600, T) == bd.klines_5m("XUSDT", T - 3600, T, get)
    assert src.funding("XUSDT", T - 7200, T) == bd.funding("XUSDT", T - 7200, T, get)
    assert src.open_interest("XUSDT", T - 3600, T) == bd.open_interest("XUSDT", T - 3600, T, get)


def test_unknown_symbol_falls_back_to_the_api(tmp_path, monkeypatch):
    src = _files(tmp_path)
    called = []
    monkeypatch.setattr(sv.cl, "fetch_symbol", lambda s, c, h=0: called.append(s) or "api")
    assert src.fetch_symbol("NOTONDISK", T) == "api" and called == ["NOTONDISK"]
    assert src.api_fallbacks == 1


def test_classifier_uses_the_injected_source(tmp_path):
    src = _files(tmp_path)
    clf = sv.Classifier(["XUSDT"], src)
    d = clf._data("XUSDT", T)
    assert d.bars and max(b[0] + 300 for b in d.bars) <= T


def test_prepare_ab_reads_only_the_observation_and_earlier_evidence(tmp_path):
    """The A/B context is built from the observation row itself; evidence is
    looked up with the observation time, and a classification made after
    the observation is not used."""
    sh = sv.open_shadow_db(str(tmp_path / "sh.db"))
    obs_at = datetime.fromtimestamp(T, UTC)
    sh.execute(
        "INSERT INTO candidate_evidence (candidate_id, side, decision_time, signal_types, regimes)"
        " VALUES ('c1', 'LONG', ?, '[\"NO_EVENT\"]', '{}')",
        (datetime.fromtimestamp(T + 60, UTC).isoformat(),),  # decided AFTER the observation
    )

    class Pos:
        candidate_id, instrument = "c1", "X-USDT"

    class Repo:
        def get_position(self, _):
            return Pos()

        def get_candidate(self, _):
            return None

    class Reader:
        def lookup(self, *a):
            raise AssertionError("must not look up evidence decided after the observation")

    o = {
        "position_id": "p1",
        "observed_at": obs_at.isoformat(),
        "state": "WATCH",
        "factors": "{}",
        "decay_score": "0.5",
        "progress_ratio": "0.1",
        "unrealized_pnl": "1",
    }
    p = sv.prepare_ab(sh, Repo(), Reader(), o)
    assert p["ev_ctx"] is None and p["base"]["new_state"] == "WATCH"
    assert "historical_evidence" not in p["base"]
