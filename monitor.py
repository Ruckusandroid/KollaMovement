import os
import json
import time
from datetime import datetime, timezone
import smtplib
import ssl
from email.message import EmailMessage

import yfinance as yf


STATE_FILE = "state.json"

SPX_TRIGGERS = [
    ("L1", -12.0, 25.0, 2),
    ("L2", -18.0, 30.0, 5),
    ("L3", -25.0, 35.0, 8),
    ("L4", -30.0, 40.0, 12),
]

BTC_TRIGGERS = [
    ("L1", -24.0),
    ("L2", -36.0),
    ("L3", -50.0),
    ("L4", -60.0),
]

GOLD_TRIGGERS = [
    ("L1", -8.0),
    ("L2", -12.0),
    ("L3", -16.0),
    ("L4", -20.0),
]

SPX_ACTIONS = {
    "L1": "Åtgärd: öka total belåning till 2%.",
    "L2": "Åtgärd: öka total belåning till 5%.",
    "L3": "Åtgärd: öka total belåning till 8%.",
    "L4": "Åtgärd: öka total belåning till 12%-16%.",
}

BTC_ACTIONS = {
    "L1": "Åtgärd: överväg första BTC-köp.(+2%)",
    "L2": "Åtgärd: öka BTC-köp till andra nivån.(+5%)",
    "L3": "Åtgärd: aggressiv BTC-köpnivå.(+8%)",
    "L4": "Åtgärd: maximal BTC-köpnivå.(+12-16%)",
}

GOLD_ACTIONS = {
    "L1": "Åtgärd: överväg första guldköp.(+2%)",
    "L2": "Åtgärd: öka guldköp till andra nivån. (+5%)",
    "L3": "Åtgärd: tredje guldköpnivån nådd. (+8%)",
    "L4": "Åtgärd: maximal guldköpnivå nådd. (+12-16%)",
}


def send_email(subject: str, body: str, html: str | None = None) -> None:
    smtp_host = os.environ.get("SMTP_HOST", "smtp.gmail.com").strip()
    smtp_port = int((os.environ.get("SMTP_PORT", "465") or "465").strip())
    smtp_user = os.environ["SMTP_USER"].strip()
    smtp_password = os.environ["SMTP_PASSWORD"].strip()
    email_from = os.environ["EMAIL_FROM"].strip()
    email_to_raw = os.environ["EMAIL_TO"]

    email_to_list = [addr.strip() for addr in email_to_raw.split(",") if addr.strip()]

    if not smtp_host:
        raise RuntimeError("SMTP_HOST är tom")
    if not smtp_user:
        raise RuntimeError("SMTP_USER är tom")
    if not smtp_password:
        raise RuntimeError("SMTP_PASSWORD är tom")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = ", ".join(email_to_list)

    if html:
        msg.set_content(body)
        msg.add_alternative(html, subtype="html")
    else:
        msg.set_content(body)

    context = ssl.create_default_context()

    server = smtplib.SMTP_SSL(host=smtp_host, port=smtp_port, context=context, timeout=30)
    try:
        server.login(smtp_user, smtp_password)
        server.send_message(msg)
    finally:
        try:
            server.quit()
        except Exception:
            pass


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {
            "spx_last_trigger": None,
            "btc_last_trigger": None,
            "gold_last_trigger": None,
            "last_email_sent_at": None,
            "last_status_email_date": None,
        }
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_last_and_ath(ticker: str, period: str = "max", retries: int = 3, delay: int = 20) -> tuple[float, float]:
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period=period, auto_adjust=False)

            if hist.empty:
                raise RuntimeError(f"Ingen data för {ticker}")

            close = float(hist["Close"].dropna().iloc[-1])
            ath = float(hist["Close"].dropna().max())
            return close, ath

        except Exception as e:
            last_error = e
            msg = str(e).lower()
            is_rate_limit = "rate limited" in msg or "too many requests" in msg or "429" in msg

            if attempt < retries and is_rate_limit:
                print(f"{ticker}: rate limited, försök {attempt}/{retries}, väntar {delay}s...")
                time.sleep(delay)
                continue

            raise

    raise last_error


def get_vix(retries: int = 3, delay: int = 20) -> float:
    last_error = None

    for attempt in range(1, retries + 1):
        try:
            t = yf.Ticker("^VIX")
            hist = t.history(period="5d", auto_adjust=False)

            if hist.empty:
                raise RuntimeError("Ingen VIX-data")

            return float(hist["Close"].dropna().iloc[-1])

        except Exception as e:
            last_error = e
            msg = str(e).lower()
            is_rate_limit = "rate limited" in msg or "too many requests" in msg or "429" in msg

            if attempt < retries and is_rate_limit:
                print(f"VIX: rate limited, försök {attempt}/{retries}, väntar {delay}s...")
                time.sleep(delay)
                continue

            raise

    raise last_error


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


def should_send_weekly_status(state: dict, now_dt: datetime) -> bool:
    if now_dt.weekday() != 0:
        return False
    today = now_dt.date().isoformat()
    return state.get("last_status_email_date") != today


def get_next_simple_trigger(drawdown_pct: float, triggers: list[tuple[str, float]]) -> str:
    for level_name, dd_threshold in triggers:
        if drawdown_pct > dd_threshold:
            diff = drawdown_pct - dd_threshold
            return f"{level_name} om ytterligare {diff:.2f} procentenheter ned"
    return "Ingen nästa nivå, redan under djupaste triggern"


def get_next_spx_trigger(drawdown_pct: float, vix: float) -> str:
    for level_name, dd_threshold, vix_threshold, leverage_target in SPX_TRIGGERS:
        if not (drawdown_pct <= dd_threshold and vix >= vix_threshold):
            dd_needed = max(0.0, drawdown_pct - dd_threshold)
            vix_needed = max(0.0, vix_threshold - vix)
            return (
                f"{level_name} kräver ytterligare {dd_needed:.2f} procentenheter ned "
                f"och {vix_needed:.2f} VIX-punkter upp "
                f"(målbelåning {leverage_target}%)"
            )
    return "Ingen nästa nivå, redan under djupaste triggern"


def get_deleveraging_signal(drawdown_pct: float, vix: float) -> str:
    if drawdown_pct > -5 or vix < 18:
        return "Stark amorteringssignal"
    if drawdown_pct > -10 and vix < 20:
        return "Tydlig amorteringssignal"
    if vix < 25:
        return "Försiktig amorteringssignal"
    return "Ingen amortering ännu"


def get_deleveraging_guidance(drawdown_pct: float, vix: float) -> str:
    signal = get_deleveraging_signal(drawdown_pct, vix)

    if signal == "Stark amorteringssignal":
        return (
            "Om du är belånad:\n"
            "- 0–2%: amortera av helt\n"
            "- 2–5%: amortera av helt\n"
            "- 5–8%: amortera ner till 0–2%\n"
            "- 8–12%: amortera ner till 0–2% snabbt\n"
            "- 12–16%: amortera aggressivt ner mot 0%\n"
        )

    if signal == "Tydlig amorteringssignal":
        return (
            "Om du är belånad:\n"
            "- 0–2%: du kan ligga kvar eller amortera av helt\n"
            "- 2–5%: amortera ner mot 0–2%\n"
            "- 5–8%: amortera ner mot 2–5%\n"
            "- 8–12%: amortera ner mot 5%\n"
            "- 12–16%: amortera tydligt ner mot 5–8%\n"
        )

    if signal == "Försiktig amorteringssignal":
        return (
            "Om du är belånad:\n"
            "- 0–2%: ingen brådska\n"
            "- 2–5%: överväg att amortera lätt mot 2%\n"
            "- 5–8%: amortera försiktigt ner mot 5%\n"
            "- 8–12%: amortera ner mot 5–8%\n"
            "- 12–16%: amortera ner mot 8%\n"
        )

    return (
        "Om du är belånad:\n"
        "- 0–2%: ingen åtgärd\n"
        "- 2–5%: ingen åtgärd\n"
        "- 5–8%: ingen åtgärd\n"
        "- 8–12%: avvakta\n"
        "- 12–16%: avvakta, men öka inte ytterligare\n"
    )


def format_level_html(label: str, text: str, is_active: bool) -> str:
    if is_active:
        return f"<li><strong>{label}: {text} ← vi är här nu</strong></li>"
    return f"<li>{label}: {text}</li>"


def build_spx_rules_html(active_trigger: str | None) -> str:
    rows = [
        ("L1", "-12% och VIX ≥ 25 → öka total belåning till 2%"),
        ("L2", "-18% och VIX ≥ 30 → öka total belåning till 5%"),
        ("L3", "-25% och VIX ≥ 35 → öka total belåning till 8%"),
        ("L4", "-30% och VIX ≥ 40 → öka total belåning till 12%-16%"),
    ]
    items = "\n".join(
        format_level_html(level, text, active_trigger == level)
        for level, text in rows
    )
    return f"""
    <h2>Huvudregel</h2>
    <p><strong>S&amp;P 500</strong></p>
    <ul>
      {items}
    </ul>
    """


def build_opportunities_html(btc_trigger: str | None, gold_trigger: str | None) -> str:
    btc_rows = [
        ("L1", "-24% från ATH → överväg första BTC-köp (+2%)"),
        ("L2", "-36% från ATH → öka BTC-köp till andra nivån (+5%)"),
        ("L3", "-50% från ATH → aggressiv BTC-köpnivå (+8%)"),
        ("L4", "-60% från ATH → maximal BTC-köpnivå (+12%-16%)"),
    ]

    gold_rows = [
        ("L1", "-8% från ATH → överväg första guldköp (+2%)"),
        ("L2", "-12% från ATH → öka guldköp till andra nivån (+5%)"),
        ("L3", "-16% från ATH → tredje guldköpnivån nådd (+8%)"),
        ("L4", "-20% från ATH → maximal guldköpnivå nådd (+12%-16%)"),
    ]

    btc_items = "\n".join(
        format_level_html(level, text, btc_trigger == level)
        for level, text in btc_rows
    )
    gold_items = "\n".join(
        format_level_html(level, text, gold_trigger == level)
        for level, text in gold_rows
    )

    return f"""
    <h2>Möjligheter</h2>
    <p><strong>BTC</strong></p>
    <ul>
      {btc_items}
    </ul>
    <p><strong>Guld</strong></p>
    <ul>
      {gold_items}
    </ul>
    """


def main() -> None:
    state = load_state()
    now_dt = datetime.now(timezone.utc)
    now = now_dt.isoformat()

    try:
        spx_close, spx_ath = get_last_and_ath("^GSPC")
        vix_close = get_vix()
        btc_close, btc_ath = get_last_and_ath("BTC-USD")
        gold_close, gold_ath = get_last_and_ath("GC=F")
    except Exception as e:
        msg = str(e).lower()
        if "rate limited" in msg or "too many requests" in msg or "429" in msg:
            print(f"Yahoo rate limit just nu, hoppar över denna körning: {e}")
            return
        raise

    spx_dd = compute_drawdown_percent(spx_close, spx_ath)
    spx_trigger, spx_target = evaluate_spx_trigger(spx_dd, vix_close)

    btc_dd = compute_drawdown_percent(btc_close, btc_ath)
    btc_trigger = evaluate_simple_trigger(btc_dd, BTC_TRIGGERS)

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
        action_text = SPX_ACTIONS.get(spx_trigger, "Åtgärd: ingen definierad.")
        messages.append(
            f"S&P 500 {spx_trigger}\n"
            f"- Kurs: {spx_close:.2f}\n"
            f"- ATH: {spx_ath:.2f}\n"
            f"- Drawdown: {spx_dd:.2f}%\n"
            f"- VIX: {vix_close:.2f}\n"
            f"- Föreslagen total belåning: {spx_target}%\n"
            f"- {action_text}\n"
        )
        state["spx_last_trigger"] = spx_trigger

    if btc_trigger is not None and btc_trigger != state.get("btc_last_trigger"):
        action_text = BTC_ACTIONS.get(btc_trigger, "Åtgärd: ingen definierad.")
        messages.append(
            f"BTC {btc_trigger}\n"
            f"- Kurs: {btc_close:.2f}\n"
            f"- ATH: {btc_ath:.2f}\n"
            f"- Drawdown: {btc_dd:.2f}%\n"
            f"- {action_text}\n"
        )
        state["btc_last_trigger"] = btc_trigger

    if gold_trigger is not None and gold_trigger != state.get("gold_last_trigger"):
        action_text = GOLD_ACTIONS.get(gold_trigger, "Åtgärd: ingen definierad.")
        messages.append(
            f"Guld {gold_trigger}\n"
            f"- Pris: {gold_close:.2f}\n"
            f"- ATH: {gold_ath:.2f}\n"
            f"- Drawdown: {gold_dd:.2f}%\n"
            f"- {action_text}\n"
        )
        state["gold_last_trigger"] = gold_trigger

    if messages:
        subject = "Lystring!"
        text_body = "Nya triggers uppfyllda:\n\n" + "\n".join(messages)
        html_body = f"""
        <html>
          <body>
            <h1 style="color: red; font-size: 32px; margin-bottom: 16px;">Lystring!</h1>
            <p><strong>Nya triggers uppfyllda:</strong></p>
            <pre style="font-size: 14px; line-height: 1.5; white-space: pre-wrap;">{text_body}</pre>
            <hr>
            {build_spx_rules_html(spx_trigger)}
            {build_opportunities_html(btc_trigger, gold_trigger)}
          </body>
        </html>
        """
        send_email(subject, text_body, html_body)
        state["last_email_sent_at"] = now

    if should_send_weekly_status(state, now_dt):
        text_body = (
            "Systemet lever och kör som vanligt.\n\n"
            "Status just nu:\n\n"
            f"S&P 500\n"
            f"- Kurs: {spx_close:.2f}\n"
            f"- ATH: {spx_ath:.2f}\n"
            f"- Från ATH: {spx_dd:.2f}%\n"
            f"- VIX: {vix_close:.2f}\n"
            f"- Aktiv trigger: {spx_trigger or 'Ingen'}\n"
            f"- Aktiv åtgärd: {SPX_ACTIONS.get(spx_trigger, 'Ingen aktiv åtgärd just nu.') if spx_trigger else 'Ingen aktiv åtgärd just nu.'}\n"
            f"- Nästa trigger: {get_next_spx_trigger(spx_dd, vix_close)}\n\n"
            f"BTC\n"
            f"- Kurs: {btc_close:.2f}\n"
            f"- ATH: {btc_ath:.2f}\n"
            f"- Från ATH: {btc_dd:.2f}%\n"
            f"- Aktiv trigger: {btc_trigger or 'Ingen'}\n"
            f"- Aktiv åtgärd: {BTC_ACTIONS.get(btc_trigger, 'Ingen aktiv åtgärd just nu.') if btc_trigger else 'Ingen aktiv åtgärd just nu.'}\n"
            f"- Nästa trigger: {get_next_simple_trigger(btc_dd, BTC_TRIGGERS)}\n\n"
            f"Guld\n"
            f"- Pris: {gold_close:.2f}\n"
            f"- ATH: {gold_ath:.2f}\n"
            f"- Från ATH: {gold_dd:.2f}%\n"
            f"- Aktiv trigger: {gold_trigger or 'Ingen'}\n"
            f"- Aktiv åtgärd: {GOLD_ACTIONS.get(gold_trigger, 'Ingen aktiv åtgärd just nu.') if gold_trigger else 'Ingen aktiv åtgärd just nu.'}\n"
            f"- Nästa trigger: {get_next_simple_trigger(gold_dd, GOLD_TRIGGERS)}\n\n"
            "Amorteringsrekommendation\n"
            f"- Signal: {get_deleveraging_signal(spx_dd, vix_close)}\n"
            f"{get_deleveraging_guidance(spx_dd, vix_close)}\n"
        )

        html_body = f"""
        <html>
          <body>
            <h1 style="font-size: 32px; margin-bottom: 16px;">Veckobrev</h1>
            <pre style="font-size: 14px; line-height: 1.5; white-space: pre-wrap;">{text_body}</pre>
            <hr>
            {build_spx_rules_html(spx_trigger)}
            {build_opportunities_html(btc_trigger, gold_trigger)}
          </body>
        </html>
        """
        send_email("Veckobrev", text_body, html_body)
        state["last_status_email_date"] = now_dt.date().isoformat()

    if vix_close < 20 and spx_dd > -5:
        state["spx_last_trigger"] = None
    if btc_dd > -10:
        state["btc_last_trigger"] = None
    if gold_dd > -4:
        state["gold_last_trigger"] = None

    save_state(state)


if __name__ == "__main__":
    main()
