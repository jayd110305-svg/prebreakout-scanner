# scanner.py
# Pre-breakout + threshold scanner with Hot List
# Runs scheduled (every 5 minutes) under GitHub Actions.

import os, time, json, requests, traceback
import yfinance as yf
from datetime import datetime, timedelta

# ---------------- CONFIG ----------------
MAX_TICKERS = 1000
BATCH_SIZE = 50
RATE_SLEEP = 1.0 # seconds between requests (tune down if you have paid API)
STATE_FILE = "state.json"

# Leading indicator settings
GAP_THRESHOLD = 3.0 # percent gap up vs yesterday close
VOLUME_MULTIPLIER = 2.0 # today volume > multiplier * 30-day avg
BREAKOUT_LOOKBACK = 20 # days to compute recent high
SENTIMENT_POSITIVE = 0.2 # minimal bullish sentiment ratio (0-1)

# Percent thresholds (alerts when current intraday change ≥ any of these)
THRESHOLDS = [5, 10, 20]

# News window (days) for headlines
NEWS_WINDOW_DAYS = 2

# ---------------- ENV (from GitHub secrets) ----------------
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "").strip()
DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "").strip()

if not FINNHUB_API_KEY or not DISCORD_WEBHOOK_URL:
raise SystemExit("Missing FINNHUB_API_KEY or DISCORD_WEBHOOK_URL in environment variables.")

# ---------------- Helpers ----------------
def now_ts():
return int(time.time())

def load_state():
if os.path.exists(STATE_FILE):
try:
return json.load(open(STATE_FILE, "r"))
except Exception:
return {}
return {}

def save_state(state):
try:
with open(STATE_FILE, "w") as f:
json.dump(state, f, indent=2)
except Exception:
traceback.print_exc()

def get_tickers(state):
tickers = state.get("tickers")
fetched_at = state.get("tickers_fetched_at", 0)
if tickers and (now_ts() - fetched_at) < 24 * 3600:
return tickers
url = f"https://finnhub.io/api/v1/stock/symbol?exchange=US&token={FINNHUB_API_KEY}"
r = requests.get(url, timeout=20)
r.raise_for_status()
data = r.json()
extracted = []
for s in data:
sym = s.get("symbol")
if not sym or not isinstance(sym, str):
continue
# basic sanitation: skip weird tickers with spaces/slashes/dots
su = sym.strip().upper()
if " " in su or "/" in su or "." in su:
continue
extracted.append(su)
if not extracted:
raise SystemExit("No tickers returned from Finnhub symbol endpoint.")
tickers = extracted[:MAX_TICKERS]
state["tickers"] = tickers
state["tickers_fetched_at"] = now_ts()
save_state(state)
return tickers

def batches_for_run(tickers):
if not tickers:
return [], 0, 0
slot = int(time.time() // 300) # 5-minute window slot
batches = [tickers[i:i+BATCH_SIZE] for i in range(0, len(tickers), BATCH_SIZE)]
idx = slot % len(batches)
return batches[idx], idx, len(batches)

def fetch_news_headlines(ticker):
to_date = datetime.utcnow().date()
from_date = (datetime.utcnow() - timedelta(days=NEWS_WINDOW_DAYS)).date()
url = f"https://finnhub.io/api/v1/company-news?symbol={ticker}&from={from_date}&to={to_date}&token={FINNHUB_API_KEY}"
try:
r = requests.get(url, timeout=8)
if r.status_code != 200:
return []
data = r.json()
if not isinstance(data, list):
return []
return data[:3]
except Exception:
return []

def fetch_news_sentiment(ticker):
# Finnhub news-sentiment endpoint (may be limited on free tier)
url = f"https://finnhub.io/api/v1/news-sentiment?symbol={ticker}&token={FINNHUB_API_KEY}"
try:
r = requests.get(url, timeout=8)
if r.status_code != 200:
return None
data = r.json()
# Try common keys: 'sentiment' -> 'bullishPercent' sometimes present
s = data.get("sentiment") if isinstance(data, dict) else None
if isinstance(s, dict):
bp = s.get("bullishPercent")
if bp is not None:
try:
return float(bp) / 100.0
except:
pass
# fallback: return None if not present
return None
except Exception:
return None

def send_discord(subject, body):
payload = {"content": f"**{subject}**\n```{body[:1900]}```"}
try:
r = requests.post(DISCORD_WEBHOOK_URL, json=payload, timeout=10)
print(f"[Discord] {r.status_code} - {subject}")
if r.status_code not in (200, 204):
print("Discord response:", r.text[:300])
return r.status_code in (200, 204)
except Exception:
print("Discord send exception:")
traceback.print_exc()
return False

# ---------------- Scanning logic ----------------
def analyze_ticker(ticker, alerts_sent):
"""
Returns dict of findings or None.
findings contains keys:
- 'type': 'prebreak' or 'threshold'
- 'triggers': list of trigger descriptions
- 'price', 'change_pct', 'vol', 'avg_vol'
"""
try:
# Use yfinance daily history 1 month
t = yf.Ticker(ticker)
hist = t.history(period="1mo", interval="1d", auto_adjust=False)
if hist is None or hist.empty or len(hist) < 3:
return None

today = hist.iloc[-1]
yesterday = hist.iloc[-2]
price = float(today.get("Close", 0) or 0.0)
prev_close = float(yesterday.get("Close", 0) or 0.0)
open_price = float(today.get("Open", price) or price)
vol_today = int(today.get("Volume", 0) or 0)

if prev_close <= 0:
return None

change_pct = (price - prev_close) / prev_close * 100.0

# average volume: use previous 30 calendar days if available
vol_series = hist["Volume"].dropna()
avg_vol = int(vol_series.tail(30).mean()) if len(vol_series) >= 5 else int(vol_series.mean() if len(vol_series) else 0)

unusual_vol = avg_vol > 0 and (vol_today > (VOLUME_MULTIPLIER * avg_vol))

# breakout check: price > recent N-day high (exclude today when computing)
high20 = None
if len(hist) >= BREAKOUT_LOOKBACK + 1:
high20 = float(hist["High"].shift(1).tail(BREAKOUT_LOOKBACK).max())
breakout = high20 is not None and (price > high20)

# news sentiment (if available)
sentiment = fetch_news_sentiment(ticker)
pos_news = sentiment is not None and sentiment >= SENTIMENT_POSITIVE

# build triggers
triggers = []
if (open_price - prev_close) / prev_close * 100.0 >= GAP_THRESHOLD:
triggers.append(f"Gap +{(open_price - prev_close) / prev_close * 100.0:.1f}%")
if unusual_vol:
triggers.append(f"UnusualVol {vol_today:,} (avg {avg_vol:,})")
if breakout and high20 is not None:
triggers.append(f"Breakout 20d > {high20:.2f}")
if pos_news:
triggers.append(f"PositiveSentiment {sentiment:.2f}")

# Check percent thresholds that haven't been alerted for this ticker
new_thresholds = []
prev_thresholds = set(alerts_sent.get(ticker, {}).get("thresholds", []))
for th in THRESHOLDS:
if change_pct >= th and th not in prev_thresholds:
new_thresholds.append(th)

result = {
"ticker": ticker,
"price": price,
"change_pct": change_pct,
"vol": vol_today,
"avg_vol": avg_vol,
"triggers": triggers,
"new_thresholds": new_thresholds,
"pos_news": pos_news,
"sentiment": sentiment
}

# decide whether to return signals:
# - threshold signal if any new_thresholds exist
# - prebreak signal if len(triggers) >= 2 and prebreak not previously alerted
prebreak_already = alerts_sent.get(ticker, {}).get("prebreak", False)
prebreak_hit = (len(triggers) >= 2) and (not prebreak_already)

if new_thresholds or prebreak_hit:
result["prebreak_hit"] = prebreak_hit
return result
return None

except Exception:
print(f"[{ticker}] analyze exception:")
traceback.print_exc()
return None

# ---------------- Main ----------------
def main():
state = load_state()
alerts_sent = state.get("alerts_sent", {}) # mapping ticker -> {"thresholds":[...], "prebreak":bool}
hot_list = set(state.get("hot_list", []))

tickers = get_tickers(state)
batch, idx, total = batches_for_run(tickers)
if not batch:
print("No batch to scan.")
return

# Scan both current batch + hot list
scan_set = list(dict.fromkeys(batch + list(hot_list))) # preserve order, avoid duplicates
print(f"{datetime.utcnow().isoformat()} | Scanning {len(scan_set)} tickers (batch {idx+1}/{total}), hotlist size={len(hot_list)}")

alerts_this_run = []

for ticker in scan_set:
try:
res = analyze_ticker(ticker, alerts_sent)
# short sleep to avoid bursting API (Tune RATE_SLEEP based on your limits)
time.sleep(RATE_SLEEP)
if not res:
continue

# handle threshold alerts
if res["new_thresholds"]:
# record thresholds in state
rec = alerts_sent.setdefault(ticker, {})
rec_thresholds = set(rec.get("thresholds", []))
for th in res["new_thresholds"]:
rec_thresholds.add(th)
subject = f"🚀 {ticker} +{res['change_pct']:.1f}% | Threshold +{th}%"
news = fetch_news_headlines(ticker)
headlines = "\n".join([f"- {n.get('headline','')[:200]} ({n.get('source','')})" for n in news]) or "No recent news."
body = (
f"Price: {res['price']:.2f} (Prev close → change {res['change_pct']:.1f}%)\n"
f"Volume: {res['vol']:,} (avg {res['avg_vol']:,})\n\n"
f"Recent News:\n{headlines}\n\n"
f"Time (UTC): {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}\n"
"⚠️ Not financial advice."
)
send_discord(subject, body)
alerts_this_run.append((ticker, f"threshold +{th}%"))
rec["thresholds"] = sorted(list(rec_thresholds))
alerts_sent[ticker] = rec
# add to hot list to monitor continuously
hot_list.add(ticker)

# handle pre-breakout alerts
if res.get("prebreak_hit"):
rec = alerts_sent.setdefault(ticker, {})
if not rec.get("prebreak"):
rec["prebreak"] = True
subject = f"⚡ PRE-BREAKOUT: {ticker} — {', '.join(res['triggers'])} | {res['change_pct']:.1f}%"
news = fetch_news_headlines(ticker)
headlines = "\n".join([f"- {n.get('headline','')[:200]} ({n.get('source','')})" for n in news]) or "No recent news."
body = (
f"Price: {res['price']:.2f} (change {res['change_pct']:.1f}%)\n"
f"Triggers: {', '.join(res['triggers'])}\n"
f"Volume: {res['vol']:,} (avg {res['avg_vol']:,})\n\n"
f"Recent News:\n{headlines}\n\n"
f"Time (UTC): {datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')}\n"
"⚠️ Possible pre-breakout signal — verify before trading."
)
send_discord(subject, body)
alerts_this_run.append((ticker, "prebreak"))
hot_list.add(ticker)

except Exception:
print(f"Exception during scan of {ticker}:")
traceback.print_exc()

# persist state for next run
state["alerts_sent"] = alerts_sent
state["hot_list"] = list(hot_list)
state["tickers"] = tickers
state["tickers_fetched_at"] = state.get("tickers_fetched_at", now_ts())
state["last_run"] = now_ts()
save_state(state)

print(f"Done. Alerts this run: {len(alerts_this_run)}")
if alerts_this_run:
for a in alerts_this_run:
print("Alerted:", a)

if __name__ == "__main__":
main()
