
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
GITHUB_RUN_ID = os.getenv("GITHUB_RUN_ID", "local").strip()

WORKFLOW_FILE = "rain-api.yml"

POLL_INTERVAL = 1
DISCORD_UPDATE_INTERVAL = 1

BIG_RAIN_THRESHOLD = 1_500_000_000

NEXT_RUN_AFTER = 27 * 60 + 30
DISPATCH_RETRY_INTERVAL = 120

HANDOFF_WAIT_SECONDS = 45
HANDOFF_CHECK_INTERVAL = 2

API_TIMEOUT = 10
GITHUB_TIMEOUT = 15

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


# ============================================================
# STATE
# ============================================================

latest_data = {
    "timer": 0,
    "participants": 0,
    "pool": 0,
    "rain_active": False
}

last_handoff_check = 0


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

API_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://donutluck.com/"
}


async def fetch_rain_state(session):
    async with session.get(
        API_URL,
        headers=API_HEADERS,
        timeout=aiohttp.ClientTimeout(total=API_TIMEOUT)
    ) as response:

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
        value=f"```{format_timer(latest_data['timer'])}```",
        inline=True
    )

    embed.add_field(
        name="🌧️ Rain Pool",
        value=f"```{format_pool(latest_data['pool'])}```",
        inline=True
    )

    embed.set_footer(
        text="Live status • Updates automatically"
    )

    return embed


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
        value=f"```{format_timer(latest_data['timer'])}```",
        inline=True
    )

    embed.add_field(
        name="👥 Participants",
        value=f"```{latest_data['participants']}```",
        inline=True
    )

    embed.add_field(
        name="🌧️ Rain Pool",
        value=f"```{format_pool(latest_data['pool'])}```",
        inline=False
    )

    embed.set_footer(
        text="Live status • Updates automatically"
    )

    return embed


# ============================================================
# CHANNEL HELPERS
# ============================================================

async def clear_previous_messages(channel):
    try:
        await channel.purge(limit=None, bulk=True)
        logger.info("Channel cleanup completed.")

    except Exception:
        logger.exception("Could not clear channel.")


async def find_dashboard_message(channel):
    async for message in channel.history(limit=50):

        if message.author != bot.user:
            continue

        if not message.embeds:
            continue

        if message.embeds[0].title == "🌧️ DonutLuck Rain Monitor":
            return message

    return None


async def send_waiting_message(channel):
    message = await channel.send(
        embed=create_waiting_embed()
    )

    logger.info("Waiting dashboard sent.")
    return message


async def send_active_message(channel, announce=False):
    role_ids = []

    if announce:
        pool = latest_data["pool"]

        if pool >= BIG_RAIN_THRESHOLD:

            if RAIN_ROLE_ID:
                role_ids.append(RAIN_ROLE_ID)

            if BIG_RAIN_ROLE_ID:
                role_ids.append(BIG_RAIN_ROLE_ID)

            logger.info("BIG RAIN detected.")

        elif pool > 0 and RAIN_ROLE_ID:
            role_ids.append(RAIN_ROLE_ID)

    valid_role_ids = []

    for role_id in role_ids:
        try:
            valid_role_ids.append(int(role_id))

        except (ValueError, TypeError):
            logger.warning("Invalid role ID: %s", role_id)

    content = " ".join(
        f"<@&{role_id}>"
        for role_id in valid_role_ids
    )

    allowed_mentions = discord.AllowedMentions(
        roles=[
            discord.Object(id=role_id)
            for role_id in valid_role_ids
        ],
        everyone=False,
        users=False
    )

    message = await channel.send(
        content=content or None,
        embed=create_active_embed(),
        allowed_mentions=allowed_mentions
    )

    logger.info("Active rain dashboard sent.")
    return message


# ============================================================
# GITHUB API
# ============================================================

def github_headers():
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28"
    }


def github_workflow_runs_url():
    return (
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
        f"/actions/workflows/{WORKFLOW_FILE}/runs"
    )


async def get_workflow_runs(session):
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        return None

    try:
        async with session.get(
            github_workflow_runs_url(),
            headers=github_headers(),
            params={"per_page": 100},
            timeout=aiohttp.ClientTimeout(total=GITHUB_TIMEOUT)
        ) as response:

            if response.status != 200:
                body = await response.text()

                logger.warning(
                    "GitHub runs request failed: HTTP %s %s",
                    response.status,
                    body[:200]
                )

                return None

            data = await response.json()

        runs = data.get("workflow_runs", [])

        return [
            run for run in runs
            if run.get("path") == f".github/workflows/{WORKFLOW_FILE}"
        ]

    except Exception:
        logger.exception("Could not retrieve GitHub workflow runs.")
        return None


def active_run(run):
    return (
        run.get("status") in (
            "queued",
            "in_progress",
            "waiting",
            "requested",
            "pending"
        )
    )


async def find_newer_run(session):
    runs = await get_workflow_runs(session)

    if runs is None:
        return None

    try:
        current_id = int(GITHUB_RUN_ID)

    except ValueError:
        return None

    newer = []

    for run in runs:
        try:
            run_id = int(run.get("id", 0))

        except (ValueError, TypeError):
            continue

        if run_id > current_id and active_run(run):
            newer.append(run)

    if not newer:
        return None

    return max(
        newer,
        key=lambda run: int(run.get("id", 0))
    )


async def has_previous_active_run(session):
    runs = await get_workflow_runs(session)

    if runs is None:
        return None

    try:
        current_id = int(GITHUB_RUN_ID)

    except ValueError:
        return False

    for run in runs:
        try:
            run_id = int(run.get("id", 0))

        except (ValueError, TypeError):
            continue

        if run_id != current_id and active_run(run):
            return True

    return False


async def is_workflow_run_active(session, run_id):
    url = (
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
        f"/actions/runs/{run_id}"
    )

    try:
        async with session.get(
            url,
            headers=github_headers(),
            timeout=aiohttp.ClientTimeout(total=GITHUB_TIMEOUT)
        ) as response:

            if response.status != 200:
                return False

            data = await response.json()

        return (
            data.get("status") in (
                "queued",
                "in_progress",
                "waiting",
                "requested",
                "pending"
            )
            and data.get("path") == f".github/workflows/{WORKFLOW_FILE}"
        )

    except Exception:
        logger.exception("Could not verify workflow run.")
        return False


# ============================================================
# HANDOFF MESSAGES
# ============================================================

def create_handoff_embed(title, run_id):
    embed = discord.Embed(
        title=title,
        description=str(run_id),
        color=discord.Color.dark_grey()
    )

    return embed


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
    if GITHUB_RUN_ID == "local":
        return True

    try:
        request = await channel.send(
            embed=create_handoff_embed(
                HANDOFF_REQUEST_TITLE,
                GITHUB_RUN_ID
            )
        )

        logger.info(
            "Handoff request sent for run %s.",
            GITHUB_RUN_ID
        )

        start = time.monotonic()

        while time.monotonic() - start < HANDOFF_WAIT_SECONDS:

            ack = await find_handoff_message(
                channel,
                HANDOFF_ACK_TITLE,
                GITHUB_RUN_ID
            )

            if ack:
                try:
                    await request.delete()
                    await ack.delete()

                except discord.NotFound:
                    pass

                logger.info("Handoff acknowledged.")
                return True

            await asyncio.sleep(1)

        try:
            await request.delete()

        except discord.NotFound:
            pass

        logger.warning("Handoff acknowledgement timed out.")
        return False

    except Exception:
        logger.exception("Handoff preparation failed.")
        return False


async def check_for_handoff(channel, session):
    global last_handoff_check

    now = time.monotonic()

    if now - last_handoff_check < HANDOFF_CHECK_INTERVAL:
        return

    last_handoff_check = now

    try:
        async for message in channel.history(limit=50):

            if message.author != bot.user:
                continue

            if not message.embeds:
                continue

            embed = message.embeds[0]

            if embed.title != HANDOFF_REQUEST_TITLE:
                continue

            try:
                requested_run_id = int(embed.description)

            except (ValueError, TypeError):
                continue

            try:
                current_run_id = int(GITHUB_RUN_ID)

            except ValueError:
                continue

            if requested_run_id <= current_run_id:
                continue

            is_active = await is_workflow_run_active(
                session,
                requested_run_id
            )

            if not is_active:
                continue

            existing_ack = await find_handoff_message(
                channel,
                HANDOFF_ACK_TITLE,
                str(requested_run_id)
            )

            if existing_ack:
                shutdown_requested.set()
                return

            await channel.send(
                embed=create_handoff_embed(
                    HANDOFF_ACK_TITLE,
                    requested_run_id
                )
            )

            logger.info(
                "Acknowledged handoff to run %s.",
                requested_run_id
            )

            shutdown_requested.set()
            return

    except Exception:
        logger.exception("Handoff check failed.")


# ============================================================
# DISPATCH NEXT WORKFLOW
# ============================================================

async def dispatch_next_run(session):
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        logger.error("Missing GitHub token or repository.")
        return False

    url = (
        f"https://api.github.com/repos/{GITHUB_REPOSITORY}"
        f"/actions/workflows/{WORKFLOW_FILE}/dispatches"
    )

    payload = {
        "ref": GITHUB_REF_NAME
    }

    try:
        async with session.post(
            url,
            headers=github_headers(),
            json=payload,
            timeout=aiohttp.ClientTimeout(total=GITHUB_TIMEOUT)
        ) as response:

            if response.status == 204:
                logger.info("Next workflow dispatch accepted.")
                return True

            body = await response.text()

            logger.error(
                "Workflow dispatch failed: HTTP %s %s",
                response.status,
                body[:300]
            )

            return False

    except Exception:
        logger.exception("Workflow dispatch request failed.")
        return False


# ============================================================
# STARTUP COORDINATION
# ============================================================

async def coordinate_startup(channel, session):
    if GITHUB_RUN_ID == "local":
        logger.info("Local run: GitHub coordination skipped.")
        return True

    for attempt in range(12):

        newer_run = await find_newer_run(session)

        if newer_run:
            logger.warning(
                "A newer workflow run already exists: %s. Exiting.",
                newer_run.get("id")
            )
            return False

        previous = await has_previous_active_run(session)

        if previous is None:
            logger.warning(
                "Could not verify existing workflow runs. "
                "Retrying startup check."
            )

            await asyncio.sleep(5)
            continue

        if not previous:
            logger.info("No previous active workflow run.")
            return True

        logger.info(
            "Previous workflow run detected. Requesting handoff."
        )

        success = await prepare_handoff(channel)

        if success:
            return True

        # If another run has appeared, this run must not take over.
        newer_run = await find_newer_run(session)

        if newer_run:
            logger.warning(
                "Another newer run appeared during handoff. Exiting."
            )
            return False

        logger.warning(
            "Handoff was not acknowledged. Retrying startup."
        )

        await asyncio.sleep(5)

    logger.error(
        "Could not safely coordinate startup. Exiting."
    )

    return False


# ============================================================
# MONITOR LOOP
# ============================================================

async def monitor_loop():
    global latest_data

    started_at = time.monotonic()
    last_dispatch_attempt = 0
    replacement_seen = False

    channel = bot.get_channel(DISCORD_CHANNEL_ID)

    if channel is None:
        channel = await bot.fetch_channel(DISCORD_CHANNEL_ID)

    if not isinstance(channel, discord.TextChannel):
        logger.error("Configured channel is not a text channel.")
        await bot.close()
        return

    timeout = aiohttp.ClientTimeout(
        total=API_TIMEOUT
    )

    async with aiohttp.ClientSession(timeout=timeout) as session:

        # ----------------------------------------------------
        # Initial API connection
        # ----------------------------------------------------

        initial_data = None

        for attempt in range(12):

            try:
                initial_data = await fetch_rain_state(session)
                break

            except Exception:
                logger.exception(
                    "Initial API check failed (%s/12).",
                    attempt + 1
                )

                await asyncio.sleep(5)

        if initial_data is None:
            logger.error("Initial API connection failed.")
            await bot.close()
            return

        latest_data = initial_data

        logger.info(
            "Initial API state: Timer=%s | Pool=%s | Active=%s",
            format_timer(latest_data["timer"]),
            format_pool(latest_data["pool"]),
            latest_data["rain_active"]
        )

        # ----------------------------------------------------
        # Startup coordination
        # ----------------------------------------------------

        can_start = await coordinate_startup(
            channel,
            session
        )

        if not can_start:
            logger.warning("This workflow run is not taking over.")
            await bot.close()
            return

        # ----------------------------------------------------
        # Dashboard initialization
        # ----------------------------------------------------

        dashboard = await find_dashboard_message(channel)

        if dashboard is None:
            logger.info("No existing dashboard found.")
            await clear_previous_messages(channel)

            if latest_data["rain_active"]:
                dashboard = await send_active_message(
                    channel,
                    announce=False
                )
            else:
                dashboard = await send_waiting_message(channel)

        else:
            if latest_data["rain_active"]:
                await dashboard.edit(
                    embed=create_active_embed()
                )
            else:
                await dashboard.edit(
                    embed=create_waiting_embed()
                )

            logger.info("Existing dashboard reused.")

        previous_active = latest_data["rain_active"]

        # ----------------------------------------------------
        # Main monitoring loop
        # ----------------------------------------------------

        while not shutdown_requested.is_set():

            await check_for_handoff(
                channel,
                session
            )

            if shutdown_requested.is_set():
                break

            elapsed = time.monotonic() - started_at

            # ------------------------------------------------
            # Replacement workflow management
            # ------------------------------------------------

            if elapsed >= NEXT_RUN_AFTER:

                newer_run = await find_newer_run(session)

                if newer_run:
                    replacement_seen = True

                    logger.info(
                        "Replacement workflow detected: %s (%s).",
                        newer_run.get("id"),
                        newer_run.get("status")
                    )

                    if newer_run.get("status") in (
                        "completed",
                        "cancelled"
                    ):
                        replacement_seen = False

                if (
                    not replacement_seen
                    and time.monotonic() - last_dispatch_attempt
                    >= DISPATCH_RETRY_INTERVAL
                ):

                    last_dispatch_attempt = time.monotonic()

                    dispatched = await dispatch_next_run(
                        session
                    )

                    if dispatched:
                        logger.info(
                            "Waiting for replacement workflow to appear."
                        )

            # ------------------------------------------------
            # Fetch rain state
            # ------------------------------------------------

            try:
                new_data = await fetch_rain_state(session)

            except asyncio.CancelledError:
                raise

            except Exception:
                logger.exception("Rain API request failed.")
                await asyncio.sleep(POLL_INTERVAL)
                continue

            latest_data = new_data

            current_active = latest_data["rain_active"]

            logger.info(
                "Timer=%s | Pool=%s | Participants=%s | Active=%s",
                format_timer(latest_data["timer"]),
                format_pool(latest_data["pool"]),
                latest_data["participants"],
                current_active
            )

            # ------------------------------------------------
            # Rain state transition
            # ------------------------------------------------

            if current_active != previous_active:

                logger.info(
                    "Rain state changed: %s -> %s",
                    previous_active,
                    current_active
                )

                await clear_previous_messages(channel)

                if current_active:
                    dashboard = await send_active_message(
                        channel,
                        announce=True
                    )

                else:
                    dashboard = await send_waiting_message(
                        channel
                    )

                previous_active = current_active

            # ------------------------------------------------
            # Update existing dashboard
            # ------------------------------------------------

            else:

                try:
                    if dashboard is None:
                        dashboard = await find_dashboard_message(
                            channel
                        )

                    if dashboard is None:
                        if current_active:
                            dashboard = await send_active_message(
                                channel,
                                announce=False
                            )
                        else:
                            dashboard = await send_waiting_message(
                                channel
                            )

                    else:
                        embed = (
                            create_active_embed()
                            if current_active
                            else create_waiting_embed()
                        )

                        await dashboard.edit(embed=embed)

                except discord.NotFound:
                    logger.warning(
                        "Dashboard message disappeared. Recreating."
                    )

                    if current_active:
                        dashboard = await send_active_message(
                            channel,
                            announce=False
                        )
                    else:
                        dashboard = await send_waiting_message(
                            channel
                        )

                except discord.HTTPException:
                    logger.exception(
                        "Could not update Discord dashboard."
                    )

                except Exception:
                    logger.exception(
                        "Unexpected dashboard update error."
                    )

            await asyncio.sleep(DISCORD_UPDATE_INTERVAL)

    logger.info("Monitor loop ended.")

    if not bot.is_closed():
        await bot.close()


# ============================================================
# DISCORD EVENTS
# ============================================================

@bot.event
async def on_ready():
    global monitor_task

    logger.info(
        "Logged in as %s (%s)",
        bot.user,
        bot.user.id
    )

    if monitor_task is None or monitor_task.done():
        monitor_task = asyncio.create_task(
            monitor_loop()
        )


@bot.event
async def on_disconnect():
    logger.warning("Discord connection lost.")


@bot.event
async def on_resumed():
    logger.info("Discord connection resumed.")


# ============================================================
# MAIN
# ============================================================

async def main():
    if not DISCORD_TOKEN:
        raise RuntimeError("DISCORD_TOKEN is missing.")

    if not DISCORD_CHANNEL_ID:
        raise RuntimeError("DISCORD_CHANNEL_ID is missing.")

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

    except Exception:
        logger.exception("Fatal bot error.")
