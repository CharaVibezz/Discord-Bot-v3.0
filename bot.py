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


# where the json files live. point this at a Railway volume's mount
# path (e.g. "/data") via the DATA_DIR env var so warnings, levels,
# and voice sessions survive redeploys. defaults to the working
# directory for local runs where there's no volume attached.
DATA_DIR = os.getenv("DATA_DIR", ".")
os.makedirs(DATA_DIR, exist_ok=True)


# ==========================
# Configuration
# ==========================

# Channel where normal users cannot send messages
TARGET_CHANNEL_ID = 1525220657560817766


# Staff roles that bypass restricted channel punishment
# Replace with your actual role IDs
STAFF_ROLES = [
    1478214213292785825,
    1478212575908073482,
    1478212776588607670,
]


# Roles allowed to use !fakeban
# Replace with your actual role IDs
FAKEBAN_ALLOWED_ROLES = [
    1478214213292785825,
    1478212575908073482,
    1478212776588607670,
    1478127021828604075
]


# Channel where the bot posts a record of every moderation action it takes.
# Replace with your actual log channel ID.
LOG_CHANNEL_ID = 1525220657560817767


WARNING_FILE = os.path.join(DATA_DIR, "warnings.json")

# how long a warning counts against a user before it's stale.
# posting once, waiting 31 days, posting again = two first offenses,
# not a ban.
WARNING_EXPIRY_DAYS = 30


# ==========================
# Leveling System Configuration
# ==========================

LEVELS_FILE = os.path.join(DATA_DIR, "levels.json")

MESSAGE_XP_MIN = 15
MESSAGE_XP_MAX = 25
MESSAGE_XP_COOLDOWN_SECONDS = 60      # per-message xp, once per this window
DAILY_BONUS_XP = 20                   # claimed via /daily, once per calendar day (UTC)

VOICE_XP_PER_MINUTE = 10
VOICE_XP_DIMINISH_AFTER_MINUTES = 60  # full rate up to this point in one session
VOICE_XP_DIMINISHED_RATE = VOICE_XP_PER_MINUTE / 2

# Channel for level-up announcements. Set to None to just post in
# whatever channel triggered the level-up (message xp) or fall back
# to the log channel (voice xp). Replace with a channel ID to pin it.
LEVEL_UP_CHANNEL_ID = None


# ==========================
# Warning System
# ==========================

def load_warnings():

    if os.path.exists(WARNING_FILE):

        with open(WARNING_FILE, "r") as file:
            return json.load(file)

    return {}



def save_warnings():

    with open(WARNING_FILE, "w") as file:
        json.dump(warned_users, file)



warned_users = load_warnings()  # {user_id: iso timestamp of last warning}



# ==========================
# Leveling System
# ==========================
# one xp total per user, fed by two sources - messages and voice time.
# level is derived from total xp on the fly rather than stored
# separately, so there's only ever one number that can drift out of
# sync with itself.

def load_levels() -> dict:

    if os.path.exists(LEVELS_FILE):

        with open(LEVELS_FILE, "r") as file:
            return json.load(file)

    return {}



def save_levels(levels: dict):

    with open(LEVELS_FILE, "w") as file:
        json.dump(levels, file)



def xp_for_level(level: int) -> int:
    # xp required to climb out of `level` into `level + 1`.
    # same shape MEE6 popularized - quadratic growth so early levels
    # come fast and later ones cost real time investment.
    return 5 * (level ** 2) + 50 * level + 100



def get_level_from_xp(total_xp: int):
    # walks the thresholds rather than solving the quadratic directly -
    # levels stay low enough in practice that this is cheap, and it
    # can't drift out of sync with xp_for_level the way a separate
    # closed-form formula could if one gets tweaked and not the other.
    level = 0
    remaining = total_xp

    while remaining >= xp_for_level(level):
        remaining -= xp_for_level(level)
        level += 1

    return level, remaining  # (current level, progress into that level)



def get_or_create_entry(levels: dict, user_id: str) -> dict:
    return levels.setdefault(
        user_id,
        {"xp": 0, "last_message_time": None, "last_daily_bonus": None}
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



# voice sessions persist to disk now instead of living only in memory -
# a restart mid-call just keeps reading the same join timestamp instead
# of losing it. keyed by str(user_id) -> iso timestamp joined, same
# shape as everything else in this file.
VOICE_SESSIONS_FILE = os.path.join(DATA_DIR, "voice_sessions.json")


def load_voice_sessions() -> dict:

    if os.path.exists(VOICE_SESSIONS_FILE):

        with open(VOICE_SESSIONS_FILE, "r") as file:
            return json.load(file)

    return {}



def save_voice_sessions(sessions: dict):

    with open(VOICE_SESSIONS_FILE, "w") as file:
        json.dump(sessions, file)



voice_sessions = load_voice_sessions()



async def announce_level_up(guild: discord.Guild, member: discord.Member, new_level: int, fallback_channel=None):

    channel = None

    if LEVEL_UP_CHANNEL_ID:
        channel = guild.get_channel(LEVEL_UP_CHANNEL_ID)

    if channel is None:
        channel = fallback_channel or guild.get_channel(LOG_CHANNEL_ID)

    if channel is not None:

        embed = discord.Embed(
            description=f"{member.mention} just hit **level {new_level}**.",
            color=discord.Color.gold()
        )
        embed.set_author(
            name=f"{member.display_name} leveled up!",
            icon_url=member.display_avatar.url
        )
        embed.set_thumbnail(url=member.display_avatar.url)

        try:
            await channel.send(embed=embed)
        except (discord.Forbidden, discord.HTTPException):
            pass

    await log_action(
        guild,
        f"⬆️ **{member.mention}** leveled up to **level {new_level}**",
        color=discord.Color.gold()
    )



async def award_message_xp(message: discord.Message):

    if message.guild is None:
        return

    levels = load_levels()
    user_id = str(message.author.id)
    entry = get_or_create_entry(levels, user_id)

    now = discord.utils.utcnow()

    on_cooldown = False
    last_time = entry.get("last_message_time")

    if last_time:
        last_dt = datetime.fromisoformat(last_time)
        on_cooldown = (now - last_dt) < timedelta(seconds=MESSAGE_XP_COOLDOWN_SECONDS)

    gained = 0

    if not on_cooldown:
        gained += random.randint(MESSAGE_XP_MIN, MESSAGE_XP_MAX)
        entry["last_message_time"] = now.isoformat()

    if gained == 0:
        return

    old_level, _ = get_level_from_xp(entry["xp"])
    entry["xp"] += gained
    new_level, _ = get_level_from_xp(entry["xp"])

    save_levels(levels)

    if new_level > old_level:
        await announce_level_up(message.guild, message.author, new_level, message.channel)



# ==========================
# Discord Setup
# ==========================

intents = discord.Intents.default()

intents.message_content = True
intents.members = True


bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


# ==========================
# Shared permission check
# ==========================
# same staff list the fakeban command uses. one source of truth,
# not two lists that drift apart over time.

def has_staff_role(member: discord.Member) -> bool:
    return any(role.id in STAFF_ROLES for role in member.roles)



async def log_action(guild: discord.Guild, description: str, color: discord.Color = discord.Color.blurple()):
    # single place every command/event routes through to write to the
    # log channel. if the channel's missing or the bot can't post there,
    # this fails quietly to the console instead of crashing the command
    # that called it - a broken log channel shouldn't break moderation.

    channel = guild.get_channel(LOG_CHANNEL_ID)

    if channel is None:
        print(f"Log channel {LOG_CHANNEL_ID} not found or not cached.")
        return

    embed = discord.Embed(
        description=description,
        color=color,
        timestamp=discord.utils.utcnow()
    )

    try:
        await channel.send(embed=embed)
    except discord.Forbidden:
        print("Missing permission to send messages in the log channel.")
    except discord.HTTPException as e:
        print(f"Failed to post log message: {e}")



async def reconcile_voice_sessions():
    # runs once at startup. two problems, one pass:
    #
    # 1. someone joined voice while the bot was fully offline - no join
    #    event ever fired for them, so there's no session on disk. give
    #    them one starting now. this undercounts (loses xp for the real
    #    join-to-startup gap) but never overcounts, which is the safe
    #    direction to be wrong in.
    #
    # 2. someone was mid-session, disconnected while the bot was down,
    #    and the leave event never fired to settle their xp. we don't
    #    know when they actually left, so crediting them risks paying
    #    out for time they weren't even connected. drop the stale
    #    session uncredited instead of guessing.

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



# ==========================
# Bot Startup
# ==========================

startup_complete = False


@bot.event
async def on_ready():

    global startup_complete

    print("----------------------------")
    print(f"Logged in as {bot.user}")

    # on_ready fires on every reconnect, not just the first connection.
    # sync and reconcile only need to run once per process - repeating
    # a global command sync on every reconnect risks hitting discord's
    # rate limit right when a flaky connection is already the problem.
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
# Fake Ban Command
# ==========================
# slash version. interaction.response can only fire once, so the
# countdown edits go through edit_original_response instead of
# ctx.send/msg.edit like the prefix version did.

@bot.tree.command(name="fakeban", description="Prank-ban a member (timeout, not a real ban)")
@app_commands.describe(member="The member to fake ban")
@app_commands.guild_only()
async def fakeban(interaction: discord.Interaction, member: discord.Member):

    allowed = any(
        role.id in FAKEBAN_ALLOWED_ROLES
        for role in interaction.user.roles
    )

    if not allowed:
        await interaction.response.send_message(
            "❌ You cannot use this command.", ephemeral=True
        )
        return


    # Prevent fake banning administrators
    if member.guild_permissions.administrator:
        await interaction.response.send_message(
            "❌ You cannot fakeban an administrator.", ephemeral=True
        )
        return


    await interaction.response.send_message(
        f"🔨 Preparing ban for {member.mention}..."
    )


    # Countdown from 5
    for number in range(5, 0, -1):

        await interaction.edit_original_response(
            content=(
                f"🔨 Preparing ban for {member.mention}\n"
                f"Executing in **{number}**..."
            )
        )

        await asyncio.sleep(1)


    # Send fake ban DM
    try:

        await member.send(
            "You've been BANNED!! 🤯🪦 (joke)"
        )

    except discord.Forbidden:

        print(
            f"Could not DM {member}. DMs are closed."
        )

    except discord.HTTPException:

        print(
            "Discord error while sending fakeban DM."
        )


    # Timeout user for 10 seconds
    try:

        await member.timeout(
            timedelta(seconds=10),
            reason="Fake ban prank"
        )

    except discord.Forbidden:

        print(
            "Cannot timeout user. "
            "Check Moderate Members permission and role order."
        )

    except discord.HTTPException:

        print(
            "Discord error while timing out user."
        )


    # Fake ban result
    await interaction.edit_original_response(
        content=(
            f"🔨 {member.mention} has been banned.\n"
            "Reason: Breaking the rules.\n"
        )
    )


    print(
        f"{interaction.user} fake banned {member}"
    )

    await log_action(
        interaction.guild,
        f"🔨 **{interaction.user.mention}** fakebanned **{member.mention}**"
    )



# ==========================
# Voice Lockdown Commands
# ==========================
# lockdown denies @everyone the Connect permission on the target
# voice channel. anyone already inside stays inside - this blocks
# new joins, it doesn't eject people. if you want them out too,
# that's a different command, ask for it separately.

@bot.tree.command(name="lockdown", description="Prevent members from joining a voice channel")
@app_commands.describe(channel="The voice channel to lock")
@app_commands.guild_only()
async def lockdown(interaction: discord.Interaction, channel: discord.VoiceChannel):

    if not has_staff_role(interaction.user):
        await interaction.response.send_message(
            "you don't have permission for that.", ephemeral=True
        )
        return

    overwrite = channel.overwrites_for(interaction.guild.default_role)
    overwrite.connect = False

    try:
        await channel.set_permissions(
            interaction.guild.default_role,
            overwrite=overwrite,
            reason=f"Voice lockdown by {interaction.user}"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "missing permission to edit that channel. check role order and Manage Channels.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(
        f"🔒 {channel.mention} is locked. nobody new gets in until someone runs `/unlock`."
    )

    await log_action(
        interaction.guild,
        f"🔒 **{interaction.user.mention}** locked voice channel **{channel.mention}**",
        color=discord.Color.red()
    )



@bot.tree.command(name="unlock", description="Allow members to join a previously locked voice channel")
@app_commands.describe(channel="The voice channel to unlock")
@app_commands.guild_only()
async def unlock(interaction: discord.Interaction, channel: discord.VoiceChannel):

    if not has_staff_role(interaction.user):
        await interaction.response.send_message(
            "you don't have permission for that.", ephemeral=True
        )
        return

    overwrite = channel.overwrites_for(interaction.guild.default_role)
    overwrite.connect = None  # reset to whatever the category/role defaults are, not force True

    try:
        await channel.set_permissions(
            interaction.guild.default_role,
            overwrite=overwrite,
            reason=f"Voice unlock by {interaction.user}"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "missing permission to edit that channel. check role order and Manage Channels.",
            ephemeral=True
        )
        return

    await interaction.response.send_message(f"🔓 {channel.mention} is unlocked.")

    await log_action(
        interaction.guild,
        f"🔓 **{interaction.user.mention}** unlocked voice channel **{channel.mention}**",
        color=discord.Color.green()
    )



# ==========================
# Move Member Command
# ==========================
# discord.Member and discord.VoiceChannel as parameter types give you
# a proper picker in the slash command UI - a dropdown searched by
# username and channel name. more reliable than parsing raw strings,
# and it's what you actually asked for functionally.

@bot.tree.command(name="move", description="Move a member into a specified voice channel")
@app_commands.describe(member="The member to move", channel="The destination voice channel")
@app_commands.guild_only()
async def move(interaction: discord.Interaction, member: discord.Member, channel: discord.VoiceChannel):

    if not has_staff_role(interaction.user):
        await interaction.response.send_message(
            "you don't have permission for that.", ephemeral=True
        )
        return

    if member.voice is None or member.voice.channel is None:
        await interaction.response.send_message(
            f"{member.display_name} isn't in a voice channel. can't move what isn't there.",
            ephemeral=True
        )
        return

    try:
        await member.move_to(channel, reason=f"Moved by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message(
            "missing permission to move members. check Move Members and role order.",
            ephemeral=True
        )
        return
    except discord.HTTPException as e:
        await interaction.response.send_message(f"discord rejected that: {e}", ephemeral=True)
        return

    await interaction.response.send_message(
        f"moved {member.display_name} to {channel.mention}."
    )

    await log_action(
        interaction.guild,
        f"➡️ **{interaction.user.mention}** moved **{member.mention}** to **{channel.mention}**"
    )



# ==========================
# Mass Move Command
# ==========================
# discord.Member parameters give you a real picker in the slash
# command UI - searched and selected, not typed. that removes the
# whole username-resolution problem: no typos, no duplicate display
# names, no "who did you mean" ambiguity. tradeoff is a hard cap -
# 10 slots here, all but the first optional. discord allows up to
# 25 options per command total, so this isn't pushing any limit.

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
        await interaction.response.send_message(
            "you don't have permission for that.", ephemeral=True
        )
        return

    await interaction.response.defer()

    # dedupe by id in case someone gets picked twice across slots
    candidates = [member1, member2, member3, member4, member5,
                  member6, member7, member8, member9, member10]

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

    await interaction.followup.send("\n".join(lines))

    if moved:
        await log_action(
            interaction.guild,
            f"➡️ **{interaction.user.mention}** mass-moved to **{channel.mention}**: "
            + ", ".join(moved)
        )



# ==========================
# Message Protection System
# ==========================

@bot.event
async def on_message(message):


    # Ignore bots

    if message.author.bot:
        return



    # Message xp applies everywhere, staff included - runs before the
    # restricted-channel checks below so it isn't short-circuited by them

    await award_message_xp(message)



    # Ignore staff

    if any(
        role.id in STAFF_ROLES
        for role in message.author.roles
    ):

        return



    # Only check restricted channel

    if message.channel.id != TARGET_CHANNEL_ID:

        return



    user_id = str(message.author.id)
    now = discord.utils.utcnow()



    # Delete message

    try:

        await message.delete()

    except discord.Forbidden:

        print(
            "Missing Manage Messages permission."
        )



    # Check whether an existing warning is still within the expiry window

    last_warned_str = warned_users.get(user_id)
    warning_expired = True

    if last_warned_str is not None:

        last_warned_dt = datetime.fromisoformat(last_warned_str)
        warning_expired = (now - last_warned_dt) > timedelta(days=WARNING_EXPIRY_DAYS)



    # First offense, or a warning old enough it no longer counts

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
            color=discord.Color.orange()
        )


        return



    # Second offense within the 30-day window

    try:


        await message.author.ban(
            reason="Ignored warning and posted again in restricted channel"
        )


        warned_users.pop(user_id, None)

        save_warnings()



        ban_message = await message.channel.send(
            f"{message.author.mention} has been banned."
        )


        await ban_message.delete(delay=10)



        print(
            f"Banned {message.author}"
        )

        await log_action(
            message.guild,
            f"🔨 banned **{message.author.mention}** for posting again in "
            f"{message.channel.mention} after a warning",
            color=discord.Color.red()
        )



    except discord.Forbidden:

        print(
            "Cannot ban user. "
            "Check Ban Members permission and role order."
        )



    except Exception as e:

        print(
            f"Ban error: {e}"
        )



# ==========================
# Voice XP Tracking
# ==========================
# a session runs from entering a countable channel to leaving one -
# switching between two real voice channels doesn't reset the timer,
# only actually disconnecting (or getting parked in the afk channel)
# does. xp is settled once, when the session ends.

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):

    if member.bot:
        return

    guild = member.guild
    afk_channel = guild.afk_channel

    before_counts = before.channel is not None and before.channel != afk_channel
    after_counts = after.channel is not None and after.channel != afk_channel

    now = discord.utils.utcnow()


    # entered a countable channel from nowhere/afk - start the clock

    if after_counts and not before_counts:
        voice_sessions[str(member.id)] = now.isoformat()
        save_voice_sessions(voice_sessions)
        return


    # left a countable channel (disconnected, or moved into afk) - settle up

    if before_counts and not after_counts:

        started_str = voice_sessions.pop(str(member.id), None)
        save_voice_sessions(voice_sessions)

        if started_str is None:
            return

        started = datetime.fromisoformat(started_str)
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



# ==========================
# Daily, Rank & Leaderboard Commands
# ==========================

def format_progress_bar(progress: int, needed: int, length: int = 12) -> str:

    filled = int(length * progress / needed) if needed else 0
    filled = max(0, min(length, filled))

    return "█" * filled + "░" * (length - filled)



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
            ephemeral=True
        )
        return

    old_level, _ = get_level_from_xp(entry["xp"])
    entry["xp"] += DAILY_BONUS_XP
    entry["last_daily_bonus"] = today
    new_level, _ = get_level_from_xp(entry["xp"])

    save_levels(levels)

    embed = discord.Embed(
        description=f"claimed **+{DAILY_BONUS_XP} xp**\ntotal xp: **{entry['xp']:,}**",
        color=discord.Color.green()
    )
    embed.set_author(
        name=f"{interaction.user.display_name}'s daily bonus",
        icon_url=interaction.user.display_avatar.url
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
        await interaction.response.send_message(
            f"{target.display_name} hasn't earned any xp yet."
        )
        return

    xp = entry["xp"]
    level, progress = get_level_from_xp(xp)
    needed = xp_for_level(level)

    sorted_users = sorted(levels.items(), key=lambda kv: kv[1].get("xp", 0), reverse=True)
    rank_position = next(
        (i for i, (uid, _) in enumerate(sorted_users, start=1) if uid == str(target.id)),
        None
    )

    bar = format_progress_bar(progress, needed)

    embed = discord.Embed(
        color=discord.Color.blurple()
    )
    embed.set_author(
        name=f"{target.display_name}'s rank",
        icon_url=target.display_avatar.url
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Level", value=str(level), inline=True)
    embed.add_field(name="Server Rank", value=f"#{rank_position}", inline=True)
    embed.add_field(name="Total XP", value=f"{xp:,}", inline=True)
    embed.add_field(
        name="Progress to Next Level",
        value=f"{bar}\n{progress:,} / {needed:,} xp",
        inline=False
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

        lines.append(f"{rank_marker}  **{name}**\nLevel {level}  ·  {entry.get('xp', 0):,} xp")

    embed = discord.Embed(
        title="🏆 Leaderboard",
        description="\n\n".join(lines),
        color=discord.Color.gold()
    )
    embed.set_footer(text=f"Top {len(top)} by total XP")

    await interaction.response.send_message(embed=embed)



# ==========================
# Start Bot
# ==========================

bot.run(TOKEN)
