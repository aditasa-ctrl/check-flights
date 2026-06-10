"""
Wizz Watch - hourly cheap-flight watcher for your own Windows PC.

  1. Opens the Wizzair fare-finder page in a real (Playwright) browser so the
     site's anti-bot tokens generate automatically.
  2. Captures the JSON the page requests from SmartSearchCheapFlightsV2.
  3. Pairs each destination's outbound (home->X) and return (X->home) legs into
     a round trip, using the REGULAR price (not the Wizz Club price).
  4. Alerts only when the round trip qualifies (default: BOTH legs each under
     the threshold). Shows a Windows notification with full flight details.

Run modes:
  python wizz_watch.py             -> run one check now
  python wizz_watch.py --loop      -> keep running, check every interval_minutes
  python wizz_watch.py --debug     -> also save raw JSON + print parsed legs
  python wizz_watch.py --testnotify-> fire a test notification right now

Setup (one time):
  python -m pip install playwright winotify
  python -m playwright install chromium
"""

import argparse
import json
import os
import smtplib
import ssl
import subprocess
import time
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
RAW_PATH = os.path.join(HERE, "last_response.json")
SEEN_PATH = os.path.join(HERE, "seen.json")
DEALS_LOG = os.path.join(HERE, "deals_log.txt")

DEFAULT_CONFIG = {
    "fare_finder_url": "https://www.wizzair.com/en-gb/flights/fare-finder/telaviv/anywhere/0/0/0/1/0/0/2026-06-03/2026-06-30?flexible=anytime&duration=1_week",
    "home_station": "TLV",
    "max_price": 50,
    "currency": "EUR",
    "match_mode": "both_legs",
    "use_club_price": False,
    "headless": True,
    "interval_minutes": 60,
    "renotify_after_hours": 12,
    "open_browser_on_click": True,
    "notify_method": "auto",
    "email_smtp_server": "smtp.gmail.com",
    "email_smtp_port": 465,
    "email_sender": "",
    "email_password": "",
    "email_receiver": "",
}


def load_config():
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg.update(json.load(f))
        except Exception as e:
            log(f"WARNING: could not read config.json ({e}); using defaults")
    return cfg


def log(msg):
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    line = f"[{timestamp}] {msg}"
    print(line, flush=True)
    try:
        with open(os.path.join(HERE, "execution_log.txt"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def fetch_response(cfg, debug=False):
    from playwright.sync_api import sync_playwright

    captured = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=cfg.get("headless", True))
        context = browser.new_context(
            locale="en-GB",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36"
            ),
        )
        page = context.new_page()

        def on_response(resp):
            if "SmartSearchCheapFlights" in resp.url:
                try:
                    captured["data"] = resp.json()
                except Exception:
                    pass

        page.on("response", on_response)
        log("Opening fare-finder page...")
        page.goto(cfg["fare_finder_url"], wait_until="domcontentloaded", timeout=60000)
        for _ in range(30):
            if "data" in captured:
                break
            page.wait_for_timeout(1000)
        context.close()
        browser.close()

    if "data" not in captured:
        log("ERROR: never captured a SmartSearchCheapFlights response. "
            "The page layout/anti-bot may have changed, or the run was blocked.")
        return None

    if debug:
        with open(RAW_PATH, "w", encoding="utf-8") as f:
            json.dump(captured["data"], f, ensure_ascii=False, indent=2)
        log(f"Saved raw API JSON to {RAW_PATH}")
    return captured["data"]


def _num(v):
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, dict):
        for k in ("amount", "value", "price"):
            if isinstance(v.get(k), (int, float)):
                return float(v[k])
    return None


def _currency(v):
    if isinstance(v, dict):
        for k in ("currencyCode", "currency", "currencyCode3"):
            if isinstance(v.get(k), str):
                return v[k]
    return None


def _station(d, *keys):
    for k in keys:
        val = d.get(k)
        if isinstance(val, str):
            return val
        if isinstance(val, dict):
            for kk in ("code", "iata", "stationCode", "id"):
                if isinstance(val.get(kk), str):
                    return val[kk]
    return None


def _first(d, *keys):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _split_dt(value):
    if not isinstance(value, str):
        return (None, None)
    if "T" in value:
        date, _, rest = value.partition("T")
        return (date, rest[:5] if len(rest) >= 5 else rest)
    return (value[:10], None)


def parse_fares(data, default_currency="EUR"):
    fares = []

    def visit(node):
        if isinstance(node, dict):
            price_field = None
            for k in node:
                if k.lower() in ("price", "regularprice", "fareprice", "totalprice",
                                 "fullprice", "basicprice"):
                    price_field = k
                    break
            if price_field is not None:
                regular = _num(node.get(price_field))
                currency = _currency(node.get(price_field)) or default_currency
                club = None
                for k in node:
                    kl = k.lower()
                    if any(t in kl for t in ("wdc", "club", "discount", "member", "reduced")):
                        cand = _num(node.get(k))
                        if cand is not None:
                            club = cand
                            cc = _currency(node.get(k))
                            if cc:
                                currency = cc
                raw_dt = _first(node, "departureDateTime", "departureDateTimeIso",
                                "std", "departureDate", "outboundDepartureDate", "date")
                date, time_ = _split_dt(raw_dt) if isinstance(raw_dt, str) else (None, None)
                if not time_:
                    t2 = _first(node, "departureTime", "stdTime")
                    if isinstance(t2, str):
                        time_ = t2[:5]
                flight_no = _first(node, "flightNumber", "flightNo", "number")
                carrier = _first(node, "carrierCode", "carrier", "airlineCode")
                if carrier and flight_no and not str(flight_no).startswith(str(carrier)):
                    flight_no = f"{carrier}{flight_no}"
                if regular is not None:
                    fares.append({
                        "dep": _station(node, "departureStation", "departure",
                                        "origin", "from", "outboundDepartureStation"),
                        "arr": _station(node, "arrivalStation", "arrival",
                                        "destination", "to", "outboundArrivalStation"),
                        "date": date,
                        "time": time_,
                        "flight_no": flight_no,
                        "duration": _first(node, "duration", "flightDuration", "durationMinutes"),
                        "regular_amount": regular,
                        "club_amount": club,
                        "currency": currency,
                    })
            for v in node.values():
                visit(v)
        elif isinstance(node, list):
            for v in node:
                visit(v)

    visit(data)
    return fares


def build_round_trips(legs, home, use_club):
    price_key = "club_amount" if use_club else "regular_amount"

    def price(leg):
        amt = leg.get(price_key)
        if amt is None:
            amt = leg.get("regular_amount")
        return amt

    outbound, inbound = {}, {}
    for leg in legs:
        amt = price(leg)
        if amt is None:
            continue
        if leg.get("dep") == home and leg.get("arr"):
            dest = leg["arr"]
            if dest not in outbound or amt < price(outbound[dest]):
                outbound[dest] = leg
        elif leg.get("arr") == home and leg.get("dep"):
            dest = leg["dep"]
            if dest not in inbound or amt < price(inbound[dest]):
                inbound[dest] = leg

    trips = []
    for dest in set(outbound) & set(inbound):
        out, ret = outbound[dest], inbound[dest]
        trips.append({
            "dest": dest,
            "out": out,
            "ret": ret,
            "out_price": price(out),
            "ret_price": price(ret),
            "total": price(out) + price(ret),
            "currency": out.get("currency") or ret.get("currency"),
        })
    return trips


def load_seen():
    if os.path.exists(SEEN_PATH):
        try:
            with open(SEEN_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_seen(seen):
    try:
        with open(SEEN_PATH, "w", encoding="utf-8") as f:
            json.dump(seen, f)
    except Exception as e:
        log(f"WARNING: could not write seen.json ({e})")


def signature(trip):
    o, r = trip["out"], trip["ret"]
    return f"{trip['dest']}|{o.get('date')}|{r.get('date')}|{trip['out_price']}|{trip['ret_price']}"


def recently_alerted(seen, sig, hours):
    ts = seen.get(sig)
    if not ts:
        return False
    try:
        return datetime.fromisoformat(ts) > datetime.now() - timedelta(hours=hours)
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Notifications: try several methods so at least one is visible.
# ---------------------------------------------------------------------------
def _append_deal_log(title, msg):
    try:
        with open(DEALS_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] {title}\n{msg}\n\n")
    except Exception:
        pass


def _toast_winotify(title, msg, url, open_browser):
    from winotify import Notification, audio
    toast = Notification(app_id="Wizz Watch", title=title, msg=msg, duration="long")
    toast.set_audio(audio.Default, loop=False)
    if url and open_browser:
        toast.add_actions(label="Open on Wizzair", launch=url)
    toast.show()


def _toast_powershell(title, msg):
    # Native Win10/11 toast via PowerShell - no pip package required.
    ps = r'''
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
$tpl = [Windows.UI.Notifications.ToastNotificationManager]::GetTemplateContent([Windows.UI.Notifications.ToastTemplateType]::ToastText02)
$texts = $tpl.GetElementsByTagName("text")
$texts.Item(0).AppendChild($tpl.CreateTextNode(@"
__TITLE__
"@)) > $null
$texts.Item(1).AppendChild($tpl.CreateTextNode(@"
__MSG__
"@)) > $null
$toast = [Windows.UI.Notifications.ToastNotification]::new($tpl)
$appId = "{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier($appId).Show($toast)
'''
    ps = ps.replace("__TITLE__", title.replace('"', "'")).replace("__MSG__", msg.replace('"', "'"))
    subprocess.run(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", ps],
        capture_output=True, timeout=25, check=True,
    )


def _popup_messagebox(title, msg):
    # Guaranteed-visible modal box; ignores Focus Assist. Blocks until dismissed.
    import ctypes
    ctypes.windll.user32.MessageBoxW(0, msg, title, 0x40 | 0x1000)


def _send_email(cfg, title, msg):
    sender = cfg.get("email_sender")
    password = cfg.get("email_password")
    receiver = cfg.get("email_receiver")
    server_addr = cfg.get("email_smtp_server")
    port = cfg.get("email_smtp_port")

    if not all([sender, password, receiver, server_addr, port]):
        log("Email notification failed: Missing email configuration in config.json")
        return

    message = MIMEMultipart()
    message["From"] = sender
    message["To"] = receiver
    message["Subject"] = title
    message.attach(MIMEText(msg, "plain"))

    context = ssl.create_default_context()
    try:
        if port == 465:
            with smtplib.SMTP_SSL(server_addr, port, context=context) as server:
                server.login(sender, password)
                server.sendmail(sender, receiver, message.as_string())
        else:
            with smtplib.SMTP(server_addr, port) as server:
                server.starttls(context=context)
                server.login(sender, password)
                server.sendmail(sender, receiver, message.as_string())
        log(f"Email sent successfully to {receiver}")
    except smtplib.SMTPAuthenticationError:
        log("Email notification failed: Authentication error (535).")
        log("TIP: For Gmail, you MUST use an 'App Password', not your regular password.")
        log("Ensure 2-Step Verification is ON in your Google Account.")
    except Exception as e:
        log(f"Failed to send email: {e}")


def notify(cfg, title, msg, url=None, open_browser=True):
    method = cfg.get("notify_method", "auto")
    _append_deal_log(title, msg)

    if method == "email":
        _send_email(cfg, title, msg)
        return

    if method == "popup":
        try:
            _popup_messagebox(title, msg)
        except Exception as e:
            log(f"popup failed: {e}")
        return

    # auto / toast: winotify -> powershell toast -> messagebox
    try:
        _toast_winotify(title, msg, url, open_browser)
        return
    except Exception as e:
        log(f"winotify toast unavailable ({e}); trying built-in PowerShell toast")
    try:
        _toast_powershell(title, msg)
        return
    except Exception as e:
        log(f"PowerShell toast failed ({e}); falling back to popup box")
    if method == "auto":
        try:
            _popup_messagebox(title, msg)
        except Exception as e:
            log(f"popup failed: {e}")


def _leg_line(label, leg, amount, currency):
    bits = [f"{label}:"]
    if leg.get("date"):
        bits.append(leg["date"])
    if leg.get("time"):
        bits.append(leg["time"])
    if leg.get("flight_no"):
        bits.append(f"#{leg['flight_no']}")
    bits.append(f"{amount:.2f} {currency}")
    return "  ".join(bits)


def run_once(cfg, debug=False):
    data = fetch_response(cfg, debug=debug)
    if data is None:
        return

    legs = parse_fares(data, default_currency=cfg.get("currency", "EUR"))
    if debug:
        log(f"Parsed {len(legs)} one-way legs. Sample:")
        for lg in legs[:12]:
            log("  " + json.dumps(lg, ensure_ascii=False))

    home = cfg.get("home_station", "TLV")
    threshold = cfg.get("max_price", 50)
    want_currency = cfg.get("currency", "EUR")
    mode = cfg.get("match_mode", "both_legs")

    trips = build_round_trips(legs, home, cfg.get("use_club_price", False))
    trips = [t for t in trips
             if not (want_currency and t["currency"] and t["currency"] != want_currency)]

    def qualifies(t):
        if mode == "total":
            return t["total"] < threshold
        if mode == "per_leg":
            return (t["out_price"] < threshold or t["ret_price"] < threshold) and t["dest"] != "LCA"
        return t["out_price"] < threshold and t["ret_price"] < threshold

    deals = sorted([t for t in trips if qualifies(t)], key=lambda t: t["total"])

    if not deals:
        log(f"No round trips matching '{mode}' under {threshold} {want_currency} "
            f"(checked {len(trips)} destinations with both legs).")
        return

    seen = load_seen()
    new_deals = [d for d in deals
                 if not recently_alerted(seen, signature(d), cfg.get("renotify_after_hours", 12))]
    if not new_deals:
        log(f"Found {len(deals)} qualifying trip(s), all already alerted recently.")
        return

    for d in new_deals[:8]:
        cur = d["currency"] or want_currency
        title = (f"WIZZ RT {home}<->{d['dest']}  {d['total']:.0f} {cur} "
                 f"(out {d['out_price']:.0f} + ret {d['ret_price']:.0f})")
        msg = "\n".join([
            _leg_line(f"{home}->{d['dest']}", d["out"], d["out_price"], cur),
            _leg_line(f"{d['dest']}->{home}", d["ret"], d["ret_price"], cur),
            f"Round-trip total: {d['total']:.2f} {cur}",
        ])
        log("ALERT -> " + title)
        notify(cfg, title, msg, url=cfg.get("fare_finder_url"),
               open_browser=cfg.get("open_browser_on_click", True))
        seen[signature(d)] = datetime.now().isoformat()

    save_seen(seen)


def main():
    log("Wizz Watch started.")
    ap = argparse.ArgumentParser(description="Wizzair cheap-flight watcher")
    ap.add_argument("--loop", action="store_true", help="run continuously")
    ap.add_argument("--debug", action="store_true", help="save raw JSON and print parsed legs")
    ap.add_argument("--testnotify", action="store_true", help="fire a test notification and exit")
    args = ap.parse_args()

    cfg = load_config()

    if args.testnotify:
        log("Sending a test notification...")
        notify(cfg, "Wizz Watch test",
               "If you can see this, notifications work. \n"
               "Real alerts will look like this.",
               url=cfg.get("fare_finder_url"),
               open_browser=cfg.get("open_browser_on_click", True))
        log("Done.")
        return

    log(f"Config: {cfg.get('match_mode')} <= {cfg['max_price']} {cfg['currency']}, "
        f"{'club' if cfg.get('use_club_price') else 'regular'} price, "
        f"home={cfg.get('home_station')}, headless={cfg.get('headless')}, "
        f"notify={cfg.get('notify_method')}")

    if not args.loop:
        run_once(cfg, debug=args.debug)
        return

    interval = max(5, int(cfg.get("interval_minutes", 60))) * 60
    log(f"Loop mode: checking every {interval // 60} min. Ctrl+C to stop.")
    while True:
        try:
            run_once(cfg, debug=args.debug)
        except Exception as e:
            log(f"ERROR during check: {e}")
        time.sleep(interval)


if __name__ == "__main__":
    main()
