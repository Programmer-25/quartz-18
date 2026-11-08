import asyncio
import os
import re
import logging
import time
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv
from playwright.async_api import (
    async_playwright,
    TimeoutError as PlaywrightTimeoutError,
    Error as PlaywrightError
)


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

# Always use visible Chromium.
# GitHub Actions must launch this script through xvfb-run.
HEADLESS = False

# Keep browser profile outside the repository by default.
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

MAX_RUN_DURATION = 11100  # 3 hours and 5 minutes


# ============================================================
# WEBSITE SELECTORS
# ============================================================

# Waiting timer
TIMER_XPATH = (
    "/html/body/div[1]/div[1]/div[2]/aside[1]/div[2]/"
    "div[2]/div[1]/div[2]/div[2]/div[1]/span[1]/span[1]"
)

# Active rain timer
ACTIVE_TIMER_XPATH = (
    "/html/body/div/div/div[2]/aside/div[2]/div[2]/"
    "div/div[2]/span[2]/span"
)

# Rain status
RAIN_STATUS_XPATH = (
    "/html/body/div/div/div[2]/aside/div[2]/div[2]/div/p[1]"
)

# Participants
PARTICIPANTS_XPATH = (
    "/html/body/div/div/div[2]/aside/div[2]/div[2]/"
    "div/div[2]/span[1]"
)

# Animated rain pool
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

        // Reads the currently visible digit from animated
        // vertical number strips.

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

        // Use the correct timer for the current state.

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

    selectors = {
        "timer": TIMER_XPATH,
        "activeTimer": ACTIVE_TIMER_XPATH,
        "status": RAIN_STATUS_XPATH,
        "participants": PARTICIPANTS_XPATH,
        "pool": POOL_XPATH
    }

    # A navigation can destroy the JavaScript execution context
    # while page.evaluate() is running. Retry that snapshot.

    for attempt in range(3):

        try:

            if page.is_closed():
                raise PlaywrightError("DonutLuck page is closed.")

            return await page.evaluate(
                script,
                selectors
            )

        except PlaywrightError as error:

            error_text = str(error).lower()

            navigation_error = (
                "execution context was destroyed" in error_text
                or "most likely because of a navigation" in error_text
                or "frame was detached" in error_text
                or "navigation" in error_text
            )

            if not navigation_error:
                raise

            if attempt < 2:

                logger.warning(
                    "DonutLuck page navigated during snapshot "
                    "(attempt %s/3). Retrying...",
                    attempt + 1
                )

                await asyncio.sleep(0.4)

            else:

                logger.warning(
                    "Could not read snapshot because the page "
                    "is still navigating. Will retry next poll."
                )

    return None


# ============================================================
# WEBSITE POLLING
# ============================================================

async def website_poll_loop(page):

    start_time = time.monotonic()

    logger.info("Website polling started.")

    while not bot.is_closed():

        if time.monotonic() - start_time > MAX_RUN_DURATION:

            logger.info(
                "Maximum runtime reached. Restarting through host."
            )

            await bot.close()

            break

        try:

            snapshot = await read_website_snapshot(page)

            # A navigation retry may return no snapshot.
            # Keep the last valid values instead of replacing
            # them with Loading or Unknown.

            if snapshot is None:

                await asyncio.sleep(POLL_INTERVAL)
                continue

            status = snapshot.get("status", "")

            latest_data["rain_active"] = (
                RAIN_START_TEXT.casefold() in status.casefold()
            )

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

        # BIG RAIN: ping both roles.

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
            "Big Rain detected: both roles selected."
        )

    else:

        # NORMAL RAIN: ping only the Rain role.

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
            "Normal Rain detected: Rain role selected."
        )
        logger.info(
            "PING DEBUG | Rain Role: %s | Big Rain Role: %s | Message: %s",
            RAIN_ROLE_ID,
            BIG_RAIN_ROLE_ID,
            content
        )
    dashboard_message = await channel.send(
        content=content,
        embed=create_active_embed(),
        allowed_mentions=allowed_mentions
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

    while not bot.is_closed():

        try:

            is_active = latest_data["rain_active"]

            # Rain starts.

            if is_active and not was_active:

                logger.info("Rain state changed to ACTIVE.")

                await clear_previous_messages(channel)

                await send_active_message(channel)

                was_active = True

            # Rain ends.

            elif not is_active and was_active:

                logger.info("Rain state changed to WAITING.")

                await clear_previous_messages(channel)

                await send_waiting_message(channel)

                was_active = False

            # Normal dashboard refresh.

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

                except discord.HTTPException:

                    logger.exception(
                        "Discord dashboard edit failed."
                    )

                # Recreate without sending another role ping.

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

            logger.exception(
                "Discord dashboard update failed."
            )

        await asyncio.sleep(DISCORD_UPDATE_INTERVAL)


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

        headless=False,

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

        title = await page.title()

        logger.info(
            "DonutLuck page title: %s",
            title
        )

    except PlaywrightError:

        logger.warning(
            "Could not read the page title during startup."
        )

    return playwright, browser_context, page


# ============================================================
# MONITOR STARTUP
# ============================================================

async def start_monitoring():

    playwright = None
    browser_context = None

    poll_task = None
    discord_task = None

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

        if browser_context is not None:

            try:

                await browser_context.close()

            except Exception:

                logger.exception(
                    "Could not close browser context."
                )

        if playwright is not None:

            try:

                await playwright.stop()

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

    # Prevent duplicate monitoring after reconnects.

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
