import asyncio
import os
import re
import logging
import time
from pathlib import Path
import discord
from discord.ext import commands
from dotenv import load_dotenv
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError
load_dotenv(Path(__file__).resolve().parent / ".env")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DISCORD_CHANNEL_ID = int(os.getenv("DISCORD_CHANNEL_ID", "1560149229907869791"))
RAIN_ROLE_ID = int(os.getenv("RAIN_ROLE_ID", "0"))
BIG_RAIN_ROLE_ID = int(os.getenv("BIG_RAIN_ROLE_ID", "0"))
WEBSITE_URL = "https://donutluck.com"
PROFILE_DIR = Path(__file__).resolve().parent / "donutluck_profile"
POLL_INTERVAL = 0.25
DISCORD_UPDATE_INTERVAL = 1.0
TIMER_XPATH = "/html/body/div/div/div/aside/div/div/div/div/div/div/span/span"
ACTIVE_TIMER_XPATH = "/html/body/div/div/div/aside/div/div/div/div/span/span"
RAIN_STATUS_XPATH = "/html/body/div/div/div/aside/div/div/div/p"
PARTICIPANTS_XPATH = "/html/body/div/div/div/aside/div/div/div/div/span"
POOL_XPATH = "/html/body/div/div/div/aside/div/div/div/div/div/div/span"
RAIN_START_TEXT = "It's about to RAIN!"
BIG_RAIN_THRESHOLD = 1_500_000_000
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("RainMonitor")
intents = discord.Intents.default()
bot = commands.Bot(command_prefix="!", intents=intents)
target_channel = None
dashboard_message = None
browser_context = None
website_page = None
monitor_task = None
latest_data = {"timer": "Loading...", "participants": "0", "pool": "Unknown", "rain_active": False}
def parse_timer(value):
    if not value:
        return None
    value = value.strip()
    match = re.search(r"(\d+):(\d{2})(?::(\d{2}))?", value)
    if match:
        first = int(match.group(1))
        second = int(match.group(2))
        third = match.group(3)
        if third is not None:
            return first * 3600 + second * 60 + int(third)
        return first * 60 + second
    if value.isdigit():
        return int(value)
    return None
def format_timer(value):
    seconds = parse_timer(value)
    if seconds is None:
        return value or "Unknown"
    seconds = max(0, seconds)
    if seconds >= 3600:
        hours = seconds // 3600
        minutes = (seconds % 3600) // 60
        remaining = seconds % 60
        return f"{hours:02d}:{minutes:02d}:{remaining:02d}"
    return f"{seconds // 60:02d}:{seconds % 60:02d}"
def format_pool(value):
    if not value:
        return "Unknown"
    value = re.sub(r"\s+", "", str(value)).replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)([KMB])?(\+)?", value, re.IGNORECASE)
    if not match:
        return value
    number = float(match.group(1))
    suffix = (match.group(2) or "").upper()
    plus = match.group(3) or ""
    if suffix == "M" and number >= 1000:
        number /= 1000
        suffix = "B"
        plus = "+"
    elif suffix == "B" and number >= 1:
        plus = plus or "+"
    return f"{number:g}{suffix}{plus}"
def pool_to_number(value):
    if not value:
        return None
    value = str(value).strip().replace(",", "")
    match = re.search(r"(\d+(?:\.\d+)?)\s*([KMB])?", value, re.IGNORECASE)
    if not match:
        return None
    number = float(match.group(1))
    suffix = (match.group(2) or "").upper()
    multiplier = {"": 1, "K": 1_000, "M": 1_000_000, "B": 1_000_000_000}.get(suffix, 1)
    return number * multiplier
async def read_website_snapshot(page):
    script = r"""
    (selectors) => {
        function xpathElement(path) {
            try {
                return document.evaluate(path, document, null, XPathResult.FIRST_ORDERED_NODE_TYPE, null).singleNodeValue;
            } catch (_) { return null; }
        }
        function cleanText(element) {
            return element ? (element.innerText || element.textContent || "").trim() : "";
        }
        function readVisibleDigits(root) {
            if (!root) return "";
            let result = "";
            for (const child of Array.from(root.children)) {
                const strip = child.querySelector(".absolute");
                if (strip) {
                    try {
                        const transform = getComputedStyle(strip).transform;
                        let translateY = 0;
                        if (transform && transform !== "none") {
                            translateY = new DOMMatrixReadOnly(transform).m42;
                        }
                        const firstRow = strip.firstElementChild;
                        const rowHeight = firstRow ? firstRow.getBoundingClientRect().height : 0;
                        if (rowHeight > 0) {
                            let row = Math.round(Math.abs(translateY) / rowHeight);
                            row = ((row % 10) + 10) % 10;
                            const digit = (9 - row + 10) % 10;
                            result += String(digit);
                            continue;
                        }
                    } catch (_) {}
                }
                const childText = (child.innerText || child.textContent || "").trim();
                if ([".", "M", "B", "K", "+"].includes(childText)) { result += childText; }
            }
            return result;
        }
        const statusElement = xpathElement(selectors.status);
        const status = cleanText(statusElement);
        const rainActive = status.toLowerCase().includes("it's about to rain!");
        const timerElement = xpathElement(rainActive ? selectors.activeTimer : selectors.timer);
        const participantsElement = xpathElement(selectors.participants);
        const poolElement = xpathElement(selectors.pool);
        let pool = "";
        if (poolElement) {
            pool = readVisibleDigits(poolElement);
            if (!pool) { pool = cleanText(poolElement); }
        }
        return {
            timer: cleanText(timerElement),
            status: status,
            participants: cleanText(participantsElement),
            pool: pool
        };
    }
    """
    return await page.evaluate(script, {"timer": TIMER_XPATH, "activeTimer": ACTIVE_TIMER_XPATH, "status": RAIN_STATUS_XPATH, "participants": PARTICIPANTS_XPATH, "pool": POOL_XPATH})
async def website_poll_loop(page):
    start_time = time.time()
    MAX_RUN_DURATION = 11100 
    was_rain_active = False
    while not bot.is_closed():
        if time.time() - start_time > MAX_RUN_DURATION:
            logger.info("3 hours completed. Gracefully shutting down to hand over to the next runner.")
            await bot.close()
            break
        try:
            snapshot = await read_website_snapshot(page)
            is_rain_active = snapshot.get("status", "").lower().find("it's about to rain!") != -1
            latest_data["timer"] = format_timer(snapshot.get("timer", ""))
            latest_data["participants"] = snapshot.get("participants", "0") or "0"
            latest_data["pool"] = format_pool(snapshot.get("pool", ""))
            latest_data["rain_active"] = is_rain_active
            if is_rain_active and not was_rain_active:
                logger.info(f"Rain detected! Pool: {latest_data['pool']}")
                bot.loop.create_task(trigger_rain_alert(latest_data["pool"]))
            was_rain_active = is_rain_active
            await asyncio.sleep(POLL_INTERVAL)
        except Exception as e:
            logger.error(f"Polling loop runtime anomaly: {e}")
            await asyncio.sleep(POLL_INTERVAL)
async def trigger_rain_alert(pool_str):
    if not target_channel:
        return
    pool_numeric = pool_to_number(pool_str) or 0
    role_ping = ""
    if pool_numeric >= BIG_RAIN_THRESHOLD and BIG_RAIN_ROLE_ID:
        role_ping = f"<@&{BIG_RAIN_ROLE_ID}>"
    elif RAIN_ROLE_ID:
        role_ping = f"<@&{RAIN_ROLE_ID}>"
    embed = discord.Embed(title="🌧️ DonutLuck Rain Event Started!", description="A fresh rain event is active right now on **DonutLuck**!", color=discord.Color.blue())
    embed.add_field(name="💰 Pool Amount", value=f"**{pool_str}**", inline=True)
    embed.add_field(name="🔗 Website Link", value=f"[Open DonutLuck]({WEBSITE_URL})", inline=True)
    embed.set_timestamp()
    try:
        await target_channel.send(content=role_ping, embed=embed)
    except Exception as e:
        logger.error(f"Failed to dispatch Discord alert embed: {e}")
async def discord_dashboard_updater():
    await bot.wait_until_ready()
    global target_channel, dashboard_message
    target_channel = bot.get_channel(DISCORD_CHANNEL_ID)
    if not target_channel:
        logger.error(f"Target channel ID {DISCORD_CHANNEL_ID} could not be resolved.")
        return
    logger.info("Discord live status layout operational.")
    while not bot.is_closed():
        try:
            embed = discord.Embed(title="🌧️ DonutLuck Live Rain Monitor Status", color=discord.Color.gold() if latest_data["rain_active"] else discord.Color.dark_gray())
            status_text = "🟢 ACTIVE RAIN SCENE" if latest_data["rain_active"] else "⚪ Waiting for next rain"
            embed.add_field(name="Current State", value=f"**{status_text}**", inline=False)
            embed.add_field(name="⏱️ Timer Remaining", value=f"`{latest_data['timer']}`", inline=True)
            embed.add_field(name="👥 Group Entrants", value=f"`{latest_data['participants']}`", inline=True)
            embed.add_field(name="💎 Accumulated Pool", value=f"`{latest_data['pool']}`", inline=True)
            embed.set_footer(text="System updates in real-time | Free 24/7 Hosting Pipeline")
            embed.set_timestamp()
                    if dashboard_message is None:
                async for msg in target_channel.history(limit=5):
                    if msg.author == bot.user and msg.embeds and msg.embeds.title == "🌧️ DonutLuck Live Rain Monitor Status":
                        dashboard_message = msg
                        break
                if dashboard_message is None:
                    dashboard_message = await target_channel.send(embed=embed)
                else:
                    await dashboard_message.edit(embed=embed)
            else:
                await dashboard_message.edit(embed=embed)
        except Exception as e:
            logger.error(f"Failed updating target dashboard profile status frame: {e}")
        await asyncio.sleep(DISCORD_UPDATE_INTERVAL)
@bot.event
async def on_ready():
    logger.info(f"Bot authenticated cleanly as user profile: {bot.user}")
    global monitor_task
    playwright_context = await async_playwright().start()
    browser = await playwright_context.chromium.launch(headless=False, args=["--no-sandbox", "--disable-dev-shm-usage"])
    context = await browser.new_context(viewport={"width": 1280, "height": 1024})
    page = await context.new_page()
    logger.info(f"Connecting browser instance framework to target: {WEBSITE_URL}")
    await page.goto(WEBSITE_URL, wait_until="networkidle")
    bot.loop.create_task(discord_dashboard_updater())
    monitor_task = bot.loop.create_task(website_poll_loop(page))
def main():
    if not DISCORD_TOKEN:
        logger.error("Missing DISCORD_TOKEN inside environment configuration profile targets.")
        return
    bot.run(DISCORD_TOKEN)
if __name__ == "__main__":
    main()

    bot.run(DISCORD_TOKEN)
if __name__ == "__main__":
    main()
