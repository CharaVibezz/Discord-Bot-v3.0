import discord
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


# Staff roles that bypass the restricted channel punishment
# Use role names
STAFF_ROLES = [
    "------The CEO's------",
    "-------The Big 3-------",
    "---------Staff---------",
    "------Bots/Apps------"
]


# Roles allowed to use !fakeban
# Use role IDs
FAKEBAN_ALLOWED_ROLES = [
    1478214213292785825,
    1478212575908073482,
    1478212776588607670,
    1478127021828604075,
]


# Warning storage
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
# Bot Startup
# ==========================

@bot.event
async def on_ready():

    print("----------------------------")
    print(f"Logged in as {bot.user}")
    print("Bot is online and running!")
    print("----------------------------")



# ==========================
# Fake Ban Command
# ==========================

@bot.command()
async def fakeban(ctx, member: discord.Member = None):


    # Check role permission

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



    countdown = await ctx.send(
        f"🔨 Preparing ban for {member.mention}..."
    )


    for number in range(5, 0, -1):

        await countdown.edit(
            content=(
                f"🔨 Preparing ban for {member.mention}\n"
                f"Executing in **{number}**..."
            )
        )

        await asyncio.sleep(1)



    await countdown.edit(
        content=(
            f"🔨 {member.mention} has been banned!\n"
            "Reason: Breaking the rules."
        )
    )


    await countdown.delete(delay=10)


    print(
        f"{ctx.author} fake banned {member}"
    )



# ==========================
# Message Protection System
# ==========================

@bot.event
async def on_message(message):


    # Ignore bots

    if message.author.bot:
        return



    # Allow commands to work

    await bot.process_commands(message)



    # Ignore staff

    if any(
        role.name in STAFF_ROLES
        for role in message.author.roles
    ):

        return



    # Check restricted channel

    if message.channel.id != TARGET_CHANNEL_ID:

        return



    user_id = str(message.author.id)



    # Delete user message

    try:

        await message.delete()

    except discord.Forbidden:

        print(
            "Missing Manage Messages permission"
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
            "Check bot role position and permissions."
        )



    except Exception as e:

        print(
            f"Ban error: {e}"
        )



# ==========================
# Start Bot
# ==========================

bot.run(TOKEN)
