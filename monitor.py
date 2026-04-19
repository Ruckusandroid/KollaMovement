import os
import json
import smtplib
import ssl
from email.message import EmailMessage
from datetime import datetime, timezone

import yfinance as yf


STATE_FILE = "state.json"

# Triggers:
# level_name, drawdown_threshold, vix_threshold, leverage_target
TRIGGERS = [
    ("L1", -12.0, 25.0, 2),
    ("L2", -18.0, 30.0, 5),
    ("L3", -25.0, 35.0, 8),
    ("L4", -30.0, 40.0, 12),  # ändra till 16 om du vill
]


def load_state() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"last_trigger": None, "last_email_sent_at": None}
    with open(STATE_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_state(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_market_data() -> tuple[float, float, float]:
    """
    Returns:
        spx_close, spx_ath, vix_close
    Uses ^GSPC for S&P 500 and ^VIX for volatility.
    """
    spx = yf.Ticker("^GSPC")
    vix = yf.Ticker("^VIX")

    # 10 years räcker för praktiskt ATH i de flesta fall; vill du ha full historik, använd period="max"
    spx_hist = spx.history(period="max", auto_adjust=False)
    vix_hist = vix.history(period="5d", auto_adjust=False)

    if spx_hist.empty or vix_hist.empty:
        raise RuntimeError("Kunde inte hämta marknadsdata.")

    spx_close = float(spx_hist["Close"].dropna().iloc[-1])
    spx_ath = float(spx_hist["Close"].dropna().max())
    vix_close = float(vix_hist["Close"].dropna().iloc[-1])

    return spx_close, spx_ath, vix_close


def compute_drawdown_percent(current: float, ath: float) -> float:
    return (current / ath - 1.0) * 100.0


def evaluate_trigger(drawdown_pct: float, vix: float) -> tuple[str | None, int | None]:
    """
    Returns highest triggered level and target leverage.
    Example: ("L2", 5)
    """
    triggered = None
    target = None
    for level_name, dd_threshold, vix_threshold, leverage_target in TRIGGERS:
        if drawdown_pct <= dd_threshold and vix >= vix_threshold:
            triggered = level_name
            target = leverage_target
    return triggered, target


def send_email(subject: str, body: str) -> None:
    smtp_host = os.environ["SMTP_HOST"]
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


def main() -> None:
    state = load_state()

    spx_close, spx_ath, vix_close = get_market_data()
    drawdown_pct = compute_drawdown_percent(spx_close, spx_ath)
    trigger_name, leverage_target = evaluate_trigger(drawdown_pct, vix_close)

    now = datetime.now(timezone.utc).isoformat()

    print(
        json.dumps(
            {
                "timestamp_utc": now,
                "spx_close": round(spx_close, 2),
                "spx_ath": round(spx_ath, 2),
                "drawdown_pct": round(drawdown_pct, 2),
                "vix_close": round(vix_close, 2),
                "trigger_name": trigger_name,
                "leverage_target": leverage_target,
                "previous_state": state,
            },
            ensure_ascii=False,
            indent=2,
        )
    )

    last_trigger = state.get("last_trigger")

    # Skicka bara när en NY högre nivå triggas, eller när inget tidigare triggat fanns.
    if trigger_name is not None and trigger_name != last_trigger:
        subject = f"Marknadstrigger {trigger_name}: drawdown {drawdown_pct:.1f}%, VIX {vix_close:.1f}"
        body = (
            f"Ny trigger uppfylld.\n\n"
            f"S&P 500: {spx_close:.2f}\n"
            f"ATH: {spx_ath:.2f}\n"
            f"Drawdown från ATH: {drawdown_pct:.2f}%\n"
            f"VIX: {vix_close:.2f}\n"
            f"Trigger: {trigger_name}\n"
            f"Föreslagen total belåning: {leverage_target}%\n\n"
            f"Regler:\n"
            f"- L1: -12% & VIX >= 25 -> 2%\n"
            f"- L2: -18% & VIX >= 30 -> 5%\n"
            f"- L3: -25% & VIX >= 35 -> 8%\n"
            f"- L4: -30% & VIX >= 40 -> 12%\n"
        )
        send_email(subject, body)
        state["last_trigger"] = trigger_name
        state["last_email_sent_at"] = now

    # Nollställ när marknaden lugnat sig, så framtida triggers kan skickas igen.
    # Du kan justera detta.
    if vix_close < 20 and drawdown_pct > -5:
        state["last_trigger"] = None

    save_state(state)


if __name__ == "__main__":
    main()
