"""בדיקות לדף המצב ולהתראות."""
import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bot  # noqa: E402
import reporting  # noqa: E402

ET = timezone(timedelta(hours=-5))

BUY = {"id": "b1", "symbol": "AAPL", "side": "buy", "status": "filled", "type": "market",
       "filled_qty": "2", "filled_avg_price": "200.00", "filled_at": "2026-11-09T15:00:00Z"}
TP = {"id": "s1", "symbol": "AAPL", "side": "sell", "status": "filled", "type": "limit",
      "filled_qty": "2", "filled_avg_price": "206.00", "filled_at": "2026-11-09T17:00:00Z"}
SL = {"id": "s2", "symbol": "MSFT", "side": "sell", "status": "filled", "type": "stop",
      "filled_qty": "1", "filled_avg_price": "394.00", "filled_at": "2026-11-09T16:00:00Z"}
BUY2 = {"id": "b2", "symbol": "MSFT", "side": "buy", "status": "filled", "type": "market",
        "filled_qty": "1", "filled_avg_price": "400.00", "filled_at": "2026-11-09T15:30:00Z"}


class PureTests(unittest.TestCase):
    def test_buy_prices_and_pnl(self):
        entry = reporting.buy_prices([BUY, BUY2, TP])
        self.assertEqual(entry, {"AAPL": 200.0, "MSFT": 400.0})
        self.assertEqual(reporting.exit_pnl(TP, entry), 12.0)
        self.assertEqual(reporting.exit_pnl(SL, entry), -6.0)

    def test_describe_exit_kinds(self):
        entry = reporting.buy_prices([BUY, BUY2])
        self.assertIn("טייק-פרופיט", reporting.describe_exit(TP, entry, {}))
        self.assertIn("+$12.00", reporting.describe_exit(TP, entry, {}))
        self.assertIn("סטופ-לוס", reporting.describe_exit(SL, entry, {}))
        mkt = dict(TP, type="market", id="s3")
        self.assertIn("סוף יום", reporting.describe_exit(mkt, entry, {"AAPL": "סוף יום"}))

    def test_filled_sells_sorted(self):
        self.assertEqual([o["id"] for o in reporting.filled_sells([TP, BUY, SL])], ["s2", "s1"])

    def test_day_summary(self):
        entry = reporting.buy_prices([BUY, BUY2])
        lines = reporting.day_summary(6.0, [TP, SL], entry, 2)
        self.assertIn("+$6.00", lines[0])
        self.assertIn("ברווח: 1", lines[1])
        self.assertIn("בהפסד: 1", lines[1])

    def test_status_md(self):
        md = reporting.build_status_md(
            now=datetime(2026, 11, 9, 17, 0, tzinfo=timezone.utc), cfg=dict(bot.DEFAULT_CONFIG),
            equity=100_006.0, daily_pl=6.0, exposure=0.0, positions=[],
            orders_today=[BUY, TP], buy_reasons={"AAPL": "תוצאות | מעל הצפי"},
            history={"timestamp": [1762646400, 1762732800], "equity": [100_000, 100_006],
                     "profit_loss": [0, 6]})
        self.assertIn("מצב הבוט", md)
        self.assertIn("אין פוזיציות פתוחות", md)
        self.assertIn("🟢 קנייה", md)
        self.assertIn("תוצאות / מעל הצפי", md)  # התו | לא שובר את הטבלה
        self.assertIn("ימים ירוקים: 1", md)
        self.assertIn("19:00", md)  # 17:00 UTC = 19:00 בישראל בחורף

    def test_run_events(self):
        lines = reporting.run_events([
            {"kind": "buy", "symbol": "AAPL", "qty": 1, "price": 200, "take_profit": 206, "stop_loss": 197,
             "reason": "טוב"},
            {"kind": "signal", "symbol": "AAPL"},
            {"kind": "close_all", "reason": "סוף יום"}])
        self.assertEqual(len(lines), 2)


class FakeSession:
    def __init__(self, issues):
        self.issues = issues
        self.posts = []
        self.headers = {}

    def get(self, url, params=None, timeout=None):
        return FakeResp(self.issues)

    def post(self, url, json=None, timeout=None):
        self.posts.append((url, json))
        if url.endswith("/issues"):
            return FakeResp({"number": 7})
        return FakeResp({"id": 1})


class FakeResp:
    def __init__(self, data):
        self.data = data

    def raise_for_status(self):
        pass

    def json(self):
        return self.data


class NotifierTests(unittest.TestCase):
    def test_creates_issue_once_and_mentions_owner(self):
        n = reporting.GitHubNotifier("tok", "me/repo", "me")
        n.session = FakeSession([])
        n.send(["a"], "h")
        n.send(["b"], "h")
        urls = [u for u, _ in n.session.posts]
        self.assertEqual(sum(u.endswith("/issues") for u in urls), 1)
        self.assertTrue(urls[1].endswith("/issues/7/comments"))
        self.assertIn("@me", n.session.posts[1][1]["body"])

    def test_reuses_existing_issue(self):
        n = reporting.GitHubNotifier("tok", "me/repo", "me")
        n.session = FakeSession([{"title": reporting.ALERTS_TITLE, "number": 3}])
        n.send(["a"], "h")
        self.assertTrue(n.session.posts[0][0].endswith("/issues/3/comments"))

    def test_disabled_without_token(self):
        n = reporting.GitHubNotifier(None, None, None)
        self.assertFalse(n.send(["a"], "h"))


class FakeNotifier:
    enabled = True

    def __init__(self):
        self.sent = []

    def send(self, lines, header):
        self.sent.append(lines)
        return True


class FakeAlpaca:
    def __init__(self, orders, positions=(), equity=100_006.0):
        self._orders, self._positions, self.equity = orders, list(positions), equity

    def orders(self, status, after=None, symbols=None, nested=True):
        return self._orders

    def account(self):
        return {"equity": str(self.equity), "last_equity": "100000"}

    def positions(self):
        return self._positions

    def portfolio_history(self):
        return {"timestamp": [], "equity": [], "profit_loss": []}


class ReporterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.journal = bot.Journal(self.root / "journal")
        self.journal.set_day("2026-11-09")

    def tearDown(self):
        self.tmp.cleanup()

    def state(self, minutes_to_close=300):
        return bot.MarketState(now=datetime(2026, 11, 9, 12, 0, tzinfo=ET), minutes_since_open=150,
                               minutes_to_close=minutes_to_close, equity=100_006, last_equity=100_000, cash=100_000)

    def test_no_email_during_the_day_and_exit_recorded_once(self):
        n = FakeNotifier()
        r = reporting.Reporter(FakeAlpaca([BUY, TP]), self.journal, n, self.root)
        cfg = dict(bot.DEFAULT_CONFIG)
        r.after_run(cfg, self.state())
        r.after_run(cfg, self.state())
        self.assertEqual(n.sent, [])  # אין מייל באמצע היום
        self.assertTrue((self.root / "STATUS.md").exists())
        saved = json.loads((self.root / "journal" / "state.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["notified_orders"], ["s1"])
        self.assertEqual(sum("טייק-פרופיט" in e for e in saved["events"]), 1)

    def test_one_summary_at_end_of_day_when_flat(self):
        n = FakeNotifier()
        cfg = dict(bot.DEFAULT_CONFIG)
        open_pos = [{"symbol": "AAPL", "qty": "2", "market_value": "410", "unrealized_pl": "10"}]
        reporting.Reporter(FakeAlpaca([BUY], open_pos), self.journal, n, self.root).after_run(cfg, self.state(20))
        self.assertEqual(n.sent, [])  # עדיין יש פוזיציה פתוחה
        r = reporting.Reporter(FakeAlpaca([BUY, TP]), self.journal, n, self.root)
        r.after_run(cfg, self.state(15))
        r.after_run(cfg, self.state(5))
        self.assertEqual(len(n.sent), 1)
        text = "\n".join(n.sent[0])
        self.assertIn("ברווח: 1", text)
        self.assertIn("טייק-פרופיט", text)

    def test_quiet_day_sends_nothing(self):
        n = FakeNotifier()
        r = reporting.Reporter(FakeAlpaca([]), self.journal, n, self.root)
        r.after_run(dict(bot.DEFAULT_CONFIG), self.state(10))
        self.assertEqual(n.sent, [])

    def test_run_once_survives_reporter_failure(self):
        class Boom:
            def after_run(self, cfg, state):
                raise RuntimeError("github down")

        from test_bot import FakeAlpaca as TradeAlpaca, FakeAI
        code = bot.run_once(dict(bot.DEFAULT_CONFIG), TradeAlpaca(), FakeAI(), self.journal, Boom())
        self.assertEqual(code, 0)
        self.assertTrue(any(e["kind"] == "report_error" for e in self.journal.entries))


if __name__ == "__main__":
    unittest.main(verbosity=2)
