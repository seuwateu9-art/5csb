"""
Activity/Duty Report Point Bot (Global Sync Fixed Version)
- discord.py 2.x (app_commands / CommandTree)
- SQLite (aiosqlite) storage
"""
from __future__ import annotations

import asyncio
import logging
import os
import time

import aiosqlite
import discord
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
DB_PATH = os.getenv("DB_PATH", "activity.db")

DEFAULT_ITEM = "Basic Report"
MIN_REPORT_LENGTH = 5

DEFAULT_STANDARDS = [
    (DEFAULT_ITEM, 10, "Default points awarded for submitting a report in the channel"),
    ("Patrol", 15, "Awarded when 'Patrol' is included in the report"),
    ("Training", 15, "Awarded when 'Training' is included in the report"),
    ("Education", 20, "Awarded when 'Education' is included in the report"),
    ("Event", 20, "Awarded when 'Event' is included in the report"),
    ("Activity", 30, "Awarded when 'Activity' is included along with photos/images and time info"),
]

DEFAULT_RANKS = [
    (1, "Private", None, 10, "", 1),
    (2, "Private First Class", None, 20, "", 1),
    (3, "Corporal", None, 30, "", 1),
    (4, "Sergeant", None, 40, "", 1),
    (5, "Staff Sergeant", None, 50, "", 1),
    (6, "Sergeant First Class", None, 60, "", 1),
    (7, "Master Sergeant", None, 70, "", 1),
    (8, "Second Lieutenant", None, 80, "", 1),
    (9, "First Lieutenant", None, 90, "", 1),
    (10, "Captain", None, 100, "", 1),
    (11, "Major", None, 110, "", 1),
    (12, "Lieutenant Colonel", None, 120, "", 1),
]

COLOR_MAIN = discord.Color.blurple()
COLOR_OK = discord.Color.green()
COLOR_ERR = discord.Color.red()

log = logging.getLogger("activity-bot")

SCHEMA = """
CREATE TABLE IF NOT EXISTS settings (
    guild_id INTEGER NOT NULL,
    key      TEXT    NOT NULL,
    value    TEXT,
    PRIMARY KEY (guild_id, key)
);
CREATE TABLE IF NOT EXISTS standards (
    guild_id    INTEGER NOT NULL,
    item        TEXT    NOT NULL,
    points      INTEGER NOT NULL,
    description TEXT    DEFAULT '',
    PRIMARY KEY (guild_id, item)
);
CREATE TABLE IF NOT EXISTS user_points (
    guild_id     INTEGER NOT NULL,
    user_id      INTEGER NOT NULL,
    total_points INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (guild_id, user_id)
);
CREATE TABLE IF NOT EXISTS activity_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id   INTEGER NOT NULL,
    user_id    INTEGER NOT NULL,
    username   TEXT    NOT NULL,
    points     INTEGER NOT NULL,
    reason     TEXT    NOT NULL,
    message_id INTEGER UNIQUE,
    created_at INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS ranks (
    guild_id        INTEGER NOT NULL,
    rank_order      INTEGER NOT NULL,
    name            TEXT    NOT NULL,
    role_id         INTEGER,
    required_points INTEGER NOT NULL,
    requirement     TEXT    DEFAULT '',
    auto_promote    INTEGER NOT NULL DEFAULT 1,
    PRIMARY KEY (guild_id, rank_order)
);
CREATE INDEX IF NOT EXISTS idx_log_user
    ON activity_log (guild_id, user_id, created_at DESC);
"""


class NotLeader(app_commands.CheckFailure):
    pass


def is_admin_or_leader():
    async def predicate(interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            raise app_commands.NoPrivateMessage()
        member = interaction.user
        if member.guild_permissions.administrator:
            return True
        role_id = await interaction.client.get_setting(interaction.guild.id, "leader_role_id")
        if role_id and any(r.id == int(role_id) for r in member.roles):
            return True
        raise NotLeader()

    return app_commands.check(predicate)


def make_embed(title: str, description: str = "", color: discord.Color = COLOR_MAIN) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=color)


def error_embed(message: str) -> discord.Embed:
    return make_embed("⚠️ Error", message, COLOR_ERR)


class ActivityBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)
        self.db: aiosqlite.Connection | None = None
        self.db_lock = asyncio.Lock()

    async def setup_hook(self) -> None:
        self.db = await aiosqlite.connect(DB_PATH)
        self.db.row_factory = aiosqlite.Row
        await self.db.executescript(SCHEMA)
        await self.db.commit()

        # 전역(Global) 명령어 동기화
        try:
            synced = await self.tree.sync()
            log.info("Successfully synced %d global commands.", len(synced))
        except Exception:
            log.exception("Failed to sync global commands")

    async def close(self) -> None:
        if self.db:
            await self.db.close()
        await super().close()

    async def get_setting(self, guild_id: int, key: str) -> str | None:
        async with self.db.execute(
            "SELECT value FROM settings WHERE guild_id=? AND key=?", (guild_id, key)
        ) as cur:
            row = await cur.fetchone()
        return row["value"] if row else None

    async def set_setting(self, guild_id: int, key: str, value: str | None) -> None:
        async with self.db_lock:
            await self.db.execute(
                "INSERT INTO settings (guild_id, key, value) VALUES (?,?,?) "
                "ON CONFLICT(guild_id, key) DO UPDATE SET value=excluded.value",
                (guild_id, key, value),
            )
            await self.db.commit()

    async def get_standards(self, guild_id: int) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT item, points, description FROM standards WHERE guild_id=? ORDER BY points DESC, item",
            (guild_id,),
        ) as cur:
            rows = await cur.fetchall()
        if rows:
            return rows
        async with self.db_lock:
            await self.db.executemany(
                "INSERT OR IGNORE INTO standards (guild_id, item, points, description) VALUES (?,?,?,?)",
                [(guild_id, i, p, d) for i, p, d in DEFAULT_STANDARDS],
            )
            await self.db.commit()
        return await self.get_standards(guild_id)

    async def get_ranks(self, guild_id: int) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT rank_order, name, role_id, required_points, requirement, auto_promote "
            "FROM ranks WHERE guild_id=? ORDER BY rank_order",
            (guild_id,),
        ) as cur:
            rows = await cur.fetchall()
        if rows:
            return rows
        async with self.db_lock:
            await self.db.executemany(
                "INSERT OR IGNORE INTO ranks "
                "(guild_id, rank_order, name, role_id, required_points, requirement, auto_promote) "
                "VALUES (?,?,?,?,?,?,?)",
                [(guild_id, o, n, r_id, p, c, a) for o, n, r_id, p, c, a in DEFAULT_RANKS],
            )
            await self.db.commit()
        return await self.get_ranks(guild_id)

    async def award_points(
        self, guild_id: int, user: discord.abc.User, points: int, reason: str, message_id: int | None
    ) -> int | None:
        async with self.db_lock:
            try:
                await self.db.execute(
                    "INSERT INTO activity_log (guild_id, user_id, username, points, reason, message_id, created_at) "
                    "VALUES (?,?,?,?,?,?,?)",
                    (guild_id, user.id, str(user), points, reason, message_id, int(time.time())),
                )
            except aiosqlite.IntegrityError:
                return None
            await self.db.execute(
                "INSERT INTO user_points (guild_id, user_id, total_points) VALUES (?,?,?) "
                "ON CONFLICT(guild_id, user_id) DO UPDATE SET total_points = total_points + excluded.total_points",
                (guild_id, user.id, points),
            )
            await self.db.commit()
        return await self.get_total(guild_id, user.id)

    async def get_total(self, guild_id: int, user_id: int) -> int:
        async with self.db.execute(
            "SELECT total_points FROM user_points WHERE guild_id=? AND user_id=?", (guild_id, user_id)
        ) as cur:
            row = await cur.fetchone()
        return row["total_points"] if row else 0

    async def get_rank(self, guild_id: int, total: int) -> int:
        async with self.db.execute(
            "SELECT COUNT(*) + 1 AS r FROM user_points WHERE guild_id=? AND total_points > ?",
            (guild_id, total),
        ) as cur:
            row = await cur.fetchone()
        return row["r"]

    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot or message.guild is None:
            return
        channel_id = await self.get_setting(message.guild.id, "report_channel_id")
        if not channel_id or message.channel.id != int(channel_id):
            return
        try:
            await self.handle_report(message)
        except Exception:
            log.exception("Error while processing report")

    async def handle_report(self, message: discord.Message) -> None:
        content = message.content.strip()
        if len(content) < MIN_REPORT_LENGTH and not message.attachments:
            return

        standards = await self.get_standards(message.guild.id)
        lowered = content.lower()
        matched = [r for r in standards if r["item"] != DEFAULT_ITEM and r["item"].lower() in lowered]

        activity_rule = next((r for r in standards if r["item"].lower() == "activity"), None)
        if activity_rule:
            has_activity_keyword = "activity" in lowered
            has_attachment = len(message.attachments) > 0
            has_time_info = any(t in lowered for t in [":", "시", "분", "time", "am", "pm", "hours", "min"])
            if has_activity_keyword and has_attachment and has_time_info:
                if not any(r["item"].lower() == "activity" for r in matched):
                    matched.append(activity_rule)
            else:
                matched = [r for r in matched if r["item"].lower() != "activity"]

        if matched:
            points = sum(r["points"] for r in matched)
            reason = " + ".join(r["item"] for r in matched)
        else:
            default = next((r for r in standards if r["item"] == DEFAULT_ITEM), None)
            points = default["points"] if default else 0
            reason = DEFAULT_ITEM

        total = await self.award_points(message.guild.id, message.author, points, reason, message.id)
        if total is None:
            return

        try:
            await message.add_reaction("✅")
        except discord.HTTPException:
            pass

        embed = make_embed(
            "✅ Points Awarded",
            f"**+{points} pts** ({reason})\nTotal Activity Points: **{total} pts**",
            COLOR_OK,
        )
        try:
            await message.reply(embed=embed, mention_author=False)
        except discord.HTTPException:
            pass


bot = ActivityBot()


@bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    if isinstance(error, NotLeader):
        msg = "This command can only be used by **Administrators** or **Faction Leaders**."
    elif isinstance(error, app_commands.MissingPermissions):
        msg = "You do not have permission to use this command. (Administrator only)"
    elif isinstance(error, app_commands.NoPrivateMessage):
        msg = "This command can only be used inside a server."
    else:
        log.exception("Command error", exc_info=error)
        msg = "An unknown error occurred."

    embed = error_embed(msg)
    if interaction.response.is_done():
        await interaction.followup.send(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="setup", description="Sets up the report channel and faction leader role. (Admin only)")
@app_commands.describe(report_channel="The channel where reports will be submitted", leader_role="Faction leader role (Optional)")
@app_commands.guild_only()
@app_commands.checks.has_permissions(administrator=True)
async def setup(
    interaction: discord.Interaction,
    report_channel: discord.TextChannel,
    leader_role: discord.Role | None = None,
):
    await bot.set_setting(interaction.guild.id, "report_channel_id", str(report_channel.id))
    desc = f"Report Channel: {report_channel.mention}"
    if leader_role:
        await bot.set_setting(interaction.guild.id, "leader_role_id", str(leader_role.id))
        desc += f"\nFaction Leader Role: {leader_role.mention}"
    await bot.get_standards(interaction.guild.id)
    await bot.get_ranks(interaction.guild.id)
    await interaction.response.send_message(embed=make_embed("⚙️ Setup Completed", desc, COLOR_OK), ephemeral=True)


@bot.tree.command(name="standard", description="Displays current point standards and rank promotion requirements.")
@app_commands.guild_only()
async def standard(interaction: discord.Interaction):
    rows = await bot.get_standards(interaction.guild.id)
    embed = make_embed(
        "📋 Point Standards & Ranks",
        f"Reports matching keywords earn points. Otherwise, **{DEFAULT_ITEM}** applies.",
    )
    for r in rows:
        embed.add_field(
            name=f"{r['item']} — {r['points']} pts",
            value=r["description"] or "No description",
            inline=False,
        )
    
    ranks = await bot.get_ranks(interaction.guild.id)
    rank_lines = [f"`{rk['rank_order']}.` **{rk['name']}** — Required: {rk['required_points']} pts" for rk in ranks]
    embed.add_field(
        name="🎖️ Promotion Ranks (Private to Lieutenant Colonel)",
        value=("\n".join(rank_lines))[:1024] if rank_lines else "No ranks registered.",
        inline=False,
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="activity_log", description="Checks your personal activity history and total points.")
@app_commands.guild_only()
async def activity_log(interaction: discord.Interaction):
    gid, uid = interaction.guild.id, interaction.user.id
    total = await bot.get_total(gid, uid)
    rank = await bot.get_rank(gid, total)
    async with bot.db.execute(
        "SELECT points, reason, created_at FROM activity_log "
        "WHERE guild_id=? AND user_id=? ORDER BY created_at DESC, id DESC LIMIT 10",
        (gid, uid),
    ) as cur:
        rows = await cur.fetchall()

    embed = make_embed(f"📝 {interaction.user.display_name}'s Activity Log")
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    embed.add_field(name="Total Activity Points", value=f"**{total} pts**", inline=True)
    embed.add_field(name="Server Rank", value=f"**{rank}th**" if total else "No records", inline=True)
    
    if rows:
        lines = [f"<t:{r['created_at']}:f> · **+{r['points']} pts** · {r['reason']}" for r in rows]
        embed.add_field(name="Recent History (Up to 10)", value="\n".join(lines), inline=False)
    else:
        embed.add_field(name="Recent History", value="No activity history yet.", inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="total_activity_points", description="Checks total activity points. (Admin/Faction Leader only)")
@app_commands.describe(user="Select a specific user to view (Leave empty for global leaderboard)")
@app_commands.guild_only()
@is_admin_or_leader()
async def total_activity_points(interaction: discord.Interaction, user: discord.Member | None = None):
    gid = interaction.guild.id

    if user:
        total = await bot.get_total(gid, user.id)
        rank = await bot.get_rank(gid, total)
        embed = make_embed(f"📊 {user.display_name}'s Total Points")
        embed.set_thumbnail(url=user.display_avatar.url)
        embed.add_field(name="Total Activity Points", value=f"**{total} pts**", inline=True)
        embed.add_field(name="Rank", value=f"**{rank}th**" if total else "No records", inline=True)
        return await interaction.response.send_message(embed=embed, ephemeral=True)

    async with bot.db.execute(
        "SELECT user_id, total_points FROM user_points WHERE guild_id=? "
        "ORDER BY total_points DESC LIMIT 25",
        (gid,),
    ) as cur:
        rows = await cur.fetchall()
    if not rows:
        return await interaction.response.send_message(
            embed=make_embed("📊 Total Activity Points", "No points have been awarded yet."), ephemeral=True
        )

    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    lines = [
        f"{medals.get(i, f'`{i}.`')} <@{r['user_id']}> — **{r['total_points']} pts**"
        for i, r in enumerate(rows, start=1)
    ]
    embed = make_embed("📊 Activity Points Leaderboard (TOP 25)", "\n".join(lines))
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="manage_points", description="Adds or removes points for a specific user. (Admin/Leader only)")
@app_commands.describe(
    user="The target user",
    action="Choose whether to add or remove points",
    points="Amount of points",
    reason="Reason for point adjustment"
)
@app_commands.choices(action=[
    app_commands.Choice(name="Add", value="add"),
    app_commands.Choice(name="Remove", value="remove")
])
@app_commands.guild_only()
@is_admin_or_leader()
async def manage_points(
    interaction: discord.Interaction,
    user: discord.Member,
    action: str,
    points: app_commands.Range[int, 1, 100000],
    reason: str = "Admin adjustment"
):
    gid = interaction.guild.id
    actual_points = points if action == "add" else -points
    reason_text = f"Admin Action ({action.upper()}): {reason}"

    total = await bot.award_points(gid, user, actual_points, reason_text, message_id=None)
    if total is None:
        total = await bot.get_total(gid, user.id)

    action_label = "Added" if action == "add" else "Removed"
    embed = make_embed(
        "⚙️ Point Management",
        f"Successfully **{action_label}** **{points} pts** for {user.mention}.\nReason: {reason}\nNew Total: **{total} pts**",
        COLOR_OK
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


async def item_autocomplete(interaction: discord.Interaction, current: str):
    rows = await bot.get_standards(interaction.guild.id)
    return [
        app_commands.Choice(name=r["item"], value=r["item"])
        for r in rows
        if current.lower() in r["item"].lower()
    ][:25]


@bot.tree.command(name="change_of_standards", description="Modifies or adds a point earning standard. (Admin/Leader only)")
@app_commands.describe(
    item="Standard item name (Select existing or type new)",
    points="Points to set",
    description="Item description (Optional)",
)
@app_commands.autocomplete(item=item_autocomplete)
@app_commands.guild_only()
@is_admin_or_leader()
async def change_of_standards(
    interaction: discord.Interaction,
    item: app_commands.Range[str, 1, 30],
    points: app_commands.Range[int, 0, 100000],
    description: app_commands.Range[str, 0, 100] | None = None,
):
    gid = interaction.guild.id
    item = item.strip()
    await bot.get_standards(gid)

    async with bot.db.execute(
        "SELECT points, description FROM standards WHERE guild_id=? AND item=?", (gid, item)
    ) as cur:
        old = await cur.fetchone()

    async with bot.db_lock:
        if old:
            await bot.db.execute(
                "UPDATE standards SET points=?, description=? WHERE guild_id=? AND item=?",
                (points, description if description is not None else old["description"], gid, item),
            )
        else:
            await bot.db.execute(
                "INSERT INTO standards (guild_id, item, points, description) VALUES (?,?,?,?)",
                (gid, item, points, description or ""),
            )
        await bot.db.commit()

    if old:
        desc = f"**{item}**: {old['points']} pts → **{points} pts**"
    else:
        desc = f"Added new item **{item}** ({points} pts)."
    await interaction.response.send_message(embed=make_embed("🔧 Standard Updated", desc, COLOR_OK))


@bot.tree.command(name="change_of_ranks", description="Modifies the promotion requirement for a specific rank order. (Admin/Leader only)")
@app_commands.describe(
    rank_order="Rank order number (1 for Private, up to 12 for Lieutenant Colonel)",
    name="Rank name",
    required_points="Points required to achieve this rank"
)
@app_commands.guild_only()
@is_admin_or_leader()
async def change_of_ranks(
    interaction: discord.Interaction,
    rank_order: app_commands.Range[int, 1, 50],
    name: app_commands.Range[str, 1, 30],
    required_points: app_commands.Range[int, 0, 100000],
):
    gid = interaction.guild.id
    async with bot.db_lock:
        await bot.db.execute(
            "INSERT INTO ranks (guild_id, rank_order, name, role_id, required_points, requirement, auto_promote) "
            "VALUES (?,?,?,NULL,?, '', 1) "
            "ON CONFLICT(guild_id, rank_order) DO UPDATE SET "
            "name=excluded.name, required_points=excluded.required_points",
            (gid, rank_order, name.strip(), required_points),
        )
        await bot.db.commit()

    embed = make_embed(
        "🎖️ Rank Standard Updated",
        f"Rank `{rank_order}` **{name.strip()}** — Required Points: **{required_points} pts**",
        COLOR_OK
    )
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="remove_standard", description="Removes a point standard item. (Admin/Leader only)")
@app_commands.describe(item="Item to remove")
@app_commands.guild_only()
@is_admin_or_leader()
async def remove_standard(interaction: discord.Interaction, item: str):
    if item == DEFAULT_ITEM:
        return await interaction.response.send_message(
            embed=error_embed(f"The **{DEFAULT_ITEM}** item cannot be deleted. Please modify its points instead."), ephemeral=True
        )
    async with bot.db_lock:
        cur = await bot.db.execute(
            "DELETE FROM standards WHERE guild_id=? AND item=?", (interaction.guild.id, item)
        )
        await bot.db.commit()
    if cur.rowcount == 0:
        return await interaction.response.send_message(
            embed=error_embed("Item not found."), ephemeral=True
        )
    await interaction.response.send_message(
        embed=make_embed("🗑️ Item Removed", f"Successfully removed **{item}**.", COLOR_OK)
    )


@bot.event
async def on_ready():
    log.info("Logged in as: %s (%s)", bot.user, bot.user.id)


if __name__ == "__main__":
    if not TOKEN:
        raise SystemExit("Please set the DISCORD_TOKEN environment variable (.env).")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    bot.run(TOKEN)