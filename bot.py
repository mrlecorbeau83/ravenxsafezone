import discord
from discord.ext import commands
from dotenv import load_dotenv
import os

load_dotenv()


class SaveZoneBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.bans = True
        intents.voice_states = True
        intents.guilds = True
        super().__init__(
            command_prefix=os.getenv("BOT_PREFIX", "!"),
            intents=intents,
            help_command=None,
        )
        self._synced = False

    async def on_ready(self):
        print(f"[SaveZone] Connecté : {self.user} ({self.user.id})")
        print(f"[SaveZone] {len(self.guilds)} serveur(s) | préfixe : {self.command_prefix}")
        if not self._synced:
            synced = await self.tree.sync()
            self._synced = True
            print(f"[SaveZone] {len(synced)} slash command(s) synchronisée(s)")

    async def on_guild_join(self, guild: discord.Guild):
        print(f"[SaveZone] Nouveau serveur rejoint : {guild.name} ({guild.id})")

    async def on_command_error(self, ctx, error):
        if isinstance(error, (commands.CommandNotFound, commands.CheckFailure)):
            return
        print(f"[SaveZone CMD ERROR] {ctx.command}: {error}")
