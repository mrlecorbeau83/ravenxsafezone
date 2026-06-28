import asyncio
import os
from dotenv import load_dotenv
from bot import SaveZoneBot

load_dotenv()

COGS = [
    "cogs.global_mod",
    "cogs.logs",
    "cogs.admin",
]


async def main():
    token = os.getenv("DISCORD_TOKEN")
    if not token:
        raise ValueError("DISCORD_TOKEN manquant dans le .env")

    bot = SaveZoneBot()
    async with bot:
        for cog in COGS:
            try:
                await bot.load_extension(cog)
                print(f"[SaveZone] ✅ {cog}")
            except Exception as e:
                print(f"[SaveZone] ❌ {cog}: {e}")
        await bot.start(token)


if __name__ == "__main__":
    asyncio.run(main())
