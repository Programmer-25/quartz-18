
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
HANDOFF_CHECK_INTERVAL = 3

API_TIMEOUT = 10
STARTUP_CHECK_RETRIES = 12

RUN_ID = os.getenv("GITHUB_RUN_ID", "local")

HANDOFF_REQUEST_TITLE = "RAIN-HANDOFF-REQUEST"
HANDOFF_ACK_TITLE = "RAIN-HANDOFF-ACK"


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
# GITHUB WORKFLOW HELPERS
# ============================================================

def github_headers():

    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }


async def has_previous_active_run():

    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:

        logger.error(
            "GitHub credentials are missing. "
            "Cannot safely check for another running bot."
        )

        return None

    if RUN_ID == "local":

        logger.info(
            "Local run detected. Skipping startup handoff check."
        )

        return False

    url = (
        f"https://api.github.com/repos/"
        f"{GITHUB_REPOSITORY}/actions/workflows/"
        f"{WORKFLOW_FILE}/runs"
    )

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
                headers=github_headers(),
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

                    return None

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

        return None


async def is_workflow_run_active(session, run_id):

    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        return False

    url = (
        f"https://api.github.com/repos/"
        f"{GITHUB_REPOSITORY}/actions/runs/{run_id}"
    )

    try:

        async with session.get(
            url,
            headers=github_headers()
        ) as response:

            if response.status == 404:
                return False

            if response.status != 200:

                body = await response.text()

                logger.warning(
                    "Could not verify handoff run %s: "
                    "HTTP %s | %s",
                    run_id,
                    response.status,
                    body[:200]
                )

                return False

            data = await response.json()

        if data.get("status") != "in_progress":
            return False

        expected_path = f".github/workflows/{WORKFLOW_FILE}"
        actual_path = data.get("path", "")

        if actual_path != expected_path:
            logger.warning(
                "Handoff request %s belongs to another workflow.",
                run_id
            )
            return False

        return True

    except Exception:

        logger.exception(
            "Could not verify GitHub run %s.",
            run_id
        )

        return False


# ============================================================
# MESSAGE-BASED HANDOFF
# ============================================================

def create_handoff_embed(title, run_id):

    return discord.Embed(
        title=title,
        description=str(run_id),
        color=discord.Color.dark_grey()
    )


async def find_handoff_message(channel, title, run_id):

    async for message in channel.history(limit=50):

        if message.author != bot.user:
            continue

        if not message.embeds:
            continue

        embed = message.embeds[0]

        if (
            embed.title == title
            and embed.description == str(run_id)
        ):
            return message

    return None


async def prepare_handoff(channel):

    logger.info(
        "Requesting handoff using Discord messages. Run ID: %s",
        RUN_ID
    )

    request_message = None

    try:

        request_message = await channel.send(
            embed=create_handoff_embed(
                HANDOFF_REQUEST_TITLE,
                RUN_ID
            )
        )

    except Exception:

        logger.exception(
            "Could not send handoff request message."
        )

        return False

    start = time.monotonic()

    while time.monotonic() - start < HANDOFF_WAIT_SECONDS:

        try:

            ack_message = await find_handoff_message(
                channel,
                HANDOFF_ACK_TITLE,
                RUN_ID
            )

            if ack_message is not None:

                logger.info(
                    "Previous bot acknowledged handoff."
                )

                try:
                    await request_message.delete()
                except discord.NotFound:
                    pass

                try:
                    await ack_message.delete()
                except discord.NotFound:
                    pass

                return True

        except Exception:

            logger.exception(
                "Could not check handoff acknowledgement."
            )

        await asyncio.sleep(1)

    logger.error(
        "Handoff acknowledgement timed out. "
        "Replacement will not take control."
    )

    try:
        await request_message.delete()
    except Exception:
        logger.warning(
            "Could not delete timed-out handoff request."
        )

    return False


async def check_for_handoff(channel, session):

    global last_handoff_check

    now = time.monotonic()

    if now - last_handoff_check < HANDOFF_CHECK_INTERVAL:
        return False

    last_handoff_check = now

    try:

        request_messages = []

        async for message in channel.history(limit=50):

            if message.author != bot.user:
                continue

            if not message.embeds:
                continue

            embed = message.embeds[0]

            if embed.title != HANDOFF_REQUEST_TITLE:
                continue

            requested_run = (embed.description or "").strip()

            if not requested_run or requested_run == RUN_ID:
                continue

            request_messages.append(
                (message, requested_run)
            )

        for request_message, requested_run in request_messages:

            # A message alone is not enough.
            # Verify the requesting GitHub Actions run is active.
            active = await is_workflow_run_active(
                session,
                requested_run
            )

            if not active:

                logger.info(
                    "Ignoring stale or inactive handoff request: %s",
                    requested_run
                )

                try:
                    await request_message.delete()
                except Exception:
                    pass

                continue

            logger.info(
                "Verified replacement run %s is active. "
                "Acknowledging handoff.",
                requested_run
            )

            ack_message = await channel.send(
                embed=create_handoff_embed(
                    HANDOFF_ACK_TITLE,
                    requested_run
                )
            )

            logger.info(
                "Handoff acknowledged. Shutting down old bot."
            )

            shutdown_requested.set()

            return True

    except Exception:

        logger.exception("Handoff check failed.")

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
                headers=github_headers(),
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

        # Wait for a reliable GitHub startup check.
        previous_run_active = None

        if RUN_ID != "local":

            for attempt in range(STARTUP_CHECK_RETRIES):

                previous_run_active = await has_previous_active_run()

                if previous_run_active is not None:
                    break

                logger.warning(
                    "GitHub startup check failed. Retry %s/%s.",
                    attempt + 1,
                    STARTUP_CHECK_RETRIES
                )

                await asyncio.sleep(5)

            if previous_run_active is None:

                logger.error(
                    "Could not determine whether an old bot is running. "
                    "Exiting to avoid duplicate dashboard controllers."
                )

                await bot.close()
                return

        else:

            previous_run_active = False

        # Only request handoff when an older run is confirmed active.
        if previous_run_active:

            handoff_success = await prepare_handoff(channel)

            if not handoff_success:

                logger.error(
                    "Handoff failed. Keeping the existing bot in control. "
                    "This replacement will exit."
                )

                await bot.close()
                return

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

                if was_active:
                    dashboard_message = await send_active_message(channel)
                else:
                    dashboard_message = await send_waiting_message(channel)

        start_time = time.monotonic()
        next_run_dispatched = False
        last_dashboard_update = 0

        logger.info("Rain monitoring started.")

        while not shutdown_requested.is_set():

            if await check_for_handoff(channel, session):
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

    # Disconnect the old bot after successful handoff.
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
