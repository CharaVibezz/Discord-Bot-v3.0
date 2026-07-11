from datetime import timedelta
import discord
from discord import app_commands
from discord.ext import commands
import json
import os
import asyncio


# ==========================
# Railway Token
# ==========================

TOKEN = os.getenv("TOKEN")

if TOKEN is None:
    raise ValueError("Missing TOKEN environment variable")


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


WARNING_FILE = "warnings.json"


# ==========================
# Warning System
# ==========================

def load_warnings():

    if os.path.exists(WARNING_FILE):

        with open(WARNING_FILE, "r") as file:
            return set(json.load(file))

    return set()



def save_warnings():

    with open(WARNING_FILE, "w") as file:
        json.dump(list(warned_users), file)



warned_users = load_warnings()



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



# ==========================
# Bot Startup
# ==========================

@bot.event
async def on_ready():

    print("----------------------------")
    print(f"Logged in as {bot.user}")
    print("Bot is online and running!")

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
    except Exception as e:
        print(f"Slash command sync failed: {e}")

    print("Bot is online and running!")
    print("----------------------------")



# ==========================
# Fake Ban Command
# ==========================

@bot.command()
async def fakeban(ctx, member: discord.Member = None):


    # Check fakeban role permission
    allowed = any(
        role.id in FAKEBAN_ALLOWED_ROLES
        for role in ctx.author.roles
    )


    if not allowed:

        msg = await ctx.send(
            f"{ctx.author.mention} ❌ You cannot use this command."
        )

        await msg.delete(delay=5)

        return



    if member is None:

        msg = await ctx.send(
            "❌ Please mention someone to fake ban."
        )

        await msg.delete(delay=5)

        return



    # Prevent fake banning administrators
    if member.guild_permissions.administrator:

        msg = await ctx.send(
            "❌ You cannot fakeban an administrator."
        )

        await msg.delete(delay=5)

        return



    countdown = await ctx.send(
        f"🔨 Preparing ban for {member.mention}..."
    )



    # Countdown from 5
    for number in range(5, 0, -1):

        await countdown.edit(
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
    await countdown.edit(
        content=(
            f"🔨 {member.mention} has been banned.\n"
            "Reason: Breaking the rules.\n"
        )
    )


    await countdown.delete(delay=10)


    print(
        f"{ctx.author} fake banned {member}"
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



@bot.tree.command(name="unlock", description="Allow members to join a previously locked voice channel")
@app_commands.describe(channel="The voice channel to unlock")
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



# ==========================
# Move Member Command
# ==========================
# discord.Member and discord.VoiceChannel as parameter types give you
# a proper picker in the slash command UI - a dropdown searched by
# username and channel name. more reliable than parsing raw strings,
# and it's what you actually asked for functionally.

@bot.tree.command(name="move", description="Move a member into a specified voice channel")
@app_commands.describe(member="The member to move", channel="The destination voice channel")
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



# ==========================
# Message Protection System
# ==========================

@bot.event
async def on_message(message):


    # Ignore bots

    if message.author.bot:
        return



    # Process commands first

    await bot.process_commands(message)



    # Prevent commands triggering channel punishment

    if message.content.startswith("!"):
        return



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



    # Delete message

    try:

        await message.delete()

    except discord.Forbidden:

        print(
            "Missing Manage Messages permission."
        )



    # First offense

    if user_id not in warned_users:


        warned_users.add(user_id)

        save_warnings()


        warning = await message.channel.send(
            f"{message.author.mention} ⚠️ **Warning**\n"
            "Messages are not allowed in this channel.\n"
            "Your next message here will result in a ban."
        )


        await warning.delete(delay=10)


        return



    # Second offense

    try:


        await message.author.ban(
            reason="Ignored warning and posted again in restricted channel"
        )


        warned_users.discard(user_id)

        save_warnings()



        ban_message = await message.channel.send(
            f"{message.author.mention} has been banned."
        )


        await ban_message.delete(delay=10)



        print(
            f"Banned {message.author}"
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
# Start Bot
# ==========================

bot.run(TOKEN)
