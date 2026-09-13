import sqlite3
from datetime import datetime, timezone, timedelta
import aiohttp
import discord
from discord.ext import tasks, commands

# =================CONFIGURATION=================
TOKEN = "YOUR_DISCORD_BOT_TOKEN"
CHANNEL_ID = 123456789012345678  # Replace with target channel ID
ROLE_ID = 987654321098765432     # Replace with role ID to ping (or set to None)
DB_PATH = "gbf_events.db"
# ===============================================

intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)

# --- SQLite Database Helper ---

def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS event_alerts (
                event_name TEXT,
                end_time_utc TEXT,
                alert_type TEXT,
                notified_at TEXT,
                PRIMARY KEY (event_name, end_time_utc, alert_type)
            )
        """)
        conn.commit()

def has_alert_sent(event_name: str, end_time_utc: str, alert_type: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT 1 FROM event_alerts WHERE event_name = ? AND end_time_utc = ? AND alert_type = ?",
            (event_name, end_time_utc, alert_type)
        )
        return cursor.fetchone() is not None

def record_alert(event_name: str, end_time_utc: str, alert_type: str):
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        now_str = datetime.now(timezone.utc).isoformat()
        cursor.execute(
            "INSERT OR IGNORE INTO event_alerts (event_name, end_time_utc, alert_type, notified_at) VALUES (?, ?, ?, ?)",
            (event_name, end_time_utc, alert_type, now_str)
        )
        conn.commit()

def cleanup_old_records():
    """Prune alerts older than 7 days to prevent database bloat."""
    with sqlite3.connect(DB_PATH) as conn:
        cursor = conn.cursor()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d %H:%M:%S")
        cursor.execute("DELETE FROM event_alerts WHERE end_time_utc < ?", (cutoff,))
        conn.commit()

# --- gbf.wiki API Functions ---

IGNORED_KEYWORDS = [
    "Surprise Special Ticket",
    "Star Premium",
    "Daily Free",
    "Campaign Quests Only",
]

def should_skip_event(name: str, category: str | None) -> bool:
    if category in {"Daily", "Rotation", "Gacha", "Archive", "Permanent"}:
        return True
    return any(keyword.lower() in name.lower() for keyword in IGNORED_KEYWORDS)

async def get_wiki_image_url(filename: str) -> str | None:
    if not filename:
        return None
    file_title = filename if filename.startswith("File:") else f"File:{filename}"
    url = "https://gbf.wiki/api.php"
    params = {
        "action": "query",
        "titles": file_title,
        "prop": "imageinfo",
        "iiprop": "url",
        "format": "json"
    }
    headers = {"User-Agent": "GBFEventNotifierBot/1.0 (DiscordBot)"}

    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            async with session.get(url, params=params) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    pages = data.get("query", {}).get("pages", {})
                    for page in pages.values():
                        info = page.get("imageinfo")
                        if info and len(info) > 0:
                            return info[0].get("url")
    except Exception as e:
        print(f"Error resolving image {filename}: {e}")
    return None

async def fetch_events():
    url = "https://gbf.wiki/api.php"
    params = {
        "action": "cargoquery",
        "tables": "events",
        "fields": "name,start,utc_end,image,category",
        "where": (
            "utc_end >= NOW() "
            "AND (category IS NULL OR category NOT IN ('Permanent', 'Archive', 'Rotation')) "
            "AND DATEDIFF(utc_end, start) <= 35"
        ),
        "order_by": "utc_end ASC",
        "limit": "15",
        "format": "json"
    }
    headers = {"User-Agent": "GBFEventNotifierBot/1.0 (DiscordBot)"}

    async with aiohttp.ClientSession(headers=headers) as session:
        async with session.get(url, params=params) as resp:
            if resp.status == 200:
                data = await resp.json()
                return data.get("cargoquery", [])
    return []

# --- Background Task Loop ---

@tasks.loop(minutes=10)
async def check_event_updates():
    channel = bot.get_channel(CHANNEL_ID)
    if not channel:
        return

    cleanup_old_records()
    now = datetime.now(timezone.utc)
    events = await fetch_events()

    for item in events:
        event = item["title"]
        name = event.get("name")
        start_str = event.get("start")
        end_str = event.get("utc_end")
        image_name = event.get("image")
        category = event.get("category")

        if not name or not end_str or not start_str:
            continue

        if should_skip_event(name, category):
            continue

        try:
            start_time = datetime.strptime(start_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
            end_time = datetime.strptime(end_str, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue

        end_ts = int(end_time.timestamp())
        content = f"<@&{ROLE_ID}>" if ROLE_ID else None

        # 1. Event Started Alert (Active within a 2-hour window after launch)
        if start_time <= now < (start_time + timedelta(hours=2)):
            if not has_alert_sent(name, end_str, "START"):
                embed = discord.Embed(
                    title=f"🎉 Event Started: {name}",
                    description=f"**{name}** is now live!\nEnds <t:{end_ts}:R> (<t:{end_ts}:f>).",
                    color=0x2ECC71  # Green
                )
                if image_name:
                    img_url = await get_wiki_image_url(image_name)
                    if img_url:
                        embed.set_image(url=img_url)

                await channel.send(content=content, embed=embed)
                record_alert(name, end_str, "START")

        # 2. Final 2-Hour Notice (Urgent priority over 24h)
        elif now < end_time <= (now + timedelta(hours=2)):
            if not has_alert_sent(name, end_str, "END_2H"):
                embed = discord.Embed(
                    title=f"🚨 FINAL CALL: {name} Ends Soon!",
                    description=(
                        f"**{name}** finishes <t:{end_ts}:R> (<t:{end_ts}:f>)!\n"
                        "Spend your remaining badges, claim pending honors, and clear trophies."
                    ),
                    color=0xED4245  # Crimson Red
                )
                if image_name:
                    img_url = await get_wiki_image_url(image_name)
                    if img_url:
                        embed.set_image(url=img_url)

                await channel.send(content=content, embed=embed)
                record_alert(name, end_str, "END_2H")

        # 3. 24-Hour Notice
        elif now < end_time <= (now + timedelta(hours=24)):
            if not has_alert_sent(name, end_str, "END_24H"):
                embed = discord.Embed(
                    title=f"⚠️ Ending in < 24 Hours: {name}",
                    description=f"**{name}** wraps up <t:{end_ts}:R> (<t:{end_ts}:f>).",
                    color=0xE67E22  # Orange
                )
                if image_name:
                    img_url = await get_wiki_image_url(image_name)
                    if img_url:
                        embed.set_image(url=img_url)

                await channel.send(content=content, embed=embed)
                record_alert(name, end_str, "END_24H")

@check_event_updates.before_loop
async def before_check():
    await bot.wait_until_ready()

@bot.event
async def on_ready():
    init_db()
    print(f"Logged in as {bot.user.name} ({bot.user.id})")
    if not check_event_updates.is_running():
        check_event_updates.start()

bot.run(TOKEN)