import os
import logging
import requests
import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, date
import pytz
import time

# ââ Logging ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ââ Config âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]
UNUSUAL_WHALES_API_KEY = os.environ.get("UNUSUAL_WHALES_API_KEY", "")
FINNHUB_API_KEY = os.environ.get("FINNHUB_API_KEY", "")

DAILY_MOVERS_CHANNEL_ID = int(os.environ["DAILY_MOVERS_CHANNEL_ID"])
OPTIONS_FLOW_CHANNEL_ID = int(os.environ["OPTIONS_FLOW_CHANNEL_ID"])
HIGH_CONVICTION_CHANNEL_ID = int(os.environ["HIGH_CONVICTION_CHANNEL_ID"])
DARK_POOL_CHANNEL_ID = int(os.environ["DARK_POOL_CHANNEL_ID"])
MARKET_NEWS_CHANNEL_ID = int(os.environ["MARKET_NEWS_CHANNEL_ID"])

ET = pytz.timezone("America/New_York")

UW_BASE = "https://api.unusualwhales.com/api"
UW_HEADERS = {"Authorization": f"Bearer {UNUSUAL_WHALES_API_KEY}", "Accept": "application/json"}

FINNHUB_BASE = "https://finnhub.io/api/v1"

WATCHLIST = [
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    "AMD", "COIN", "PLTR", "SOFI", "MARA", "RIOT", "JPM", "GS", "BAC",
]

# ââ Bot setup âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=ET)

# ââ Helpers ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
def uw_get(path: str, params: dict = None):
    """Call Unusual Whales API. Returns JSON or None on error."""
    if not UNUSUAL_WHALES_API_KEY:
        return None
    try:
        r = requests.get(f"{UW_BASE}{path}", headers=UW_HEADERS, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"UW API error {path}: {e}")
        return None

def finnhub_quote(symbol: str):
    """Get real-time quote from Finnhub. Returns dict or None.
    Keys: c (current price), d (change $), dp (change %), pc (prev close)
    """
    if not FINNHUB_API_KEY:
        return None
    try:
        r = requests.get(
            f"{FINNHUB_BASE}/quote",
            params={"symbol": symbol, "token": FINNHUB_API_KEY},
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if not data or data.get("c", 0) == 0:
            return None
        return data
    except Exception as e:
        log.debug(f"Finnhub quote error {symbol}: {e}")
        return None

def finnhub_candles(symbol: str, days: int = 7):
    """Get daily OHLCV candles from Finnhub. Returns list of dicts or []."""
    if not FINNHUB_API_KEY:
        return []
    try:
        now_ts = int(time.time())
        from_ts = now_ts - (days * 2 * 86400)  # 2x buffer for weekends
        r = requests.get(
            f"{FINNHUB_BASE}/stock/candle",
            params={
                "symbol": symbol,
                "resolution": "D",
                "from": from_ts,
                "to": now_ts,
                "token": FINNHUB_API_KEY,
            },
            timeout=10,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("s") != "ok" or not data.get("c"):
            return []
        return [
            {"c": c, "v": v, "t": t}
            for c, v, t in zip(data["c"], data["v"], data["t"])
        ]
    except Exception as e:
        log.debug(f"Finnhub candle error {symbol}: {e}")
        return []

def pct(val):
    try:
        return f"{float(val):+.2f}%"
    except Exception:
        return str(val)

def usd(val):
    try:
        v = float(val)
        return f"${v:,.0f}" if v >= 1000 else f"${v:.2f}"
    except Exception:
        return str(val)

def fmt_large(val):
    try:
        v = float(val)
        if v >= 1_000_000_000:
            return f"${v/1_000_000_000:.1f}B"
        if v >= 1_000_000:
            return f"${v/1_000_000:.1f}M"
        return f"${v:,.0f}"
    except Exception:
        return str(val)

# ââ Task: Daily Movers (9:31 AM ET) ââââââââââââââââââââââââââââââââââââââââââ
async def post_daily_movers():
    channel = bot.get_channel(DAILY_MOVERS_CHANNEL_ID)
    if not channel:
        return

    now = datetime.now(ET).strftime("%B %d, %Y")

    if not FINNHUB_API_KEY:
        embed = discord.Embed(
            title=f"ð Daily Movers â {now}",
            description=(
                "â ï¸ **Finnhub API key not configured.**\n"
                "Sign up free at [finnhub.io](https://finnhub.io) and add "
                "`FINNHUB_API_KEY` to Railway environment variables."
            ),
            color=0x00C851,
        )
        await channel.send(embed=embed)
        return

    gainers, losers = [], []
    for ticker in WATCHLIST:
        q = finnhub_quote(ticker)
        if not q:
            continue
        price = q["c"]
        change_pct = q["dp"]
        if change_pct > 0:
            gainers.append((ticker, price, change_pct))
        elif change_pct < 0:
            losers.append((ticker, price, change_pct))

    gainers.sort(key=lambda x: x[2], reverse=True)
    losers.sort(key=lambda x: x[2])

    embed = discord.Embed(
        title=f"ð Daily Movers â {now}",
        color=0x00C851,
        timestamp=datetime.now(ET),
    )

    if gainers:
        embed.add_field(
            name="ð¢ Top Gainers",
            value="\n".join(f"**{t}** â {usd(p)} ({pct(c)})" for t, p, c in gainers[:5]),
            inline=True,
        )
    if losers:
        embed.add_field(
            name="ð´ Top Losers",
            value="\n".join(f"**{t}** â {usd(p)} ({pct(c)})" for t, p, c in losers[:5]),
            inline=True,
        )
    if not gainers and not losers:
        embed.description = "â ï¸ No market data right now. Markets may be closed."

    embed.set_footer(text="WIF Market Alerts â¢ Data via Finnhub")
    await channel.send(embed=embed)
    log.info("Posted daily movers")

# ââ Task: Options Flow (every 30 min, 9:30â4:00 ET) ââââââââââââââââââââââââââ
async def post_options_flow():
    channel = bot.get_channel(OPTIONS_FLOW_CHANNEL_ID)
    if not channel:
        return

    if not UNUSUAL_WHALES_API_KEY:
        embed = discord.Embed(
            title="ð Unusual Options Flow",
            description=(
                "â ï¸ **Unusual Whales API key not configured.**\n"
                "This feature requires an [Unusual Whales](https://unusualwhales.com) "
                "subscription. Add your `UNUSUAL_WHALES_API_KEY` in Railway environment variables."
            ),
            color=0x7B68EE,
        )
        await channel.send(embed=embed)
        log.info("No options flow data (API key missing)")
        return

    data = uw_get("/options/flow", params={"limit": 10, "order": "premium", "is_unusual": "true"})
    if not data:
        log.info("No options flow data (API error)")
        return

    flows = data.get("data", [])
    if not flows:
        return

    embed = discord.Embed(
        title="ð Unusual Options Flow",
        color=0x7B68EE,
        timestamp=datetime.now(ET),
    )
    lines = []
    for f in flows[:8]:
        ticker = f.get("ticker", "?")
        side = "ð CALL" if str(f.get("put_call", "")).upper() == "CALL" else "ð PUT"
        strike = f.get("strike", "?")
        expiry = f.get("expiry", "?")
        premium = fmt_large(f.get("premium", 0))
        sentiment = f.get("sentiment", "").upper()
        lines.append(f"**{ticker}** {side} ${strike} exp {expiry} â {premium} [{sentiment}]")

    embed.description = "\n".join(lines) if lines else "No unusual flow right now."
    embed.set_footer(text="WIF Market Alerts â¢ Data via Unusual Whales")
    await channel.send(embed=embed)
    log.info("Posted options flow")

# ââ Task: High Conviction Alerts (10:00 AM & 2:00 PM ET) âââââââââââââââââââââ
async def post_high_conviction():
    channel = bot.get_channel(HIGH_CONVICTION_CHANNEL_ID)
    if not channel:
        return

    if not FINNHUB_API_KEY:
        embed = discord.Embed(
            title="â¡ High Conviction Alerts",
            description=(
                "â ï¸ **Finnhub API key not configured.**\n"
                "Sign up free at [finnhub.io](https://finnhub.io) and add "
                "`FINNHUB_API_KEY` to Railway environment variables."
            ),
            color=0xFF6B35,
        )
        await channel.send(embed=embed)
        return

    alerts = []
    for ticker in WATCHLIST:
        try:
            candles = finnhub_candles(ticker, days=7)
            if len(candles) < 2:
                continue
            candles.sort(key=lambda x: x["t"])
            today = candles[-1]
            prev_days = candles[:-1]
            avg_vol = sum(c["v"] for c in prev_days) / len(prev_days) if prev_days else 0
            prev_close = candles[-2]["c"]
            today_close = today["c"]
            today_vol = today["v"]
            if not prev_close or not today_close:
                continue
            change_pct = (today_close - prev_close) / prev_close * 100
            vol_ratio = today_vol / avg_vol if avg_vol else 0
            if vol_ratio >= 2.0 and abs(change_pct) >= 2.0:
                direction = "ð" if change_pct > 0 else "ð£"
                alerts.append((ticker, today_close, change_pct, vol_ratio, direction))
        except Exception as e:
            log.debug(f"HC scan error {ticker}: {e}")

    embed = discord.Embed(
        title="â¡ High Conviction Alerts",
        color=0xFF6B35,
        timestamp=datetime.now(ET),
    )
    if not alerts:
        embed.description = "No stocks meeting high conviction criteria right now (needs 2Ã volume + 2% move)."
    else:
        embed.description = "Stocks with unusual volume (2Ã+ avg) + significant price move"
        alerts.sort(key=lambda x: abs(x[2]), reverse=True)
        for ticker, price, chg, vol_ratio, icon in alerts[:6]:
            embed.add_field(
                name=f"{icon} {ticker}",
                value=f"Price: {usd(price)}\nMove: {pct(chg)}\nVol ratio: {vol_ratio:.1f}Ã",
                inline=True,
            )
        log.info(f"Posted {len(alerts)} high conviction alerts")

    embed.set_footer(text="WIF Market Alerts â¢ Data via Finnhub")
    await channel.send(embed=embed)

# ââ Task: Dark Pool Activity (11:00 AM & 3:00 PM ET) âââââââââââââââââââââââââ
async def post_dark_pool():
    channel = bot.get_channel(DARK_POOL_CHANNEL_ID)
    if not channel:
        return

    if not UNUSUAL_WHALES_API_KEY:
        embed = discord.Embed(
            title="ð Dark Pool Activity",
            description=(
                "â ï¸ **Unusual Whales API key not configured.**\n"
                "This feature requires an [Unusual Whales](https://unusualwhales.com) "
                "subscription. Add your `UNUSUAL_WHALES_API_KEY` in Railway environment variables."
            ),
            color=0x2C2C54,
        )
        await channel.send(embed=embed)
        log.info("No dark pool data (API key missing)")
        return

    data = uw_get("/darkpool/recent", params={"limit": 10})
    if not data:
        log.info("No dark pool data (API error)")
        return

    trades = data.get("data", [])
    if not trades:
        return

    embed = discord.Embed(
        title="ð Dark Pool Activity",
        color=0x2C2C54,
        timestamp=datetime.now(ET),
    )
    lines = []
    for tr in trades[:8]:
        ticker = tr.get("ticker", "?")
        size = tr.get("size", 0)
        price = tr.get("price", 0)
        value = fmt_large(float(size) * float(price)) if size and price else "?"
        lines.append(f"**{ticker}** â {int(size):,} shares @ {usd(price)} = **{value}**")

    embed.description = "\n".join(lines) if lines else "No significant dark pool prints."
    embed.set_footer(text="WIF Market Alerts â¢ Data via Unusual Whales")
    await channel.send(embed=embed)
    log.info("Posted dark pool activity")

# ââ Task: Market News (8:00 AM, 12:00 PM, 4:15 PM ET) ââââââââââââââââââââââââ
async def post_market_news():
    channel = bot.get_channel(MARKET_NEWS_CHANNEL_ID)
    if not channel:
        return

    data = uw_get("/news/headlines", params={"limit": 6})

    embed = discord.Embed(
        title="ð° Market News",
        color=0x1DA1F2,
        timestamp=datetime.now(ET),
    )

    if data:
        articles = data.get("data", [])
        lines = []
        for a in articles[:6]:
            headline = a.get("title", "")
            url = a.get("url", "")
            src = a.get("source", "")
            if headline and url:
                lines.append(f"[{headline}]({url}) â *{src}*")
        embed.description = "\n\n".join(lines) if lines else "Check financial news sources for the latest."
    elif FINNHUB_API_KEY:
        try:
            spy = finnhub_quote("SPY")
            qqq = finnhub_quote("QQQ")
            nvda = finnhub_quote("NVDA")
            if spy:
                embed.add_field(name="SPY", value=f"{usd(spy['c'])} ({pct(spy['dp'])})", inline=True)
            if qqq:
                embed.add_field(name="QQQ", value=f"{usd(qqq['c'])} ({pct(qqq['dp'])})", inline=True)
            if nvda:
                embed.add_field(name="NVDA", value=f"{usd(nvda['c'])} ({pct(nvda['dp'])})", inline=True)
            embed.description = "Market snapshot (add Unusual Whales key for full news feed)"
        except Exception:
            embed.description = "Market data temporarily unavailable."
    else:
        embed.description = (
            "â ï¸ Add `FINNHUB_API_KEY` in Railway variables for market data.\n"
            "Optionally add `UNUSUAL_WHALES_API_KEY` for a full news feed."
        )

    embed.set_footer(text="WIF Market Alerts")
    await channel.send(embed=embed)
    log.info("Posted market news")

# ââ Bot events ââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

    scheduler.add_job(post_market_news, CronTrigger(hour="8,12", minute="0", timezone=ET))
    scheduler.add_job(post_market_news, CronTrigger(hour="16", minute="15", timezone=ET))
    scheduler.add_job(post_daily_movers, CronTrigger(hour="9", minute="31", timezone=ET))
    scheduler.add_job(post_options_flow, CronTrigger(
        day_of_week="mon-fri", hour="9-15", minute="30,0", timezone=ET
    ))
    scheduler.add_job(post_high_conviction, CronTrigger(
        day_of_week="mon-fri", hour="10,14", minute="0", timezone=ET
    ))
    scheduler.add_job(post_dark_pool, CronTrigger(
        day_of_week="mon-fri", hour="11,15", minute="0", timezone=ET
    ))

    scheduler.start()
    log.info("Scheduler started. All jobs registered.")

@bot.command(name="movers")
async def cmd_movers(ctx):
    """Manually trigger daily movers post."""
    await post_daily_movers()

@bot.command(name="flow")
async def cmd_flow(ctx):
    """Manually trigger options flow post."""
    await post_options_flow()

@bot.command(name="darkpool")
async def cmd_darkpool(ctx):
    """Manually trigger dark pool post."""
    await post_dark_pool()

@bot.command(name="hc")
async def cmd_hc(ctx):
    """Manually trigger high conviction scan."""
    await post_high_conviction()

@bot.command(name="news")
async def cmd_news(ctx):
    """Manually trigger market news post."""
    await post_market_news()

@bot.command(name="status")
async def cmd_status(ctx):
    """Show bot status."""
    jobs = scheduler.get_jobs()
    finnhub_st = "â Connected" if FINNHUB_API_KEY else "â Missing â add FINNHUB_API_KEY in Railway"
    uw_st = "â Connected" if UNUSUAL_WHALES_API_KEY else "â Missing â optional paid API"
    msg = (
        f"â **WIF Market Alerts is running**\n"
        f"{len(jobs)} scheduled jobs active.\n"
        f"ð Finnhub: {finnhub_st}\n"
        f"ð Unusual Whales: {uw_st}\n"
        f"**Commands:** `!movers` `!flow` `!darkpool` `!hc` `!news`"
    )
    await ctx.send(msg)

# ââ Run âââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââââ
bot.run(DISCORD_BOT_TOKEN)
