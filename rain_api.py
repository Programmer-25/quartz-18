
import asyncio
import logging
import os
import time
from pathlib import Path

import aiohttp
import discord
from discord.ext import commands
from dotenv import load_dotenv


# ============================================================
# CONFIGURATION
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

API_URL = "https://donutluck.com/api/chat/rain/state"

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
DISCORD_CHANNEL_ID = int(
    os.getenv("DISCORD_CHANNEL_ID", "1560149229907869791")
)

RAIN_ROLE_ID = os.getenv("RAIN_ROLE_ID", "").strip()
BIG_RAIN_ROLE_ID = os.getenv("BIG_RAIN_ROLE_ID", "").strip()

GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "").strip()
GITHUB_REPOSITORY = os.getenv("GITHUB_REPOSITORY", "").strip()
GITHUB_REF_NAME = os.getenv("GITHUB_REF_NAME", "main").strip()

WORKFLOW_FILE = "rain-api.yml"

POLL_INTERVAL = 1.0
DISCORD_UPDATE_INTERVAL = 1.0

BIG_RAIN_THRESHOLD = 1_500_000_000

NEXT_RUN_AFTER = 27 * 60 + 30
HANDOFF_WAIT_SECONDS = 45

API_TIMEOUT = 10
HANDOFF_CHECK_INTERVAL = 3

RUN_ID = os.getenv("GITHUB_RUN_ID", "local")


# ============================================================
# LOGGING
# ============================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("DonutLuckRain")


# ============================================================
# DISCORD
# ============================================================

intents = discord.Intents.default()

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)

monitor_task = None
shutdown_requested = asyncio.Event()

last_handoff_check = 0


# ============================================================
# CURRENT RAIN DATA
# ============================================================

latest_data = {
    "timer": 0,
    "participants": 0,
    "pool": 0,
    "rain_active": False
}


# ============================================================
# FORMATTING
# ============================================================

def format_timer(seconds):

    try:
        seconds = max(0, int(seconds))
    except (ValueError, TypeError):
        seconds = 0

    minutes, seconds = divmod(seconds, 60)

    return f"{minutes:02d}:{seconds:02d}"


def format_pool(value):

    try:
        value = float(value)
    except (ValueError, TypeError):
        return "Unknown"

    if value >= 1_000_000_000:
        return f"{value / 1_000_000_000:.2f}B"

    if value >= 1_000_000:
        return f"{value / 1_000_000:.2f}M"

    if value >= 1_000:
        return f"{value / 1_000:.2f}K"

    return str(int(value))


# ============================================================
# API
# ============================================================

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://donutluck.com/",
}


async def fetch_rain_state(session):

    async with session.get(API_URL) as response:

        if response.status != 200:
            body = await response.text()

            raise RuntimeError(
                f"API HTTP {response.status}: {body[:300]}"
            )

        data = await response.json(content_type=None)

    if not isinstance(data, dict):
        raise ValueError("Invalid API response")

    if "active" not in data:
        raise ValueError("Missing active field")

    if "time_remaining" not in data:
        raise ValueError("Missing time_remaining field")

    if "prize" not in data:
        raise ValueError("Missing prize field")

    return {
        "timer": int(data["time_remaining"]),
        "participants": int(data.get("participants_count") or 0),
        "pool": int(data["prize"]),
        "rain_active": bool(data["active"])
    }


# ============================================================
# EMBEDS
# ============================================================

def create_waiting_embed():

    embed = discord.Embed(
        title="⏳ WAITING FOR RAIN",
        color=discord.Color.gold()
    )

    embed.add_field(
        name="Rain Timer",
        value=format_timer(latest_data["timer"]),
        inline=True
    )

    embed.add_field(
        name="Rain Pool",
        value=format_pool(latest_data["pool"]),
        inline=True
    )

    return embed


def create_active_embed():

    embed = discord.Embed(
        title="🔴 RAIN IS ACTIVE!",
        color=discord.Color.red()
    )

    embed.add_field(
        name="Rain Timer",
        value=format_timer(latest_data["timer"]),
        inline=True
    )

    embed.add_field(
        name="Participants",
        value=str(latest_data["participants"]),
        inline=True
    )

    embed.add_field(
        name="Rain Pool",
        value=format_pool(latest_data["pool"]),
        inline=True
    )

    return embed


# ============================================================
# CHANNEL HELPERS
# ============================================================

async def clear_previous_messages(channel):

    try:

        await channel.purge(
            limit=None,
            bulk=True
        )

        logger.info("Channel cleanup completed.")

    except Exception:

        logger.exception("Could not clear channel.")


async def find_dashboard_message(channel):

    async for message in channel.history(limit=50):

        if message.author != bot.user:
            continue

        if not message.embeds:
            continue

        title = message.embeds[0].title or ""

        if title in (
            "⏳ WAITING FOR RAIN",
            "🔴 RAIN IS ACTIVE!"
        ):
            return message

    return None


async def send_waiting_message(channel):

    message = await channel.send(
        embed=create_waiting_embed()
    )

    logger.info("Waiting dashboard sent.")

    return message


async def send_active_message(channel):

    pool = latest_data["pool"]

    mentions = []

    if pool >= BIG_RAIN_THRESHOLD:

        if RAIN_ROLE_ID:
            mentions.append(f"<@&{RAIN_ROLE_ID}>")

        if BIG_RAIN_ROLE_ID:
            mentions.append(f"<@&{BIG_RAIN_ROLE_ID}>")

        logger.info("BIG RAIN detected.")

    elif pool > 0:

        if RAIN_ROLE_ID:
            mentions.append(f"<@&{RAIN_ROLE_ID}>")

    content = " ".join(mentions) if mentions else None

    message = await channel.send(
        content=content,
        embed=create_active_embed(),
        allowed_mentions=discord.AllowedMentions(
            roles=True,
            everyone=False
        )
    )

    logger.info("Active rain dashboard sent.")

    return message


# ============================================================
# CHECK FOR ANOTHER ACTIVE GITHUB RUN
# ============================================================

async def has_previous_active_run():

    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        logger.info(
            "GitHub credentials unavailable. "
            "Skipping startup handoff check."
        )
        return False

    if RUN_ID == "local":
        logger.info(
            "Local run detected. Skipping startup handoff."
        )
        return False

    url = (
        f"https://api.github.com/repos/"
        f"{GITHUB_REPOSITORY}/actions/workflows/"
        f"{WORKFLOW_FILE}/runs"
    )

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    params = {
        "status": "in_progress",
        "per_page": 20
    }

    timeout = aiohttp.ClientTimeout(total=15)

    try:

        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            async with session.get(
                url,
                headers=headers,
                params=params
            ) as response:

                if response.status != 200:

                    body = await response.text()

                    logger.warning(
                        "Could not check active GitHub runs: "
                        "HTTP %s | %s",
                        response.status,
                        body[:300]
                    )

                    return False

                data = await response.json()

        runs = data.get("workflow_runs", [])

        for run in runs:

            other_run_id = str(run.get("id", ""))

            if (
                other_run_id
                and other_run_id != RUN_ID
                and run.get("status") == "in_progress"
            ):

                logger.info(
                    "Found previous active workflow run: %s",
                    other_run_id
                )

                return True

        logger.info("No previous active workflow run found.")

        return False

    except Exception:

        logger.exception(
            "Could not check previous GitHub workflow runs."
        )

        return False


# ============================================================
# HANDOFF SYSTEM
# ============================================================

def request_marker(run_id):
    return f"RAIN-HANDOFF-REQUEST:{run_id}"


def acknowledge_marker(run_id):
    return f"RAIN-HANDOFF-ACK:{run_id}"


async def prepare_handoff(channel):

    original_topic = channel.topic or ""

    marker = request_marker(RUN_ID)

    logger.info(
        "Requesting handoff. Run ID: %s",
        RUN_ID
    )

    try:

        await channel.edit(topic=marker)

    except Exception:

        logger.exception(
            "Could not write handoff request to channel topic."
        )

        return False

    start = time.monotonic()
    last_refresh = 0

    while time.monotonic() - start < HANDOFF_WAIT_SECONDS:

        try:

            now = time.monotonic()

            if now - last_refresh >= HANDOFF_CHECK_INTERVAL:

                channel = await bot.fetch_channel(
                    DISCORD_CHANNEL_ID
                )

                last_refresh = now

            if channel.topic == acknowledge_marker(RUN_ID):

                logger.info(
                    "Previous bot acknowledged handoff."
                )

                try:
                    await channel.edit(topic=original_topic)
                except Exception:
                    logger.exception(
                        "Could not restore original channel topic."
                    )

                return True

        except Exception:

            logger.exception("Handoff check failed.")

        await asyncio.sleep(1)

    logger.warning(
        "Handoff acknowledgement timed out. "
        "Starting monitoring anyway."
    )

    try:
        await channel.edit(topic=original_topic)
    except Exception:
        logger.exception("Could not restore channel topic.")

    return False


async def check_for_handoff(channel):

    global last_handoff_check

    now = time.monotonic()

    if now - last_handoff_check < HANDOFF_CHECK_INTERVAL:
        return False

    last_handoff_check = now

    try:

        # Always refresh the channel.
        # The cached channel topic may be outdated.
        fresh_channel = await bot.fetch_channel(
            DISCORD_CHANNEL_ID
        )

        topic = fresh_channel.topic or ""

        if not topic.startswith("RAIN-HANDOFF-REQUEST:"):
            return False

        requested_run = topic.split(":", 1)[1]

        if requested_run == RUN_ID:
            return False

        logger.info(
            "Replacement bot is ready. "
            "Acknowledging run %s.",
            requested_run
        )

        await fresh_channel.edit(
            topic=acknowledge_marker(requested_run)
        )

        shutdown_requested.set()

        logger.info(
            "Handoff acknowledged. Shutting down old bot."
        )

        return True

    except Exception:

        logger.exception("Handoff acknowledgement failed.")

        return False


# ============================================================
# GITHUB WORKFLOW DISPATCH
# ============================================================

async def dispatch_next_run():

    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:

        logger.error(
            "GitHub token or repository is missing. "
            "Cannot start replacement."
        )

        return False

    url = (
        f"https://api.github.com/repos/"
        f"{GITHUB_REPOSITORY}/actions/workflows/"
        f"{WORKFLOW_FILE}/dispatches"
    )

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }

    payload = {
        "ref": GITHUB_REF_NAME
    }

    timeout = aiohttp.ClientTimeout(total=20)

    try:

        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            async with session.post(
                url,
                headers=headers,
                json=payload
            ) as response:

                if response.status == 204:

                    logger.info(
                        "Next GitHub Actions run successfully triggered."
                    )

                    return True

                body = await response.text()

                logger.error(
                    "GitHub dispatch failed: HTTP %s | %s",
                    response.status,
                    body[:500]
                )

                return False

    except Exception:

        logger.exception("Could not dispatch next workflow.")

        return False


# ============================================================
# MONITOR
# ============================================================

async def monitor_loop():

    channel = bot.get_channel(DISCORD_CHANNEL_ID)

    if channel is None:

        channel = await bot.fetch_channel(
            DISCORD_CHANNEL_ID
        )

    if not isinstance(channel, discord.TextChannel):

        logger.error("Configured channel is not a text channel.")

        return

    logger.info(
        "Connected to Discord channel: %s",
        channel.name
    )

    timeout = aiohttp.ClientTimeout(total=API_TIMEOUT)

    async with aiohttp.ClientSession(
        headers=HEADERS,
        timeout=timeout
    ) as session:

        # Require valid API data before taking control.
        while not shutdown_requested.is_set():

            try:

                latest_data.update(
                    await fetch_rain_state(session)
                )

                break

            except Exception:

                logger.exception(
                    "Initial API request failed. Retrying."
                )

                await asyncio.sleep(3)

        if shutdown_requested.is_set():
            return

        # Only request a handoff if another workflow is actually active.
        previous_run_active = await has_previous_active_run()

        if previous_run_active:

            await prepare_handoff(channel)

        else:

            logger.info(
                "No old bot is running. "
                "Starting without a handoff."
            )

        if shutdown_requested.is_set():
            return

        dashboard_message = await find_dashboard_message(channel)

        was_active = latest_data["rain_active"]

        if dashboard_message is None:

            await clear_previous_messages(channel)

            if was_active:
                dashboard_message = await send_active_message(channel)
            else:
                dashboard_message = await send_waiting_message(channel)

        else:

            try:

                if was_active:
                    await dashboard_message.edit(
                        embed=create_active_embed()
                    )
                else:
                    await dashboard_message.edit(
                        embed=create_waiting_embed()
                    )

            except discord.NotFound:

                dashboard_message = await send_waiting_message(channel)

        start_time = time.monotonic()
        next_run_dispatched = False
        last_dashboard_update = 0

        logger.info("Rain monitoring started.")

        while not shutdown_requested.is_set():

            if await check_for_handoff(channel):
                break

            elapsed = time.monotonic() - start_time

            # Start replacement after 27 minutes 30 seconds.
            if elapsed >= NEXT_RUN_AFTER and not next_run_dispatched:

                success = await dispatch_next_run()

                if success:
                    next_run_dispatched = True

            try:

                new_data = await fetch_rain_state(session)

                latest_data.update(new_data)

            except asyncio.CancelledError:
                raise

            except Exception:

                logger.exception("API request failed.")

                await asyncio.sleep(POLL_INTERVAL)
                continue

            rain_active = latest_data["rain_active"]

            # Rain started.
            if rain_active and not was_active:

                logger.info("RAIN STARTED.")

                await clear_previous_messages(channel)

                dashboard_message = await send_active_message(channel)

                was_active = True

            # Rain ended.
            elif not rain_active and was_active:

                logger.info("RAIN ENDED.")

                await clear_previous_messages(channel)

                dashboard_message = await send_waiting_message(channel)

                was_active = False

            # Update dashboard.
            now = time.monotonic()

            if now - last_dashboard_update >= DISCORD_UPDATE_INTERVAL:

                try:

                    if rain_active:
                        embed = create_active_embed()
                    else:
                        embed = create_waiting_embed()

                    await dashboard_message.edit(
                        embed=embed
                    )

                except discord.NotFound:

                    if rain_active:
                        dashboard_message = await send_active_message(channel)
                    else:
                        dashboard_message = await send_waiting_message(channel)

                except discord.HTTPException:

                    logger.exception(
                        "Discord dashboard update failed."
                    )

                last_dashboard_update = now

            await asyncio.sleep(POLL_INTERVAL)

    logger.info("Monitoring stopped.")

    # Actually disconnect the old bot after a successful handoff.
    if shutdown_requested.is_set():

        logger.info("Closing old Discord bot connection.")

        await bot.close()


# ============================================================
# DISCORD EVENTS
# ============================================================

@bot.event
async def on_ready():

    global monitor_task

    logger.info("Logged in as %s", bot.user)

    if monitor_task is None or monitor_task.done():

        monitor_task = asyncio.create_task(
            monitor_loop()
        )

        logger.info("Monitoring task started.")


# ============================================================
# MAIN
# ============================================================

async def main():

    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing.")

    logger.info("Starting DonutLuck API Rain Bot.")
    logger.info("Run ID: %s", RUN_ID)

    try:

        await bot.start(DISCORD_TOKEN)

    finally:

        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":

    try:

        asyncio.run(main())

    except KeyboardInterrupt:

        logger.info("Bot stopped manually.")
