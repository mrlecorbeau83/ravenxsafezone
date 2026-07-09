import asyncio
import os
from dotenv import load_dotenv
from bot import SafeZoneBot

load_dotenv()

COGS = [
    "cogs.global_mod",
]


async def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN manquant dans le .env")

    bot = SafeZoneBot()
    async with bot:
        for cog in COGS:
            try:
                await bot.load_extension(cog)
                print(f"[SafeZone] ✅ {cog}")
            except Exception as e:
                print(f"[SafeZone] ❌ {cog}: {e}")
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
