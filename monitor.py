import os
import json
from datetime import datetime, timezone
import smtplib
import ssl
from email.message import EmailMessage

import yfinance as yf


STATE_FILE = "state.json"

# S&P 500: drawdown + VIX
SPX_TRIGGERS = [
    ("L1", -12.0, 25.0, 2),
    ("L2", -18.0, 30.0, 5),
    ("L3", -25.0, 35.0, 8),
    ("L4", -30.0, 40.0, 12),  # ändra till 16 om du vill
]

# BTC: ungefär dubbla nivåer från ATH
BTC_TRIGGERS = [
    ("L1", -24.0),
    ("L2", -36.0),
    ("L3", -50.0),
    ("L4", -60.0),
]

# Guld: lugnare rörelser
GOLD_TRIGGERS = [
    ("L1", -8.0),
    ("L2", -12.0),
    ("L3", -16.0),
    ("L4", -20.0),
]


def send_email(subject: str, body: str) -> None:
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.environ.get("SMTP_PORT", "465"))
    smtp_user = os.environ["SMTP_USER"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    email_from = os.environ["EMAIL_FROM"]
    email_to = os.environ["EMAIL_TO"]

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    msg.set_content(body)

    context = ssl.create_default_context()
    with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context) as server:
        server.login(smtp_user, smtp_password)
        server.send_message(msg)


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {
            "spx_last_trigger": None,
            "btc_last_trigger": None,
            "gold_last_trigger": None,
            "last_email_sent_at": None,
        }
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_last_and_ath(ticker: str, period: str = "max") -> tuple[float, float]:
    t = yf.Ticker(ticker)
    hist = t.history(period=period, auto_adjust=False)
    if hist.empty:
        raise RuntimeError(f"Kunde inte hämta data för {ticker}")
    close = float(hist["Close"].dropna().iloc[-1])
    ath = float(hist["Close"].dropna().max())
    return close, ath


def get_vix() -> float:
    t = yf.Ticker("^VIX")
    hist = t.history(period="5d", auto_adjust=False)
    if hist.empty:
        raise RuntimeError("Kunde inte hämta VIX-data")
    return float(hist["Close"].dropna().iloc[-1])


def compute_drawdown_percent(current: float, ath: float) -> float:
    return (current / ath - 1.0) * 100.0


def evaluate_spx_trigger(drawdown_pct: float, vix: float) -> tuple[str | None, int | None]:
    triggered = None
    target = None
    for level_name, dd_threshold, vix_threshold, leverage_target in SPX_TRIGGERS:
        if drawdown_pct <= dd_threshold and vix >= vix_threshold:
            triggered = level_name
            target = leverage_target
    return triggered, target


def evaluate_simple_trigger(drawdown_pct: float, triggers: list[tuple[str, float]]) -> str | None:
    triggered = None
    for level_name, dd_threshold in triggers:
        if drawdown_pct <= dd_threshold:
            triggered = level_name
    return triggered


def main() -> None:
    state = load_state()
    now = datetime.now(timezone.utc).isoformat()

    # S&P 500
    spx_close, spx_ath = get_last_and_ath("^GSPC")
    vix_close = get_vix()
    spx_dd = compute_drawdown_percent(spx_close, spx_ath)
    spx_trigger, spx_target = evaluate_spx_trigger(spx_dd, vix_close)

    # BTC
    btc_close, btc_ath = get_last_and_ath("BTC-USD")
    btc_dd = compute_drawdown_percent(btc_close, btc_ath)
    btc_trigger = evaluate_simple_trigger(btc_dd, BTC_TRIGGERS)

    # Guld
    gold_close, gold_ath = get_last_and_ath("GC=F")
    gold_dd = compute_drawdown_percent(gold_close, gold_ath)
    gold_trigger = evaluate_simple_trigger(gold_dd, GOLD_TRIGGERS)

    print(json.dumps({
        "timestamp_utc": now,
        "spx": {
            "close": round(spx_close, 2),
            "ath": round(spx_ath, 2),
            "drawdown_pct": round(spx_dd, 2),
            "vix": round(vix_close, 2),
            "trigger": spx_trigger,
            "target_leverage": spx_target,
        },
        "btc": {
            "close": round(btc_close, 2),
            "ath": round(btc_ath, 2),
            "drawdown_pct": round(btc_dd, 2),
            "trigger": btc_trigger,
        },
        "gold": {
            "close": round(gold_close, 2),
            "ath": round(gold_ath, 2),
            "drawdown_pct": round(gold_dd, 2),
            "trigger": gold_trigger,
        },
        "state": state,
    }, ensure_ascii=False, indent=2))

    messages = []

    if spx_trigger is not None and spx_trigger != state.get("spx_last_trigger"):
        messages.append(
            f"S&P 500 {spx_trigger}\n"
            f"- Kurs: {spx_close:.2f}\n"
            f"- ATH: {spx_ath:.2f}\n"
            f"- Drawdown: {spx_dd:.2f}%\n"
            f"- VIX: {vix_close:.2f}\n"
            f"- Föreslagen total belåning: {spx_target}%\n"
        )
        state["spx_last_trigger"] = spx_trigger

    if btc_trigger is not None and btc_trigger != state.get("btc_last_trigger"):
        messages.append(
            f"BTC {btc_trigger}\n"
            f"- Kurs: {btc_close:.2f}\n"
            f"- ATH: {btc_ath:.2f}\n"
            f"- Drawdown: {btc_dd:.2f}%\n"
        )
        state["btc_last_trigger"] = btc_trigger

    if gold_trigger is not None and gold_trigger != state.get("gold_last_trigger"):
        messages.append(
            f"Guld {gold_trigger}\n"
            f"- Pris: {gold_close:.2f}\n"
            f"- ATH: {gold_ath:.2f}\n"
            f"- Drawdown: {gold_dd:.2f}%\n"
        )
        state["gold_last_trigger"] = gold_trigger

    if messages:
        subject = "Marknadstrigger"
        body = "Nya triggers uppfyllda:\n\n" + "\n".join(messages)
        send_email(subject, body)
        state["last_email_sent_at"] = now

    # Reset när läget lugnar sig
    if vix_close < 20 and spx_dd > -5:
        state["spx_last_trigger"] = None
    if btc_dd > -10:
        state["btc_last_trigger"] = None
    if gold_dd > -4:
        state["gold_last_trigger"] = None

    save_state(state)


if __name__ == "__main__":
    main()
