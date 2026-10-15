#!/usr/bin/env python3
"""
Børsvarsel: varsler når S&P 500 faller 3 %, 5 % eller 7 % fra forrige sluttkurs,
og lager en liste over aksjene i indeksen som har falt mest.

Oppsett:
    pip install yfinance pandas requests lxml

Bruk:
    python borsvarsel.py --test      # sender et testvarsel til telefonen
    python borsvarsel.py --once      # sjekker én gang og avslutter
    python borsvarsel.py --loop      # sjekker hvert 5. minutt i børstiden (standard)
    python borsvarsel.py --losers    # skriver ut tapslisten nå, uavhengig av nivå

Varsler: installer appen "ntfy" (iPhone/Android), abonner på emnet (topic) du setter under.
"""

import argparse
import io
import json
import os
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

# ---------------------------- INNSTILLINGER ----------------------------
# Emnet leses fra miljøvariabelen NTFY_TOPIC (GitHub Secret). Skal IKKE skrives inn i koden
# hvis repoet er offentlig. Lokalt kan du sette det her i stedet for ""-verdien under.
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
INDEX_SYMBOL = "^GSPC"                           # S&P 500. Nasdaq 100 = "^NDX"
THRESHOLDS = [3.0, 5.0, 7.0]                     # fall i prosent som utløser varsel
CHECK_EVERY_SECONDS = 300                        # hvor ofte det sjekkes i --loop
TOP_N = 20                                       # antall aksjer i tapslisten
# -----------------------------------------------------------------------

NY = ZoneInfo("America/New_York")
BASE = Path(__file__).parent
STATE_FILE = BASE / "alert_state.json"
CONSTITUENTS_FILE = BASE / "sp500_constituents.csv"


def send_push(title: str, message: str, priority: str = "default", tags: str = "chart_with_downwards_trend"):
    """Sender push via ntfy.sh. Tittel må være ASCII (HTTP-header)."""
    if not NTFY_TOPIC:
        raise SystemExit("NTFY_TOPIC er ikke satt (miljøvariabel / GitHub Secret).")
    r = requests.post(
        f"https://ntfy.sh/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Tags": tags},
        timeout=15,
    )
    r.raise_for_status()


def market_is_open(now_ny: datetime | None = None) -> bool:
    now_ny = now_ny or datetime.now(NY)
    if now_ny.weekday() >= 5:
        return False
    minutes = now_ny.hour * 60 + now_ny.minute
    return 9 * 60 + 30 <= minutes <= 16 * 60  # 15:30-22:00 norsk tid (helligdager ignoreres)


def index_change_pct() -> tuple[float, float, float]:
    """Returnerer (endring i %, siste kurs, forrige sluttkurs)."""
    fi = yf.Ticker(INDEX_SYMBOL).fast_info
    last = float(fi["last_price"])
    prev = float(fi["previous_close"])
    return (last / prev - 1) * 100, last, prev


def load_state() -> dict:
    today = datetime.now(NY).strftime("%Y-%m-%d")
    if STATE_FILE.exists():
        try:
            s = json.loads(STATE_FILE.read_text())
            if s.get("date") == today:
                return s
        except Exception:
            pass
    return {"date": today, "sent": []}  # nullstilles hver handelsdag


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state))


def get_constituents() -> pd.DataFrame:
    """Henter S&P 500-listen fra Wikipedia (cachet i 7 dager)."""
    if CONSTITUENTS_FILE.exists():
        age_days = (time.time() - CONSTITUENTS_FILE.stat().st_mtime) / 86400
        if age_days < 7:
            return pd.read_csv(CONSTITUENTS_FILE)
    html = requests.get(
        "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies",
        headers={"User-Agent": "Mozilla/5.0 borsvarsel-script"},
        timeout=20,
    ).text
    df = pd.read_html(io.StringIO(html))[0]
    df = df[["Symbol", "Security", "GICS Sector"]].copy()
    df["Symbol"] = df["Symbol"].str.replace(".", "-", regex=False)  # BRK.B -> BRK-B
    df.to_csv(CONSTITUENTS_FILE, index=False)
    return df


def biggest_losers(n: int = TOP_N) -> pd.DataFrame:
    """Rangerer aksjene i indeksen etter prosentvis fall hittil i dag."""
    cons = get_constituents()
    tickers = cons["Symbol"].tolist()
    data = yf.download(tickers, period="5d", interval="1d", auto_adjust=False,
                       progress=False, group_by="column", threads=True)["Close"]
    data = data.dropna(how="all")
    last, prev = data.iloc[-1], data.iloc[-2]
    chg = ((last / prev - 1) * 100).rename("Endring %")
    out = cons.set_index("Symbol").join(chg).dropna(subset=["Endring %"])
    out["Kurs"] = last
    out = out.sort_values("Endring %").head(n).round({"Endring %": 2, "Kurs": 2})
    return out.reset_index()


def format_losers(df: pd.DataFrame, limit: int = 10) -> str:
    lines = [f"{r.Symbol:<6} {r['Endring %']:>6.2f}%  {r.Security[:22]}" for _, r in df.head(limit).iterrows()]
    return "\n".join(lines)


def save_losers_csv(df: pd.DataFrame) -> Path:
    path = BASE / f"tapere_{datetime.now(NY).strftime('%Y-%m-%d_%H%M')}.csv"
    df.to_csv(path, index=False)
    return path


def check_once() -> None:
    change, last, prev = index_change_pct()
    now = datetime.now().strftime("%H:%M")
    print(f"[{now}] {INDEX_SYMBOL}: {change:+.2f}% (siste {last:.2f}, forrige slutt {prev:.2f})")

    drop = -change
    state = load_state()
    hit = [t for t in THRESHOLDS if drop >= t and t not in state["sent"]]
    if not hit:
        return

    level = max(hit)  # kun ett varsel, for det høyeste nye nivået
    try:
        losers = biggest_losers()
        csv_path = save_losers_csv(losers)
        body_losers = format_losers(losers)
        print(f"Tapsliste lagret: {csv_path}")
    except Exception as e:
        body_losers = f"(Kunne ikke hente tapsliste: {e})"

    message = (
        f"{INDEX_SYMBOL} er ned {change:.2f}% i dag (nivå {level:.0f}%).\n\n"
        f"Største fall:\n{body_losers}"
    )
    priority = "urgent" if level >= 7 else "high" if level >= 5 else "default"
    send_push(f"Boers-varsel: ned {level:.0f}%", message, priority=priority)
    state["sent"] = sorted(set(state["sent"]) | set(hit))  # merk alle nådde nivåer som sendt
    save_state(state)
    print(f"Varsel sendt (nivå {level:.0f}%).")


def main():
    p = argparse.ArgumentParser()
    g = p.add_mutually_exclusive_group()
    g.add_argument("--test", action="store_true", help="send testvarsel")
    g.add_argument("--once", action="store_true", help="sjekk én gang")
    g.add_argument("--loop", action="store_true", help="sjekk jevnlig i børstiden (standard)")
    g.add_argument("--losers", action="store_true", help="vis tapslisten nå")
    p.add_argument("--force", action="store_true", help="med --once: sjekk selv om børsen er stengt")
    args = p.parse_args()

    if args.test:
        send_push("Boers-varsel: test", "Hvis du ser dette, fungerer varslene.", tags="white_check_mark")
        print("Testvarsel sendt.")
        return
    if args.losers:
        df = biggest_losers()
        print(df.to_string(index=False))
        print(f"\nLagret: {save_losers_csv(df)}")
        return
    if args.once:
        if args.force or market_is_open():
            check_once()
        else:
            print("Børsen er stengt, ingenting å sjekke.")
        return

    print(f"Starter overvåking av {INDEX_SYMBOL}. Nivåer: {THRESHOLDS}. Ctrl+C for å stoppe.")
    while True:
        try:
            if market_is_open():
                check_once()
            else:
                print(f"[{datetime.now().strftime('%H:%M')}] Børsen er stengt.")
        except Exception as e:
            print(f"Feil: {e}", file=sys.stderr)
        time.sleep(CHECK_EVERY_SECONDS)


if __name__ == "__main__":
    main()
