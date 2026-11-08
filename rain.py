
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


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent

load_dotenv(BASE_DIR / ".env")

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()

DISCORD_CHANNEL_ID = int(
    os.getenv("DISCORD_CHANNEL_ID", "1560149229907869791")
)

RAIN_ROLE_ID = int(os.getenv("RAIN_ROLE_ID", "0"))
BIG_RAIN_ROLE_ID = int(os.getenv("BIG_RAIN_ROLE_ID", "0"))

WEBSITE_URL = "https://donutluck.com/"

# GitHub Actions uses Xvfb, allowing visible Chromium.
HEADLESS = os.getenv("HEADLESS", "false").lower() in (
    "true", "1", "yes"
)

PROFILE_DIR = Path(
    os.getenv(
        "PLAYWRIGHT_PROFILE_DIR",
        "/tmp/donutluck_profile"
    )
)

POLL_INTERVAL = 0.25
DISCORD_UPDATE_INTERVAL = 1.0

RAIN_START_TEXT = "It's about to RAIN!"

BIG_RAIN_THRESHOLD = 1_500_000_000

MAX_RUN_DURATION = 11100

# Signals both monitoring loops to stop before Discord disconnects.
monitor_stop_event = asyncio.Event()


# ============================================================
# WEBSITE SELECTORS
# ============================================================

TIMER_XPATH = (
    "/html/body/div[1]/div[1]/div[2]/aside[1]/div[2]/"
    "div[2]/div[1]/div[2]/div[2]/div[1]/span[1]/span[1]"
)

ACTIVE_TIMER_XPATH = (
    "/html/body/div/div/div[2]/aside/div[2]/div[2]/"
    "div/div[2]/span[2]/span"
)

RAIN_STATUS_XPATH = (
    "/html/body/div/div/div[2]/aside/div[2]/div[2]/div/p[1]"
)

PARTICIPANTS_XPATH = (
    "/html/body/div/div/div[2]/aside/div[2]/div[2]/"
    "div/div[2]/span[1]"
)

POOL_XPATH = (
    "/html/body/div/div/div[2]/aside/div[2]/div[2]/"
    "div/div[2]/div[1]/div/span[1]"
)


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("RainMonitor")


# ============================================================
# DISCORD SETUP
# ============================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

target_channel = None
dashboard_message = None
monitor_task = None

latest_data = {
    "timer": "Loading...",
    "participants": "0",
    "pool": "Unknown",
    "rain_active": False
}


# ============================================================
# TIMER HELPERS
# ============================================================

def parse_timer(value):

    if not value:
        return None

    value = str(value).strip()

    match = re.search(
        r"(\d+):(\d{2})(?::(\d{2}))?",
        value
    )

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


# ============================================================
# POOL HELPERS
# ============================================================

def format_pool(value):

    if not value:
        return "Unknown"

    value = re.sub(
        r"\s+",
        "",
        str(value)
    ).replace(",", "")

    match = re.search(
        r"(\d+(?:\.\d+)?)([KMB])?(\+)?",
        value,
        re.IGNORECASE
    )

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

    match = re.search(
        r"(\d+(?:\.\d+)?)\s*([KMB])?",
        value,
        re.IGNORECASE
    )

    if not match:
        return None

    number = float(match.group(1))
    suffix = (match.group(2) or "").upper()

    multiplier = {
        "": 1,
        "K": 1_000,
        "M": 1_000_000,
        "B": 1_000_000_000
    }.get(suffix, 1)

    return number * multiplier


# ============================================================
# WEBSITE READING SYSTEM
# ============================================================

async def read_website_snapshot(page):

    script = r"""
    (selectors) => {

        function xpathElement(path) {

            try {

                return document.evaluate(
                    path,
                    document,
                    null,
                    XPathResult.FIRST_ORDERED_NODE_TYPE,
                    null
                ).singleNodeValue;

            } catch (_) {

                return null;
            }
        }

        function cleanText(element) {

            return element
                ? (element.innerText || element.textContent || "").trim()
                : "";
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

                            translateY = new DOMMatrixReadOnly(
                                transform
                            ).m42;
                        }

                        const firstRow = strip.firstElementChild;

                        const rowHeight = firstRow
                            ? firstRow.getBoundingClientRect().height
                            : 0;

                        if (rowHeight > 0) {

                            let row = Math.round(
                                Math.abs(translateY) / rowHeight
                            );

                            row = ((row % 10) + 10) % 10;

                            const digit = (9 - row + 10) % 10;

                            result += String(digit);

                            continue;
                        }

                    } catch (_) {}
                }

                const childText = (
                    child.innerText || child.textContent || ""
                ).trim();

                if (
                    [".", "M", "B", "K", "+"].includes(childText)
                ) {

                    result += childText;
                }
            }

            return result;
        }

        const statusElement = xpathElement(selectors.status);

        const status = cleanText(statusElement);

        const rainActive = status.toLowerCase().includes(
            "it's about to rain!"
        );

        const timerElement = xpathElement(
            rainActive
                ? selectors.activeTimer
                : selectors.timer
        );

        const participantsElement = xpathElement(
            selectors.participants
        );

        const poolElement = xpathElement(
            selectors.pool
        );

        let pool = "";

        if (poolElement) {

            pool = readVisibleDigits(poolElement);

            if (!pool) {
                pool = cleanText(poolElement);
            }
        }

        return {

            timer: cleanText(timerElement),

            status: status,

            participants: cleanText(participantsElement),

            pool: pool
        };
    }
    """

    return await page.evaluate(
        script,
        {
            "timer": TIMER_XPATH,
            "activeTimer": ACTIVE_TIMER_XPATH,
            "status": RAIN_STATUS_XPATH,
            "participants": PARTICIPANTS_XPATH,
            "pool": POOL_XPATH
        }
    )


# ============================================================
# WEBSITE POLLING
# ============================================================

async def website_poll_loop(page):

    start_time = time.monotonic()
    last_debug_time = 0

    logger.info("Website polling started.")

    while not bot.is_closed() and not monitor_stop_event.is_set():

        if time.monotonic() - start_time > MAX_RUN_DURATION:

            logger.info(
                "Maximum runtime reached. Beginning graceful shutdown."
            )

            monitor_stop_event.set()
            break

        try:

            snapshot = await read_website_snapshot(page)

            status = snapshot.get("status", "")
            detected_active = (
                RAIN_START_TEXT.casefold() in status.casefold()
            )

            # Log website status every 5 seconds.
            if time.monotonic() - last_debug_time >= 5:

                logger.info(
                    "RAIN DEBUG | Status: %r | Timer: %r | "
                    "Detected active: %s | Pool: %r",
                    status,
                    snapshot.get("timer"),
                    detected_active,
                    snapshot.get("pool")
                )

                last_debug_time = time.monotonic()

            if detected_active and not latest_data["rain_active"]:

                logger.warning(
                    "WEBSITE RAIN DETECTED! Status: %r",
                    status
                )

            latest_data["rain_active"] = detected_active

            if snapshot.get("timer"):

                latest_data["timer"] = format_timer(
                    snapshot["timer"]
                )

            if snapshot.get("participants"):

                match = re.search(
                    r"\d[\d,]*",
                    snapshot["participants"]
                )

                if match:

                    latest_data["participants"] = (
                        match.group(0).replace(",", "")
                    )

            if snapshot.get("pool"):

                latest_data["pool"] = format_pool(
                    snapshot["pool"]
                )

        except asyncio.CancelledError:

            raise

        except Exception:

            logger.exception("Website polling failed.")

        await asyncio.sleep(POLL_INTERVAL)


# ============================================================
# WAITING EMBED
# ============================================================

def create_waiting_embed():

    embed = discord.Embed(
        title="🌧️ DonutLuck Rain Monitor",
        description=(
            "```ansi\n"
            "\u001b[1;33m⏳ WAITING FOR RAIN\u001b[0m\n"
            "```"
        ),
        color=discord.Color.gold()
    )

    embed.add_field(
        name="⏱️ Rain Timer",
        value=f"```{latest_data['timer']}```",
        inline=True
    )

    embed.add_field(
        name="🌧️ Rain Pool",
        value=f"```{latest_data['pool']}```",
        inline=True
    )

    embed.set_footer(
        text="Live status • Updates automatically"
    )

    embed.timestamp = discord.utils.utcnow()

    return embed


# ============================================================
# ACTIVE RAIN EMBED
# ============================================================

def create_active_embed():

    embed = discord.Embed(
        title="🌧️ DonutLuck Rain Monitor",
        description=(
            "```ansi\n"
            "\u001b[1;31m🔴 RAIN IS ACTIVE!\u001b[0m\n"
            "```"
        ),
        color=discord.Color.red()
    )

    embed.add_field(
        name="⏱️ Rain Timer",
        value=f"```{latest_data['timer']}```",
        inline=True
    )

    embed.add_field(
        name="👥 Participants",
        value=f"```{latest_data['participants']}```",
        inline=True
    )

    embed.add_field(
        name="🌧️ Rain Pool",
        value=f"```{latest_data['pool']}```",
        inline=False
    )

    embed.set_footer(
        text="Live status • Updates automatically"
    )

    embed.timestamp = discord.utils.utcnow()

    return embed


# ============================================================
# CHANNEL CLEANUP
# ============================================================

async def clear_previous_messages(channel):

    logger.info(
        "Clearing messages in #%s...",
        channel.name
    )

    deleted = await channel.purge(
        limit=None,
        bulk=True,
        reason="DonutLuck rain monitor refresh"
    )

    logger.info(
        "Deleted %s messages.",
        len(deleted)
    )


# ============================================================
# ROLE ALERTS
# ============================================================

async def send_active_message(channel):

    global dashboard_message

    logger.info(
        "ALERT FUNCTION CALLED | Rain Role ID: %s | "
        "Big Rain Role ID: %s | Pool: %s",
        RAIN_ROLE_ID,
        BIG_RAIN_ROLE_ID,
        latest_data["pool"]
    )

    pool_value = pool_to_number(
        latest_data["pool"]
    )

    if pool_value is None:

        content = (
            "⚠️ **RAIN IS STARTING NOW! "
            "Pool value unavailable.**"
        )

        allowed_mentions = discord.AllowedMentions.none()

        logger.warning(
            "Pool unavailable; no role was pinged."
        )

    elif pool_value >= BIG_RAIN_THRESHOLD:

        role_ids = [
            role_id
            for role_id in (
                RAIN_ROLE_ID,
                BIG_RAIN_ROLE_ID
            )
            if role_id
        ]

        if role_ids:

            mentions = " ".join(
                f"<@&{role_id}>"
                for role_id in role_ids
            )

            content = (
                f"{mentions} 🌧️ **BIG RAIN IS STARTING NOW!**"
            )

            allowed_mentions = discord.AllowedMentions(
                roles=[
                    discord.Object(id=role_id)
                    for role_id in role_ids
                ]
            )

        else:

            content = (
                "🌧️ **BIG RAIN IS STARTING NOW!**"
            )

            allowed_mentions = discord.AllowedMentions.none()

        logger.info(
            "BIG RAIN ALERT | Roles selected: %s | Message: %s",
            role_ids,
            content
        )

    else:

        if RAIN_ROLE_ID:

            content = (
                f"<@&{RAIN_ROLE_ID}> "
                "🌧️ **RAIN IS STARTING NOW!**"
            )

            allowed_mentions = discord.AllowedMentions(
                roles=[
                    discord.Object(id=RAIN_ROLE_ID)
                ]
            )

        else:

            content = (
                "🌧️ **RAIN IS STARTING NOW!**"
            )

            allowed_mentions = discord.AllowedMentions.none()

        logger.info(
            "NORMAL RAIN ALERT | Role selected: %s | Message: %s",
            RAIN_ROLE_ID,
            content
        )

    logger.info(
        "SENDING DISCORD ALERT NOW."
    )

    dashboard_message = await channel.send(
        content=content,
        embed=create_active_embed(),
        allowed_mentions=allowed_mentions
    )

    logger.info(
        "DISCORD ALERT SENT | Message ID: %s",
        dashboard_message.id
    )


# ============================================================
# WAITING MESSAGE
# ============================================================

async def send_waiting_message(channel):

    global dashboard_message

    dashboard_message = await channel.send(
        embed=create_waiting_embed()
    )


# ============================================================
# DISCORD DASHBOARD UPDATE LOOP
# ============================================================

async def update_dashboard():

    global dashboard_message

    channel = target_channel

    was_active = False

    await clear_previous_messages(channel)

    await send_waiting_message(channel)

    logger.info("Discord dashboard initialized.")

    while not bot.is_closed() and not monitor_stop_event.is_set():

        try:

            is_active = latest_data["rain_active"]

            if is_active and not was_active:

                logger.info(
                    "DASHBOARD DETECTED RAIN START | "
                    "Calling send_active_message()."
                )

                await clear_previous_messages(channel)

                await send_active_message(channel)

                was_active = True

            elif not is_active and was_active:

                logger.info("Rain state changed to WAITING.")

                await clear_previous_messages(channel)

                await send_waiting_message(channel)

                was_active = False

            elif dashboard_message is not None:

                embed = (
                    create_active_embed()
                    if was_active
                    else create_waiting_embed()
                )

                try:

                    await dashboard_message.edit(
                        embed=embed
                    )

                except discord.NotFound:

                    logger.warning(
                        "Dashboard message disappeared."
                    )

                    dashboard_message = None

                except Exception:

                    if (
                        bot.is_closed()
                        or monitor_stop_event.is_set()
                    ):

                        logger.info(
                            "Dashboard edit skipped during shutdown."
                        )

                        break

                    logger.exception(
                        "Discord dashboard edit failed; will retry."
                    )

                if dashboard_message is None:

                    if was_active:

                        dashboard_message = await channel.send(
                            embed=create_active_embed()
                        )

                    else:

                        await send_waiting_message(channel)

        except asyncio.CancelledError:

            raise

        except Exception:

            if (
                bot.is_closed()
                or monitor_stop_event.is_set()
            ):

                logger.info(
                    "Discord dashboard loop stopped during shutdown."
                )

                break

            logger.exception(
                "Discord dashboard update failed."
            )

        await asyncio.sleep(DISCORD_UPDATE_INTERVAL)

    logger.info("Discord dashboard update loop stopped.")


# ============================================================
# PLAYWRIGHT BROWSER
# ============================================================

async def start_browser():

    playwright = await async_playwright().start()

    PROFILE_DIR.mkdir(
        parents=True,
        exist_ok=True
    )

    logger.info(
        "Starting Chromium. Headless mode: %s",
        HEADLESS
    )

    browser_context = await playwright.chromium.launch_persistent_context(

        user_data_dir=str(PROFILE_DIR),

        headless=HEADLESS,

        args=[
            "--no-sandbox",
            "--disable-dev-shm-usage",
            "--disable-gpu",
            "--disable-background-timer-throttling",
            "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows",
            "--no-first-run",
            "--no-default-browser-check"
        ],

        viewport={
            "width": 1280,
            "height": 900
        },

        locale="en-US"
    )

    page = (
        browser_context.pages[0]
        if browser_context.pages
        else await browser_context.new_page()
    )

    page.set_default_timeout(5000)

    logger.info(
        "Opening DonutLuck: %s",
        WEBSITE_URL
    )

    try:

        await page.goto(
            WEBSITE_URL,
            wait_until="domcontentloaded",
            timeout=60000
        )

    except PlaywrightTimeoutError:

        logger.warning(
            "Page load timed out. Continuing with the open page."
        )

    await asyncio.sleep(3)

    logger.info(
        "DonutLuck browser is ready. URL: %s",
        page.url
    )

    try:

        logger.info(
            "DonutLuck page title: %s",
            await page.title()
        )

    except Exception:

        logger.warning(
            "Could not read page title."
        )

    return playwright, browser_context, page


# ============================================================
# MONITOR STARTUP
# ============================================================

async def start_monitoring():

    global monitor_stop_event

    playwright = None
    browser_context = None

    poll_task = None
    discord_task = None

    monitor_stop_event.clear()

    try:

        playwright, browser_context, page = await start_browser()

        poll_task = asyncio.create_task(
            website_poll_loop(page)
        )

        discord_task = asyncio.create_task(
            update_dashboard()
        )

        await asyncio.gather(
            poll_task,
            discord_task
        )

        # Both monitoring loops have stopped before Discord disconnects.
        if monitor_stop_event.is_set() and not bot.is_closed():

            logger.info(
                "Monitoring loops stopped. Closing Discord connection cleanly."
            )

            await bot.close()

    except asyncio.CancelledError:

        raise

    except Exception:

        logger.exception(
            "Monitor stopped unexpectedly."
        )

    finally:

        for task in (
            poll_task,
            discord_task
        ):

            if task and not task.done():

                task.cancel()

        pending_tasks = [
            task
            for task in (
                poll_task,
                discord_task
            )
            if task and not task.done()
        ]

        if pending_tasks:

            await asyncio.gather(
                *pending_tasks,
                return_exceptions=True
            )

        if browser_context is not None:

            try:

                await browser_context.close()

                logger.info(
                    "Browser context closed."
                )

            except Exception:

                logger.exception(
                    "Could not close browser context."
                )

        if playwright is not None:

            try:

                await playwright.stop()

                logger.info(
                    "Playwright stopped."
                )

            except Exception:

                logger.exception(
                    "Could not stop Playwright."
                )


# ============================================================
# DISCORD BOT EVENTS
# ============================================================

@bot.event
async def on_ready():

    global target_channel
    global monitor_task

    logger.info(
        "Logged in as %s",
        bot.user
    )

    channel = bot.get_channel(
        DISCORD_CHANNEL_ID
    )

    if channel is None:

        try:

            channel = await bot.fetch_channel(
                DISCORD_CHANNEL_ID
            )

        except discord.HTTPException:

            logger.exception(
                "Could not find the configured Discord channel."
            )

            return

    target_channel = channel

    if monitor_task is None or monitor_task.done():

        logger.info(
            "Starting DonutLuck monitoring."
        )

        monitor_task = asyncio.create_task(
            start_monitoring()
        )


# ============================================================
# MAIN
# ============================================================

async def main():

    if not DISCORD_TOKEN:

        raise RuntimeError(
            "DISCORD_TOKEN is missing from your .env file."
        )

    logger.info(
        "CONFIG | Rain Role ID: %s | Big Rain Role ID: %s",
        RAIN_ROLE_ID,
        BIG_RAIN_ROLE_ID
    )

    async with bot:

        await bot.start(
            DISCORD_TOKEN
        )


if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        logger.info(
            "Rain monitor stopped."
        )
