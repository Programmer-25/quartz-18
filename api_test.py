
import asyncio
import json
import logging
from datetime import datetime

import aiohttp


# ============================================================
# DONUTLUCK API TEST
# ============================================================

API_URL = "https://donutluck.com/api/chat/rain/state"

POLL_INTERVAL = 1.0
LOG_INTERVAL = 5.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s"
)

logger = logging.getLogger("DonutLuckAPI")


async def fetch_rain_state(session):
    async with session.get(API_URL) as response:
        if response.status != 200:
            body = await response.text()
            raise RuntimeError(
                f"HTTP {response.status}: {body[:300]}"
            )

        return await response.json(content_type=None)


async def main():
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/131.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://donutluck.com/",
    }

    timeout = aiohttp.ClientTimeout(total=10)

    previous_state = None
    last_log_time = 0
    consecutive_errors = 0

    logger.info("Starting DonutLuck API monitor...")
    logger.info("Endpoint: %s", API_URL)

    async with aiohttp.ClientSession(
        headers=headers,
        timeout=timeout
    ) as session:

        while True:
            try:
                data = await fetch_rain_state(session)

                if not isinstance(data, dict):
                    raise ValueError("Unexpected API response format")

                active = data.get("active")
                timer = data.get("time_remaining")
                participants = data.get("participants_count")
                prize = data.get("prize")

                if active is None or timer is None or prize is None:
                    raise ValueError(
                        f"Missing expected fields: {data}"
                    )

                state = (
                    active,
                    timer,
                    participants,
                    prize
                )

                if state != previous_state:
                    logger.info(
                        "STATE CHANGE | Active: %s | "
                        "Timer: %s | Participants: %s | Prize: %s",
                        active,
                        timer,
                        participants,
                        prize
                    )

                    previous_state = state

                now = asyncio.get_running_loop().time()

                if now - last_log_time >= LOG_INTERVAL:
                    logger.info(
                        "RAIN DEBUG | Active: %s | "
                        "Time remaining: %s | "
                        "Participants: %s | Prize: %s",
                        active,
                        timer,
                        participants,
                        prize
                    )

                    last_log_time = now

                consecutive_errors = 0

            except asyncio.CancelledError:
                raise

            except Exception as error:
                consecutive_errors += 1

                logger.error(
                    "API request failed (%s): %s",
                    consecutive_errors,
                    error
                )

                if consecutive_errors >= 5:
                    logger.warning(
                        "Repeated API failures. "
                        "Check whether DonutLuck is blocking direct requests."
                    )

            await asyncio.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("API monitor stopped.")
