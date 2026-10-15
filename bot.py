import os
import asyncio
import logging

import aiohttp
import discord

RAIN_URL = "https://donutluck.com/api/chat/rain/state"
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
CHANNEL_ID = os.getenv("DISCORD_CHANNEL_ID")

POLL_SECONDS = 15

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

log = logging.getLogger("donutluck-monitor")


class RainMonitor(discord.Client):
    async def setup_hook(self):
        self.monitor_task = asyncio.create_task(
            self.monitor_rain()
        )

    async def on_ready(self):
        log.info("Discord connected as %s", self.user)
        log.info("DonutLuck rain monitor started.")

    async def monitor_rain(self):
        previous_active = None
        last_error = None
        timeout = aiohttp.ClientTimeout(total=10)

        async with aiohttp.ClientSession(
            timeout=timeout
        ) as session:

            while not self.is_closed():
                try:
                    async with session.get(
                        RAIN_URL,
                        headers={"Accept": "application/json"},
                    ) as response:

                        if response.status != 200:
                            error = (
                                f"DonutLuck returned HTTP "
                                f"{response.status}"
                            )

                            if error != last_error:
                                log.error(error)
                                last_error = error

                            await asyncio.sleep(60)
                            continue

                        data = await response.json(
                            content_type=None
                        )

                    active = bool(data["active"])
                    remaining = max(
                        0, int(data["time_remaining"])
                    )
                    participants = int(
                        data["participants_count"]
                    )
                    prize = int(data["prize"])

                    if last_error is not None:
                        log.info("Rain endpoint is responding again.")
                        last_error = None

                    log.info(
                        "Active=%s | Remaining=%ss | "
                        "Participants=%s | Prize=%s",
                        active,
                        remaining,
                        participants,
                        prize,
                    )

                    if active and previous_active is not True:
                        channel = self.get_channel(
                            int(CHANNEL_ID)
                        )

                        if channel is None:
                            channel = await self.fetch_channel(
                                int(CHANNEL_ID)
                            )

                        embed = discord.Embed(
                            title="🌧️ DonutLuck Rain Started!",
                            description=(
                                "A rain is active on DonutLuck."
                            ),
                            color=discord.Color.blue(),
                        )

                        embed.add_field(
                            name="Time remaining",
                            value=f"{remaining} seconds",
                        )
                        embed.add_field(
                            name="Participants",
                            value=f"{participants:,}",
                        )
                        embed.add_field(
                            name="Raw prize",
                            value=f"{prize:,}",
                        )
                        embed.set_footer(
                            text="Read-only alert • No automatic joining"
                        )

                        await channel.send(embed=embed)
                        log.info("Discord notification sent.")

                    previous_active = active

                except asyncio.CancelledError:
                    raise

                except Exception:
                    log.exception(
                        "Monitor error; retrying shortly."
                    )

                await asyncio.sleep(POLL_SECONDS)


def main():
    if not DISCORD_TOKEN:
        raise RuntimeError(
            "Missing DISCORD_TOKEN environment variable."
        )

    if not CHANNEL_ID:
        raise RuntimeError(
            "Missing DISCORD_CHANNEL_ID environment variable."
        )

    intents = discord.Intents.none()
    client = RainMonitor(intents=intents)
    client.run(DISCORD_TOKEN)


if __name__ == "__main__":
    main()
