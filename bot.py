import discord
from discord.ext import commands
import json
import os


# Load bot token from Railway Variables
TOKEN = os.getenv("TOKEN")

if TOKEN is None:
    raise ValueError("Missing TOKEN environment variable")


# Channel where messages are forbidden
TARGET_CHANNEL_ID = 1525220657560817766


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


@bot.event
async def on_message(message):

    # Ignore bots
    if message.author.bot:
        return


    # Check restricted channel
    if message.channel.id == TARGET_CHANNEL_ID:

        user_id = str(message.author.id)


        # Delete the user's message
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

            # Delete bot warning after 10 seconds
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

            # Delete bot ban message after 10 seconds
            await ban_msg.delete(delay=10)

            print(
                f"Banned {message.author}"
            )


        except discord.Forbidden:
            print(
                "Cannot ban user. "
                "Check that the bot has Ban Members permission "
                "and its role is above the user."
            )


        except Exception as e:
            print(f"Ban error: {e}")


    await bot.process_commands(message)


bot.run(TOKEN)