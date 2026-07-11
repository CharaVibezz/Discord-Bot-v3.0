from datetime import timedelta, datetime
import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import asyncio
import random

# ==========================
# Railway Token
# ==========================

TOKEN = os.getenv("TOKEN")
if TOKEN is None:
    raise ValueError("Missing TOKEN environment variable")

DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)

# ==========================
# Configuration
# ==========================

TARGET_CHANNEL_ID = 1525220657560817766

STAFF_ROLES = [
    1478214213292785825,
    1478212575908073482,
    1478212776588607670,
]

FAKEBAN_ALLOWED_ROLES = [
    1478214213292785825,
    1478212575908073482,
    1478212776588607670,
    1478127021828604075,
]

LOG_CHANNEL_ID = 1525220657560817767

WARNING_FILE = os.path.join(DATA_DIR, "warnings.json")
WARNING_EXPIRY_DAYS = 30

LEVELS_FILE = os.path.join(DATA_DIR, "levels.json")

MESSAGE_XP_MIN = 15
MESSAGE_XP_MAX = 25
MESSAGE_XP_COOLDOWN_SECONDS = 60
DAILY_BONUS_XP = 20

VOICE_XP_PER_MINUTE = 10
VOICE_XP_DIMINISH_AFTER_MINUTES = 60
VOICE_XP_DIMINISHED_RATE = VOICE_XP_PER_MINUTE / 2

LEVEL_UP_CHANNEL_ID = 1525392046989246525

# ==========================
# Helpers
# ==========================

def safe_load_json(path: str, default):
    try:
        if os.path.exists(path):
            with open(path, "r") as file:
                return json.load(file)
    except (json.JSONDecodeError, OSError):
        pass
    return default

def safe_save_json(path: str, data):
    with open(path, "w") as file:
        json.dump(data, file)

def load_warnings():
    return safe_load_json(WARNING_FILE, {})

def save_warnings():
    safe_save_json(WARNING_FILE, warned_users)

warned_users = load_warnings()

def load_levels() -> dict:
    return safe_load_json(LEVELS_FILE, {})

def save_levels(levels: dict):
    safe_save_json(LEVELS_FILE, levels)

def xp_for_level(level: int) -> int:
    return 5 * (level ** 2) + 50 * level + 100

def get_level_from_xp(total_xp: int):
    level = 0
    remaining = total_xp
    while True:
        needed = xp_for_level(level)
        if remaining < needed:
            break
        remaining -= needed
        level += 1
    return level, remaining

def get_or_create_entry(levels: dict, user_id: str) -> dict:
    return levels.setdefault(
        user_id,
        {"xp": 0, "last_message_time": None, "last_daily_bonus": None},
    )

def calculate_voice_xp(elapsed_seconds: float) -> int:
    minutes = int(elapsed_seconds // 60)
    if minutes <= 0:
        return 0
    if minutes <= VOICE_XP_DIMINISH_AFTER_MINUTES:
        return int(minutes * VOICE_XP_PER_MINUTE)
    base = VOICE_XP_DIMINISH_AFTER_MINUTES * VOICE_XP_PER_MINUTE
    extra_minutes = minutes - VOICE_XP_DIMINISH_AFTER_MINUTES
    return int(base + extra_minutes * VOICE_XP_DIMINISHED_RATE)

VOICE_SESSIONS_FILE = os.path.join(DATA_DIR, "voice_sessions.json")

def load_voice_sessions() -> dict:
    return safe_load_json(VOICE_SESSIONS_FILE, {})

def save_voice_sessions(sessions: dict):
    safe_save_json(VOICE_SESSIONS_FILE, sessions)

voice_sessions = load_voice_sessions()

def has_staff_role(member: discord.Member) -> bool:
    return any(role.id in STAFF_ROLES for role in member.roles)

async def log_action(
    guild: discord.Guild,
    description: str,
    color: discord.Color = discord.Color.blurple(),
):
    channel = guild.get_channel(LOG_CHANNEL_ID)
    if channel is None:
        print(f"Log channel {LOG_CHANNEL_ID} not found or not cached.")
        return

    embed = discord.Embed(
        description=description,
        color=color,
        timestamp=discord.utils.utcnow(),
    )

    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        print("Missing permission to send messages in the log channel.")
    except discord.HTTPException as e:
        print(f"Failed to post log message: {e}")

async def announce_level_up(
    guild: discord.Guild,
    member: discord.Member,
    new_level: int,
    fallback_channel=None,
):
    channel = guild.get_channel(LEVEL_UP_CHANNEL_ID) if LEVEL_UP_CHANNEL_ID else None
    if channel is None:
        channel = fallback_channel or guild.get_channel(LOG_CHANNEL_ID)

    if channel is not None:
        embed = discord.Embed(
            description=f"{member.mention} just hit **level {new_level}**.",
            color=discord.Color.gold(),
        )
        embed.set_author(
            name=f"{member.display_name} leveled up!",
            icon_url=member.display_avatar.url,
        )
        embed.set_thumbnail(url=member.display_avatar.url)
        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    await log_action(
        guild,
        f"⬆️ **{member.mention}** leveled up to **level {new_level}**",
        color=discord.Color.gold(),
    )

async def award_message_xp(message: discord.Message):
    if message.guild is None or message.author.bot:
        return

    levels = load_levels()
    user_id = str(message.author.id)
    entry = get_or_create_entry(levels, user_id)

    now = discord.utils.utcnow()
    last_time = entry.get("last_message_time")
    on_cooldown = False

    if last_time:
        try:
            last_dt = datetime.fromisoformat(last_time)
            on_cooldown = (now - last_dt) < timedelta(seconds=MESSAGE_XP_COOLDOWN_SECONDS)
        except ValueError:
            on_cooldown = False

    if on_cooldown:
        return

    gained = random.randint(MESSAGE_XP_MIN, MESSAGE_XP_MAX)
    entry["last_message_time"] = now.isoformat()

    old_level, _ = get_level_from_xp(entry["xp"])
    entry["xp"] += gained
    new_level, _ = get_level_from_xp(entry["xp"])
    save_levels(levels)

    if new_level > old_level:
        await announce_level_up(message.guild, message.author, new_level, message.channel)

async def reconcile_voice_sessions():
    now = discord.utils.utcnow()
    active_ids = set()

    for guild in bot.guilds:
        afk_channel = guild.afk_channel

        for channel in guild.voice_channels:
            if channel == afk_channel:
                continue

            for member in channel.members:
                if member.bot:
                    continue
                user_id = str(member.id)
                active_ids.add(user_id)
                if user_id not in voice_sessions:
                    voice_sessions[user_id] = now.isoformat()

    stale = [uid for uid in voice_sessions if uid not in active_ids]
    for user_id in stale:
        del voice_sessions[user_id]

    save_voice_sessions(voice_sessions)
    if stale:
        print(f"Dropped {len(stale)} stale voice session(s) from before restart.")

def format_progress_bar(progress: int, needed: int, length: int = 12) -> str:
    filled = int(length * progress / needed) if needed else 0
    filled = max(0, min(length, filled))
    return "█" * filled + "░" * (length - filled)

# ==========================
# Discord Setup
# ==========================

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

# ==========================
# Startup
# ==========================

startup_complete = False

@bot.event
async def on_ready():
    global startup_complete
    print("----------------------------")
    print(f"Logged in as {bot.user}")

    if not startup_complete:
        try:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} slash command(s).")
        except Exception as e:
            print(f"Slash command sync failed: {e}")

        await reconcile_voice_sessions()
        startup_complete = True

    print("Bot is online and running!")
    print("----------------------------")

# ==========================
# Commands
# ==========================

@bot.tree.command(name="fakeban", description="Prank-ban a member (timeout, not a real ban)")
@app_commands.describe(member="The member to fake ban")
@app_commands.guild_only()
async def fakeban(interaction: discord.Interaction, member: discord.Member):
    allowed = any(role.id in FAKEBAN_ALLOWED_ROLES for role in interaction.user.roles)
    if not allowed:
        await interaction.response.send_message("❌ You cannot use this command.", ephemeral=True)
        return

    if member.guild_permissions.administrator:
        await interaction.response.send_message("❌ You cannot fakeban an administrator.", ephemeral=True)
        return

    await interaction.response.send_message(f"🔨 Preparing ban for {member.mention}...")
    for number in range(5, 0, -1):
        await interaction.edit_original_response(
            content=f"🔨 Preparing ban for {member.mention}\nExecuting in **{number}**..."
        )
        await asyncio.sleep(1)

    try:
        await member.send("You've been BANNED!! 🤯🪦 (joke)")
    except discord.Forbidden:
        print(f"Could not DM {member}. DMs are closed.")
    except discord.HTTPException:
        print("Discord error while sending fakeban DM.")

    try:
        await member.timeout(timedelta(seconds=10), reason="Fake ban prank")
    except discord.Forbidden:
        print("Cannot timeout user. Check Moderate Members permission and role order.")
    except discord.HTTPException:
        print("Discord error while timing out user.")

    await interaction.edit_original_response(
        content=f"🔨 {member.mention} has been banned.\nReason: Breaking the rules."
    )
    await log_action(
        interaction.guild,
        f"🔨 **{interaction.user.mention}** fakebanned **{member.mention}**",
    )

@bot.tree.command(name="lockdown", description="Prevent members from joining a voice channel")
@app_commands.describe(channel="The voice channel to lock")
@app_commands.guild_only()
async def lockdown(interaction: discord.Interaction, channel: discord.VoiceChannel):
    if not has_staff_role(interaction.user):
        await interaction.response.send_message("you don't have permission for that.", ephemeral=True)
        return

    overwrite = channel.overwrites_for(interaction.guild.default_role)
    overwrite.connect = False

    try:
        await channel.set_permissions(
            interaction.guild.default_role,
            overwrite=overwrite,
            reason=f"Voice lockdown by {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "missing permission to edit that channel. check role order and Manage Channels.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"🔒 {channel.mention} is locked. nobody new gets in until someone runs `/unlock`."
    )
    await log_action(
        interaction.guild,
        f"🔒 **{interaction.user.mention}** locked voice channel **{channel.mention}**",
        color=discord.Color.red(),
    )

@bot.tree.command(name="unlock", description="Allow members to join a previously locked voice channel")
@app_commands.describe(channel="The voice channel to unlock")
@app_commands.guild_only()
async def unlock(interaction: discord.Interaction, channel: discord.VoiceChannel):
    if not has_staff_role(interaction.user):
        await interaction.response.send_message("you don't have permission for that.", ephemeral=True)
        return

    overwrite = channel.overwrites_for(interaction.guild.default_role)
    overwrite.connect = None

    try:
        await channel.set_permissions(
            interaction.guild.default_role,
            overwrite=overwrite,
            reason=f"Voice unlock by {interaction.user}",
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "missing permission to edit that channel. check role order and Manage Channels.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(f"🔓 {channel.mention} is unlocked.")
    await log_action(
        interaction.guild,
        f"🔓 **{interaction.user.mention}** unlocked voice channel **{channel.mention}**",
        color=discord.Color.green(),
    )

@bot.tree.command(name="move", description="Move a member into a specified voice channel")
@app_commands.describe(member="The member to move", channel="The destination voice channel")
@app_commands.guild_only()
async def move(interaction: discord.Interaction, member: discord.Member, channel: discord.VoiceChannel):
    if not has_staff_role(interaction.user):
        await interaction.response.send_message("you don't have permission for that.", ephemeral=True)
        return

    if member.voice is None or member.voice.channel is None:
        await interaction.response.send_message(
            f"{member.display_name} isn't in a voice channel. can't move what isn't there.",
            ephemeral=True,
        )
        return

    try:
        await member.move_to(channel, reason=f"Moved by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message(
            "missing permission to move members. check Move Members and role order.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as e:
        await interaction.response.send_message(f"discord rejected that: {e}", ephemeral=True)
        return

    await interaction.response.send_message(f"moved {member.display_name} to {channel.mention}.")
    await log_action(
        interaction.guild,
        f"➡️ **{interaction.user.mention}** moved **{member.mention}** to **{channel.mention}**",
    )

@bot.tree.command(name="mass-move", description="Move multiple members into a specified voice channel")
@app_commands.describe(
    channel="The destination voice channel",
    member1="Member to move",
    member2="Member to move (optional)",
    member3="Member to move (optional)",
    member4="Member to move (optional)",
    member5="Member to move (optional)",
    member6="Member to move (optional)",
    member7="Member to move (optional)",
    member8="Member to move (optional)",
    member9="Member to move (optional)",
    member10="Member to move (optional)",
)
@app_commands.guild_only()
async def mass_move(
    interaction: discord.Interaction,
    channel: discord.VoiceChannel,
    member1: discord.Member,
    member2: discord.Member = None,
    member3: discord.Member = None,
    member4: discord.Member = None,
    member5: discord.Member = None,
    member6: discord.Member = None,
    member7: discord.Member = None,
    member8: discord.Member = None,
    member9: discord.Member = None,
    member10: discord.Member = None,
):
    if not has_staff_role(interaction.user):
        await interaction.response.send_message("you don't have permission for that.", ephemeral=True)
        return

    await interaction.response.defer()

    candidates = [member1, member2, member3, member4, member5, member6, member7, member8, member9, member10]
    seen = set()
    members = []
    for m in candidates:
        if m is not None and m.id not in seen:
            seen.add(m.id)
            members.append(m)

    moved = []
    not_in_voice = []
    failed = []

    for member in members:
        if member.voice is None or member.voice.channel is None:
            not_in_voice.append(member.display_name)
            continue
        try:
            await member.move_to(channel, reason=f"Mass moved by {interaction.user}")
            moved.append(member.display_name)
        except (discord.Forbidden, discord.HTTPException):
            failed.append(member.display_name)

    lines = []
    if moved:
        lines.append(f"✅ moved to {channel.mention}: " + ", ".join(moved))
    if not_in_voice:
        lines.append("⚪ not in a voice channel: " + ", ".join(not_in_voice))
    if failed:
        lines.append("❌ move failed (permissions/role order): " + ", ".join(failed))

    if not lines:
        lines.append("Nothing to do.")

    await interaction.followup.send("\n".join(lines))

    if moved:
        await log_action(
            interaction.guild,
            f"➡️ **{interaction.user.mention}** mass-moved to **{channel.mention}**: " + ", ".join(moved),
        )

@bot.tree.command(name="say", description="Send a message through the bot")
@app_commands.describe(
    message="What the bot should say",
    channel="Channel to post in (defaults to this channel)",
)
@app_commands.guild_only()
async def say(interaction: discord.Interaction, message: str, channel: discord.TextChannel = None):
    if not has_staff_role(interaction.user):
        await interaction.response.send_message("you don't have permission for that.", ephemeral=True)
        return

    target_channel = channel or interaction.channel
    if not isinstance(target_channel, discord.abc.Messageable):
        await interaction.response.send_message("That channel can't receive messages.", ephemeral=True)
        return

    try:
        sent = await target_channel.send(message)
    except discord.Forbidden:
        await interaction.response.send_message("missing permission to send messages there.", ephemeral=True)
        return
    except discord.HTTPException as e:
        await interaction.response.send_message(f"discord rejected that: {e}", ephemeral=True)
        return

    await interaction.response.send_message(
        f"sent to {target_channel.mention}. message id: `{sent.id}`",
        ephemeral=True,
    )
    await log_action(
        interaction.guild,
        f"💬 **{interaction.user.mention}** used /say in {target_channel.mention} — message id `{sent.id}`",
    )

@bot.tree.command(name="edit", description="Edit a previous message sent by the bot")
@app_commands.describe(
    message_id="The ID of the bot's message to edit",
    new_content="The new message content",
    channel="Channel the message is in (defaults to this channel)",
)
@app_commands.guild_only()
async def edit(interaction: discord.Interaction, message_id: str, new_content: str, channel: discord.TextChannel = None):
    if not has_staff_role(interaction.user):
        await interaction.response.send_message("you don't have permission for that.", ephemeral=True)
        return

    target_channel = channel or interaction.channel
    if not isinstance(target_channel, discord.abc.Messageable):
        await interaction.response.send_message("That channel can't be read here.", ephemeral=True)
        return

    try:
        message_id_int = int(message_id)
    except ValueError:
        await interaction.response.send_message("that's not a valid message id.", ephemeral=True)
        return

    try:
        target_message = await target_channel.fetch_message(message_id_int)
    except discord.NotFound:
        await interaction.response.send_message("no message with that id in that channel.", ephemeral=True)
        return
    except discord.Forbidden:
        await interaction.response.send_message(
            "missing permission to read that channel's message history.",
            ephemeral=True,
        )
        return
    except discord.HTTPException as e:
        await interaction.response.send_message(f"discord rejected that: {e}", ephemeral=True)
        return

    if bot.user is None or target_message.author.id != bot.user.id:
        await interaction.response.send_message(
            "that message wasn't sent by this bot. can't edit it.",
            ephemeral=True,
        )
        return

    try:
        await target_message.edit(content=new_content)
    except discord.Forbidden:
        await interaction.response.send_message("missing permission to edit that message.", ephemeral=True)
        return
    except discord.HTTPException as e:
        await interaction.response.send_message(f"discord rejected that: {e}", ephemeral=True)
        return

    await interaction.response.send_message(
        f"edited message `{target_message.id}` in {target_channel.mention}.",
        ephemeral=True,
    )
    await log_action(
        interaction.guild,
        f"✏️ **{interaction.user.mention}** edited bot message `{target_message.id}` in {target_channel.mention}",
    )

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    await award_message_xp(message)

    if any(role.id in STAFF_ROLES for role in message.author.roles):
        return

    if message.channel.id != TARGET_CHANNEL_ID:
        return

    user_id = str(message.author.id)
    now = discord.utils.utcnow()

    try:
        await message.delete()
    except discord.Forbidden:
        print("Missing Manage Messages permission.")

    last_warned_str = warned_users.get(user_id)
    warning_expired = True

    if last_warned_str is not None:
        try:
            last_warned_dt = datetime.fromisoformat(last_warned_str)
            warning_expired = (now - last_warned_dt) > timedelta(days=WARNING_EXPIRY_DAYS)
        except ValueError:
            warning_expired = True

    if last_warned_str is None or warning_expired:
        warned_users[user_id] = now.isoformat()
        save_warnings()

        warning = await message.channel.send(
            f"{message.author.mention} ⚠️ **Warning**\n"
            "Messages are not allowed in this channel.\n"
            "Your next message here will result in a ban."
        )
        await warning.delete(delay=10)

        await log_action(
            message.guild,
            f"⚠️ warned **{message.author.mention}** for posting in {message.channel.mention}",
            color=discord.Color.orange(),
        )
        return

    try:
        await message.author.ban(reason="Ignored warning and posted again in restricted channel")
        warned_users.pop(user_id, None)
        save_warnings()

        ban_message = await message.channel.send(f"{message.author.mention} has been banned.")
        await ban_message.delete(delay=10)

        print(f"Banned {message.author}")

        await log_action(
            message.guild,
            f"🔨 banned **{message.author.mention}** for posting again in {message.channel.mention} after a warning",
            color=discord.Color.red(),
        )
    except discord.Forbidden:
        print("Cannot ban user. Check Ban Members permission and role order.")
    except Exception as e:
        print(f"Ban error: {e}")

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return

    guild = member.guild
    afk_channel = guild.afk_channel

    before_counts = before.channel is not None and before.channel != afk_channel
    after_counts = after.channel is not None and after.channel != afk_channel

    now = discord.utils.utcnow()

    if after_counts and not before_counts:
        voice_sessions[str(member.id)] = now.isoformat()
        save_voice_sessions(voice_sessions)
        return

    if before_counts and not after_counts:
        started_str = voice_sessions.pop(str(member.id), None)
        save_voice_sessions(voice_sessions)

        if started_str is None:
            return

        try:
            started = datetime.fromisoformat(started_str)
        except ValueError:
            return

        elapsed_seconds = (now - started).total_seconds()
        xp_gained = calculate_voice_xp(elapsed_seconds)

        if xp_gained <= 0:
            return

        levels = load_levels()
        user_id = str(member.id)
        entry = get_or_create_entry(levels, user_id)

        old_level, _ = get_level_from_xp(entry["xp"])
        entry["xp"] += xp_gained
        new_level, _ = get_level_from_xp(entry["xp"])
        save_levels(levels)

        if new_level > old_level:
            await announce_level_up(guild, member, new_level)

@bot.tree.command(name="daily", description="Claim your once-a-day XP bonus")
@app_commands.guild_only()
async def daily(interaction: discord.Interaction):
    levels = load_levels()
    user_id = str(interaction.user.id)
    entry = get_or_create_entry(levels, user_id)

    now = discord.utils.utcnow()
    today = now.date().isoformat()

    if entry.get("last_daily_bonus") == today:
        await interaction.response.send_message(
            "already claimed today's bonus. resets at 00:00 UTC.",
            ephemeral=True,
        )
        return

    old_level, _ = get_level_from_xp(entry["xp"])
    entry["xp"] += DAILY_BONUS_XP
    entry["last_daily_bonus"] = today
    new_level, _ = get_level_from_xp(entry["xp"])
    save_levels(levels)

    embed = discord.Embed(
        description=f"claimed **+{DAILY_BONUS_XP} xp**\ntotal xp: **{entry['xp']:,}**",
        color=discord.Color.green(),
    )
    embed.set_author(
        name=f"{interaction.user.display_name}'s daily bonus",
        icon_url=interaction.user.display_avatar.url,
    )

    await interaction.response.send_message(embed=embed)

    if new_level > old_level:
        await announce_level_up(interaction.guild, interaction.user, new_level, interaction.channel)

@bot.tree.command(name="rank", description="Check your level and XP, or someone else's")
@app_commands.describe(member="Member to check (defaults to you)")
@app_commands.guild_only()
async def rank(interaction: discord.Interaction, member: discord.Member = None):
    target = member or interaction.user
    levels = load_levels()
    entry = levels.get(str(target.id))

    if entry is None or entry.get("xp", 0) == 0:
        await interaction.response.send_message(f"{target.display_name} hasn't earned any xp yet.")
        return

    xp = entry["xp"]
    level, progress = get_level_from_xp(xp)
    needed = xp_for_level(level)

    sorted_users = sorted(levels.items(), key=lambda kv: kv[1].get("xp", 0), reverse=True)
    rank_position = next(
        (i for i, (uid, _) in enumerate(sorted_users, start=1) if uid == str(target.id)),
        None,
    )

    bar = format_progress_bar(progress, needed)

    embed = discord.Embed(color=discord.Color.blurple())
    embed.set_author(name=f"{target.display_name}'s rank", icon_url=target.display_avatar.url)
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Level", value=str(level), inline=True)
    embed.add_field(name="Server Rank", value=f"#{rank_position or '?'}", inline=True)
    embed.add_field(name="Total XP", value=f"{xp:,}", inline=True)
    embed.add_field(
        name="Progress to Next Level",
        value=f"{bar}\n{progress:,} / {needed:,} xp",
        inline=False,
    )

    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="Show the top members by XP")
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction):
    levels = load_levels()
    sorted_users = sorted(levels.items(), key=lambda kv: kv[1].get("xp", 0), reverse=True)
    top = sorted_users[:10]

    if not top:
        await interaction.response.send_message("nobody's earned xp yet.")
        return

    lines = []
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}

    for i, (user_id, entry) in enumerate(top, start=1):
        member = interaction.guild.get_member(int(user_id))
        name = member.display_name if member else f"unknown user ({user_id})"
        level, _ = get_level_from_xp(entry.get("xp", 0))
        rank_marker = medals.get(i, f"**{i}.**")
        lines.append(f"{rank_marker} **{name}**\nLevel {level} · {entry.get('xp', 0):,} xp")

    embed = discord.Embed(
        title="🏆 Leaderboard",
        description="\n\n".join(lines),
        color=discord.Color.gold(),
    )
    embed.set_footer(text=f"Top {len(top)} by total XP")

    await interaction.response.send_message(embed=embed)

# ==========================
# Start Bot
# ==========================

bot.run(TOKEN)
