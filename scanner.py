import yfinance as yf
import pandas as pd
from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
import datetime

analyzer = SentimentIntensityAnalyzer()

def analyze_one_ticker(ticker: str):
    try:
        stock = yf.Ticker(ticker)
        hist = stock.history(period="6mo", interval="1d")

        if hist.empty:
            return None

        # Basic metrics
        last_price = hist["Close"].iloc[-1]
        change_1d = ((hist["Close"].iloc[-1] / hist["Close"].iloc[-2]) - 1) * 100
        change_5d = ((hist["Close"].iloc[-1] / hist["Close"].iloc[-6]) - 1) * 100 if len(hist) > 6 else 0
        change_1m = ((hist["Close"].iloc[-1] / hist["Close"].iloc[-21]) - 1) * 100 if len(hist) > 21 else 0
        high_52 = hist["High"].max()
        low_52 = hist["Low"].min()

        # Volume comparison
        avg_volume = hist["Volume"].tail(20).mean()
        current_volume = hist["Volume"].iloc[-1]
        volume_ratio = current_volume / avg_volume if avg_volume > 0 else 0

        # Technicals
        ema20 = hist["Close"].ewm(span=20, adjust=False).mean().iloc[-1]
        ema50 = hist["Close"].ewm(span=50, adjust=False).mean().iloc[-1]
        rsi = compute_rsi(hist["Close"], 14)
        macd_signal = compute_macd(hist["Close"])

        # News sentiment
        news = stock.news
        headlines = [n["title"] for n in news[:5]] if news else []
        sentiment = (
            sum(analyzer.polarity_scores(title)["compound"] for title in headlines) / len(headlines)
            if headlines else 0
        )

        rec = (
            "CALL" if last_price > ema20 > ema50 and macd_signal > 0 and rsi < 70
            else "PUT" if last_price < ema20 < ema50 and macd_signal < 0 and rsi > 30
            else "HOLD"
        )

        return {
            "ticker": ticker.upper(),
            "rec": rec,
            "last_price": last_price,
            "change_1d": change_1d,
            "change_5d": change_5d,
            "change_1m": change_1m,
            "high_52": high_52,
            "low_52": low_52,
            "volume_ratio": volume_ratio,
            "rsi": rsi,
            "macd": macd_signal,
            "sentiment": sentiment,
        }

    except Exception as e:
        print(f"Error analyzing {ticker}: {e}")
        return None


def compute_rsi(series, period=14):
    delta = series.diff(1).dropna()
    gain = delta.where(delta > 0, 0)
    loss = -delta.where(delta < 0, 0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss
    rsi = 100 - (100 / (1 + rs))
    return round(rsi.iloc[-1], 2)


def compute_macd(series):
    exp1 = series.ewm(span=12, adjust=False).mean()
    exp2 = series.ewm(span=26, adjust=False).mean()
    macd = exp1 - exp2
    signal = macd.ewm(span=9, adjust=False).mean()
    return round(macd.iloc[-1] - signal.iloc[-1], 2)


def earnings_watch_text(days_ahead=7):
    """Return tickers with earnings within ±days_ahead."""
    today = datetime.date.today()
    upcoming = []
    for t in ["AAPL", "MSFT", "NVDA", "TSLA", "AMZN", "META", "GOOGL", "JPM", "AMD"]:
        try:
            stock = yf.Ticker(t)
            cal = stock.calendar
            if "Earnings Date" in cal:
                date = pd.to_datetime(cal.loc["Earnings Date"][0]).date()
                if abs((date - today).days) <= days_ahead:
                    upcoming.append((t, date))
        except Exception:
            pass
    if not upcoming:
        return "No earnings within ±7 days."
    lines = [f"**{t}** → {d}" for t, d in upcoming]
    return "\n".join(lines)
