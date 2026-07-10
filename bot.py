import discord
from discord.ext import commands
import json
import os
import asyncio


# Load bot token from Railway Variables
TOKEN = os.getenv("TOKEN")

if TOKEN is None:
    raise ValueError("Missing TOKEN environment variable")


# Channel where messages are forbidden
TARGET_CHANNEL_ID = 1525220657560817766


# Roles that are allowed to bypass the punishment system
STAFF_ROLES = [
    "Moderator",
    "Staff",
    "Admin",
    "Helper"
]


WARNING_FILE = "warnings.json"


# Load saved warnings
def load_warnings():
    if os.path.exists(WARNING_FILE):
        with open(WARNING_FILE, "r") as file:
            return set(json.load(file))

    return set()


# Save warnings
def save_warnings():
    with open(WARNING_FILE, "w") as file:
        json.dump(list(warned_users), file)


warned_users = load_warnings()


# Discord intents
intents = discord.Intents.default()
intents.message_content = True
intents.members = True


bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


@bot.event
async def on_ready():
    print("----------------------------")
    print(f"Logged in as {bot.user}")
    print("Bot is online and running!")
    print("----------------------------")


# Fake ban command
@bot.command()
async def fakeban(ctx, member: discord.Member = None):

    if member is None:
        await ctx.send("❌ Please mention a user to fake ban.")
        return

    processing_msg = await ctx.send(
        f"🔨 Processing ban for {member.mention}..."
    )

    await asyncio.sleep(3)

    await processing_msg.edit(
        content=(
            f"🔨 {member.mention} has been banned!\n"
            "Reason: Breaking the rules."
        )
    )

    await processing_msg.delete(delay=10)

    print(
        f"{ctx.author} used fakeban on {member}"
    )


@bot.event
async def on_message(message):

    # Ignore bots
    if message.author.bot:
        return


    # Ignore staff roles
    if any(role.name in STAFF_ROLES for role in message.author.roles):
        await bot.process_commands(message)
        return


    # Check restricted channel
    if message.channel.id == TARGET_CHANNEL_ID:

        user_id = str(message.author.id)


        # Delete user's message
        try:
            await message.delete()

        except discord.Forbidden:
            print("Cannot delete message. Missing permissions.")


        # First offense
        if user_id not in warned_users:

            warned_users.add(user_id)
            save_warnings()

            warning_msg = await message.channel.send(
                f"{message.author.mention} ⚠️ **Warning**\n"
                "Messages are not allowed in this channel.\n"
                "Your next message here will result in a ban."
            )

            await warning_msg.delete(delay=10)

            return


        # Second offense
        try:
            await message.author.ban(
                reason="Ignored warning and posted again in restricted channel"
            )

            warned_users.discard(user_id)
            save_warnings()

            ban_msg = await message.channel.send(
                f"{message.author.mention} has been banned."
            )

            await ban_msg.delete(delay=10)

            print(
                f"Banned {message.author}"
            )


        except discord.Forbidden:
            print(
                "Cannot ban user. "
                "Check bot permissions and role position."
            )


        except Exception as e:
            print(f"Ban error: {e}")


    # Allows commands like !fakeban to work
    await bot.process_commands(message)


bot.run(TOKEN)
