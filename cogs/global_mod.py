import discord
from discord.ext import commands, tasks
from discord import app_commands, Interaction
from datetime import datetime, timezone
import asyncio
import sqlite3
import os
import re
import traceback

DISCORD_LINK_RE = re.compile(
    r'^https://(?:ptb\.|canary\.)?discord(?:app)?\.com/channels/\d+/\d+/\d+$'
)

BOT_NAME = os.getenv("BOT_NAME", "safezone")

# admins fixes, gèrent /globaladmin et /global watch/unwatch
ADMIN_IDS = {
    1335581687760949313,
}

# accès à /global ban/unban/check/logs, extensible via /globaladmin
ALLOWED_IDS: set[int] = set(ADMIN_IDS)


async def _require(interaction: Interaction, allowed: set[int]) -> bool:
    if interaction.user.id in allowed:
        return True
    await interaction.response.send_message("❌ Accès refusé.", ephemeral=True)
    return False


# --- DB ---

def _db_path():
    path = os.getenv("SHARED_GLOBAL_DB_PATH", "data/database/global/global_lists.db")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    return path


_schema_migrated = False


def get_db():
    conn = sqlite3.connect(_db_path(), check_same_thread=False, timeout=15)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row

    conn.execute("""CREATE TABLE IF NOT EXISTS global_blacklist (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        type TEXT NOT NULL,
        value TEXT NOT NULL,
        reason TEXT DEFAULT '',
        added_at TEXT NOT NULL,
        source_guild_id TEXT DEFAULT '',
        source_guild_name TEXT DEFAULT '',
        UNIQUE(type, value))""")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_bl_type ON global_blacklist(type)")

    conn.execute("""CREATE TABLE IF NOT EXISTS guild_ban_sync (
        guild_id TEXT PRIMARY KEY,
        enabled INTEGER DEFAULT 0,
        enabled_at TEXT,
        notification_channel_id TEXT)""")

    conn.execute("""CREATE TABLE IF NOT EXISTS global_cmd_allowed (
        user_id TEXT PRIMARY KEY,
        added_by TEXT NOT NULL,
        added_at TEXT NOT NULL)""")

    # table inter-bots — Raven et SafeZone écrivent ici pour se notifier
    conn.execute("""CREATE TABLE IF NOT EXISTS cross_bot_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_bot TEXT NOT NULL,
        event_type TEXT NOT NULL,
        target_id TEXT NOT NULL,
        reason TEXT DEFAULT '',
        banner_name TEXT DEFAULT '',
        banner_id TEXT DEFAULT '',
        message_link TEXT DEFAULT '',
        source_guild_id TEXT DEFAULT '',
        source_guild_name TEXT DEFAULT '',
        created_at TEXT NOT NULL,
        processed_by TEXT DEFAULT '')""")

    # Salons (forums de report externes) surveillés pour la détection auto de Global Ban
    # (tables partagées avec Raven — même fichier DB, même schéma)
    conn.execute("""CREATE TABLE IF NOT EXISTS global_watch_channels (
        channel_id TEXT PRIMARY KEY,
        guild_id   TEXT NOT NULL,
        added_by   TEXT NOT NULL,
        added_at   TEXT NOT NULL)""")

    conn.execute("""CREATE TABLE IF NOT EXISTS global_pending_reports (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id     TEXT NOT NULL,
        reason        TEXT DEFAULT '',
        message_link  TEXT DEFAULT '',
        source_guild_id   TEXT DEFAULT '',
        source_guild_name TEXT DEFAULT '',
        status        TEXT DEFAULT 'pending',
        created_at    TEXT NOT NULL)""")

    conn.execute("""CREATE TABLE IF NOT EXISTS admin_audit_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        action TEXT NOT NULL,
        target_id TEXT NOT NULL,
        target_name TEXT DEFAULT '',
        moderator_id TEXT NOT NULL,
        moderator_name TEXT NOT NULL,
        reason TEXT DEFAULT '',
        proof_link TEXT DEFAULT '',
        guild_name TEXT DEFAULT '',
        created_at TEXT NOT NULL)""")

    global _schema_migrated
    if not _schema_migrated:
        # rattrape les colonnes ajoutées après coup, au cas où la DB partagée avec Raven
        # ne les aurait pas encore (une seule fois par process, pas à chaque get_db())
        for table, col in (
            ("global_blacklist", "source_guild_id TEXT DEFAULT ''"),
            ("global_blacklist", "source_guild_name TEXT DEFAULT ''"),
            ("guild_ban_sync", "notification_channel_id TEXT"),
            ("cross_bot_events", "source_guild_id TEXT DEFAULT ''"),
            ("cross_bot_events", "source_guild_name TEXT DEFAULT ''"),
        ):
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {col}")
            except Exception:
                pass
        _schema_migrated = True

    conn.commit()
    return conn


def _write_audit(action: str, target_id: int, target_name: str,
                 moderator_id: int, moderator_name: str,
                 reason: str = "", proof_link: str = "", guild_name: str = ""):
    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO admin_audit_log
               (action, target_id, target_name, moderator_id, moderator_name,
                reason, proof_link, guild_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (action, str(target_id), target_name, str(moderator_id), moderator_name,
             reason, proof_link or "", guild_name, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[Audit SZ] write error: {e}")


async def _send_dm(bot, user_id: int, embed: discord.Embed):
    try:
        user = await bot.fetch_user(user_id)
        await user.send(embed=embed)
    except Exception:
        pass


# --- global_cmd_allowed ---

def load_allowed():
    try:
        conn = get_db()
        for row in conn.execute("SELECT user_id FROM global_cmd_allowed").fetchall():
            ALLOWED_IDS.add(int(row[0]))
        conn.close()
    except Exception:
        pass


def add_allowed(user_id: int, added_by: str) -> bool:
    try:
        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO global_cmd_allowed (user_id, added_by, added_at) VALUES (?, ?, ?)",
            (str(user_id), added_by, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        changed = conn.execute("SELECT changes()").fetchone()[0]
        conn.close()
        if changed:
            ALLOWED_IDS.add(user_id)
            return True
        return False
    except Exception:
        return False


def remove_allowed(user_id: int) -> bool:
    try:
        conn = get_db()
        conn.execute("DELETE FROM global_cmd_allowed WHERE user_id = ?", (str(user_id),))
        conn.commit()
        changed = conn.execute("SELECT changes()").fetchone()[0]
        conn.close()
        if changed:
            ALLOWED_IDS.discard(user_id)
            return True
        return False
    except Exception:
        return False


def list_allowed() -> list:
    try:
        conn = get_db()
        rows = conn.execute("SELECT user_id, added_by, added_at FROM global_cmd_allowed ORDER BY added_at").fetchall()
        result = [dict(r) for r in rows]
        conn.close()
        return result
    except Exception:
        return []


def get_optin_guilds() -> set:
    try:
        conn = get_db()
        rows = conn.execute("SELECT guild_id FROM guild_ban_sync WHERE enabled=1").fetchall()
        result = {r[0] for r in rows}
        conn.close()
        return result
    except Exception:
        return set()


def get_notif_channel(guild_id: int) -> int | None:
    try:
        conn = get_db()
        row = conn.execute("SELECT notification_channel_id FROM guild_ban_sync WHERE guild_id=?",
                           (str(guild_id),)).fetchone()
        conn.close()
        if row and row[0]:
            return int(row[0])
    except Exception:
        pass
    return None


# Serveur de review où sont postées les propositions détectées automatiquement
REVIEW_GUILD_ID = 1319470415705276486


def get_watch_channel_ids() -> set[int]:
    try:
        conn = get_db()
        rows = conn.execute("SELECT channel_id FROM global_watch_channels").fetchall()
        conn.close()
        return {int(r[0]) for r in rows}
    except Exception:
        return set()


def add_watch_channel(channel_id: int, guild_id: int, added_by: str) -> bool:
    try:
        conn = get_db()
        conn.execute(
            "INSERT OR IGNORE INTO global_watch_channels (channel_id, guild_id, added_by, added_at) VALUES (?, ?, ?, ?)",
            (str(channel_id), str(guild_id), added_by, datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        changed = conn.execute("SELECT changes()").fetchone()[0]
        conn.close()
        return bool(changed)
    except Exception:
        return False


def remove_watch_channel(channel_id: int) -> bool:
    try:
        conn = get_db()
        conn.execute("DELETE FROM global_watch_channels WHERE channel_id = ?", (str(channel_id),))
        conn.commit()
        changed = conn.execute("SELECT changes()").fetchone()[0]
        conn.close()
        return bool(changed)
    except Exception:
        return False


def save_pending_report(target_id: int, reason: str, message_link: str,
                         source_guild_id: int, source_guild_name: str) -> int | None:
    """Enregistre une proposition détectée auto. Retourne None si ce post a déjà été traité
    (évite les doublons si Raven et SafeZone surveillent le même salon)."""
    try:
        conn = get_db()
        existing = conn.execute(
            "SELECT id FROM global_pending_reports WHERE message_link = ?", (message_link,)
        ).fetchone()
        if existing:
            conn.close()
            return None
        cur = conn.execute(
            """INSERT INTO global_pending_reports
               (target_id, reason, message_link, source_guild_id, source_guild_name, status, created_at)
               VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
            (str(target_id), reason, message_link, str(source_guild_id), source_guild_name,
             datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        pending_id = cur.lastrowid
        conn.close()
        return pending_id
    except Exception as e:
        print(f"[GlobalBanWatch SZ] Erreur enregistrement pending report: {e}")
        return None


def get_pending_report(pending_id: int) -> dict | None:
    try:
        conn = get_db()
        row = conn.execute("SELECT * FROM global_pending_reports WHERE id = ?", (pending_id,)).fetchone()
        conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def set_pending_report_status(pending_id: int, status: str):
    try:
        conn = get_db()
        conn.execute("UPDATE global_pending_reports SET status = ? WHERE id = ?", (status, pending_id))
        conn.commit()
        conn.close()
    except Exception:
        pass


# --- cross-bot ---

def write_event(event_type: str, target_id: int, reason="", banner_name="",
                banner_id=0, message_link="", guild_id=0, guild_name=""):
    try:
        conn = get_db()
        conn.execute(
            """INSERT INTO cross_bot_events
               (source_bot, event_type, target_id, reason, banner_name, banner_id,
                message_link, source_guild_id, source_guild_name, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (BOT_NAME, event_type, str(target_id), reason, banner_name, str(banner_id),
             message_link or "", str(guild_id), guild_name,
             datetime.now(timezone.utc).isoformat())
        )
        conn.commit()
        conn.close()
    except Exception as e:
        print(f"[CrossBot SZ] write_event error: {e}")


# --- boutons persistants ---

# admin du serveur local (pas ALLOWED_IDS) : chaque serveur opt-in gère lui-même qui peut
# cliquer sur ses propres alertes, indépendamment de qui a accès aux commandes /global
async def _check_admin(interaction: discord.Interaction) -> bool:
    if not interaction.guild:
        return False
    m = interaction.guild.get_member(interaction.user.id)
    return bool(m and m.guild_permissions.administrator)


class BanButton(discord.ui.DynamicItem[discord.ui.Button], template=r'szgban_ban:(?P<tid>[0-9]+)'):
    def __init__(self, tid: int):
        super().__init__(discord.ui.Button(
            label="🔨 Bannir de ce serveur",
            style=discord.ButtonStyle.danger,
            custom_id=f"szgban_ban:{tid}",
        ))
        self.tid = tid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(tid=int(match["tid"]))

    async def callback(self, interaction: discord.Interaction):
        if not await _check_admin(interaction):
            return await interaction.response.send_message("❌ Administrateur requis.", ephemeral=True)

        msg = interaction.message
        orig = msg.embeds[0].copy() if msg.embeds else discord.Embed(title="⚠️ Global Ban")
        pending = orig.copy()
        pending.color = discord.Color.orange()
        pending.add_field(name="⏳ En cours...", value=interaction.user.mention, inline=False)
        await interaction.response.edit_message(embed=pending, view=None)

        try:
            await interaction.guild.ban(discord.Object(id=self.tid),
                                        reason=f"[Global Ban SZ] par {interaction.user}",
                                        delete_message_days=0)
            done = orig.copy()
            done.color = discord.Color.dark_red()
            done.add_field(name="✅ Banni", value=interaction.user.mention, inline=False)
            await msg.edit(embed=done, view=None)
        except discord.Forbidden:
            fail = orig.copy()
            fail.color = discord.Color.orange()
            fail.add_field(name="❌ Échec", value="Permissions insuffisantes", inline=False)
            await msg.edit(embed=fail, view=None)
        except Exception as e:
            fail = orig.copy()
            fail.add_field(name="❌ Erreur", value=str(e)[:200], inline=False)
            try:
                await msg.edit(embed=fail, view=None)
            except Exception:
                pass


class IgnoreButton(discord.ui.DynamicItem[discord.ui.Button], template=r'szgban_ignore:(?P<tid>[0-9]+)'):
    def __init__(self, tid: int):
        super().__init__(discord.ui.Button(
            label="✅ Ignorer",
            style=discord.ButtonStyle.secondary,
            custom_id=f"szgban_ignore:{tid}",
        ))
        self.tid = tid

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(tid=int(match["tid"]))

    async def callback(self, interaction: discord.Interaction):
        if not await _check_admin(interaction):
            return await interaction.response.send_message("❌ Administrateur requis.", ephemeral=True)
        embed = interaction.message.embeds[0].copy() if interaction.message.embeds else discord.Embed()
        embed.color = discord.Color.greyple()
        embed.add_field(name="⏭️ Ignoré", value=interaction.user.mention, inline=False)
        await interaction.response.edit_message(embed=embed, view=None)


class AlertView(discord.ui.View):
    def __init__(self, tid: int):
        super().__init__(timeout=None)
        self.add_item(BanButton(tid))
        self.add_item(IgnoreButton(tid))


async def send_alerts(bot, target_id: int, reason: str, banner: str,
                      link=None, guild_name=""):
    optin = get_optin_guilds()
    try:
        u = await bot.fetch_user(target_id)
        target_str = f"{u} (`{target_id}`)"
        avatar = u.display_avatar.url
    except Exception:
        target_str = f"`{target_id}`"
        avatar = None

    embed = discord.Embed(
        title="⚠️ Alerte — Global Ban Réseau",
        description="Un utilisateur a été banni globalement.\nActionnez les boutons si vous voulez l'action sur votre serveur.",
        color=discord.Color.dark_red(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(name="Utilisateur", value=target_str, inline=True)
    embed.add_field(name="Banni par", value=banner, inline=True)
    embed.add_field(name="Raison", value=reason, inline=False)
    if guild_name:
        embed.add_field(name="Serveur d'origine", value=f"**{guild_name}** (via SafeZone)", inline=False)
    if link:
        embed.add_field(name="Preuve", value=f"[Voir le message]({link})", inline=False)
    if avatar:
        embed.set_thumbnail(url=avatar)
    embed.set_footer(text="SafeZone • admin requis pour agir")

    view = AlertView(tid=target_id)
    for g in bot.guilds:
        if str(g.id) not in optin:
            continue
        ch_id = get_notif_channel(g.id)
        if not ch_id:
            continue
        ch = g.get_channel(ch_id)
        if not ch:
            continue
        try:
            await ch.send(embed=embed, view=view)
        except Exception as e:
            print(f"[GlobalBan SZ] alert failed {g.name}: {e}")
        await asyncio.sleep(0.3)


def _finalize_global_ban(bot, target_id: int, raison: str, executor, message_link: str,
                          source_guild_id: int, source_guild_name: str) -> bool:
    """Insère dans la blacklist globale. Si nouveau : audit, event cross-bot, DM à la cible,
    alerte réseau. Renvoie False sans rien notifier si déjà présent (évite les doublons)."""
    conn = get_db()
    conn.execute(
        """INSERT OR IGNORE INTO global_blacklist
           (type, value, reason, added_at, source_guild_id, source_guild_name)
           VALUES (?, ?, ?, ?, ?, ?)""",
        ("user", str(target_id), raison, datetime.now(timezone.utc).isoformat(),
         str(source_guild_id), source_guild_name)
    )
    conn.commit()
    changed = conn.execute("SELECT changes()").fetchone()[0]
    conn.close()
    if not changed:
        return False

    write_event("global_ban", target_id, raison, str(executor), executor.id,
                message_link or "", source_guild_id, source_guild_name)
    _write_audit("global_ban", target_id, str(target_id), executor.id, str(executor),
                 raison, message_link or "", source_guild_name)

    dm = discord.Embed(
        title="⛔ Tu as été signalé sur le réseau SafeZone",
        color=discord.Color.dark_red(),
        timestamp=datetime.now(timezone.utc)
    )
    dm.add_field(name="Raison", value=raison or "—", inline=False)
    dm.add_field(name="Serveur d'origine", value=source_guild_name or "—", inline=True)
    if message_link:
        dm.add_field(name="Preuve", value=f"[Voir le message]({message_link})", inline=False)
    dm.set_footer(text="Si tu penses qu'il s'agit d'une erreur, contacte les administrateurs du réseau.")
    asyncio.create_task(_send_dm(bot, target_id, dm))

    asyncio.create_task(send_alerts(bot, target_id, raison, str(executor), message_link, source_guild_name))
    return True


async def execute_pending_report(bot, pending_id: int, executor) -> tuple[bool, object]:
    """Transforme une proposition en attente en Global Ban réel (blacklist + alertes réseau)."""
    report = get_pending_report(pending_id)
    if not report:
        return False, "Proposition introuvable (déjà traitée ?)."

    target_id = int(report["target_id"])
    raison = report["reason"] or "Ban Global Automatique (détecté via forum de report)"

    _finalize_global_ban(bot, target_id, raison, executor, report["message_link"],
                         int(report["source_guild_id"] or 0), report["source_guild_name"])
    set_pending_report_status(pending_id, "confirmed")
    return True, target_id


class ReportConfirmDynamic(discord.ui.DynamicItem[discord.ui.Button], template=r'szreport_confirm:(?P<pending_id>[0-9]+)'):

    def __init__(self, pending_id: int):
        super().__init__(discord.ui.Button(
            label="✅ Confirmer le Global Ban",
            style=discord.ButtonStyle.danger,
            custom_id=f"szreport_confirm:{pending_id}",
        ))
        self.pending_id = pending_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(pending_id=int(match["pending_id"]))

    async def callback(self, interaction: discord.Interaction):
        if not await _require(interaction, ALLOWED_IDS):
            return

        message = interaction.message
        original = message.embeds[0].copy() if message.embeds else discord.Embed(title="⚠️ Proposition Global Ban")
        pending = original.copy()
        pending.color = discord.Color.orange()
        pending.add_field(name="⏳ Validation en cours...", value=interaction.user.mention, inline=False)
        await interaction.response.edit_message(embed=pending, view=None)

        ok, result = await execute_pending_report(interaction.client, self.pending_id, interaction.user)

        final = original.copy()
        if ok:
            final.color = discord.Color.dark_red()
            final.add_field(name="✅ Global Ban confirmé", value=f"Validé par {interaction.user.mention} — `{result}` ajouté à la blacklist globale.", inline=False)
        else:
            final.color = discord.Color.orange()
            final.add_field(name="❌ Échec", value=str(result), inline=False)
        await message.edit(embed=final, view=None)


class ReportRejectDynamic(discord.ui.DynamicItem[discord.ui.Button], template=r'szreport_reject:(?P<pending_id>[0-9]+)'):

    def __init__(self, pending_id: int):
        super().__init__(discord.ui.Button(
            label="🗑️ Ignorer",
            style=discord.ButtonStyle.secondary,
            custom_id=f"szreport_reject:{pending_id}",
        ))
        self.pending_id = pending_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(pending_id=int(match["pending_id"]))

    async def callback(self, interaction: discord.Interaction):
        if not await _require(interaction, ALLOWED_IDS):
            return
        set_pending_report_status(self.pending_id, "rejected")
        embed = interaction.message.embeds[0].copy() if interaction.message.embeds else discord.Embed(title="⚠️ Proposition Global Ban")
        embed.color = discord.Color.greyple()
        embed.add_field(name="⏭️ Ignoré", value=interaction.user.mention, inline=False)
        await interaction.response.edit_message(embed=embed, view=None)


class ReportView(discord.ui.View):
    """Vue persistante pour les propositions de Global Ban auto-détectées."""
    def __init__(self, pending_id: int):
        super().__init__(timeout=None)
        self.add_item(ReportConfirmDynamic(pending_id))
        self.add_item(ReportRejectDynamic(pending_id))


# --- cog ---

class GlobalModCog(commands.Cog, name="GlobalMod"):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        load_allowed()
        self.bot.add_dynamic_items(BanButton, IgnoreButton, ReportConfirmDynamic, ReportRejectDynamic)
        self.crossbot_task.start()
        print(f"[SafeZone] GlobalModCog ok (bot={BOT_NAME})")

    def cog_unload(self):
        self.crossbot_task.cancel()

    # --- Détection auto Global Ban (forum de report surveillé) ---

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        watch_ids = get_watch_channel_ids()
        if thread.parent_id not in watch_ids:
            return

        match = re.search(r"[\s-](\d{15,20})\s*$", thread.name)
        if not match:
            print(f"[GlobalBanWatch SZ] Post « {thread.name} » ignoré : pas d'ID détecté dans le titre.")
            return
        target_id = int(match.group(1))

        tags = [t.name for t in getattr(thread, "applied_tags", [])]

        try:
            starter = await thread.fetch_message(thread.id)
        except Exception:
            starter = None

        excerpt = starter.content[:500] if starter and starter.content else ""
        proof_url = starter.attachments[0].url if starter and starter.attachments else None

        reason = f"Signalé via forum de report « {thread.name} »"
        if tags:
            reason += " — Tags: " + ", ".join(tags)

        message_link = f"https://discord.com/channels/{thread.guild.id}/{thread.id}/{thread.id}"

        pending_id = save_pending_report(
            target_id=target_id,
            reason=reason,
            message_link=message_link,
            source_guild_id=thread.guild.id,
            source_guild_name=thread.guild.name,
        )
        if pending_id is None:
            return

        review_guild = self.bot.get_guild(REVIEW_GUILD_ID)
        if not review_guild:
            return
        # les propositions atterrissent dans le salon /globalsync du serveur de review,
        # pas un salon dédié séparé
        ch_id = get_notif_channel(REVIEW_GUILD_ID)
        log_ch = review_guild.get_channel(ch_id) if ch_id else None
        if not log_ch:
            return

        try:
            target_user = self.bot.get_user(target_id) or await self.bot.fetch_user(target_id)
            target_display = f"{target_user} (`{target_id}`)"
            target_avatar = target_user.display_avatar.url
        except Exception:
            target_display = f"ID `{target_id}`"
            target_avatar = None

        embed = discord.Embed(
            title="🔎 Proposition de Global Ban — détection automatique",
            description=f"Nouveau report détecté sur **{thread.guild.name}**. Vérifie la preuve avant de confirmer.",
            color=discord.Color.gold(),
            timestamp=datetime.now(timezone.utc),
        )
        embed.add_field(name="👤 Utilisateur ciblé", value=target_display, inline=True)
        embed.add_field(name="📂 Salon source", value=thread.parent.mention if thread.parent else "?", inline=True)
        if tags:
            embed.add_field(name="🏷️ Tags", value=", ".join(tags), inline=False)
        if excerpt:
            embed.add_field(name="📝 Extrait du report", value=excerpt, inline=False)
        embed.add_field(name="🔗 Lien du report", value=f"[Voir le post]({message_link})", inline=False)
        if target_avatar:
            embed.set_thumbnail(url=target_avatar)
        if proof_url:
            embed.set_image(url=proof_url)
        embed.set_footer(text=f"Proposition #{pending_id} • accès /global requis pour valider")

        try:
            await log_ch.send(embed=embed, view=ReportView(pending_id=pending_id))
        except Exception as e:
            print(f"[GlobalBanWatch SZ] Échec envoi proposition #{pending_id}: {e}")

    # lit les events de Raven toutes les 15s

    @tasks.loop(seconds=15)
    async def crossbot_task(self):
        try:
            conn = get_db()
            rows = conn.execute(
                """SELECT id, source_bot, event_type, target_id, reason,
                          banner_name, message_link, source_guild_id, source_guild_name
                   FROM cross_bot_events
                   WHERE source_bot != ?
                     AND (processed_by NOT LIKE ? OR processed_by IS NULL OR processed_by = '')
                   ORDER BY id ASC LIMIT 20""",
                (BOT_NAME, f"%{BOT_NAME}%")
            ).fetchall()
            conn.close()
            for row in rows:
                await self._handle_event(dict(row))
        except Exception:
            print(f"[CrossBot SZ] task error\n{traceback.format_exc()}")

    @crossbot_task.before_loop
    async def _before_crossbot(self):
        await self.bot.wait_until_ready()

    async def _handle_event(self, ev: dict):
        eid = ev["id"]
        etype = ev["event_type"]
        tid = int(ev["target_id"])
        reason = ev.get("reason", "")
        guild_name = ev.get("source_guild_name", "")
        src = ev.get("source_bot", "raven")

        try:
            if etype == "global_ban":
                conn = get_db()
                conn.execute(
                    """INSERT OR IGNORE INTO global_blacklist
                       (type, value, reason, added_at, source_guild_id, source_guild_name)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    ("user", str(tid), reason, datetime.now(timezone.utc).isoformat(),
                     ev.get("source_guild_id", ""), guild_name)
                )
                conn.commit()
                conn.close()
                # pas d'alerte relayée : seul SafeZone diffuse ses propres bans
                print(f"[CrossBot SZ] ban reçu de {src}: {tid} ({guild_name})")

            elif etype == "global_unban":
                conn = get_db()
                conn.execute("DELETE FROM global_blacklist WHERE type='user' AND value=?", (str(tid),))
                conn.commit()
                conn.close()
                print(f"[CrossBot SZ] unban reçu de {src}: {tid}")

            # marquer comme traité
            conn = get_db()
            row = conn.execute("SELECT processed_by FROM cross_bot_events WHERE id=?", (eid,)).fetchone()
            cur = row[0] if row and row[0] else ""
            new = (cur + "," + BOT_NAME).strip(",") if cur else BOT_NAME
            conn.execute("UPDATE cross_bot_events SET processed_by=? WHERE id=?", (new, eid))
            conn.commit()
            conn.close()

        except Exception as e:
            print(f"[CrossBot SZ] event #{eid} error: {e}")

    # --- /global ---

    global_group = app_commands.Group(
        name="global",
        description="Commandes global ban/unban/check",
    )

    @global_group.command(name="ban", description="Global ban un user + alerte tous les serveurs opt-in")
    @app_commands.describe(
        user_id="ID de l'user à bannir",
        raison="Raison (obligatoire)",
        message_link="Lien de preuve Discord (obligatoire)"
    )
    async def global_ban(self, interaction: Interaction, user_id: str,
                         raison: str, message_link: str):
        if not await _require(interaction, ALLOWED_IDS):
            return
        if not raison.strip():
            return await interaction.response.send_message("❌ La raison est obligatoire.", ephemeral=True)
        if not DISCORD_LINK_RE.match(message_link):
            return await interaction.response.send_message("❌ Lien invalide. Format attendu : `https://discord.com/channels/GUILD/CHANNEL/MESSAGE`", ephemeral=True)

        try:
            tid = int(user_id)
        except ValueError:
            return await interaction.response.send_message("❌ ID invalide.", ephemeral=True)
        if tid == interaction.user.id:
            return await interaction.response.send_message("❌ Tu ne peux pas te bannir toi-même.", ephemeral=True)

        try:
            target = await self.bot.fetch_user(tid)
            target_str = f"{target} (`{tid}`)"
            avatar = target.display_avatar.url
        except Exception:
            target_str = f"`{tid}`"
            avatar = None

        gname = interaction.guild.name if interaction.guild else "DM"

        embed = discord.Embed(
            title="⚠️ Confirmation — Global Ban",
            description="Tu es sur le point d'ajouter cet utilisateur à la blacklist globale.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Utilisateur", value=target_str, inline=True)
        embed.add_field(name="Raison", value=raison, inline=False)
        embed.add_field(name="Serveur d'origine", value=gname, inline=True)
        if message_link:
            embed.add_field(name="Preuve", value=f"[Voir]({message_link})", inline=False)
        if avatar:
            embed.set_thumbnail(url=avatar)
        embed.set_footer(text="Cette action est irréversible sans /global unban")

        class ConfirmView(discord.ui.View):
            def __init__(self_v):
                super().__init__(timeout=60)
                self_v.confirmed = False

            @discord.ui.button(label="✅ Confirmer le ban", style=discord.ButtonStyle.danger)
            async def confirm(self_v, btn_interaction: discord.Interaction, button: discord.ui.Button):
                if btn_interaction.user.id != interaction.user.id:
                    return await btn_interaction.response.send_message("❌ Pas pour toi.", ephemeral=True)
                self_v.confirmed = True
                self_v.stop()

                gid = interaction.guild.id if interaction.guild else 0
                optin = get_optin_guilds()
                notified = sum(1 for g in self.bot.guilds if str(g.id) in optin)

                changed = _finalize_global_ban(self.bot, tid, raison, interaction.user,
                                               message_link, gid, gname)

                if changed:
                    done = discord.Embed(title="🌍 Global Ban — OK", color=discord.Color.dark_red(),
                                         timestamp=datetime.now(timezone.utc))
                    done.add_field(name="Serveurs éligibles (SZ)", value=str(notified), inline=True)
                    done.add_field(name="Raven notifié", value="✅ via DB (15s)", inline=False)
                else:
                    done = discord.Embed(title="ℹ️ Déjà dans la blacklist globale", color=discord.Color.greyple(),
                                         timestamp=datetime.now(timezone.utc))
                done.add_field(name="Utilisateur", value=target_str, inline=True)
                done.add_field(name="Raison", value=raison, inline=False)
                done.add_field(name="Serveur d'origine", value=gname, inline=True)
                if message_link:
                    done.add_field(name="Preuve", value=f"[Voir]({message_link})", inline=False)
                done.set_footer(text=f"par {interaction.user}")

                await btn_interaction.response.edit_message(embed=done, view=None)

            @discord.ui.button(label="❌ Annuler", style=discord.ButtonStyle.secondary)
            async def cancel(self_v, btn_interaction: discord.Interaction, button: discord.ui.Button):
                if btn_interaction.user.id != interaction.user.id:
                    return await btn_interaction.response.send_message("❌ Pas pour toi.", ephemeral=True)
                self_v.stop()
                cancel_embed = discord.Embed(title="❌ Ban annulé", color=discord.Color.greyple())
                await btn_interaction.response.edit_message(embed=cancel_embed, view=None)

        await interaction.response.send_message(embed=embed, view=ConfirmView(), ephemeral=True)

    @global_group.command(name="unban", description="Retire de la blacklist globale + débannit des serveurs opt-in")
    @app_commands.describe(user_id="ID de l'user", raison="Raison")
    async def global_unban(self, interaction: Interaction, user_id: str,
                           raison: str = "Unban Global SafeZone"):
        if not await _require(interaction, ALLOWED_IDS):
            return
        await interaction.response.defer(ephemeral=True)
        try:
            tid = int(user_id)
        except ValueError:
            return await interaction.followup.send("❌ ID invalide.", ephemeral=True)

        ok, fail, skip = 0, 0, 0
        optin = get_optin_guilds()
        obj = discord.Object(id=tid)

        for g in self.bot.guilds:
            if str(g.id) not in optin:
                continue
            try:
                await g.unban(obj, reason=f"Global Unban SZ par {interaction.user}: {raison}")
                ok += 1
            except discord.NotFound:
                skip += 1
            except Exception:
                fail += 1

        conn = get_db()
        conn.execute("DELETE FROM global_blacklist WHERE type='user' AND value=?", (str(tid),))
        conn.commit()
        conn.close()

        gid = interaction.guild.id if interaction.guild else 0
        gname = interaction.guild.name if interaction.guild else "DM"
        write_event("global_unban", tid, raison, str(interaction.user),
                    interaction.user.id, guild_id=gid, guild_name=gname)

        try:
            u = await self.bot.fetch_user(tid)
            target_name = str(u)
        except Exception:
            target_name = str(tid)
        _write_audit("global_unban", tid, target_name, interaction.user.id,
                     str(interaction.user), raison, "", gname)

        dm = discord.Embed(
            title="✅ Tu as été retiré de la liste noire du réseau SafeZone",
            color=discord.Color.green(),
            timestamp=datetime.now(timezone.utc)
        )
        dm.add_field(name="Raison", value=raison or "—", inline=False)
        dm.set_footer(text="Tu peux à nouveau rejoindre les serveurs du réseau.")
        asyncio.create_task(_send_dm(self.bot, tid, dm))

        embed = discord.Embed(title="🌍 Global Unban — OK", color=discord.Color.green(),
                              timestamp=datetime.now(timezone.utc))
        embed.description = f"`{tid}` retiré de la blacklist. Raven notifié automatiquement."
        embed.add_field(name="✅ Débanni", value=str(ok), inline=True)
        embed.add_field(name="⏭️ Pas banni", value=str(skip), inline=True)
        embed.add_field(name="❌ Échec", value=str(fail), inline=True)
        embed.set_footer(text=f"par {interaction.user}")
        await interaction.followup.send(embed=embed)

    @global_group.command(name="check", description="Voir si un user est dans la blacklist globale")
    @app_commands.describe(user_id="ID de l'user")
    async def global_check(self, interaction: Interaction, user_id: str):
        if not await _require(interaction, ALLOWED_IDS):
            return
        try:
            tid = int(user_id)
        except ValueError:
            return await interaction.response.send_message("❌ ID invalide.", ephemeral=True)

        conn = get_db()
        row = conn.execute("SELECT * FROM global_blacklist WHERE type='user' AND value=?",
                           (str(tid),)).fetchone()
        conn.close()

        embed = discord.Embed(timestamp=datetime.now(timezone.utc))
        if row:
            row = dict(row)
            embed.title = "🚫 Blacklisté"
            embed.color = discord.Color.red()
            embed.add_field(name="ID", value=f"`{tid}`", inline=True)
            embed.add_field(name="Raison", value=row.get("reason") or "—", inline=False)
            embed.add_field(name="Ajouté le", value=(row.get("added_at") or "?")[:10], inline=True)
            embed.add_field(name="Serveur d'origine", value=row.get("source_guild_name") or "—", inline=True)
        else:
            embed.title = "✅ Pas blacklisté"
            embed.color = discord.Color.green()
            embed.description = f"`{tid}` n'est pas dans la blacklist globale."

        try:
            u = await self.bot.fetch_user(tid)
            embed.set_author(name=str(u), icon_url=u.display_avatar.url)
        except Exception:
            pass

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @global_group.command(name="logs", description="Voir les dernières actions d'administration globale")
    async def global_logs(self, interaction: Interaction):
        if not await _require(interaction, ALLOWED_IDS):
            return

        conn = get_db()
        rows = conn.execute(
            """SELECT action, target_id, target_name, moderator_name, reason, proof_link, guild_name, created_at
               FROM admin_audit_log ORDER BY id DESC LIMIT 15"""
        ).fetchall()
        conn.close()

        _ACTIONS = {
            "global_ban":   ("🔨", "Ban global"),
            "global_unban": ("✅", "Unban global"),
        }

        embed = discord.Embed(title="📋 Logs d'administration globale", color=0x5865F2,
                              timestamp=datetime.now(timezone.utc))
        if not rows:
            embed.description = "*Aucune action enregistrée.*"
        else:
            lines = []
            for r in rows:
                icon, label = _ACTIONS.get(r[0], ("❓", r[0]))
                date = r[7][:10] if r[7] else "?"
                target = r[2] or f"`{r[1]}`"
                reason = f" — {r[4]}" if r[4] else ""
                proof = f" [[preuve]]({r[5]})" if r[5] else ""
                lines.append(f"{icon} **{label}** {target}{reason}{proof}\n"
                             f"  ↳ par **{r[3]}** depuis *{r[6] or '?'}* le {date}")
            embed.description = "\n".join(lines)
        embed.set_footer(text="15 dernières actions")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- /global watch / unwatch ---

    @global_group.command(name="watch", description="⚠️ Surveille un salon (forum de report) pour proposer des Global Ban automatiquement")
    @app_commands.describe(salon="Le salon forum à surveiller (doit être dans ce serveur)")
    async def global_watch(self, interaction: Interaction, salon: discord.ForumChannel):
        if not await _require(interaction, ADMIN_IDS):
            return
        if not interaction.guild or interaction.guild.id != REVIEW_GUILD_ID:
            return await interaction.response.send_message(
                "❌ Cette commande n'est utilisable que sur le serveur de review.", ephemeral=True
            )

        added = add_watch_channel(salon.id, salon.guild.id, str(interaction.user))
        if added:
            await interaction.response.send_message(
                f"✅ Le salon {salon.mention} est maintenant **surveillé**. "
                f"Les nouveaux posts contenant un ID en fin de titre (`nom - id`) déclencheront une proposition de Global Ban "
                f"dans le salon de review.",
                ephemeral=True
            )
        else:
            await interaction.response.send_message(f"ℹ️ Le salon {salon.mention} est déjà surveillé.", ephemeral=True)

    @global_group.command(name="unwatch", description="⚠️ Retire un salon de la surveillance Global Ban automatique")
    @app_commands.describe(salon="Le salon forum à ne plus surveiller")
    async def global_unwatch(self, interaction: Interaction, salon: discord.ForumChannel):
        if not await _require(interaction, ADMIN_IDS):
            return
        if not interaction.guild or interaction.guild.id != REVIEW_GUILD_ID:
            return await interaction.response.send_message(
                "❌ Cette commande n'est utilisable que sur le serveur de review.", ephemeral=True
            )

        removed = remove_watch_channel(salon.id)
        if removed:
            await interaction.response.send_message(f"✅ Le salon {salon.mention} n'est plus surveillé.", ephemeral=True)
        else:
            await interaction.response.send_message(f"ℹ️ Le salon {salon.mention} n'était pas surveillé.", ephemeral=True)

    # --- /globaladmin ---

    gadmin = app_commands.Group(
        name="globaladmin",
        description="Gérer qui peut utiliser /global ban etc.",
    )

    @gadmin.command(name="add", description="Donner accès aux commandes /global à quelqu'un")
    @app_commands.describe(utilisateur="L'user à autoriser")
    async def gadmin_add(self, interaction: Interaction, utilisateur: discord.User):
        if not await _require(interaction, ADMIN_IDS):
            return
        if utilisateur.id in ADMIN_IDS:
            return await interaction.response.send_message("ℹ️ Cette personne a toujours accès.", ephemeral=True)
        if add_allowed(utilisateur.id, str(interaction.user)):
            await interaction.response.send_message(f"✅ **{utilisateur}** peut maintenant faire `/global ban` etc.", ephemeral=True)
        else:
            await interaction.response.send_message(f"ℹ️ `{utilisateur.id}` avait déjà accès.", ephemeral=True)

    @gadmin.command(name="remove", description="Retirer l'accès aux commandes /global")
    @app_commands.describe(utilisateur="L'user à révoquer")
    async def gadmin_remove(self, interaction: Interaction, utilisateur: discord.User):
        if not await _require(interaction, ADMIN_IDS):
            return
        if utilisateur.id in ADMIN_IDS:
            return await interaction.response.send_message("❌ Impossible de retirer l'accès à un admin.", ephemeral=True)
        if remove_allowed(utilisateur.id):
            await interaction.response.send_message(f"✅ `{utilisateur.id}` n'a plus accès.", ephemeral=True)
        else:
            await interaction.response.send_message(f"ℹ️ `{utilisateur.id}` pas dans la liste.", ephemeral=True)

    @gadmin.command(name="list", description="Voir les admins global ban")
    async def gadmin_list(self, interaction: Interaction):
        if not await _require(interaction, ALLOWED_IDS):
            return
        embed = discord.Embed(title="🌍 Admins Global Ban", color=discord.Color.dark_purple(),
                              timestamp=datetime.now(timezone.utc))
        lines = [f"👑 <@{uid}> (`{uid}`) — *admin (permanent)*" for uid in ADMIN_IDS]
        for row in list_allowed():
            uid = row["user_id"]
            date = (row["added_at"] or "?")[:10]
            try:
                u = self.bot.get_user(int(uid)) or await self.bot.fetch_user(int(uid))
                name = str(u)
            except Exception:
                name = f"ID {uid}"
            lines.append(f"• <@{uid}> **{name}** — {date} par {row['added_by']}")
        embed.description = "\n".join(lines)
        embed.set_footer(text=f"{len(ALLOWED_IDS)} ID(s) en mémoire")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- /globalsync (opt-in par serveur) ---

    @app_commands.command(name="globalsync", description="Activer/désactiver la sync des global bans pour ce serveur")
    @app_commands.describe(
        action="on / off / status",
        salon="Salon de notification (requis si on)"
    )
    @app_commands.choices(action=[
        app_commands.Choice(name="on — activer", value="on"),
        app_commands.Choice(name="off — désactiver", value="off"),
        app_commands.Choice(name="status — voir l'état", value="status"),
    ])
    @app_commands.default_permissions(administrator=True)
    async def globalsync(self, interaction: Interaction,
                         action: app_commands.Choice[str],
                         salon: discord.TextChannel = None):
        if not interaction.guild:
            return await interaction.response.send_message("❌ Commande serveur uniquement.", ephemeral=True)
        gid = str(interaction.guild.id)

        if action.value == "on":
            if not salon:
                return await interaction.response.send_message("❌ Tu dois spécifier un salon de notification.", ephemeral=True)
            conn = get_db()
            conn.execute(
                "INSERT OR REPLACE INTO guild_ban_sync (guild_id, enabled, enabled_at, notification_channel_id) VALUES (?, 1, ?, ?)",
                (gid, datetime.now(timezone.utc).isoformat(), str(salon.id))
            )
            conn.commit()
            conn.close()
            await interaction.response.send_message(
                f"✅ Sync Global Ban activée. Alertes dans {salon.mention}.", ephemeral=True
            )

        elif action.value == "off":
            conn = get_db()
            conn.execute("UPDATE guild_ban_sync SET enabled=0 WHERE guild_id=?", (gid,))
            conn.commit()
            conn.close()
            await interaction.response.send_message("❌ Sync Global Ban désactivée.", ephemeral=True)

        else:
            conn = get_db()
            row = conn.execute("SELECT enabled, notification_channel_id FROM guild_ban_sync WHERE guild_id=?",
                               (gid,)).fetchone()
            conn.close()
            if row:
                status = "✅ Activée" if row[0] else "❌ Désactivée"
                ch = f"<#{row[1]}>" if row[1] else "Non configuré"
                await interaction.response.send_message(f"**Sync Global Ban** : {status}\nSalon : {ch}", ephemeral=True)
            else:
                await interaction.response.send_message("**Sync Global Ban** : ❌ Non configurée", ephemeral=True)


    @app_commands.command(name="list", description="Voir la liste des utilisateurs blacklistés globalement")
    async def list_users(self, interaction: Interaction):
        if not await _require(interaction, ALLOWED_IDS):
            return
        conn = get_db()
        rows = conn.execute(
            "SELECT value, reason, added_at, source_guild_name FROM global_blacklist WHERE type='user' ORDER BY added_at DESC"
        ).fetchall()
        conn.close()
        rows = [dict(r) for r in rows]
        embed = discord.Embed(
            title=f"🚫 Utilisateurs blacklistés ({len(rows)})",
            color=0xED4245,
            timestamp=datetime.now(timezone.utc)
        )
        if not rows:
            embed.description = "*Aucun utilisateur blacklisté.*"
        else:
            lines = []
            for r in rows[:20]:
                origin = f" *(via {r['source_guild_name']})*" if r.get("source_guild_name") else ""
                date = (r.get("added_at") or "?")[:10]
                reason = r.get("reason") or "—"
                lines.append(f"• `{r['value']}` — {reason} [{date}]{origin}")
            if len(rows) > 20:
                lines.append(f"*+{len(rows) - 20} autres...*")
            embed.description = "\n".join(lines)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- /aidesafezone (staff) ---

    @app_commands.command(name="aidesafezone", description="📖 Mode d'emploi de configuration du réseau Global Ban (staff)")
    @app_commands.default_permissions(administrator=True)
    async def aide_safezone(self, interaction: Interaction):
        embed = discord.Embed(
            title="📖 Configurer le réseau SafeZone / Global Ban",
            color=discord.Color.dark_purple(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="1️⃣ Recevoir les alertes de ban", value=(
            "`/globalsync on #salon` — active les alertes (boutons Bannir/Ignorer) dans ce salon\n"
            "`/globalsync off` — désactive\n"
            "`/globalsync status` — voir l'état actuel"
        ), inline=False)
        embed.add_field(name="2️⃣ Agir sur la blacklist globale", value=(
            "`/global ban <id> <raison> <lien>` — bannir partout + alerter les serveurs opt-in "
            "(raison et lien de preuve obligatoires)\n"
            "`/global unban <id>` — retirer de la blacklist + débannir\n"
            "`/global check <id>` — voir si quelqu'un est blacklisté\n"
            "`/global logs` — historique des dernières actions\n"
            "`/list` — voir tous les utilisateurs blacklistés"
        ), inline=False)
        embed.add_field(name="3️⃣ Détection automatique des reports", value=(
            "`/global watch #forum` — surveille un forum de report ; chaque nouveau post avec un ID en fin de titre "
            "génère automatiquement une proposition (avec preuve) dans le salon de review\n"
            "`/global unwatch #forum` — arrête la surveillance\n"
            "-# Utilisable uniquement sur ce serveur de review, et seulement par les admins fixés dans le code "
            "(indépendant de /globaladmin). La proposition est postée automatiquement, mais reste soumise "
            "à validation humaine (✅ Confirmer / 🗑️ Ignorer) avant le ban réel."
        ), inline=False)
        embed.add_field(name="4️⃣ Gérer qui a accès à ces commandes", value=(
            "`/globaladmin add <user>` — donner l'accès à /global ban/unban/check/logs et /list\n"
            "`/globaladmin remove <user>` — retirer l'accès\n"
            "`/globaladmin list` — voir qui a accès\n"
            "-# /global watch et /unwatch ne sont pas concernés par /globaladmin — accès fixé dans le code, "
            "et seul un des admins fixés peut en ajouter/retirer d'autres."
        ), inline=False)
        embed.set_footer(text="Réservé aux admins du serveur")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- /aideadmin (admin d'un serveur qui reçoit juste les alertes) ---

    @app_commands.command(name="aideadmin", description="📖 Comment recevoir les alertes de Global Ban sur ton serveur")
    @app_commands.default_permissions(administrator=True)
    async def aide_admin(self, interaction: Interaction):
        embed = discord.Embed(
            title="📖 Configurer ton serveur pour recevoir les alertes",
            description="Ce bot fait partie d'un réseau inter-serveurs : quand un utilisateur est banni globalement ailleurs, ton serveur peut être alerté et bannir en un clic.",
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="1️⃣ Activer les alertes", value=(
            "`/globalsync on #salon` — les alertes (avec boutons ✅ Bannir / ❌ Ignorer) arrivent dans ce salon\n"
            "`/globalsync off` — désactive\n"
            "`/globalsync status` — voir l'état actuel"
        ), inline=False)
        embed.set_footer(text="/global ban, check et la gestion des accès sont réservés aux comptes autorisés via /globaladmin")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # --- /aide (membre) ---

    @app_commands.command(name="aide", description="ℹ️ Ce que ce bot fait et les commandes disponibles pour toi")
    async def aide_membre(self, interaction: Interaction):
        embed = discord.Embed(
            title="ℹ️ À propos de ce bot",
            description=(
                "Ce bot fait partie d'un **réseau de modération inter-serveurs** (Global Ban) : "
                "si quelqu'un est banni pour une raison grave sur un serveur du réseau, il peut être "
                "banni automatiquement sur les autres.\n\n"
                "Toutes ses commandes sont réservées au staff — il n'y a **aucune commande disponible "
                "pour un membre normal**.\n\n"
                "Si tu as été banni via ce réseau, tu as normalement reçu un message privé du bot "
                "avec la raison et le serveur d'origine. Pour contester, contacte le staff de ce serveur."
            ),
            color=discord.Color.blurple(),
            timestamp=datetime.now(timezone.utc)
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(GlobalModCog(bot))
