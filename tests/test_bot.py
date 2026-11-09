"""בדיקות ללוגיקת הבוט — רצות בלי אינטרנט, עם ברוקר ו-AI מזויפים."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot  # noqa: E402

ET = timezone(timedelta(hours=-5))


def cfg(**over):
    c = dict(bot.DEFAULT_CONFIG)
    c.update(over)
    return bot.validate_config(c)


def state(**over):
    base = dict(now=datetime(2026, 11, 9, 11, 0, tzinfo=ET), minutes_since_open=90, minutes_to_close=300,
                equity=100_000, last_equity=100_000, cash=100_000)
    base.update(over)
    return bot.MarketState(**base)


class FakeAlpaca:
    def __init__(self, *, is_open=True, now="2026-11-09T11:00:00.123456789-05:00", close="2026-11-09T16:00:00-05:00",
                 equity=100_000, last_equity=100_000, positions=None, orders_today=None, news=None, prices=None,
                 reject=()):
        self.is_open, self.now, self.close = is_open, now, close
        self.equity, self.last_equity = equity, last_equity
        self._positions = positions or []
        self._orders_today = orders_today or []
        self._news = news or []
        self._prices = prices or {}
        self.reject = set(reject)
        self.submitted, self.closed, self.closed_all = [], [], 0

    def account(self):
        return {"status": "ACTIVE", "equity": str(self.equity), "last_equity": str(self.last_equity),
                "cash": str(self.equity), "account_number": "PA123", "trading_blocked": False}

    def clock(self):
        return {"is_open": self.is_open, "timestamp": self.now, "next_close": self.close,
                "next_open": "2026-11-10T09:30:00-05:00"}

    def calendar(self, day):
        return [{"date": day, "open": "09:30", "close": "16:00"}]

    def positions(self):
        return self._positions

    def orders(self, status, after=None, symbols=None, nested=True):
        return self._orders_today if status == "all" else []

    def news(self, symbols, start, max_pages=3):
        return self._news

    def latest_prices(self, symbols):
        return {s: p for s, p in self._prices.items() if s in symbols}

    def submit_bracket_buy(self, buy, cid):
        if buy.symbol in self.reject:
            raise bot.ApiError("rejected", 422)
        self.submitted.append((buy, cid))
        return {"id": f"ord-{buy.symbol}"}

    def close_position(self, symbol):
        self.closed.append(symbol)

    def close_all(self):
        self.closed_all += 1


class FakeAI:
    def __init__(self, signals=None, error=None):
        self.signals, self.error, self.calls = signals or [], error, 0
        self.used_model = "fake"

    def classify(self, news_by_symbol, now):
        self.calls += 1
        if self.error:
            raise self.error
        return [s for s in self.signals if s.symbol in news_by_symbol]


def news_item(sym, headline, minutes_ago=10, now=datetime(2026, 11, 9, 16, 0, tzinfo=timezone.utc)):
    t = now - timedelta(minutes=minutes_ago)
    return {"headline": headline, "summary": "", "symbols": [sym], "source": "benzinga",
            "created_at": t.strftime("%Y-%m-%dT%H:%M:%SZ")}


class ConfigTests(unittest.TestCase):
    def test_shipped_config_is_valid(self):
        c = bot.load_config(Path(bot.ROOT / "config.json"))
        self.assertEqual(c["max_total_capital_usd"], 1000)
        self.assertIn("AAPL", c["watchlist"])

    def test_rejects_out_of_range(self):
        with self.assertRaises(bot.ConfigError):
            cfg(stop_loss_pct=0)
        with self.assertRaises(bot.ConfigError):
            cfg(max_position_usd=5000, max_total_capital_usd=1000)
        with self.assertRaises(bot.ConfigError):
            cfg(dry_run="yes")
        with self.assertRaises(bot.ConfigError):
            cfg(watchlist=["AAPL", "not a ticker"])
        with self.assertRaises(bot.ConfigError):
            bot.validate_config({"unknown_key": 1})

    def test_ignores_comment_keys_and_normalizes_watchlist(self):
        c = bot.validate_config({"_הסבר": {"x": "y"}, "watchlist": ["aapl", "AAPL", "msft"]})
        self.assertEqual(c["watchlist"], ["AAPL", "MSFT"])


class ParsingTests(unittest.TestCase):
    def test_parse_ts_nanoseconds(self):
        t = bot.parse_ts("2026-11-09T10:15:30.123456789-05:00")
        self.assertEqual((t.hour, t.minute, t.microsecond), (10, 15, 123456))
        self.assertEqual(bot.parse_ts("2026-11-09T15:00:00Z").tzinfo, timezone.utc)

    def test_parse_signals_filters_and_clamps(self):
        text = json.dumps({"signals": [
            {"symbol": "aapl", "sentiment": 3, "confidence": 0.9, "reason_he": "תוצאות חזקות"},
            {"symbol": "EVIL", "sentiment": 1, "confidence": 1, "reason_he": "לא ברשימה"},
            {"symbol": "AAPL", "sentiment": -1, "confidence": 1, "reason_he": "כפילות"},
            {"symbol": "MSFT", "sentiment": "abc", "confidence": 1, "reason_he": "שבור"},
            {"symbol": "NVDA", "sentiment": -0.5, "confidence": -2, "reason_he": "x" * 500},
        ]})
        sigs = bot.parse_signals(text, {"AAPL", "MSFT", "NVDA"})
        self.assertEqual([s.symbol for s in sigs], ["AAPL", "NVDA"])
        self.assertEqual(sigs[0].sentiment, 1.0)
        self.assertEqual(sigs[1].confidence, 0.0)
        self.assertLessEqual(len(sigs[1].reason), 200)

    def test_parse_signals_handles_fences_and_garbage(self):
        fenced = '```json\n{"signals":[{"symbol":"AAPL","sentiment":0.7,"confidence":0.8,"reason_he":"ok"}]}\n```'
        self.assertEqual(len(bot.parse_signals(fenced, {"AAPL"})), 1)
        self.assertEqual(bot.parse_signals("I refuse", {"AAPL"}), [])
        self.assertEqual(bot.parse_signals("", {"AAPL"}), [])
        self.assertEqual(bot.parse_signals('{"signals": "nope"}', {"AAPL"}), [])

    def test_group_news(self):
        now = datetime(2026, 11, 9, 16, 0, tzinfo=timezone.utc)
        items = [news_item("AAPL", "A1"), news_item("AAPL", "A1"), news_item("AAPL", "old", 500),
                 news_item("XYZ", "not on list"), news_item("MSFT", "M1"), news_item("MSFT", "M2"),
                 news_item("MSFT", "M3"), {"headline": "broken"}]
        out = bot.group_news(items, ["AAPL", "MSFT"], now - timedelta(minutes=120), max_per_symbol=2)
        self.assertEqual([h["headline"] for h in out["AAPL"]], ["A1"])
        self.assertEqual(len(out["MSFT"]), 2)
        self.assertNotIn("XYZ", out)


class RiskTests(unittest.TestCase):
    def test_daily_loss_kill_switch(self):
        self.assertEqual(bot.pre_checks(state(equity=99_940), cfg())[0], "close_all")
        self.assertIsNone(bot.pre_checks(state(equity=99_960), cfg()))

    def test_flatten_before_close(self):
        self.assertEqual(bot.pre_checks(state(minutes_to_close=25), cfg())[0], "close_all")

    def test_emergency_switch(self):
        self.assertEqual(bot.pre_checks(state(), cfg(close_everything_now=True))[0], "close_all")

    def test_entry_time_windows_and_limits(self):
        sig = [bot.Signal("AAPL", 0.8, 0.9, "טוב")]
        self.assertEqual(bot.entry_candidates(state(minutes_since_open=5), sig, cfg())[0], [])
        self.assertEqual(bot.entry_candidates(state(minutes_to_close=45), sig, cfg())[0], [])
        self.assertEqual(bot.entry_candidates(state(bot_trades_today=6), sig, cfg())[0], [])
        self.assertEqual(bot.entry_candidates(state(), sig, cfg(new_trades_enabled=False))[0], [])
        self.assertEqual(len(bot.entry_candidates(state(), sig, cfg())[0]), 1)

    def test_entry_thresholds_and_duplicates(self):
        sigs = [bot.Signal("AAPL", 0.8, 0.9, ""), bot.Signal("MSFT", 0.5, 0.9, ""),
                bot.Signal("NVDA", 0.9, 0.6, ""), bot.Signal("AMD", 0.9, 0.95, ""),
                bot.Signal("META", 0.7, 0.8, "")]
        st = state(positions={"AMD": bot.Position("AMD", 1, 100, 0)}, bot_symbols_today={"META"})
        cands, _ = bot.entry_candidates(st, sigs, cfg())
        self.assertEqual([c.symbol for c in cands], ["AAPL"])

    def test_plan_buys_respects_caps(self):
        cands = [bot.Signal("AAPL", 0.9, 0.9, ""), bot.Signal("MSFT", 0.8, 0.9, ""),
                 bot.Signal("NVDA", 0.7, 0.9, "")]
        st = state(positions={"TSLA": bot.Position("TSLA", 2, 600, 0)})  # 600 מתוך 1000 כבר בשימוש
        buys, notes = bot.plan_buys(st, cands, {"AAPL": 100.0, "MSFT": 300.0, "NVDA": 50.0}, cfg())
        # AAPL: min(250, 400) -> 2 מניות (200$). נשאר 200 -> MSFT ב-300 לא נכנס. NVDA: 200 -> 4 מניות.
        self.assertEqual([(b.symbol, b.qty) for b in buys], [("AAPL", 2), ("NVDA", 4)])
        self.assertTrue(any("MSFT" in n for n in notes))
        total = 600 + sum(b.qty * b.price for b in buys)
        self.assertLessEqual(total, 1000)

    def test_plan_buys_prices(self):
        buys, _ = bot.plan_buys(state(), [bot.Signal("AAPL", 0.9, 0.9, "")], {"AAPL": 200.0}, cfg())
        b = buys[0]
        self.assertEqual((b.qty, b.take_profit, b.stop_loss), (1, 206.0, 197.0))

    def test_plan_buys_slots(self):
        cands = [bot.Signal(s, 0.9, 0.9, "") for s in ("AAPL", "MSFT", "NVDA")]
        st = state(positions={s: bot.Position(s, 1, 10, 0) for s in ("AMZN", "META", "TSLA")})
        buys, _ = bot.plan_buys(st, cands, {"AAPL": 10.0, "MSFT": 10.0, "NVDA": 10.0}, cfg())
        self.assertEqual(len(buys), 1)  # מקסימום 4 פוזיציות

    def test_exit_on_negative_news(self):
        st = state(positions={"AAPL": bot.Position("AAPL", 1, 200, 0)})
        sigs = [bot.Signal("AAPL", -0.6, 0.8, "תביעה"), bot.Signal("MSFT", -0.9, 0.9, "לא מחזיקים")]
        self.assertEqual([e[0] for e in bot.exits_from_signals(st, sigs, cfg())], ["AAPL"])
        weak = [bot.Signal("AAPL", -0.6, 0.4, "לא בטוח")]
        self.assertEqual(bot.exits_from_signals(st, weak, cfg()), [])


class RunTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.journal = bot.Journal(Path(self.tmp.name))

    def tearDown(self):
        self.tmp.cleanup()

    def test_market_closed_does_nothing(self):
        alp, ai = FakeAlpaca(is_open=False), FakeAI()
        self.assertEqual(bot.run_once(cfg(), alp, ai, self.journal), 0)
        self.assertEqual((ai.calls, alp.submitted), (0, []))

    def test_full_buy_flow(self):
        alp = FakeAlpaca(news=[news_item("AAPL", "Apple beats", 5), news_item("MSFT", "Meh", 5)],
                         prices={"AAPL": 200.0})
        ai = FakeAI([bot.Signal("AAPL", 0.8, 0.9, "תוצאות מעל הציפיות"), bot.Signal("MSFT", 0.1, 0.3, "")])
        self.assertEqual(bot.run_once(cfg(), alp, ai, self.journal), 0)
        self.assertEqual(len(alp.submitted), 1)
        buy, cid = alp.submitted[0]
        self.assertEqual((buy.symbol, buy.qty), ("AAPL", 1))
        self.assertTrue(cid.startswith("aibot-AAPL-"))
        files = list(Path(self.tmp.name).glob("*.jsonl"))
        self.assertEqual(len(files), 1)
        kinds = [json.loads(l)["kind"] for l in files[0].read_text(encoding="utf-8").splitlines()]
        self.assertIn("buy", kinds)

    def test_ai_failure_means_no_trades(self):
        alp = FakeAlpaca(news=[news_item("AAPL", "Apple beats", 5)], prices={"AAPL": 200.0})
        ai = FakeAI(error=bot.ApiError("boom", 500))
        self.assertEqual(bot.run_once(cfg(), alp, ai, self.journal), 0)
        self.assertEqual(alp.submitted, [])

    def test_ai_cannot_pick_symbol_outside_watchlist(self):
        alp = FakeAlpaca(news=[news_item("AAPL", "x", 5)], prices={"GME": 20.0, "AAPL": 200.0})
        ai = FakeAI([bot.Signal("GME", 1, 1, "to the moon")])
        bot.run_once(cfg(), alp, ai, self.journal)
        self.assertEqual(alp.submitted, [])

    def test_daily_loss_closes_everything_without_ai(self):
        alp = FakeAlpaca(equity=99_900, positions=[{"symbol": "AAPL", "qty": "1", "market_value": "200",
                                                     "unrealized_pl": "-100"}])
        ai = FakeAI()
        bot.run_once(cfg(), alp, ai, self.journal)
        self.assertEqual((alp.closed_all, ai.calls), (1, 0))

    def test_flatten_near_close(self):
        alp = FakeAlpaca(now="2026-11-09T15:40:00-05:00",
                         positions=[{"symbol": "AAPL", "qty": "1", "market_value": "200", "unrealized_pl": "1"}])
        bot.run_once(cfg(), alp, FakeAI(), self.journal)
        self.assertEqual(alp.closed_all, 1)

    def test_negative_news_exits_held_position(self):
        alp = FakeAlpaca(news=[news_item("AAPL", "Apple sued", 5)],
                         positions=[{"symbol": "AAPL", "qty": "1", "market_value": "200", "unrealized_pl": "1"}])
        bot.run_once(cfg(), alp, FakeAI([bot.Signal("AAPL", -0.8, 0.9, "תביעה")]), self.journal)
        self.assertEqual(alp.closed, ["AAPL"])
        self.assertEqual(alp.submitted, [])

    def test_already_traded_today_is_skipped(self):
        alp = FakeAlpaca(news=[news_item("AAPL", "Apple beats", 5)], prices={"AAPL": 200.0},
                         orders_today=[{"client_order_id": "aibot-AAPL-1", "side": "buy", "status": "filled",
                                        "symbol": "AAPL"}])
        bot.run_once(cfg(), alp, FakeAI([bot.Signal("AAPL", 0.9, 0.9, "")]), self.journal)
        self.assertEqual(alp.submitted, [])

    def test_rejected_order_does_not_crash(self):
        alp = FakeAlpaca(news=[news_item("AAPL", "a", 5), news_item("MSFT", "m", 5)],
                         prices={"AAPL": 200.0, "MSFT": 100.0}, reject={"AAPL"})
        ai = FakeAI([bot.Signal("AAPL", 0.9, 0.9, ""), bot.Signal("MSFT", 0.8, 0.9, "")])
        self.assertEqual(bot.run_once(cfg(), alp, ai, self.journal), 0)
        self.assertEqual([b.symbol for b, _ in alp.submitted], ["MSFT"])


class FakeResponse:
    def __init__(self, status, payload=None):
        self.status_code = status
        self._payload = payload
        self.content = b"" if payload is None else json.dumps(payload).encode()
        self.text = self.content.decode()

    def json(self):
        return self._payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.headers = {}

    def request(self, method, url, timeout=None, **kw):
        self.calls.append((method, url, kw))
        return self.responses.pop(0)


class HttpAndAlpacaTests(unittest.TestCase):
    def setUp(self):
        bot.time.sleep = lambda s: None  # בלי המתנות בבדיקות

    def alpaca(self, responses):
        a = bot.Alpaca("k", "s", bot.Journal(None))
        a.http.session = FakeSession(responses)
        return a

    def test_retries_on_server_error_then_succeeds(self):
        a = self.alpaca([FakeResponse(503, {"m": "busy"}), FakeResponse(200, {"is_open": True})])
        self.assertEqual(a.clock(), {"is_open": True})
        self.assertEqual(len(a.http.session.calls), 2)

    def test_client_error_raises_immediately(self):
        a = self.alpaca([FakeResponse(403, {"message": "forbidden"})])
        with self.assertRaises(bot.ApiError) as ctx:
            a.account()
        self.assertEqual(ctx.exception.status, 403)

    def test_only_paper_endpoint_is_used(self):
        a = self.alpaca([FakeResponse(200, {})])
        a.account()
        self.assertTrue(a.http.session.calls[0][1].startswith("https://paper-api.alpaca.markets/"))

    def test_bracket_order_body(self):
        a = self.alpaca([FakeResponse(200, {"id": "1"})])
        a.submit_bracket_buy(bot.Buy("AAPL", 3, 200.0, 206.0, 197.0, ""), "aibot-AAPL-1")
        method, url, kw = a.http.session.calls[0]
        body = kw["json"]
        self.assertEqual((method, body["order_class"], body["qty"]), ("POST", "bracket", "3"))
        self.assertEqual(body["take_profit"], {"limit_price": "206.00"})
        self.assertEqual(body["stop_loss"], {"stop_price": "197.00"})

    def test_close_position_cancels_legs_then_retries(self):
        a = self.alpaca([
            FakeResponse(200, [{"id": "tp"}, {"id": "sl"}]),       # הפקודות הפתוחות של המניה
            FakeResponse(204), FakeResponse(204),                   # ביטול שתיהן
            FakeResponse(403, {"message": "insufficient qty"}),     # עדיין "תפוסות"
            FakeResponse(200, {"id": "close"}),                     # ניסיון שני מצליח
        ])
        self.assertEqual(a.close_position("AAPL"), {"id": "close"})
        methods = [c[0] for c in a.http.session.calls]
        self.assertEqual(methods, ["GET", "DELETE", "DELETE", "DELETE", "DELETE"])

    def test_latest_prices_and_news_parsing(self):
        a = self.alpaca([
            FakeResponse(200, {"trades": {"AAPL": {"p": 201.5}, "BAD": {"x": 1}}}),
            FakeResponse(200, {"news": [{"id": 1}], "next_page_token": "t"}),
            FakeResponse(200, {"news": [{"id": 2}], "next_page_token": None}),
        ])
        self.assertEqual(a.latest_prices(["AAPL", "BAD"]), {"AAPL": 201.5})
        items = a.news(["AAPL"], datetime(2026, 11, 9, 14, 0, tzinfo=timezone.utc))
        self.assertEqual([i["id"] for i in items], [1, 2])
        params = a.http.session.calls[1][2]["params"]
        self.assertEqual(params["start"], "2026-11-09T14:00:00Z")

    def test_dry_run_sends_nothing(self):
        a = self.alpaca([])
        a.dry_run = True
        a.submit_bracket_buy(bot.Buy("AAPL", 1, 1.0, 2.0, 0.5, ""), "x")
        a.close_position("AAPL")
        a.close_all()
        self.assertEqual(a.http.session.calls, [])


class LoopAlpaca(FakeAlpaca):
    """שוק שהמצב שלו (פתוח/סגור) משתנה לפי רשימה, קריאה אחרי קריאה."""

    def __init__(self, opens, next_open="2026-11-10T09:30:00-05:00", **kw):
        super().__init__(**kw)
        self.opens, self.next_open, self.clock_calls = list(opens), next_open, 0

    def clock(self):
        self.is_open = self.opens[min(self.clock_calls, len(self.opens) - 1)]
        self.clock_calls += 1
        c = super().clock()
        c["next_open"] = self.next_open
        return c


class LoopTests(unittest.TestCase):
    def run_loop(self, alpaca, minutes, every=10):
        t = [0.0]
        sleeps, saves = [], []
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text(json.dumps(cfg()), encoding="utf-8")
            code = bot.run_loop(path, alpaca, FakeAI(), bot.Journal(None), None, minutes, every,
                                save=lambda: saves.append(t[0]),
                                sleep=lambda s: (sleeps.append(s), t.__setitem__(0, t[0] + s)),
                                clock=lambda: t[0])
        return code, sleeps, saves

    def test_stops_when_market_closes_for_the_day(self):
        # סבב 1: פתוח, סבב 2: נסגר. הפתיחה הבאה מחר — מסיימים.
        code, sleeps, saves = self.run_loop(LoopAlpaca([True, True, True, False]), minutes=330)
        self.assertEqual(code, 0)
        self.assertEqual(sleeps, [600])
        self.assertEqual(len(saves), 2)

    def test_stops_before_job_time_runs_out(self):
        code, sleeps, saves = self.run_loop(LoopAlpaca([True]), minutes=30)
        self.assertEqual(len(saves), 2)  # 0 ו-10 דקות. סבב שלישי לא היה מספיק להסתיים בזמן
        self.assertEqual(sleeps, [600])

    def test_waits_for_the_open_when_it_is_soon(self):
        alpaca = LoopAlpaca([False, False, True, True, True, False], next_open="2026-11-09T11:30:00-05:00")
        code, sleeps, saves = self.run_loop(alpaca, minutes=330)
        self.assertAlmostEqual(sleeps[0], 30 * 60 + 60, delta=1)  # מחכה לפתיחה (עוד חצי שעה) ולא יוצא
        self.assertEqual(sleeps[1], 600)  # ואז סוחר כרגיל כל 10 דקות
        self.assertEqual(code, 0)

    def test_bad_config_at_start_stops(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "config.json"
            path.write_text("{not json", encoding="utf-8")
            code = bot.run_loop(path, LoopAlpaca([True]), FakeAI(), bot.Journal(None), None, 60, 10,
                                sleep=lambda s: None, clock=lambda: 0.0)
        self.assertEqual(code, 2)


class GeminiTests(unittest.TestCase):
    def test_falls_back_when_model_missing(self):
        g = bot.Gemini("key", "gemini-does-not-exist")
        calls = []

        def fake_call(model, text, with_schema):
            calls.append((model, with_schema))
            if model == "gemini-does-not-exist":
                raise bot.ApiError("not found", 404)
            if with_schema:
                raise bot.ApiError("bad schema", 400)
            return '{"signals":[{"symbol":"AAPL","sentiment":0.5,"confidence":0.5,"reason_he":"ok"}]}'

        g._call = fake_call
        sigs = g.classify({"AAPL": []}, datetime.now(timezone.utc))
        self.assertEqual(len(sigs), 1)
        self.assertEqual(g.used_model, bot.FALLBACK_MODELS[0])
        self.assertEqual(calls[0], ("gemini-does-not-exist", True))

    def test_falls_back_when_model_busy(self):
        g = bot.Gemini("key", "busy-model")
        calls = []

        def fake_call(model, text, with_schema):
            calls.append(model)
            if model == "busy-model":
                raise bot.ApiError("high demand", 503)
            return '{"signals":[{"symbol":"AAPL","sentiment":0.5,"confidence":0.5,"reason_he":"ok"}]}'

        g._call = fake_call
        self.assertEqual(len(g.classify({"AAPL": []}, datetime.now(timezone.utc))), 1)
        self.assertEqual(calls, ["busy-model", bot.FALLBACK_MODELS[0]])

    def test_bad_key_is_not_retried_on_other_models(self):
        g = bot.Gemini("key", "m")
        calls = []

        def fake_call(model, text, with_schema):
            calls.append(model)
            raise bot.ApiError("API key not valid", 403)

        g._call = fake_call
        with self.assertRaises(bot.ApiError):
            g.classify({"AAPL": []}, datetime.now(timezone.utc))
        self.assertEqual(calls, ["m"])
        self.assertTrue(bot.is_busy(bot.ApiError("x", 503)))
        self.assertFalse(bot.is_busy(bot.ApiError("x", 403)))

    def test_request_body_shape(self):
        g = bot.Gemini("key", "m")
        captured = {}

        def fake_request(method, url, **kw):
            captured.update(method=method, url=url, body=kw["json"])
            return {"candidates": [{"content": {"parts": [{"text": "thinking...", "thought": True},
                                                          {"text": '{"signals": []}'}]}}]}

        g.http.request = fake_request
        self.assertEqual(g._call("m", "hello", True), '{"signals": []}')
        self.assertIn("/models/m:generateContent", captured["url"])
        self.assertEqual(captured["body"]["generationConfig"]["responseMimeType"], "application/json")
        self.assertIn("untrusted", captured["body"]["systemInstruction"]["parts"][0]["text"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
