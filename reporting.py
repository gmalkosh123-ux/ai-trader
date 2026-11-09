"""
דיווח: דף מצב (STATUS.md) והתראות דרך GitHub, במקום טלגרם.

- דף המצב מתעדכן בכל הרצה בזמן שהשוק פתוח: שווי, רווח/הפסד, פוזיציות, עסקאות היום ו-30 הימים האחרונים.
- התראה אחת בלבד ביום: סיכום בסוף יום המסחר, כתגובה ב-Issue קבוע בריפו, עם תיוג שלך.
  ביום בלי עסקאות לא נשלח כלום. כל השאר מופיע ב-STATUS.md.
- תקלה בדיווח לעולם לא עוצרת את המסחר.
"""
from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

import requests

try:
    from zoneinfo import ZoneInfo
    ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
except Exception:  # pragma: no cover - אם אין מסד אזורי זמן
    ISRAEL_TZ = timezone.utc

ALERTS_TITLE = "📣 התראות בוט המסחר"
EXIT_TYPES = {"limit": "טייק-פרופיט 🎯", "stop": "סטופ-לוס 🛑", "market": "יציאה יזומה"}


def usd(x: float) -> str:
    sign = "-" if x < 0 else ("+" if x > 0 else "")
    return f"{sign}${abs(x):,.2f}"


def il_time(dt: datetime | str | None) -> str:
    if dt is None:
        return "?"
    if isinstance(dt, str):
        from bot import parse_ts  # ייבוא מאוחר כדי למנוע תלות מעגלית
        dt = parse_ts(dt)
    return dt.astimezone(ISRAEL_TZ).strftime("%H:%M")


# ---------------------------------------------------------------------------
# התראות דרך GitHub Issues
# ---------------------------------------------------------------------------
class GitHubNotifier:
    def __init__(self, token: str | None, repo: str | None, owner: str | None):
        self.enabled = bool(token and repo)
        self.repo = repo
        self.owner = owner
        self.session = requests.Session()
        if token:
            self.session.headers.update({"Authorization": f"Bearer {token}",
                                         "Accept": "application/vnd.github+json"})
        self._issue: int | None = None

    @classmethod
    def from_env(cls) -> "GitHubNotifier":
        return cls(os.environ.get("GITHUB_TOKEN"), os.environ.get("GITHUB_REPOSITORY"),
                   os.environ.get("GITHUB_REPOSITORY_OWNER"))

    def _url(self, path: str) -> str:
        return f"https://api.github.com/repos/{self.repo}{path}"

    def find_issue(self) -> int | None:
        """מחפש את ה-Issue של ההתראות בלי ליצור אותו ובלי לשלוח מייל."""
        if self._issue:
            return self._issue
        r = self.session.get(self._url("/issues"), params={"state": "open", "per_page": 100}, timeout=20)
        r.raise_for_status()
        for issue in r.json():
            if issue.get("title") == ALERTS_TITLE and "pull_request" not in issue:
                self._issue = issue["number"]
                return self._issue
        return None

    def _issue_number(self) -> int:
        if self.find_issue():
            return self._issue
        body = ("כאן הבוט כותב סיכום אחד בסוף כל יום מסחר שהיו בו עסקאות.\n\n"
                "GitHub שולח לך מייל אחד על כל סיכום. אפשר להשתיק דרך כפתור Unsubscribe.\n\n"
                "המסחר הוא בחשבון דמו בלבד.")
        r = self.session.post(self._url("/issues"), json={"title": ALERTS_TITLE, "body": body}, timeout=20)
        r.raise_for_status()
        self._issue = r.json()["number"]
        return self._issue

    def send(self, lines: list[str], header: str) -> bool:
        if not self.enabled or not lines:
            return False
        mention = f"@{self.owner} " if self.owner else ""
        body = f'<div dir="rtl">\n\n{mention}**{header}**\n\n' + "\n".join(f"- {l}" for l in lines) + "\n\n</div>"
        r = self.session.post(self._url(f"/issues/{self._issue_number()}/comments"), json={"body": body}, timeout=20)
        r.raise_for_status()
        return True


# ---------------------------------------------------------------------------
# מצב בין הרצות (אילו מכירות כבר דווחו, האם נשלח סיכום יומי)
# ---------------------------------------------------------------------------
def load_state(path: Path, day: str) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        data = {}
    if data.get("date") != day:
        data = {"date": day, "notified_orders": [], "summary_sent": False}
    return data


def save_state(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------
# לוגיקה טהורה (נבדקת בבדיקות)
# ---------------------------------------------------------------------------
def buy_prices(orders: list) -> dict[str, float]:
    """מחיר קנייה ממוצע לכל מניה, מתוך פקודות הקנייה שבוצעו היום."""
    totals: dict[str, list[float]] = {}
    for o in orders:
        if o.get("side") == "buy" and o.get("status") == "filled" and o.get("filled_avg_price"):
            qty, price = float(o.get("filled_qty") or 0), float(o["filled_avg_price"])
            t = totals.setdefault(o["symbol"], [0.0, 0.0])
            t[0] += qty * price
            t[1] += qty
    return {s: v[0] / v[1] for s, v in totals.items() if v[1] > 0}


def filled_sells(orders: list) -> list:
    return sorted([o for o in orders if o.get("side") == "sell" and o.get("status") == "filled"],
                  key=lambda o: str(o.get("filled_at") or ""))


def exit_pnl(order: dict, entry_prices: dict[str, float]) -> float | None:
    entry = entry_prices.get(order["symbol"])
    if entry is None or not order.get("filled_avg_price"):
        return None
    return (float(order["filled_avg_price"]) - entry) * float(order.get("filled_qty") or 0)


def describe_exit(order: dict, entry_prices: dict[str, float], close_reasons: dict[str, str]) -> str:
    kind = EXIT_TYPES.get(order.get("type", ""), order.get("type", ""))
    if order.get("type") == "market" and order["symbol"] in close_reasons:
        kind = close_reasons[order["symbol"]]
    pnl = exit_pnl(order, entry_prices)
    emoji = "✅" if (pnl or 0) > 0 else ("❌" if (pnl or 0) < 0 else "➖")
    pnl_txt = f" → {emoji} {usd(pnl)}" if pnl is not None else ""
    return (f"🔴 נמכר {order['symbol']} × {float(order.get('filled_qty') or 0):g} "
            f"ב-${float(order['filled_avg_price']):,.2f} ({kind}){pnl_txt}")


def day_summary(daily_pl: float, sells: list, entry_prices: dict[str, float], buys_count: int) -> list[str]:
    pnls = [p for p in (exit_pnl(o, entry_prices) for o in sells) if p is not None]
    wins = sum(1 for p in pnls if p > 0)
    losses = sum(1 for p in pnls if p < 0)
    emoji = "🟢" if daily_pl > 0 else ("🔴" if daily_pl < 0 else "⚪")
    return [f"{emoji} רווח/הפסד היום: **{usd(daily_pl)}**",
            f"עסקאות: {buys_count} | ברווח: {wins} | בהפסד: {losses}"]


def journal_reasons(path: Path | None) -> tuple[dict[str, str], dict[str, str]]:
    """מהיומן של היום: למה קנינו כל מניה, ולמה סגרנו."""
    buys, closes = {}, {}
    if path is None or not path.exists():
        return buys, closes
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            e = json.loads(line)
        except json.JSONDecodeError:
            continue
        if e.get("kind") == "buy":
            buys[e["symbol"]] = str(e.get("reason") or "")
        elif e.get("kind") == "close":
            closes[e["symbol"]] = "יציאה מוקדמת: חדשות שליליות"
        elif e.get("kind") == "close_all":
            for s in e.get("positions") or []:
                closes[s] = "סגירה כללית: " + str(e.get("reason") or "")[:60]
    return buys, closes


def run_events(entries: list[dict]) -> list[str]:
    """אירועים מההרצה הנוכחית שכדאי לדווח עליהם."""
    out = []
    for e in entries:
        k = e.get("kind")
        if k == "buy":
            out.append(f"🟢 קניתי {e['qty']} × {e['symbol']} בערך ${float(e['price']):,.2f} "
                       f"(יעד ${float(e['take_profit']):,.2f}, סטופ ${float(e['stop_loss']):,.2f}). "
                       f"סיבה: {e.get('reason', '')}")
        elif k == "order_error":
            out.append(f"⚠️ פקודה על {e.get('symbol')} נדחתה על ידי הברוקר.")
        elif k == "close_all":
            out.append(f"🧹 {e.get('reason', '')}")
    return out


class Reporter:
    """מריץ אחרי כל הרצה בזמן שהשוק פתוח: התראות + עדכון STATUS.md."""

    def __init__(self, alpaca, journal, notifier: GitHubNotifier, root: Path):
        self.alpaca = alpaca
        self.journal = journal
        self.notifier = notifier
        self.root = root

    def after_run(self, cfg: dict, state):
        day = state.now.date().isoformat()
        state_path = self.root / "journal" / "state.json"
        mem = load_state(state_path, day)
        day_start = state.now.replace(hour=0, minute=0, second=0, microsecond=0)
        orders = self.alpaca.orders("all", after=day_start.isoformat(), nested=False) or []
        entry = buy_prices(orders)
        buy_reasons, close_reasons = journal_reasons(self.journal.path)

        # האירועים נאספים לאורך היום ונשלחים בהודעה אחת בסוף היום (מייל אחד ביום לכל היותר)
        events = mem.setdefault("events", [])
        events.extend(run_events(self.journal.entries))
        for o in filled_sells(orders):
            if o.get("id") not in mem["notified_orders"]:
                events.append(describe_exit(o, entry, close_reasons))
                mem["notified_orders"].append(o.get("id"))

        account = self.alpaca.account()
        positions = self.alpaca.positions() or []
        equity = float(account.get("equity") or state.equity)
        daily_pl = equity - float(account.get("last_equity") or state.last_equity)
        exposure = sum(abs(float(p.get("market_value") or 0)) for p in positions)

        end_of_day = state.minutes_to_close <= cfg["flatten_minutes_before_close"]
        buys_count = sum(1 for o in orders if o.get("side") == "buy" and o.get("status") == "filled")
        if end_of_day and not positions and not mem["summary_sent"]:
            if buys_count or events:  # ביום בלי עסקאות לא נשלחת הודעה בכלל
                lines = day_summary(daily_pl, filled_sells(orders), entry, buys_count)
                lines += ["**מה קרה היום:**"] + events
                try:
                    self.notifier.send(lines, f"📊 סיכום יום המסחר ({state.now:%d/%m})")
                except Exception as e:
                    self.journal.log("notify_error", error=str(e)[:300])
            mem["summary_sent"] = True
        save_state(state_path, mem)

        try:
            history = self.alpaca.portfolio_history()
        except Exception:
            history = None
        md = build_status_md(now=state.now, cfg=cfg, equity=equity, daily_pl=daily_pl, exposure=exposure,
                             positions=positions, orders_today=orders, buy_reasons=buy_reasons, history=history,
                             mode_note="מצב ניסוי (dry_run): הבוט לא שולח פקודות." if cfg.get("dry_run") else "")
        (self.root / "STATUS.md").write_text(md, encoding="utf-8")


def build_status_md(*, now: datetime, cfg: dict, equity: float, daily_pl: float, exposure: float,
                    positions: list, orders_today: list, buy_reasons: dict[str, str],
                    history: dict | None, mode_note: str = "") -> str:
    entry = buy_prices(orders_today)
    lines = ['<div dir="rtl">', "", "# 📊 מצב הבוט", "",
             f"**עודכן:** {now.astimezone(ISRAEL_TZ):%d/%m/%Y %H:%M} (שעון ישראל). הדף מתעדכן כל 10 דקות כשהשוק פתוח.", ""]
    if mode_note:
        lines += [f"> {mode_note}", ""]
    emoji = "🟢" if daily_pl > 0 else ("🔴" if daily_pl < 0 else "⚪")
    lines += ["| | |", "|---|---|",
              f"| שווי חשבון הדמו | ${equity:,.2f} |",
              f"| רווח/הפסד היום | {emoji} {usd(daily_pl)} |",
              f"| בשימוש הבוט | ${exposure:,.2f} מתוך ${cfg['max_total_capital_usd']:,.0f} |", ""]

    lines += ["## פוזיציות פתוחות", ""]
    if positions:
        lines += ["| מניה | כמות | מחיר קנייה | מחיר עכשיו | רווח/הפסד |", "|---|---|---|---|---|"]
        for p in positions:
            lines.append(f"| {p['symbol']} | {float(p['qty']):g} | ${float(p.get('avg_entry_price') or 0):,.2f} | "
                         f"${float(p.get('current_price') or 0):,.2f} | {usd(float(p.get('unrealized_pl') or 0))} |")
    else:
        lines.append("אין פוזיציות פתוחות.")
    lines.append("")

    lines += ["## עסקאות היום", ""]
    fills = sorted([o for o in orders_today if o.get("status") == "filled"], key=lambda o: str(o.get("filled_at") or ""))
    if fills:
        lines += ["| שעה | פעולה | מניה | כמות | מחיר | פרטים |", "|---|---|---|---|---|---|"]
        for o in fills:
            if o.get("side") == "buy":
                action, details = "🟢 קנייה", buy_reasons.get(o["symbol"], "")
            else:
                action = "🔴 מכירה"
                pnl = exit_pnl(o, entry)
                details = EXIT_TYPES.get(o.get("type", ""), "") + (f" ({usd(pnl)})" if pnl is not None else "")
            details = details.replace("|", "/")[:120]
            lines.append(f"| {il_time(o.get('filled_at'))} | {action} | {o['symbol']} | "
                         f"{float(o.get('filled_qty') or 0):g} | ${float(o['filled_avg_price']):,.2f} | {details} |")
    else:
        lines.append("עוד לא היו עסקאות היום.")
    lines.append("")

    if history and history.get("timestamp"):
        lines += ["## 30 הימים האחרונים", "", "| תאריך | שווי | רווח/הפסד |", "|---|---|---|"]
        rows = list(zip(history["timestamp"], history.get("equity") or [], history.get("profit_loss") or []))
        total, green, red = 0.0, 0, 0
        for ts, eq, pl in reversed(rows):
            if eq is None:
                continue
            pl = float(pl or 0)
            total += pl
            green += pl > 0
            red += pl < 0
            day = datetime.fromtimestamp(int(ts), timezone.utc).astimezone(ISRAEL_TZ)
            dot = "🟢" if pl > 0 else ("🔴" if pl < 0 else "⚪")
            lines.append(f"| {day:%d/%m} | ${float(eq):,.2f} | {dot} {usd(pl)} |")
        lines += ["", f"**סה\"כ בתקופה:** {usd(total)} | ימים ירוקים: {green} | ימים אדומים: {red}", ""]

    lines += ["---", "",
              "לצפייה חיה: app.alpaca.markets (חשבון Paper). כל ההחלטות והסיבות: תיקיית `journal/`.", "", "</div>", ""]
    return "\n".join(lines)
