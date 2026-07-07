import discord
from discord.ext import commands
from discord import app_commands
import asyncio
import os
import json
import time

OWNER_ID = int(os.getenv("PROTECTED_USER_ID", "1458518253373493353"))
# Super admins : proprio + co-admins pouvant gérer le bot (mêmes IDs que global_mod.py)
SUPER_ADMIN_IDS = {OWNER_ID, 1335581687760949313}
REBOOT_FILE = ".reboot_pending.json"


class AdminCog(commands.Cog, name="Admin"):
    def __init__(self, bot):
        self.bot = bot
        self._start_time = time.time()

    async def cog_load(self):
        asyncio.create_task(self._check_post_reboot())

    def _uptime(self) -> str:
        elapsed = int(time.time() - self._start_time)
        h, r = divmod(elapsed, 3600)
        m, s = divmod(r, 60)
        return f"{h}h {m}m {s}s" if h else f"{m}m {s}s"

    async def _check_post_reboot(self):
        if not os.path.exists(REBOOT_FILE):
            return
        try:
            with open(REBOOT_FILE) as f:
                data = json.load(f)
            os.remove(REBOOT_FILE)
            await self.bot.wait_until_ready()
            channel = self.bot.get_channel(data["channel_id"])
            if not channel:
                return
            elapsed = round(time.time() - data["timestamp"])
            embed = discord.Embed(
                title="✅ Bot redémarré",
                color=0x57F287,
                timestamp=discord.utils.utcnow()
            )
            embed.add_field(name="⏱️ Temps de reboot", value=f"**{elapsed}s**", inline=True)
            embed.add_field(name="Serveurs", value=str(len(self.bot.guilds)), inline=True)
            embed.add_field(name="Latence", value=f"{round(self.bot.latency * 1000)}ms", inline=True)
            embed.set_footer(text=f"Demandé par {data.get('author', 'owner')}")
            await channel.send(embed=embed)
        except Exception as e:
            print(f"[Reboot] Erreur post-reboot : {e}")

    # sync les slash commands
    @commands.command(name="sync")
    async def sync_cmd(self, ctx, guild_id: str = None):
        if ctx.author.id not in SUPER_ADMIN_IDS:
            return
        if guild_id:
            try:
                guild = discord.Object(id=int(guild_id))
                self.bot.tree.copy_global_to(guild=guild)
                synced = await self.bot.tree.sync(guild=guild)
                await ctx.send(f"✅ {len(synced)} commande(s) sync sur le serveur `{guild_id}`")
            except Exception as e:
                await ctx.send(f"❌ Erreur : {e}")
        else:
            synced = await self.bot.tree.sync()
            await ctx.send(f"✅ {len(synced)} commande(s) sync globalement (1h de délai Discord)")

    @commands.command(name="reload")
    async def reload_cmd(self, ctx, cog: str = None):
        if ctx.author.id not in SUPER_ADMIN_IDS:
            return
        if cog == "all" or cog is None:
            reloaded = []
            failed = []
            for ext in list(self.bot.extensions.keys()):
                try:
                    await self.bot.reload_extension(ext)
                    reloaded.append(ext)
                except Exception as e:
                    failed.append(f"{ext}: {e}")
            msg = f"✅ Rechargé : {', '.join(reloaded)}"
            if failed:
                msg += f"\n❌ Échecs : {', '.join(failed)}"
            await ctx.send(msg)
        else:
            ext = cog if "." in cog else f"cogs.{cog}"
            try:
                await self.bot.reload_extension(ext)
                await ctx.send(f"✅ `{ext}` rechargé")
            except Exception as e:
                await ctx.send(f"❌ Erreur : {e}")

    @commands.command(name="cogs")
    async def list_cogs(self, ctx):
        if ctx.author.id not in SUPER_ADMIN_IDS:
            return
        lines = [f"• `{ext}`" for ext in self.bot.extensions.keys()]
        await ctx.send("**Cogs chargés :**\n" + "\n".join(lines))

    @commands.command(name="bots")
    async def bots_info(self, ctx):
        if ctx.author.id not in SUPER_ADMIN_IDS:
            return
        bot_name = os.getenv("BOT_NAME", "savezone")
        guilds = len(self.bot.guilds)
        users = sum(g.member_count or 0 for g in self.bot.guilds)
        latency = round(self.bot.latency * 1000)
        embed = discord.Embed(title=f"🤖 SaveZone Bot — {bot_name}", color=0x5865F2)
        embed.add_field(name="Serveurs", value=str(guilds), inline=True)
        embed.add_field(name="Utilisateurs", value=str(users), inline=True)
        embed.add_field(name="Latence", value=f"{latency}ms", inline=True)
        embed.add_field(name="Uptime", value=self._uptime(), inline=True)
        embed.add_field(name="BOT_NAME", value=f"`{bot_name}`", inline=True)
        shared_db = os.getenv("SHARED_GLOBAL_DB_PATH", "non configuré")
        embed.add_field(name="DB Partagée", value=f"`{shared_db}`", inline=False)
        await ctx.send(embed=embed)

    @commands.command(name="reboot")
    async def reboot_cmd(self, ctx):
        if ctx.author.id not in SUPER_ADMIN_IDS:
            return

        embed = discord.Embed(
            title="🔄 Redémarrer le bot ?",
            color=discord.Color.orange()
        )
        embed.add_field(name="Uptime actuel", value=self._uptime(), inline=True)
        embed.add_field(name="Serveurs", value=str(len(self.bot.guilds)), inline=True)
        embed.add_field(name="Latence", value=f"{round(self.bot.latency * 1000)}ms", inline=True)

        view = discord.ui.View(timeout=30)
        btn_confirm = discord.ui.Button(label="✅ Confirmer", style=discord.ButtonStyle.danger)
        btn_cancel  = discord.ui.Button(label="❌ Annuler",   style=discord.ButtonStyle.secondary)

        async def on_confirm(interaction: discord.Interaction):
            if interaction.user.id not in SUPER_ADMIN_IDS:
                return await interaction.response.send_message("❌ Pas pour toi.", ephemeral=True)
            view.stop()
            with open(REBOOT_FILE, "w") as f:
                json.dump({
                    "channel_id": ctx.channel.id,
                    "timestamp": time.time(),
                    "author": str(ctx.author),
                }, f)
            bye = discord.Embed(
                title="🔄 Redémarrage en cours…",
                description="Je vous préviens dès mon retour !",
                color=discord.Color.orange()
            )
            bye.set_footer(text=f"Uptime : {self._uptime()}")
            await interaction.response.edit_message(embed=bye, view=None)
            await asyncio.sleep(1.5)
            await self.bot.close()

        async def on_cancel(interaction: discord.Interaction):
            if interaction.user.id not in SUPER_ADMIN_IDS:
                return await interaction.response.send_message("❌ Pas pour toi.", ephemeral=True)
            view.stop()
            await interaction.response.edit_message(
                embed=discord.Embed(title="❌ Redémarrage annulé", color=discord.Color.greyple()),
                view=None
            )

        btn_confirm.callback = on_confirm
        btn_cancel.callback  = on_cancel
        view.add_item(btn_confirm)
        view.add_item(btn_cancel)
        await ctx.send(embed=embed, view=view)


async def setup(bot):
    await bot.add_cog(AdminCog(bot))
