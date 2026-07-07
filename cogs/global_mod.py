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

BOT_NAME = os.getenv("BOT_NAME", "savezone")
OWNER_ID = int(os.getenv("PROTECTED_USER_ID", "1458518253373493353"))

# Super admins : proprio + co-admins pouvant gérer le bot et accorder l'accès /global
SUPER_ADMIN_IDS = {OWNER_ID, 1335581687760949313}

# set chargé au démarrage depuis la DB
ALLOWED_IDS: set[int] = set(SUPER_ADMIN_IDS)

_TYPE_LABELS = {
    "user":   ("👤", "Utilisateur"),
    "word":   ("🔤", "Mot"),
    "domain": ("🌐", "Domaine"),
    "guild":  ("🏠", "Serveur"),
}


# --- DB ---

def _db_path():
    path = os.getenv("SHARED_GLOBAL_DB_PATH", "data/database/global/global_lists.db")
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    return path


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

    # migration si colonnes pas encore là
    for col in ("source_guild_id TEXT DEFAULT ''", "source_guild_name TEXT DEFAULT ''"):
        try:
            conn.execute(f"ALTER TABLE global_blacklist ADD COLUMN {col}")
            conn.commit()
        except Exception:
            pass

    conn.execute("CREATE INDEX IF NOT EXISTS idx_bl_type ON global_blacklist(type)")

    conn.execute("""CREATE TABLE IF NOT EXISTS guild_ban_sync (
        guild_id TEXT PRIMARY KEY,
        enabled INTEGER DEFAULT 0,
        enabled_at TEXT,
        notification_channel_id TEXT)""")
    try:
        conn.execute("ALTER TABLE guild_ban_sync ADD COLUMN notification_channel_id TEXT")
        conn.commit()
    except Exception:
        pass

    conn.execute("""CREATE TABLE IF NOT EXISTS pending_ban_sync (
        guild_id TEXT PRIMARY KEY,
        queued_at TEXT NOT NULL)""")

    conn.execute("""CREATE TABLE IF NOT EXISTS global_cmd_allowed (
        user_id TEXT PRIMARY KEY,
        added_by TEXT NOT NULL,
        added_at TEXT NOT NULL)""")

    # table inter-bots — Raven et SaveZone écrivent ici pour se notifier
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

    for col in ("source_guild_id TEXT DEFAULT ''", "source_guild_name TEXT DEFAULT ''"):
        try:
            conn.execute(f"ALTER TABLE cross_bot_events ADD COLUMN {col}")
            conn.commit()
        except Exception:
            pass

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


# --- helpers blacklist ---

def is_blacklisted(type_: str, value: str) -> bool:
    try:
        conn = get_db()
        r = conn.execute("SELECT 1 FROM global_blacklist WHERE type=? AND value=?",
                         (type_, str(value).lower())).fetchone()
        conn.close()
        return r is not None
    except Exception:
        return False


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
                      link=None, guild_name="", source_bot=BOT_NAME):
    optin = get_optin_guilds()
    try:
        u = await bot.fetch_user(target_id)
        target_str = f"{u} (`{target_id}`)"
        avatar = u.display_avatar.url
    except Exception:
        target_str = f"`{target_id}`"
        avatar = None

    origin = "Raven" if source_bot == "raven" else "SaveZone"

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
        embed.add_field(name="Serveur d'origine", value=f"**{guild_name}** (via {origin})", inline=False)
    if link:
        embed.add_field(name="Preuve", value=f"[Voir le message]({link})", inline=False)
    if avatar:
        embed.set_thumbnail(url=avatar)
    embed.set_footer(text="SaveZone • admin requis pour agir")

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


# --- cog ---

class GlobalModCog(commands.Cog, name="GlobalMod"):
    def __init__(self, bot):
        self.bot = bot

    async def cog_load(self):
        load_allowed()
        self.bot.add_dynamic_items(BanButton, IgnoreButton)
        self.crossbot_task.start()
        print(f"[SaveZone] GlobalModCog ok (bot={BOT_NAME})")

    def cog_unload(self):
        self.crossbot_task.cancel()

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
        banner = ev.get("banner_name", "Bot distant")
        link = ev.get("message_link") or None
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
                asyncio.create_task(send_alerts(self.bot, tid, reason, banner, link, guild_name, src))
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
        default_permissions=discord.Permissions(administrator=True)
    )

    @global_group.command(name="ban", description="Global ban un user + alerte tous les serveurs opt-in")
    @app_commands.describe(
        user_id="ID de l'user à bannir",
        raison="Raison",
        message_link="Lien de preuve Discord (optionnel)"
    )
    async def global_ban(self, interaction: Interaction, user_id: str,
                         raison: str = "Ban Global SaveZone", message_link: str = None):
        if interaction.user.id not in ALLOWED_IDS:
            return await interaction.response.send_message("❌ Accès refusé.", ephemeral=True)
        if message_link and not DISCORD_LINK_RE.match(message_link):
            return await interaction.response.send_message("❌ Lien invalide. Format attendu : `https://discord.com/channels/GUILD/CHANNEL/MESSAGE`", ephemeral=True)

        try:
            tid = int(user_id)
        except ValueError:
            return await interaction.response.send_message("❌ ID invalide.", ephemeral=True)
        if tid == interaction.user.id:
            return await interaction.response.send_message("❌ Tu ne peux pas te bannir toi-même.", ephemeral=True)

        # récupérer infos user
        try:
            target = await self.bot.fetch_user(tid)
            target_str = f"{target} (`{tid}`)"
            avatar = target.display_avatar.url
        except Exception:
            target_str = f"`{tid}`"
            avatar = None

        gname = interaction.guild.name if interaction.guild else "DM"

        # embed de confirmation
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

                conn = get_db()
                conn.execute(
                    """INSERT OR IGNORE INTO global_blacklist
                       (type, value, reason, added_at, source_guild_id, source_guild_name)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    ("user", str(tid), raison, datetime.now(timezone.utc).isoformat(), str(gid), gname)
                )
                conn.commit()
                conn.close()

                write_event("global_ban", tid, raison, str(interaction.user),
                            interaction.user.id, message_link or "", gid, gname)
                _write_audit("global_ban", tid, target_str, interaction.user.id,
                             str(interaction.user), raison, message_link or "", gname)

                # DM à l'utilisateur banni
                dm = discord.Embed(
                    title="⛔ Tu as été signalé sur le réseau SaveZone",
                    color=discord.Color.dark_red(),
                    timestamp=datetime.now(timezone.utc)
                )
                dm.add_field(name="Raison", value=raison or "—", inline=False)
                dm.add_field(name="Serveur d'origine", value=gname, inline=True)
                if message_link:
                    dm.add_field(name="Preuve", value=f"[Voir le message]({message_link})", inline=False)
                dm.set_footer(text="Si tu penses qu'il s'agit d'une erreur, contacte les administrateurs du réseau.")
                asyncio.create_task(_send_dm(self.bot, tid, dm))

                done = discord.Embed(title="🌍 Global Ban — OK", color=discord.Color.dark_red(),
                                     timestamp=datetime.now(timezone.utc))
                done.add_field(name="Utilisateur", value=target_str, inline=True)
                done.add_field(name="Raison", value=raison, inline=False)
                done.add_field(name="Serveur d'origine", value=gname, inline=True)
                done.add_field(name="Serveurs notifiés (SZ)", value=str(notified), inline=True)
                done.add_field(name="Raven notifié", value="✅ via DB (15s)", inline=False)
                if message_link:
                    done.add_field(name="Preuve", value=f"[Voir]({message_link})", inline=False)
                done.set_footer(text=f"par {interaction.user}")

                await btn_interaction.response.edit_message(embed=done, view=None)
                asyncio.create_task(send_alerts(self.bot, tid, raison, str(interaction.user),
                                                message_link, gname, BOT_NAME))

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
                           raison: str = "Unban Global SaveZone"):
        if interaction.user.id not in ALLOWED_IDS:
            return await interaction.response.send_message("❌ Accès refusé.", ephemeral=True)
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

        # DM à l'utilisateur débanni
        dm = discord.Embed(
            title="✅ Tu as été retiré de la liste noire du réseau SaveZone",
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
        is_admin = False
        if interaction.user.id in ALLOWED_IDS:
            is_admin = True
        elif interaction.guild:
            m = interaction.guild.get_member(interaction.user.id)
            if m and m.guild_permissions.administrator:
                is_admin = True
        if not is_admin:
            return await interaction.response.send_message("❌ Réservé aux administrateurs.", ephemeral=True)
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

    # --- /global blacklist ---

    bl_group = app_commands.Group(
        name="blacklist",
        description="Gérer la blacklist globale",
        parent=global_group
    )

    _TYPES = ("user", "word", "domain", "guild")

    @bl_group.command(name="add", description="Ajouter un utilisateur à la blacklist globale")
    @app_commands.describe(
        id="ID Discord de l'utilisateur",
        raison="Raison (optionnel)",
        lien="Lien de preuve Discord (optionnel)"
    )
    async def bl_add(self, interaction: Interaction, id: str, raison: str = "", lien: str = None):
        if interaction.user.id not in ALLOWED_IDS:
            return await interaction.response.send_message("❌ Accès refusé.", ephemeral=True)
        if lien and not DISCORD_LINK_RE.match(lien):
            return await interaction.response.send_message("❌ Lien invalide. Format attendu : `https://discord.com/channels/GUILD/CHANNEL/MESSAGE`", ephemeral=True)
        try:
            tid = int(id.strip())
        except ValueError:
            return await interaction.response.send_message("❌ ID invalide.", ephemeral=True)

        try:
            target = await self.bot.fetch_user(tid)
            target_str = f"{target} (`{tid}`)"
            avatar = target.display_avatar.url
        except Exception:
            target_str = f"`{tid}`"
            avatar = None

        gname = interaction.guild.name if interaction.guild else "DM"

        embed = discord.Embed(
            title="⚠️ Confirmation — Blacklist",
            description="Tu es sur le point d'ajouter cet utilisateur à la blacklist globale.",
            color=discord.Color.orange(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="Utilisateur", value=target_str, inline=True)
        embed.add_field(name="Raison", value=raison or "—", inline=False)
        if lien:
            embed.add_field(name="Preuve", value=f"[Voir]({lien})", inline=False)
        if avatar:
            embed.set_thumbnail(url=avatar)

        class ConfirmView(discord.ui.View):
            def __init__(self_v):
                super().__init__(timeout=60)

            @discord.ui.button(label="✅ Confirmer", style=discord.ButtonStyle.danger)
            async def confirm(self_v, btn: discord.Interaction, button: discord.ui.Button):
                if btn.user.id != interaction.user.id:
                    return await btn.response.send_message("❌ Pas pour toi.", ephemeral=True)
                self_v.stop()
                gid = str(interaction.guild.id) if interaction.guild else ""
                conn = get_db()
                conn.execute(
                    """INSERT OR IGNORE INTO global_blacklist
                       (type, value, reason, added_at, source_guild_id, source_guild_name)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    ("user", str(tid), raison, datetime.now(timezone.utc).isoformat(), gid, gname)
                )
                conn.commit()
                changed = conn.execute("SELECT changes()").fetchone()[0]
                conn.close()

                if changed:
                    gid_int = interaction.guild.id if interaction.guild else 0
                    write_event("global_ban", tid, raison, str(btn.user),
                                btn.user.id, lien or "", gid_int, gname)
                    _write_audit("blacklist_add", tid, target_str, btn.user.id,
                                 str(btn.user), raison, lien or "", gname)

                    # DM à l'utilisateur blacklisté
                    dm = discord.Embed(
                        title="⛔ Tu as été signalé sur le réseau SaveZone",
                        color=discord.Color.dark_red(),
                        timestamp=datetime.now(timezone.utc)
                    )
                    dm.add_field(name="Raison", value=raison or "—", inline=False)
                    dm.add_field(name="Serveur d'origine", value=gname, inline=True)
                    if lien:
                        dm.add_field(name="Preuve", value=f"[Voir le message]({lien})", inline=False)
                    dm.set_footer(text="Si tu penses qu'il s'agit d'une erreur, contacte les administrateurs du réseau.")
                    asyncio.create_task(_send_dm(self.bot, tid, dm))

                    done = discord.Embed(title="🚫 Utilisateur blacklisté", color=0xED4245,
                                         timestamp=datetime.now(timezone.utc))
                    done.add_field(name="Utilisateur", value=target_str, inline=True)
                    done.add_field(name="Raison", value=raison or "—", inline=False)
                    if lien:
                        done.add_field(name="Preuve", value=f"[Voir]({lien})", inline=False)
                    done.add_field(name="Raven notifié", value="✅ via DB (15s)", inline=False)
                    done.set_footer(text=f"par {btn.user}")
                    if avatar:
                        done.set_thumbnail(url=avatar)
                    await btn.response.edit_message(embed=done, view=None)
                    asyncio.create_task(send_alerts(self.bot, tid, raison, str(btn.user),
                                                    lien, gname, BOT_NAME))
                else:
                    await btn.response.edit_message(
                        embed=discord.Embed(title=f"ℹ️ `{tid}` est déjà dans la blacklist.", color=discord.Color.greyple()),
                        view=None
                    )

            @discord.ui.button(label="❌ Annuler", style=discord.ButtonStyle.secondary)
            async def cancel(self_v, btn: discord.Interaction, button: discord.ui.Button):
                if btn.user.id != interaction.user.id:
                    return await btn.response.send_message("❌ Pas pour toi.", ephemeral=True)
                self_v.stop()
                await btn.response.edit_message(
                    embed=discord.Embed(title="❌ Annulé", color=discord.Color.greyple()),
                    view=None
                )

        await interaction.response.send_message(embed=embed, view=ConfirmView(), ephemeral=True)

    @bl_group.command(name="remove", description="Retirer de la blacklist")
    @app_commands.describe(type="Type", valeur="Valeur à retirer")
    async def bl_remove(self, interaction: Interaction, type: str, valeur: str):
        if interaction.user.id not in ALLOWED_IDS:
            return await interaction.response.send_message("❌ Accès refusé.", ephemeral=True)
        if type not in self._TYPES:
            return await interaction.response.send_message(f"❌ Types valides : {', '.join(self._TYPES)}", ephemeral=True)
        conn = get_db()
        conn.execute("DELETE FROM global_blacklist WHERE type=? AND value=?", (type, valeur.strip().lower()))
        conn.commit()
        changed = conn.execute("SELECT changes()").fetchone()[0]
        conn.close()
        await interaction.response.send_message(
            f"🗑️ `{valeur}` retiré." if changed else f"❌ `{valeur}` non trouvé.", ephemeral=True
        )

    @bl_group.command(name="liste", description="Voir la blacklist globale")
    @app_commands.describe(type="Filtrer par type (laisser vide = tout)")
    async def bl_liste(self, interaction: Interaction, type: str = "all"):
        if interaction.user.id not in ALLOWED_IDS:
            return await interaction.response.send_message("❌ Accès refusé.", ephemeral=True)
        conn = get_db()
        if type and type != "all":
            rows = conn.execute("SELECT * FROM global_blacklist WHERE type=? ORDER BY added_at DESC", (type,)).fetchall()
        else:
            rows = conn.execute("SELECT * FROM global_blacklist ORDER BY type, added_at DESC").fetchall()
        conn.close()
        rows = [dict(r) for r in rows]

        embed = discord.Embed(title="🚫 Blacklist globale", color=0xED4245, timestamp=datetime.now(timezone.utc))
        if not rows:
            embed.description = "*Vide.*"
        else:
            by_type: dict = {}
            for r in rows:
                by_type.setdefault(r["type"], []).append(r)
            for t, entries in by_type.items():
                em, lbl = _TYPE_LABELS.get(t, ("🔹", t))
                lines = []
                for e in entries[:10]:
                    orig = f" *(via {e['source_guild_name']})*" if e.get("source_guild_name") else ""
                    lines.append(f"• `{e['value']}`{orig}")
                if len(entries) > 10:
                    lines.append(f"*+{len(entries) - 10} autres*")
                embed.add_field(name=f"{em} {lbl} ({len(entries)})", value="\n".join(lines), inline=False)
            embed.set_footer(text=f"{len(rows)} entrée(s)")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @global_group.command(name="logs", description="Voir les dernières actions d'administration globale")
    async def global_logs(self, interaction: Interaction):
        is_admin = interaction.user.id in ALLOWED_IDS
        if not is_admin and interaction.guild:
            m = interaction.guild.get_member(interaction.user.id)
            if m and m.guild_permissions.administrator:
                is_admin = True
        if not is_admin:
            return await interaction.response.send_message("❌ Réservé aux administrateurs.", ephemeral=True)

        conn = get_db()
        rows = conn.execute(
            """SELECT action, target_id, target_name, moderator_name, reason, proof_link, guild_name, created_at
               FROM admin_audit_log ORDER BY id DESC LIMIT 15"""
        ).fetchall()
        conn.close()

        _ACTIONS = {
            "global_ban":    ("🔨", "Ban global"),
            "global_unban":  ("✅", "Unban global"),
            "blacklist_add": ("🚫", "Blacklist ajout"),
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

    # --- /globaladmin ---

    gadmin = app_commands.Group(
        name="globaladmin",
        description="Gérer qui peut utiliser /global ban etc.",
        default_permissions=discord.Permissions(administrator=True)
    )

    @gadmin.command(name="add", description="Donner accès aux commandes /global à quelqu'un")
    @app_commands.describe(utilisateur="L'user à autoriser")
    async def gadmin_add(self, interaction: Interaction, utilisateur: discord.User):
        if interaction.user.id not in SUPER_ADMIN_IDS:
            return await interaction.response.send_message("❌ Réservé aux super admins.", ephemeral=True)
        if utilisateur.id in SUPER_ADMIN_IDS:
            return await interaction.response.send_message("ℹ️ Cette personne a toujours accès.", ephemeral=True)
        if add_allowed(utilisateur.id, str(interaction.user)):
            await interaction.response.send_message(f"✅ **{utilisateur}** peut maintenant faire `/global ban` etc.", ephemeral=True)
        else:
            await interaction.response.send_message(f"ℹ️ `{utilisateur.id}` avait déjà accès.", ephemeral=True)

    @gadmin.command(name="remove", description="Retirer l'accès aux commandes /global")
    @app_commands.describe(utilisateur="L'user à révoquer")
    async def gadmin_remove(self, interaction: Interaction, utilisateur: discord.User):
        if interaction.user.id not in SUPER_ADMIN_IDS:
            return await interaction.response.send_message("❌ Réservé aux super admins.", ephemeral=True)
        if utilisateur.id in SUPER_ADMIN_IDS:
            return await interaction.response.send_message("❌ Impossible de retirer l'accès à un super admin.", ephemeral=True)
        if remove_allowed(utilisateur.id):
            await interaction.response.send_message(f"✅ `{utilisateur.id}` n'a plus accès.", ephemeral=True)
        else:
            await interaction.response.send_message(f"ℹ️ `{utilisateur.id}` pas dans la liste.", ephemeral=True)

    @gadmin.command(name="list", description="Voir les admins global ban")
    async def gadmin_list(self, interaction: Interaction):
        if interaction.user.id not in ALLOWED_IDS:
            return await interaction.response.send_message("❌ Accès refusé.", ephemeral=True)
        embed = discord.Embed(title="🌍 Admins Global Ban", color=discord.Color.dark_purple(),
                              timestamp=datetime.now(timezone.utc))
        lines = [f"👑 <@{OWNER_ID}> (`{OWNER_ID}`) — *proprio (permanent)*"]
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

    # commandes prefix globaladmin (owner only)

    @commands.group(name="globaladmin", aliases=["gadmin"], invoke_without_command=True)
    async def gadmin_prefix(self, ctx: commands.Context):
        if ctx.author.id not in SUPER_ADMIN_IDS:
            return
        p = ctx.prefix
        await ctx.send(f"`{p}globaladmin add <id>` | `remove <id>` | `list`")

    @gadmin_prefix.command(name="add")
    async def gadmin_prefix_add(self, ctx, user_id: str):
        if ctx.author.id not in SUPER_ADMIN_IDS:
            return
        try:
            uid = int(user_id)
        except ValueError:
            return await ctx.send("❌ ID invalide.")
        if uid in SUPER_ADMIN_IDS:
            return await ctx.send("ℹ️ Cette personne a toujours accès.")
        if add_allowed(uid, str(ctx.author)):
            try:
                u = await self.bot.fetch_user(uid)
                await ctx.send(f"✅ **{u}** peut utiliser `/global`.")
            except Exception:
                await ctx.send(f"✅ `{uid}` ajouté.")
        else:
            await ctx.send(f"ℹ️ `{uid}` avait déjà accès.")

    @gadmin_prefix.command(name="remove")
    async def gadmin_prefix_remove(self, ctx, user_id: str):
        if ctx.author.id not in SUPER_ADMIN_IDS:
            return
        try:
            uid = int(user_id)
        except ValueError:
            return await ctx.send("❌ ID invalide.")
        if uid in SUPER_ADMIN_IDS:
            return await ctx.send("❌ Impossible.")
        await ctx.send(f"✅ `{uid}` retiré." if remove_allowed(uid) else f"ℹ️ `{uid}` pas dans la liste.")

    @gadmin_prefix.command(name="list")
    async def gadmin_prefix_list(self, ctx):
        if ctx.author.id not in SUPER_ADMIN_IDS:
            return
        embed = discord.Embed(title="🌍 Admins Global Ban", color=discord.Color.dark_purple())
        lines = [f"👑 <@{OWNER_ID}> — *proprio*"]
        for row in list_allowed():
            lines.append(f"• <@{row['user_id']}> — {(row['added_at'] or '?')[:10]}")
        embed.description = "\n".join(lines)
        await ctx.send(embed=embed)

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
    @app_commands.default_permissions(administrator=True)
    async def list_users(self, interaction: Interaction):
        if interaction.user.id not in ALLOWED_IDS:
            return await interaction.response.send_message("❌ Accès refusé.", ephemeral=True)
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

    @commands.command(name="list")
    async def list_prefix(self, ctx: commands.Context):
        if ctx.author.id not in SUPER_ADMIN_IDS:
            return
        p = ctx.prefix
        embed = discord.Embed(
            title="📋 Commandes préfixe",
            color=0x5865F2,
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="🔧 Admin", value=(
            f"`{p}sync [guild_id]` — sync les slash commands\n"
            f"`{p}reload [cog|all]` — recharge un cog\n"
            f"`{p}cogs` — liste les cogs chargés\n"
            f"`{p}bots` — infos du bot\n"
            f"`{p}reboot` — redémarre le bot"
        ), inline=False)
        embed.add_field(name="🌍 Global Admin", value=(
            f"`{p}globaladmin add <id>` — donner accès à /global\n"
            f"`{p}globaladmin remove <id>` — retirer l'accès\n"
            f"`{p}globaladmin list` — voir les admins global ban"
        ), inline=False)
        embed.add_field(name="📋 Listes", value=(
            f"`{p}list` — cette aide\n"
        ), inline=False)
        embed.set_footer(text=f"Owner only • {len(ctx.bot.commands)} commandes enregistrées")
        await ctx.send(embed=embed)

    # --- /aidesafezone (staff) ---

    @app_commands.command(name="aidesafezone", description="📖 Mode d'emploi de configuration du réseau Global Ban (staff)")
    @app_commands.default_permissions(administrator=True)
    async def aide_safezone(self, interaction: Interaction):
        embed = discord.Embed(
            title="📖 Configurer le réseau SaveZone / Global Ban",
            color=discord.Color.dark_purple(),
            timestamp=datetime.now(timezone.utc)
        )
        embed.add_field(name="1️⃣ Recevoir les alertes de ban", value=(
            "`/globalsync on #salon` — active les alertes (boutons Bannir/Ignorer) dans ce salon\n"
            "`/globalsync off` — désactive\n"
            "`/globalsync status` — voir l'état actuel"
        ), inline=False)
        embed.add_field(name="2️⃣ Agir sur la blacklist globale", value=(
            "`/global ban <id>` — bannir partout + alerter les serveurs opt-in\n"
            "`/global unban <id>` — retirer de la blacklist + débannir\n"
            "`/global check <id>` — voir si quelqu'un est blacklisté\n"
            "`/global logs` — historique des dernières actions"
        ), inline=False)
        embed.add_field(name="3️⃣ Gérer qui a accès à ces commandes", value=(
            "`/globaladmin add <user>` — donner l'accès à /global\n"
            "`/globaladmin remove <user>` — retirer l'accès\n"
            "`/globaladmin list` — voir qui a accès"
        ), inline=False)
        embed.add_field(name="4️⃣ Logs classiques du serveur (optionnel)", value=(
            "`/logsetup set <type> #salon` — configurer un salon de log\n"
            "`/logsetup remove <type>` — désactiver un type de log\n"
            "`/logsetup view` — voir la config actuelle"
        ), inline=False)
        embed.set_footer(text="Réservé aux admins du serveur")
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
