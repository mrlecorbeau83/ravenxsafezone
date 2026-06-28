import discord
from discord.ext import commands
from discord import app_commands
from datetime import datetime, timezone
import asyncio
import sqlite3
import os

# ---
# LOG TYPES disponibles (utilisés comme clés en DB et dans /logsetup)
# ---
LOG_TYPES = {
    "member":    "Membres (join/leave/update)",
    "ban":       "Bans / Unbans",
    "delete":    "Messages supprimés",
    "edit":      "Messages modifiés",
    "roles":     "Rôles (création/suppression/modif)",
    "channels":  "Salons (création/suppression/modif)",
    "threads":   "Threads",
    "voice":     "Vocal (connect/disconnect/move)",
    "guild":     "Serveur (invitations, modifications)",
    "automod":   "AutoMod actions",
    "scheduled": "Événements planifiés",
    "global_ban":"Global Ban réseau",
}

DB_PATH = os.getenv("LOGS_DB_PATH", "data/sz_logs_config.db")


def get_db():
    os.makedirs(os.path.dirname(os.path.abspath(DB_PATH)), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS log_channels (
        guild_id   TEXT NOT NULL,
        log_type   TEXT NOT NULL,
        channel_id TEXT NOT NULL,
        PRIMARY KEY (guild_id, log_type))""")
    conn.commit()
    return conn


def get_log_channel_id(guild_id: int, log_type: str) -> int | None:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT channel_id FROM log_channels WHERE guild_id=? AND log_type=?",
                (str(guild_id), log_type))
    row = cur.fetchone()
    conn.close()
    return int(row[0]) if row else None


def set_log_channel(guild_id: int, log_type: str, channel_id: int):
    conn = get_db()
    conn.execute(
        "INSERT OR REPLACE INTO log_channels (guild_id, log_type, channel_id) VALUES (?, ?, ?)",
        (str(guild_id), log_type, str(channel_id))
    )
    conn.commit()
    conn.close()


def remove_log_channel(guild_id: int, log_type: str):
    conn = get_db()
    conn.execute("DELETE FROM log_channels WHERE guild_id=? AND log_type=?",
                 (str(guild_id), log_type))
    conn.commit()
    conn.close()


def get_all_log_channels(guild_id: int) -> dict:
    conn = get_db()
    cur = conn.cursor()
    cur.execute("SELECT log_type, channel_id FROM log_channels WHERE guild_id=?", (str(guild_id),))
    result = {row[0]: int(row[1]) for row in cur.fetchall()}
    conn.close()
    return result


class LogsCog(commands.Cog, name="Logs"):
    def __init__(self, bot):
        self.bot = bot
        # une queue par salon pour pas spam rate limit
        self._queues: dict[int, asyncio.Queue] = {}
        self._workers: dict[int, asyncio.Task] = {}

    async def cog_load(self):
        print("[SaveZone] LogsCog chargé")

    def cog_unload(self):
        for task in self._workers.values():
            task.cancel()

    # --- helpers queue ---

    def get_queue(self, channel_id: int) -> asyncio.Queue:
        if channel_id not in self._queues:
            self._queues[channel_id] = asyncio.Queue()
            self._workers[channel_id] = asyncio.create_task(self._worker(channel_id))
        return self._queues[channel_id]

    async def _worker(self, channel_id: int):
        q = self._queues[channel_id]
        while True:
            embed, files = await q.get()
            try:
                ch = self.bot.get_channel(channel_id)
                if ch:
                    await ch.send(embed=embed, files=files or [])
            except discord.HTTPException:
                pass
            except Exception:
                pass
            await asyncio.sleep(0.6)
            q.task_done()

    async def send_log(self, guild_id: int, log_type: str, embed: discord.Embed, files=None):
        ch_id = get_log_channel_id(guild_id, log_type)
        if not ch_id:
            return
        q = self.get_queue(ch_id)
        await q.put((embed, files or []))

    def ts(self):
        return datetime.now(timezone.utc)

    # --- MEMBRES ---

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        embed = discord.Embed(
            title="📥 Membre rejoint",
            color=0x57F287,
            timestamp=self.ts()
        )
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.add_field(name="ID", value=str(member.id), inline=True)
        created = member.created_at.strftime("%d/%m/%Y")
        embed.add_field(name="Compte créé", value=created, inline=True)
        embed.set_footer(text=f"Membres : {member.guild.member_count}")
        await self.send_log(member.guild.id, "member", embed)

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        # peut être un kick, on vérifie l'audit log
        action_label = "📤 Membre parti"
        banner = None
        reason = None
        try:
            await asyncio.sleep(0.5)
            async for entry in member.guild.audit_logs(limit=5, action=discord.AuditLogAction.kick):
                if entry.target and entry.target.id == member.id:
                    action_label = "👢 Membre kické"
                    banner = entry.user
                    reason = entry.reason
                    break
        except Exception:
            pass
        embed = discord.Embed(title=action_label, color=0xFEE75C, timestamp=self.ts())
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.add_field(name="ID", value=str(member.id), inline=True)
        roles = [r.mention for r in member.roles if r.name != "@everyone"]
        if roles:
            embed.add_field(name="Rôles", value=" ".join(roles[-5:]), inline=False)
        if banner:
            embed.add_field(name="Kické par", value=str(banner), inline=True)
        if reason:
            embed.add_field(name="Raison", value=reason, inline=False)
        embed.set_footer(text=f"Membres : {member.guild.member_count}")
        await self.send_log(member.guild.id, "member", embed)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        changes = []
        if before.nick != after.nick:
            changes.append(("Surnom", before.nick or "*aucun*", after.nick or "*aucun*"))
        if set(before.roles) != set(after.roles):
            added = [r.mention for r in after.roles if r not in before.roles]
            removed = [r.mention for r in before.roles if r not in after.roles]
            if added:
                changes.append(("Rôle ajouté", "", ", ".join(added)))
            if removed:
                changes.append(("Rôle retiré", "", ", ".join(removed)))
        if before.timed_out_until != after.timed_out_until:
            if after.timed_out_until:
                until = after.timed_out_until.strftime("%d/%m %H:%M")
                changes.append(("Timeout jusqu'au", "", until))
            else:
                changes.append(("Timeout", "retiré", ""))
        if not changes:
            return
        embed = discord.Embed(title="✏️ Membre modifié", color=0x5865F2, timestamp=self.ts())
        embed.set_author(name=str(after), icon_url=after.display_avatar.url)
        for name, old, new in changes:
            if old and new:
                embed.add_field(name=name, value=f"`{old}` → `{new}`", inline=False)
            elif new:
                embed.add_field(name=name, value=new, inline=False)
            else:
                embed.add_field(name=name, value=old, inline=False)
        embed.set_footer(text=f"ID : {after.id}")
        await self.send_log(after.guild.id, "member", embed)

    # --- BANS ---

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        banner = None
        reason = None
        try:
            await asyncio.sleep(0.5)
            async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.ban):
                if entry.target and entry.target.id == user.id:
                    banner = entry.user
                    reason = entry.reason
                    break
        except Exception:
            pass
        embed = discord.Embed(title="🔨 Ban", color=0xED4245, timestamp=self.ts())
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        embed.add_field(name="ID", value=str(user.id), inline=True)
        if banner:
            embed.add_field(name="Banni par", value=str(banner), inline=True)
        if reason:
            embed.add_field(name="Raison", value=reason, inline=False)
        await self.send_log(guild.id, "ban", embed)

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        unbanner = None
        reason = None
        try:
            await asyncio.sleep(0.5)
            async for entry in guild.audit_logs(limit=5, action=discord.AuditLogAction.unban):
                if entry.target and entry.target.id == user.id:
                    unbanner = entry.user
                    reason = entry.reason
                    break
        except Exception:
            pass
        embed = discord.Embed(title="✅ Unban", color=0x57F287, timestamp=self.ts())
        embed.set_author(name=str(user), icon_url=user.display_avatar.url)
        embed.add_field(name="ID", value=str(user.id), inline=True)
        if unbanner:
            embed.add_field(name="Débanni par", value=str(unbanner), inline=True)
        if reason:
            embed.add_field(name="Raison", value=reason, inline=False)
        await self.send_log(guild.id, "ban", embed)

    # --- MESSAGES ---

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if not message.guild or message.author.bot:
            return
        embed = discord.Embed(
            title="🗑️ Message supprimé",
            color=0xEB459E,
            timestamp=self.ts()
        )
        embed.set_author(name=str(message.author), icon_url=message.author.display_avatar.url)
        if message.content:
            content = message.content[:1000]
            embed.add_field(name="Contenu", value=content, inline=False)
        if message.attachments:
            urls = "\n".join(a.url for a in message.attachments[:3])
            embed.add_field(name=f"Pièces jointes ({len(message.attachments)})", value=urls, inline=False)
        embed.add_field(name="Salon", value=message.channel.mention, inline=True)
        embed.set_footer(text=f"ID message : {message.id} | Auteur ID : {message.author.id}")
        await self.send_log(message.guild.id, "delete", embed)

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]):
        if not messages or not messages[0].guild:
            return
        guild = messages[0].guild
        channel = messages[0].channel
        count = len(messages)
        embed = discord.Embed(
            title=f"🗑️ Suppression en masse — {count} messages",
            color=0xEB459E,
            timestamp=self.ts()
        )
        embed.add_field(name="Salon", value=channel.mention, inline=True)
        # résumé des 5 premiers
        lines = []
        for m in messages[:5]:
            txt = m.content[:80].replace("\n", " ") if m.content else "*[vide]*"
            lines.append(f"**{m.author}** : {txt}")
        if lines:
            embed.add_field(name="Aperçu", value="\n".join(lines), inline=False)
        await self.send_log(guild.id, "delete", embed)

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if not after.guild or after.author.bot:
            return
        if before.content == after.content:
            return
        embed = discord.Embed(
            title="✏️ Message modifié",
            color=0xFEE75C,
            timestamp=self.ts()
        )
        embed.set_author(name=str(after.author), icon_url=after.author.display_avatar.url)
        embed.add_field(name="Avant", value=before.content[:500] or "*vide*", inline=False)
        embed.add_field(name="Après", value=after.content[:500] or "*vide*", inline=False)
        embed.add_field(name="Salon", value=after.channel.mention, inline=True)
        embed.add_field(name="Lien", value=f"[Voir le message]({after.jump_url})", inline=True)
        embed.set_footer(text=f"ID : {after.id}")
        await self.send_log(after.guild.id, "edit", embed)

    # --- ROLES ---

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        embed = discord.Embed(title="🎭 Rôle créé", color=role.color, timestamp=self.ts())
        embed.add_field(name="Nom", value=role.mention, inline=True)
        embed.add_field(name="ID", value=str(role.id), inline=True)
        embed.add_field(name="Couleur", value=str(role.color), inline=True)
        embed.add_field(name="Mentionnable", value="Oui" if role.mentionable else "Non", inline=True)
        embed.add_field(name="Affiché séparément", value="Oui" if role.hoist else "Non", inline=True)
        await self.send_log(role.guild.id, "roles", embed)

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        embed = discord.Embed(title="🗑️ Rôle supprimé", color=0xED4245, timestamp=self.ts())
        embed.add_field(name="Nom", value=role.name, inline=True)
        embed.add_field(name="ID", value=str(role.id), inline=True)
        await self.send_log(role.guild.id, "roles", embed)

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        changes = []
        if before.name != after.name:
            changes.append(f"Nom : `{before.name}` → `{after.name}`")
        if before.color != after.color:
            changes.append(f"Couleur : `{before.color}` → `{after.color}`")
        if before.permissions != after.permissions:
            # liste les perms changées
            p_before = set(str(p) for p, v in before.permissions if v)
            p_after = set(str(p) for p, v in after.permissions if v)
            added = p_after - p_before
            removed = p_before - p_after
            if added:
                changes.append(f"Perms ajoutées : {', '.join(added)}")
            if removed:
                changes.append(f"Perms retirées : {', '.join(removed)}")
        if before.hoist != after.hoist:
            changes.append(f"Affiché séparément : {'Oui' if after.hoist else 'Non'}")
        if before.mentionable != after.mentionable:
            changes.append(f"Mentionnable : {'Oui' if after.mentionable else 'Non'}")
        if not changes:
            return
        embed = discord.Embed(title="✏️ Rôle modifié", color=after.color, timestamp=self.ts())
        embed.add_field(name="Rôle", value=after.mention, inline=True)
        embed.add_field(name="Changements", value="\n".join(changes[:8]), inline=False)
        await self.send_log(after.guild.id, "roles", embed)

    # --- SALONS ---

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        embed = discord.Embed(title="📢 Salon créé", color=0x57F287, timestamp=self.ts())
        embed.add_field(name="Nom", value=channel.mention if hasattr(channel, 'mention') else channel.name, inline=True)
        embed.add_field(name="Type", value=str(channel.type), inline=True)
        embed.add_field(name="ID", value=str(channel.id), inline=True)
        if hasattr(channel, 'category') and channel.category:
            embed.add_field(name="Catégorie", value=channel.category.name, inline=True)
        await self.send_log(channel.guild.id, "channels", embed)

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        embed = discord.Embed(title="🗑️ Salon supprimé", color=0xED4245, timestamp=self.ts())
        embed.add_field(name="Nom", value=f"#{channel.name}", inline=True)
        embed.add_field(name="Type", value=str(channel.type), inline=True)
        embed.add_field(name="ID", value=str(channel.id), inline=True)
        await self.send_log(channel.guild.id, "channels", embed)

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        changes = []
        if before.name != after.name:
            changes.append(f"Nom : `#{before.name}` → `#{after.name}`")
        if hasattr(before, 'topic') and before.topic != after.topic:
            changes.append(f"Description modifiée")
        if hasattr(before, 'nsfw') and before.nsfw != after.nsfw:
            changes.append(f"NSFW : {'Activé' if after.nsfw else 'Désactivé'}")
        if hasattr(before, 'slowmode_delay') and before.slowmode_delay != after.slowmode_delay:
            changes.append(f"Slowmode : {before.slowmode_delay}s → {after.slowmode_delay}s")
        if not changes:
            return
        embed = discord.Embed(title="✏️ Salon modifié", color=0xFEE75C, timestamp=self.ts())
        ch_ref = after.mention if hasattr(after, 'mention') else f"#{after.name}"
        embed.add_field(name="Salon", value=ch_ref, inline=True)
        embed.add_field(name="Changements", value="\n".join(changes[:6]), inline=False)
        await self.send_log(after.guild.id, "channels", embed)

    # --- THREADS ---

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        embed = discord.Embed(title="🧵 Thread créé", color=0x5865F2, timestamp=self.ts())
        embed.add_field(name="Nom", value=thread.mention, inline=True)
        embed.add_field(name="Salon parent", value=f"<#{thread.parent_id}>", inline=True)
        if thread.owner:
            embed.add_field(name="Créé par", value=str(thread.owner), inline=True)
        await self.send_log(thread.guild.id, "threads", embed)

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread):
        embed = discord.Embed(title="🗑️ Thread supprimé", color=0xED4245, timestamp=self.ts())
        embed.add_field(name="Nom", value=f"#{thread.name}", inline=True)
        embed.add_field(name="ID", value=str(thread.id), inline=True)
        await self.send_log(thread.guild.id, "threads", embed)

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
        changes = []
        if before.name != after.name:
            changes.append(f"Nom : `{before.name}` → `{after.name}`")
        if before.archived != after.archived:
            changes.append("Archivé" if after.archived else "Désarchivé")
        if before.locked != after.locked:
            changes.append("Verrouillé" if after.locked else "Déverrouillé")
        if not changes:
            return
        embed = discord.Embed(title="✏️ Thread modifié", color=0xFEE75C, timestamp=self.ts())
        embed.add_field(name="Thread", value=after.mention, inline=True)
        embed.add_field(name="Changements", value="\n".join(changes), inline=False)
        await self.send_log(after.guild.id, "threads", embed)

    # --- VOCAL ---

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if before.channel == after.channel:
            # mute/deaf/stream, pas intéressant en log
            return
        if after.channel and not before.channel:
            title = "🎙️ Connexion vocale"
            ch_name = after.channel.name
            color = 0x57F287
        elif before.channel and not after.channel:
            title = "🔇 Déconnexion vocale"
            ch_name = before.channel.name
            color = 0xED4245
        else:
            title = "🔀 Changement de salon vocal"
            ch_name = f"`{before.channel.name}` → `{after.channel.name}`"
            color = 0xFEE75C
        embed = discord.Embed(title=title, color=color, timestamp=self.ts())
        embed.set_author(name=str(member), icon_url=member.display_avatar.url)
        embed.add_field(name="Salon", value=ch_name, inline=True)
        await self.send_log(member.guild.id, "voice", embed)

    # --- SERVEUR (invitations, mises à jour) ---

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        embed = discord.Embed(title="🔗 Invitation créée", color=0x57F287, timestamp=self.ts())
        embed.add_field(name="Code", value=f"`{invite.code}`", inline=True)
        if invite.inviter:
            embed.add_field(name="Créée par", value=str(invite.inviter), inline=True)
        if invite.channel:
            embed.add_field(name="Salon", value=f"<#{invite.channel.id}>", inline=True)
        max_uses = str(invite.max_uses) if invite.max_uses else "∞"
        embed.add_field(name="Utilisations max", value=max_uses, inline=True)
        await self.send_log(invite.guild.id, "guild", embed)

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        embed = discord.Embed(title="🗑️ Invitation supprimée", color=0xED4245, timestamp=self.ts())
        embed.add_field(name="Code", value=f"`{invite.code}`", inline=True)
        if invite.channel:
            embed.add_field(name="Salon", value=f"<#{invite.channel.id}>", inline=True)
        await self.send_log(invite.guild.id, "guild", embed)

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        changes = []
        if before.name != after.name:
            changes.append(f"Nom : `{before.name}` → `{after.name}`")
        if before.description != after.description:
            changes.append("Description modifiée")
        if before.icon != after.icon:
            changes.append("Icône modifiée")
        if before.verification_level != after.verification_level:
            changes.append(f"Niveau de vérif : `{before.verification_level}` → `{after.verification_level}`")
        if not changes:
            return
        embed = discord.Embed(title="⚙️ Serveur modifié", color=0x5865F2, timestamp=self.ts())
        embed.add_field(name="Changements", value="\n".join(changes), inline=False)
        await self.send_log(after.id, "guild", embed)

    # --- EMOJIS ---

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before, after):
        b_set = {e.id for e in before}
        a_set = {e.id for e in after}
        added = [e for e in after if e.id not in b_set]
        removed = [e for e in before if e.id not in a_set]
        if not added and not removed:
            return
        embed = discord.Embed(title="😀 Emojis modifiés", color=0xFEE75C, timestamp=self.ts())
        if added:
            embed.add_field(name="Ajoutés", value=" ".join(str(e) for e in added[:10]), inline=False)
        if removed:
            embed.add_field(name="Supprimés", value=", ".join(f"`:{e.name}:`" for e in removed[:10]), inline=False)
        await self.send_log(guild.id, "guild", embed)

    # --- AUTOMOD ---

    @commands.Cog.listener()
    async def on_automod_action(self, execution: discord.AutoModAction):
        if not execution.guild_id:
            return
        embed = discord.Embed(title="🛡️ AutoMod déclenché", color=0xFF7700, timestamp=self.ts())
        embed.add_field(name="Utilisateur", value=f"<@{execution.user_id}>", inline=True)
        embed.add_field(name="Action", value=str(execution.action.type), inline=True)
        if execution.channel_id:
            embed.add_field(name="Salon", value=f"<#{execution.channel_id}>", inline=True)
        if execution.content:
            embed.add_field(name="Message", value=execution.content[:500], inline=False)
        if execution.matched_content:
            embed.add_field(name="Correspondance", value=f"`{execution.matched_content}`", inline=True)
        await self.send_log(execution.guild_id, "automod", embed)

    # --- ÉVÉNEMENTS PLANIFIÉS ---

    @commands.Cog.listener()
    async def on_scheduled_event_create(self, event: discord.ScheduledEvent):
        embed = discord.Embed(title="📅 Événement créé", color=0x57F287, timestamp=self.ts())
        embed.add_field(name="Nom", value=event.name, inline=True)
        if event.creator:
            embed.add_field(name="Créé par", value=str(event.creator), inline=True)
        if event.start_time:
            embed.add_field(name="Début", value=event.start_time.strftime("%d/%m/%Y %H:%M"), inline=True)
        await self.send_log(event.guild_id, "scheduled", embed)

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, event: discord.ScheduledEvent):
        embed = discord.Embed(title="🗑️ Événement supprimé", color=0xED4245, timestamp=self.ts())
        embed.add_field(name="Nom", value=event.name, inline=True)
        await self.send_log(event.guild_id, "scheduled", embed)

    @commands.Cog.listener()
    async def on_scheduled_event_update(self, before: discord.ScheduledEvent, after: discord.ScheduledEvent):
        changes = []
        if before.name != after.name:
            changes.append(f"Nom : `{before.name}` → `{after.name}`")
        if before.status != after.status:
            changes.append(f"Statut : `{before.status}` → `{after.status}`")
        if not changes:
            return
        embed = discord.Embed(title="✏️ Événement modifié", color=0xFEE75C, timestamp=self.ts())
        embed.add_field(name="Nom", value=after.name, inline=True)
        embed.add_field(name="Changements", value="\n".join(changes), inline=False)
        await self.send_log(after.guild_id, "scheduled", embed)

    # --- SLASH COMMANDS LOG ---

    @commands.Cog.listener()
    async def on_app_command_completion(self, interaction: discord.Interaction, command):
        if not interaction.guild:
            return
        embed = discord.Embed(title="⚡ Commande slash utilisée", color=0x5865F2, timestamp=self.ts())
        embed.set_author(name=str(interaction.user), icon_url=interaction.user.display_avatar.url)
        embed.add_field(name="Commande", value=f"`/{command.qualified_name}`", inline=True)
        if interaction.channel:
            embed.add_field(name="Salon", value=interaction.channel.mention, inline=True)
        # on loggue dans guild pour ne pas surcharger un salon dédié
        await self.send_log(interaction.guild.id, "guild", embed)

    # --- LOGSETUP command ---

    logsetup_group = app_commands.Group(
        name="logsetup",
        description="⚙️ Configurer les salons de logs",
        default_permissions=discord.Permissions(administrator=True)
    )

    @logsetup_group.command(name="set", description="Définir un salon de log")
    @app_commands.describe(
        type="Type de log (voir /logsetup view pour la liste)",
        salon="Salon Discord où envoyer ces logs"
    )
    async def logsetup_set(self, interaction: discord.Interaction, type: str, salon: discord.TextChannel):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Commande serveur uniquement.", ephemeral=True)
        type = type.lower()
        if type not in LOG_TYPES:
            types_list = "\n".join(f"`{k}` — {v}" for k, v in LOG_TYPES.items())
            return await interaction.response.send_message(
                f"❌ Type invalide. Types disponibles :\n{types_list}", ephemeral=True
            )
        set_log_channel(interaction.guild.id, type, salon.id)
        await interaction.response.send_message(
            f"✅ Logs `{type}` → {salon.mention}", ephemeral=True
        )

    @logsetup_group.command(name="remove", description="Retirer un salon de log")
    @app_commands.describe(type="Type de log à désactiver")
    async def logsetup_remove(self, interaction: discord.Interaction, type: str):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Commande serveur uniquement.", ephemeral=True)
        type = type.lower()
        remove_log_channel(interaction.guild.id, type)
        await interaction.response.send_message(f"✅ Logs `{type}` désactivés.", ephemeral=True)

    @logsetup_group.command(name="view", description="Voir la config des logs de ce serveur")
    async def logsetup_view(self, interaction: discord.Interaction):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Commande serveur uniquement.", ephemeral=True)
        config = get_all_log_channels(interaction.guild.id)
        embed = discord.Embed(
            title="⚙️ Configuration des logs",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc)
        )
        lines = []
        for log_type, label in LOG_TYPES.items():
            if log_type in config:
                lines.append(f"✅ **{log_type}** — {label}\n   → <#{config[log_type]}>")
            else:
                lines.append(f"❌ **{log_type}** — {label}")
        embed.description = "\n".join(lines)
        embed.set_footer(text="Utilisez /logsetup set <type> <#salon> pour configurer")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # autocomplete pour le paramètre type
    @logsetup_set.autocomplete("type")
    @logsetup_remove.autocomplete("type")
    async def type_autocomplete(self, interaction: discord.Interaction, current: str):
        return [
            app_commands.Choice(name=f"{k} — {v}", value=k)
            for k, v in LOG_TYPES.items()
            if current.lower() in k.lower()
        ][:25]


async def setup(bot):
    await bot.add_cog(LogsCog(bot))
