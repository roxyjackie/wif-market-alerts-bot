import os
import logging
import requests
import yfinance as yf
import discord
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from datetime import datetime, date
import pytz


# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Config ───────────────────────────────────────────────────────────────────
DISCORD_BOT_TOKEN          = os.environ["DISCORD_BOT_TOKEN"]
UNUSUAL_WHALES_API_KEY     = os.environ.get("UNUSUAL_WHALES_API_KEY", "")

DAILY_MOVERS_CHANNEL_ID    = int(os.environ["DAILY_MOVERS_CHANNEL_ID"])
OPTIONS_FLOW_CHANNEL_ID    = int(os.environ["OPTIONS_FLOW_CHANNEL_ID"])
HIGH_CONVICTION_CHANNEL_ID = int(os.environ["HIGH_CONVICTION_CHANNEL_ID"])
DARK_POOL_CHANNEL_ID       = int(os.environ["DARK_POOL_CHANNEL_ID"])
MARKET_NEWS_CHANNEL_ID     = int(os.environ["MARKET_NEWS_CHANNEL_ID"])

ET = pytz.timezone("America/New_York")

UW_BASE = "https://api.unusualwhales.com/api"
UW_HEADERS = {"Authorization": f"Bearer {UNUSUAL_WHALES_API_KEY}", "Accept": "application/json"}

# Watchlist for high-conviction scanner
WATCHLIST = [
    "SPY", "QQQ", "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA",
    "AMD", "COIN", "PLTR", "SOFI", "MARA", "RIOT", "JPM", "GS", "BAC",
]

# ── Bot setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
scheduler = AsyncIOScheduler(timezone=ET)


# ── Helpers ──────────────────────────────────────────────────────────────────
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


# ── Task: Daily Movers (9:31 AM ET, market open) ─────────────────────────────
async def post_daily_movers():
    channel = bot.get_channel(DAILY_MOVERS_CHANNEL_ID)
    if not channel:
        return

    now = datetime.now(ET).strftime("%B %d, %Y")
    gainers, losers = [], []

    for ticker in WATCHLIST:
        try:
            info = yf.Ticker(ticker).fast_info
            change = getattr(info, "three_month_change", None)
            prev_close = getattr(info, "previous_close", None)
            last = getattr(info, "last_price", None)
            if prev_close and last:
                change = (last - prev_close) / prev_close * 100
                gainers.append((ticker, last, change)) if change > 0 else losers.append((ticker, last, change))
        except Exception as e:
            log.debug(f"yfinance error {ticker}: {e}")

    gainers.sort(key=lambda x: x[2], reverse=True)
    losers.sort(key=lambda x: x[2])

    embed = discord.Embed(
        title=f"📊 Daily Movers — {now}",
        color=0x00C851,
        timestamp=datetime.now(ET),
    )

    if gainers:
        top = gainers[:5]
        embed.add_field(
            name="🟢 Top Gainers",
            value="\n".join(f"**{t}** — {usd(p)} ({pct(c)})" for t, p, c in top),
            inline=True,
        )
    if losers:
        bot5 = losers[:5]
        embed.add_field(
            name="🔴 Top Losers",
            value="\n".join(f"**{t}** — {usd(p)} ({pct(c)})" for t, p, c in bot5),
            inline=True,
        )

    embed.set_footer(text="WIF Market Alerts • Data via yfinance")
    await channel.send(embed=embed)
    log.info("Posted daily movers")


# ── Task: Options Flow (every 30 min, 9:30–4:00 ET) ──────────────────────────
async def post_options_flow():
    channel = bot.get_channel(OPTIONS_FLOW_CHANNEL_ID)
    if not channel:
        return

    data = uw_get("/options/flow", params={"limit": 10, "order": "premium", "is_unusual": "true"})
    if not data:
        log.info("No options flow data (API key missing or error)")
        return

    flows = data.get("data", [])
    if not flows:
        return

    embed = discord.Embed(
        title="🌊 Unusual Options Flow",
        color=0x7B68EE,
        timestamp=datetime.now(ET),
    )

    lines = []
    for f in flows[:8]:
        ticker  = f.get("ticker", "?")
        side    = "📈 CALL" if str(f.get("put_call", "")).upper() == "CALL" else "📉 PUT"
        strike  = f.get("strike", "?")
        expiry  = f.get("expiry", "?")
        premium = fmt_large(f.get("premium", 0))
        sentiment = f.get("sentiment", "").upper()
        lines.append(f"**{ticker}** {side} ${strike} exp {expiry} — {premium} [{sentiment}]")

    embed.description = "\n".join(lines) if lines else "No unusual flow right now."
    embed.set_footer(text="WIF Market Alerts • Data via Unusual Whales")
    await channel.send(embed=embed)
    log.info("Posted options flow")


# ── Task: High Conviction Alerts (10:00 AM & 2:00 PM ET) ─────────────────────
async def post_high_conviction():
    channel = bot.get_channel(HIGH_CONVICTION_CHANNEL_ID)
    if not channel:
        return

    alerts = []
    for ticker in WATCHLIST:
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period="5d", interval="1d")
            if hist.empty or len(hist) < 2:
                continue
            today_vol  = hist["Volume"].iloc[-1]
            avg_vol    = hist["Volume"].iloc[:-1].mean()
            today_chg  = (hist["Close"].iloc[-1] - hist["Close"].iloc[-2]) / hist["Close"].iloc[-2] * 100
            price      = hist["Close"].iloc[-1]
            vol_ratio  = today_vol / avg_vol if avg_vol else 0

            # High conviction = >2x avg volume AND >2% move
            if vol_ratio >= 2.0 and abs(today_chg) >= 2.0:
                direction = "🚀" if today_chg > 0 else "💣"
                alerts.append((ticker, price, today_chg, vol_ratio, direction))
        except Exception as e:
            log.debug(f"HC scan error {ticker}: {e}")

    if not alerts:
        return  # Don't post if nothing qualifies

    alerts.sort(key=lambda x: abs(x[2]), reverse=True)

    embed = discord.Embed(
        title="⚡ High Conviction Alerts",
        description="Stocks with unusual volume (2×+ avg) + significant price move",
        color=0xFF6B35,
        timestamp=datetime.now(ET),
    )

    for ticker, price, chg, vol_ratio, icon in alerts[:6]:
        embed.add_field(
            name=f"{icon} {ticker}",
            value=f"Price: {usd(price)}\nMove: {pct(chg)}\nVol ratio: {vol_ratio:.1f}×",
            inline=True,
        )

    embed.set_footer(text="WIF Market Alerts • Scan via yfinance")
    await channel.send(embed=embed)
    log.info(f"Posted {len(alerts)} high conviction alerts")


# ── Task: Dark Pool Activity (11:00 AM & 3:00 PM ET) ─────────────────────────
async def post_dark_pool():
    channel = bot.get_channel(DARK_POOL_CHANNEL_ID)
    if not channel:
        return

    data = uw_get("/darkpool/recent", params={"limit": 10})
    if not data:
        log.info("No dark pool data (API key missing or error)")
        return

    trades = data.get("data", [])
    if not trades:
        return

    embed = discord.Embed(
        title="🌑 Dark Pool Activity",
        color=0x2C2C54,
        timestamp=datetime.now(ET),
    )

    lines = []
    for tr in trades[:8]:
        ticker = tr.get("ticker", "?")
        size   = tr.get("size", 0)
        price  = tr.get("price", 0)
        value  = fmt_large(float(size) * float(price)) if size and price else "?"
        lines.append(f"**{ticker}** — {int(size):,} shares @ {usd(price)} = **{value}**")

    embed.description = "\n".join(lines) if lines else "No significant dark pool prints."
    embed.set_footer(text="WIF Market Alerts • Data via Unusual Whales")
    await channel.send(embed=embed)
    log.info("Posted dark pool activity")


# ── Task: Market News (8:00 AM, 12:00 PM, 4:15 PM ET) ────────────────────────
async def post_market_news():
    channel = bot.get_channel(MARKET_NEWS_CHANNEL_ID)
    if not channel:
        return

    data = uw_get("/news/headlines", params={"limit": 6})

    embed = discord.Embed(
        title="📰 Market News",
        color=0x1DA1F2,
        timestamp=datetime.now(ET),
    )

    if data:
        articles = data.get("data", [])
        lines = []
        for a in articles[:6]:
            headline = a.get("title", "")
            url      = a.get("url", "")
            src      = a.get("source", "")
            if headline and url:
                lines.append(f"[{headline}]({url}) — *{src}*")
        embed.description = "\n\n".join(lines) if lines else "Check financial news sources for the latest."
    else:
        # Fallback: post SPY/QQQ summary when no API key
        try:
            spy  = yf.Ticker("SPY").fast_info
            qqq  = yf.Ticker("QQQ").fast_info
            spy_p  = getattr(spy, "last_price", "?")
            qqq_p  = getattr(qqq, "last_price", "?")
            spy_pc = getattr(spy, "previous_close", None)
            qqq_pc = getattr(qqq, "previous_close", None)
            spy_chg = pct((spy_p - spy_pc) / spy_pc * 100) if spy_pc else "?"
            qqq_chg = pct((qqq_p - qqq_pc) / qqq_pc * 100) if qqq_pc else "?"
            embed.add_field(name="SPY", value=f"{usd(spy_p)} ({spy_chg})", inline=True)
            embed.add_field(name="QQQ", value=f"{usd(qqq_p)} ({qqq_chg})", inline=True)
            embed.description = "Market snapshot (add Unusual Whales key for full news feed)"
        except Exception:
            embed.description = "Market data temporarily unavailable."

    embed.set_footer(text="WIF Market Alerts")
    await channel.send(embed=embed)
    log.info("Posted market news")


# ── Bot events ────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    log.info(f"Logged in as {bot.user} (ID: {bot.user.id})")

    # Market News:        8:00 AM, 12:00 PM, 4:15 PM ET
    scheduler.add_job(post_market_news,    CronTrigger(hour="8,12",  minute="0",  timezone=ET))
    scheduler.add_job(post_market_news,    CronTrigger(hour="16",    minute="15", timezone=ET))

    # Daily Movers:       9:31 AM ET (just after open)
    scheduler.add_job(post_daily_movers,   CronTrigger(hour="9",     minute="31", timezone=ET))

    # Options Flow:       every 30 min from 9:30 AM to 4:00 PM ET, weekdays
    scheduler.add_job(post_options_flow,   CronTrigger(
        day_of_week="mon-fri", hour="9-15", minute="30,0", timezone=ET
    ))

    # High Conviction:    10:00 AM and 2:00 PM ET, weekdays
    scheduler.add_job(post_high_conviction, CronTrigger(
        day_of_week="mon-fri", hour="10,14", minute="0", timezone=ET
    ))

    # Dark Pool:          11:00 AM and 3:00 PM ET, weekdays
    scheduler.add_job(post_dark_pool,      CronTrigger(
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
    msg = f"✅ **WIF Market Alerts is running**\n{len(jobs)} scheduled jobs active.\n"
    msg += "**Manual commands:** `!movers` `!flow` `!darkpool` `!hc` `!news`"
    await ctx.send(msg)


# ── Run ───────────────────────────────────────────────────────────────────────
bot.run(DISCORD_BOT_TOKEN)
