import asyncio
import json
import os
import random
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, Dict, List, Tuple, Any
from pathlib import Path

import discord
from discord import app_commands
from discord.ext import commands, tasks

# ==========================
# Configuration
# ==========================

class Config:
    """Centralized configuration with validation"""
    
    # Bot Token
    TOKEN = os.getenv("TOKEN")
    if not TOKEN:
        raise ValueError("Missing TOKEN environment variable")
    
    # Data Directory
    DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
    DATA_DIR.mkdir(exist_ok=True)
    
    # Channel IDs - UPDATE THESE WITH CORRECT IDs!
    TARGET_CHANNEL_ID = 1525220657560817766
    LOG_CHANNEL_ID = 1525377874868305940  # ← THIS IS THE PROBLEM
    LEVEL_UP_CHANNEL_ID = 1525392046989246525
    
    # Role IDs
    STAFF_ROLES = {
        1478214213292785825,
        1478212575908073482,
        1478212776588607670,
    }
    
    FAKEBAN_ALLOWED_ROLES = STAFF_ROLES | {1478127021828604075}

    # Separate, deliberately not reusing FAKEBAN_ALLOWED_ROLES — this command has a
    # bigger blast radius than a prank timeout. Left empty on purpose: an empty
    # whitelist means the command is inert until someone explicitly staffs it.
    SELFDESTRUCT_ALLOWED_ROLES = {
        1525540205761794213
        # add the specific role id(s) you trust with this
    }
    
    # XP Settings
    MESSAGE_XP_MIN = 15
    MESSAGE_XP_MAX = 25
    MESSAGE_XP_COOLDOWN_SECONDS = 60
    DAILY_BONUS_XP = 20
    
    VOICE_XP_PER_MINUTE = 10
    VOICE_XP_DIMINISH_AFTER_MINUTES = 60
    VOICE_XP_DIMINISHED_RATE = VOICE_XP_PER_MINUTE / 2
    
    # Warning Settings
    WARNING_EXPIRY_DAYS = 30
    
    # File paths
    WARNING_FILE = DATA_DIR / "warnings.json"
    LEVELS_FILE = DATA_DIR / "levels.json"
    VOICE_SESSIONS_FILE = DATA_DIR / "voice_sessions.json"
    
    # Logging
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    
    @classmethod
    def validate(cls, guild: discord.Guild) -> List[str]:
        """Validate all configuration settings"""
        errors = []
        
        # Check channels
        log_channel = guild.get_channel(cls.LOG_CHANNEL_ID)
        if not log_channel:
            errors.append(f"Log channel {cls.LOG_CHANNEL_ID} not found")
        
        target_channel = guild.get_channel(cls.TARGET_CHANNEL_ID)
        if not target_channel:
            errors.append(f"Target channel {cls.TARGET_CHANNEL_ID} not found")
        
        level_channel = guild.get_channel(cls.LEVEL_UP_CHANNEL_ID)
        if not level_channel:
            errors.append(f"Level up channel {cls.LEVEL_UP_CHANNEL_ID} not found")
        
        # Check roles
        for role_id in cls.STAFF_ROLES:
            if not guild.get_role(role_id):
                errors.append(f"Staff role {role_id} not found")
        
        return errors

# ==========================
# Logging Setup
# ==========================

logging.basicConfig(
    level=getattr(logging, Config.LOG_LEVEL),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(Config.DATA_DIR / "bot.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# ==========================
# Data Manager
# ==========================

class DataManager:
    """Thread-safe data management with automatic backups"""
    
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self._cache: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
    
    async def load(self, filename: str, default: Any = None) -> Any:
        """Load data from file with caching"""
        async with self._lock:
            if filename in self._cache:
                return self._cache[filename]
            
            filepath = self.data_dir / filename
            try:
                with open(filepath, 'r') as f:
                    data = json.load(f)
            except (FileNotFoundError, json.JSONDecodeError):
                data = default or {}
                if filepath.exists():
                    # Create backup of corrupted file
                    backup = filepath.with_suffix('.json.bak')
                    filepath.rename(backup)
                    logger.warning(f"Corrupted {filename} backed up to {backup}")
                self._save_sync(filename, data)
            
            self._cache[filename] = data
            return data
    
    async def save(self, filename: str, data: Any):
        """Save data to file and update cache"""
        async with self._lock:
            self._cache[filename] = data
            self._save_sync(filename, data)
    
    def _save_sync(self, filename: str, data: Any):
        """Synchronous save with atomic write"""
        filepath = self.data_dir / filename
        temp_path = filepath.with_suffix('.tmp')
        
        try:
            with open(temp_path, 'w') as f:
                json.dump(data, f, indent=2)
            temp_path.rename(filepath)
        except Exception as e:
            logger.error(f"Failed to save {filename}: {e}")
            raise
    
    async def cleanup_cache(self):
        """Clear cache to free memory"""
        async with self._lock:
            self._cache.clear()

# ==========================
# XP System
# ==========================

class XPSystem:
    """Enhanced XP management system"""
    
    def __init__(self, data_manager: DataManager):
        self.data_manager = data_manager
    
    @staticmethod
    def xp_for_level(level: int) -> int:
        """Calculate XP needed for a level"""
        return 5 * (level ** 2) + 50 * level + 100
    
    @staticmethod
    def get_level_from_xp(total_xp: int) -> Tuple[int, int]:
        """Calculate level and progress from total XP"""
        level = 0
        remaining = total_xp
        while True:
            needed = XPSystem.xp_for_level(level)
            if remaining < needed:
                break
            remaining -= needed
            level += 1
        return level, remaining
    
    @staticmethod
    def calculate_voice_xp(elapsed_seconds: float) -> int:
        """Calculate XP from voice session"""
        minutes = int(elapsed_seconds // 60)
        if minutes <= 0:
            return 0
        
        if minutes <= Config.VOICE_XP_DIMINISH_AFTER_MINUTES:
            return int(minutes * Config.VOICE_XP_PER_MINUTE)
        
        base = Config.VOICE_XP_DIMINISH_AFTER_MINUTES * Config.VOICE_XP_PER_MINUTE
        extra = minutes - Config.VOICE_XP_DIMINISH_AFTER_MINUTES
        return int(base + extra * Config.VOICE_XP_DIMINISHED_RATE)
    
    async def award_message_xp(self, message: discord.Message) -> bool:
        """Award XP for a message. Returns True if XP was awarded."""
        if not message.guild or message.author.bot:
            return False
        
        levels = await self.data_manager.load("levels.json", {})
        user_id = str(message.author.id)
        
        # Initialize user data
        if user_id not in levels:
            levels[user_id] = {"xp": 0, "last_message_time": None}
        
        entry = levels[user_id]
        now = datetime.now(timezone.utc)
        
        # Check cooldown
        if entry.get("last_message_time"):
            try:
                last_time = datetime.fromisoformat(entry["last_message_time"])
                if (now - last_time).total_seconds() < Config.MESSAGE_XP_COOLDOWN_SECONDS:
                    return False
            except ValueError:
                pass
        
        # Award XP
        gained = random.randint(Config.MESSAGE_XP_MIN, Config.MESSAGE_XP_MAX)
        entry["last_message_time"] = now.isoformat()
        
        old_level, _ = self.get_level_from_xp(entry["xp"])
        entry["xp"] += gained
        new_level, _ = self.get_level_from_xp(entry["xp"])
        
        await self.data_manager.save("levels.json", levels)
        
        if new_level > old_level:
            return True  # Leveled up
        
        return False
    
    async def add_voice_xp(self, member: discord.Member, elapsed_seconds: float) -> bool:
        """Add XP from voice session. Returns True if leveled up."""
        xp_gained = self.calculate_voice_xp(elapsed_seconds)
        if xp_gained <= 0:
            return False
        
        levels = await self.data_manager.load("levels.json", {})
        user_id = str(member.id)
        
        if user_id not in levels:
            levels[user_id] = {"xp": 0, "last_message_time": None}
        
        entry = levels[user_id]
        old_level, _ = self.get_level_from_xp(entry["xp"])
        entry["xp"] += xp_gained
        new_level, _ = self.get_level_from_xp(entry["xp"])
        
        await self.data_manager.save("levels.json", levels)
        
        return new_level > old_level
    
    async def get_rank(self, user_id: str) -> Tuple[int, int, int]:
        """Get user's rank, level, and total XP"""
        levels = await self.data_manager.load("levels.json", {})
        entry = levels.get(user_id, {"xp": 0})
        xp = entry["xp"]
        level, _ = self.get_level_from_xp(xp)
        
        # Calculate rank
        sorted_users = sorted(
            levels.items(),
            key=lambda kv: kv[1].get("xp", 0),
            reverse=True
        )
        rank = next(
            (i for i, (uid, _) in enumerate(sorted_users, 1) if uid == user_id),
            None
        )
        
        return rank, level, xp

# ==========================
# Logging Service
# ==========================

class LoggingService:
    """Centralized logging with retry mechanism"""
    
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._retry_count = 3
        self._retry_delay = 1
    
    async def log_action(
        self,
        guild: discord.Guild,
        description: str,
        color: discord.Color = discord.Color.blurple(),
        embed_kwargs: Optional[Dict] = None
    ):
        """Log an action with retry mechanism"""
        channel = guild.get_channel(Config.LOG_CHANNEL_ID)
        if not channel:
            logger.error(f"Log channel {Config.LOG_CHANNEL_ID} not found")
            return
        
        embed_kwargs = embed_kwargs or {}
        embed = discord.Embed(
            description=description,
            color=color,
            timestamp=datetime.now(timezone.utc),
            **embed_kwargs
        )
        
        for attempt in range(self._retry_count):
            try:
                await channel.send(embed=embed)
                return
            except (discord.Forbidden, discord.HTTPException) as e:
                if attempt == self._retry_count - 1:
                    logger.error(f"Failed to log action after {self._retry_count} attempts: {e}")
                else:
                    await asyncio.sleep(self._retry_delay * (attempt + 1))
    
    async def announce_level_up(
        self,
        guild: discord.Guild,
        member: discord.Member,
        new_level: int,
        fallback_channel: discord.TextChannel = None
    ):
        """Announce a level up"""
        # Try level-up channel first
        channel = guild.get_channel(Config.LEVEL_UP_CHANNEL_ID)
        if not channel:
            channel = fallback_channel or guild.get_channel(Config.LOG_CHANNEL_ID)
        
        if channel:
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
            except (discord.Forbidden, discord.HTTPException) as e:
                logger.warning(f"Failed to send level-up announcement: {e}")
        
        # Log it
        await self.log_action(
            guild,
            f"⬆️ **{member.mention}** leveled up to **level {new_level}**",
            discord.Color.gold()
        )

# ==========================
# Permission Service
# ==========================

class PermissionService:
    """Centralized permission checking"""
    
    @staticmethod
    def has_staff_role(member: discord.Member) -> bool:
        """Check if member has any staff role"""
        return any(role.id in Config.STAFF_ROLES for role in member.roles)
    
    @staticmethod
    def can_use_fakeban(member: discord.Member) -> bool:
        """Check if member can use fakeban"""
        return any(role.id in Config.FAKEBAN_ALLOWED_ROLES for role in member.roles)

    @staticmethod
    def can_use_selfdestruct(member: discord.Member) -> bool:
        """Check if member can trigger self-destruct"""
        return any(role.id in Config.SELFDESTRUCT_ALLOWED_ROLES for role in member.roles)
    
    @staticmethod
    def is_admin(member: discord.Member) -> bool:
        """Check if member is administrator"""
        return member.guild_permissions.administrator
    
    @staticmethod
    async def require_staff(interaction: discord.Interaction) -> bool:
        """Check staff permission and respond if denied"""
        if PermissionService.has_staff_role(interaction.user):
            return True
        
        await interaction.response.send_message(
            "❌ You don't have permission for that.",
            ephemeral=True
        )
        return False

# ==========================
# Discord Bot
# ==========================

class ModBot(commands.Bot):
    """Main bot class with enhanced features"""
    
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        
        super().__init__(
            command_prefix="!",
            intents=intents,
            help_command=None  # Custom help command
        )
        
        self.data_manager = DataManager(Config.DATA_DIR)
        self.xp_system = XPSystem(self.data_manager)
        self.logging_service = LoggingService(self)
        self.permission_service = PermissionService()
        
        # Voice sessions cache
        self.voice_sessions: Dict[str, datetime] = {}
        
        # Note: Background tasks are started in setup_hook
    
    async def setup_hook(self):
        """Setup hook - runs before bot starts"""
        # Start background tasks (event loop is running)
        self.voice_cleanup.start()
        
        # Sync slash commands
        try:
            await self.tree.sync()
            logger.info("Slash commands synced")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")
    
    async def on_ready(self):
        """Called when bot is ready"""
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        
        # Validate configuration
        for guild in self.guilds:
            errors = Config.validate(guild)
            if errors:
                logger.warning(f"Configuration errors in {guild.name}:")
                for error in errors:
                    logger.warning(f"  - {error}")
        
        # Load voice sessions
        sessions = await self.data_manager.load("voice_sessions.json", {})
        for user_id, timestamp in sessions.items():
            try:
                self.voice_sessions[user_id] = datetime.fromisoformat(timestamp)
            except ValueError:
                logger.warning(f"Invalid timestamp for voice session {user_id}")
        
        logger.info(f"Bot is online in {len(self.guilds)} guilds")
    
    def format_progress_bar(self, progress: int, needed: int, length: int = 12) -> str:
        """Format a progress bar"""
        if needed <= 0:
            return "░" * length
        
        filled = min(length, int(length * progress / needed))
        return "█" * filled + "░" * (length - filled)
    
    # ==========================
    # Voice Cleanup Task
    # ==========================
    
    @tasks.loop(minutes=5)
    async def voice_cleanup(self):
        """Clean up stale voice sessions"""
        try:
            # Ensure bot is ready before running
            await self.wait_until_ready()
            
            now = datetime.now(timezone.utc)
            active_users = set()
            
            for guild in self.guilds:
                afk_channel = guild.afk_channel
                for voice_channel in guild.voice_channels:
                    if voice_channel == afk_channel:
                        continue
                    for member in voice_channel.members:
                        if not member.bot:
                            active_users.add(str(member.id))
            
            # Remove stale sessions
            stale_users = [uid for uid in self.voice_sessions if uid not in active_users]
            for user_id in stale_users:
                del self.voice_sessions[user_id]
            
            if stale_users:
                logger.info(f"Cleaned up {len(stale_users)} stale voice sessions")
                await self.data_manager.save("voice_sessions.json", {
                    uid: dt.isoformat() for uid, dt in self.voice_sessions.items()
                })
                
        except Exception as e:
            logger.error(f"Voice cleanup failed: {e}")
    
    @voice_cleanup.before_loop
    async def before_voice_cleanup(self):
        """Wait for bot to be ready before starting the loop"""
        await self.wait_until_ready()

# ==========================
# Bot Instance
# ==========================

bot = ModBot()

# ==========================
# Event Handlers
# ==========================

@bot.event
async def on_message(message: discord.Message):
    """Handle message events"""
    if message.author.bot or not message.guild:
        return
    
    # Process XP
    leveled_up = await bot.xp_system.award_message_xp(message)
    if leveled_up:
        _, new_level, _ = await bot.xp_system.get_rank(str(message.author.id))
        await bot.logging_service.announce_level_up(
            message.guild,
            message.author,
            new_level,
            message.channel
        )
    
    # Check staff first
    if bot.permission_service.has_staff_role(message.author):
        return
    
    # Only process target channel
    if message.channel.id != Config.TARGET_CHANNEL_ID:
        return
    
    # Handle forbidden messages
    await handle_forbidden_message(message)

async def handle_forbidden_message(message: discord.Message):
    """Handle messages in restricted channel"""
    user_id = str(message.author.id)
    now = datetime.now(timezone.utc)
    
    # Delete the message
    try:
        await message.delete()
    except discord.Forbidden:
        logger.warning(f"Missing Manage Messages permission in {message.channel}")
        return
    
    # Check warning status
    warnings = await bot.data_manager.load("warnings.json", {})
    last_warning = warnings.get(user_id)
    
    # Check if warning has expired
    warning_valid = False
    if last_warning:
        try:
            last_time = datetime.fromisoformat(last_warning)
            warning_valid = (now - last_time).total_seconds() < (Config.WARNING_EXPIRY_DAYS * 86400)
        except ValueError:
            pass
    
    if not warning_valid:
        # First warning
        warnings[user_id] = now.isoformat()
        await bot.data_manager.save("warnings.json", warnings)
        
        warning_msg = await message.channel.send(
            f"{message.author.mention} ⚠️ **Warning**\n"
            "Messages are not allowed in this channel.\n"
            "Your next message here will result in a ban."
        )
        await warning_msg.delete(delay=10)
        
        await bot.logging_service.log_action(
            message.guild,
            f"⚠️ Warned **{message.author.mention}** for posting in {message.channel.mention}",
            discord.Color.orange()
        )
        return
    
    # Ban the user
    try:
        await message.author.ban(reason="Ignored warning in restricted channel")
        warnings.pop(user_id, None)
        await bot.data_manager.save("warnings.json", warnings)
        
        ban_msg = await message.channel.send(f"🔨 {message.author.mention} has been banned.")
        await ban_msg.delete(delay=10)
        
        logger.info(f"Banned {message.author} from {message.guild}")
        await bot.logging_service.log_action(
            message.guild,
            f"🔨 Banned **{message.author.mention}** for posting in {message.channel.mention} after warning",
            discord.Color.red()
        )
        
    except discord.Forbidden:
        logger.warning(f"Cannot ban {message.author} - missing permissions")
    except Exception as e:
        logger.error(f"Ban error: {e}")

@bot.event
async def on_voice_state_update(
    member: discord.Member,
    before: discord.VoiceState,
    after: discord.VoiceState
):
    """Handle voice state changes for XP"""
    if member.bot:
        return
    
    guild = member.guild
    afk_channel = guild.afk_channel
    
    before_counts = before.channel and before.channel != afk_channel
    after_counts = after.channel and after.channel != afk_channel
    
    now = datetime.now(timezone.utc)
    
    if after_counts and not before_counts:
        # Joined a voice channel
        bot.voice_sessions[str(member.id)] = now
        await bot.data_manager.save("voice_sessions.json", {
            uid: dt.isoformat() for uid, dt in bot.voice_sessions.items()
        })
        
    elif before_counts and not after_counts:
        # Left a voice channel
        user_id = str(member.id)
        if user_id in bot.voice_sessions:
            start_time = bot.voice_sessions.pop(user_id)
            elapsed = (now - start_time).total_seconds()
            
            if elapsed > 60:  # Only award if at least 1 minute
                leveled_up = await bot.xp_system.add_voice_xp(member, elapsed)
                if leveled_up:
                    _, new_level, _ = await bot.xp_system.get_rank(user_id)
                    await bot.logging_service.announce_level_up(guild, member, new_level)
            
            await bot.data_manager.save("voice_sessions.json", {
                uid: dt.isoformat() for uid, dt in bot.voice_sessions.items()
            })

# ==========================
# Slash Commands
# ==========================

@bot.tree.command(name="fakeban", description="Prank-ban a member (timeout, not a real ban)")
@app_commands.describe(member="The member to fake ban")
@app_commands.guild_only()
async def fakeban(interaction: discord.Interaction, member: discord.Member):
    """Fake ban command with animation"""
    # Permission check
    if not bot.permission_service.can_use_fakeban(interaction.user):
        await interaction.response.send_message("❌ You cannot use this command.", ephemeral=True)
        return
    
    if bot.permission_service.is_admin(member):
        await interaction.response.send_message("❌ You cannot fakeban an administrator.", ephemeral=True)
        return
    
    # Animation
    await interaction.response.send_message(f"🔨 Preparing ban for {member.mention}...")
    for i in range(5, 0, -1):
        await interaction.edit_original_response(
            content=f"🔨 Preparing ban for {member.mention}\nExecuting in **{i}**..."
        )
        await asyncio.sleep(1)
    
    # DM the user
    try:
        await member.send("You've been BANNED!! 🤯🪦 (joke)")
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.debug(f"Could not DM {member}: {e}")
    
    # Timeout
    try:
        await member.timeout(timedelta(seconds=10), reason="Fake ban prank")
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.warning(f"Failed to timeout {member}: {e}")
    
    # Final message
    await interaction.edit_original_response(
        content=f"🔨 {member.mention} has been banned.\nReason: Breaking the rules."
    )
    await bot.logging_service.log_action(
        interaction.guild,
        f"🔨 **{interaction.user.mention}** fakebanned **{member.mention}**"
    )

@bot.tree.command(name="lockdown", description="Prevent members from joining a voice channel")
@app_commands.describe(channel="The voice channel to lock")
@app_commands.guild_only()
async def lockdown(interaction: discord.Interaction, channel: discord.VoiceChannel):
    """Lock a voice channel"""
    if not await bot.permission_service.require_staff(interaction):
        return
    
    try:
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        overwrite.connect = False
        await channel.set_permissions(
            interaction.guild.default_role,
            overwrite=overwrite,
            reason=f"Voice lockdown by {interaction.user}"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Missing permission to edit that channel.",
            ephemeral=True
        )
        return
    
    await interaction.response.send_message(
        f"🔒 {channel.mention} is locked. Use `/unlock` to reopen."
    )
    await bot.logging_service.log_action(
        interaction.guild,
        f"🔒 **{interaction.user.mention}** locked voice channel **{channel.mention}**",
        discord.Color.red()
    )

@bot.tree.command(name="unlock", description="Allow members to join a previously locked voice channel")
@app_commands.describe(channel="The voice channel to unlock")
@app_commands.guild_only()
async def unlock(interaction: discord.Interaction, channel: discord.VoiceChannel):
    """Unlock a voice channel"""
    if not await bot.permission_service.require_staff(interaction):
        return
    
    try:
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        overwrite.connect = None
        await channel.set_permissions(
            interaction.guild.default_role,
            overwrite=overwrite,
            reason=f"Voice unlock by {interaction.user}"
        )
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Missing permission to edit that channel.",
            ephemeral=True
        )
        return
    
    await interaction.response.send_message(f"🔓 {channel.mention} is unlocked.")
    await bot.logging_service.log_action(
        interaction.guild,
        f"🔓 **{interaction.user.mention}** unlocked voice channel **{channel.mention}**",
        discord.Color.green()
    )

@bot.tree.command(name="move", description="Move a member into a specified voice channel")
@app_commands.describe(
    member="The member to move",
    channel="The destination voice channel"
)
@app_commands.guild_only()
async def move(interaction: discord.Interaction, member: discord.Member, channel: discord.VoiceChannel):
    """Move a single member"""
    if not await bot.permission_service.require_staff(interaction):
        return
    
    if not member.voice or not member.voice.channel:
        await interaction.response.send_message(
            f"❌ {member.display_name} isn't in a voice channel.",
            ephemeral=True
        )
        return
    
    try:
        await member.move_to(channel, reason=f"Moved by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message(
            "❌ Missing Move Members permission or role order issue.",
            ephemeral=True
        )
        return
    except discord.HTTPException as e:
        await interaction.response.send_message(
            f"❌ Discord error: {e}",
            ephemeral=True
        )
        return
    
    await interaction.response.send_message(
        f"✅ Moved {member.display_name} to {channel.mention}."
    )
    await bot.logging_service.log_action(
        interaction.guild,
        f"➡️ **{interaction.user.mention}** moved **{member.mention}** to **{channel.mention}**"
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
    """Mass move multiple members"""
    if not await bot.permission_service.require_staff(interaction):
        return
    
    await interaction.response.defer()
    
    # Collect unique members
    candidates = [member1, member2, member3, member4, member5, member6, member7, member8, member9, member10]
    seen = set()
    members = []
    for m in candidates:
        if m and m.id not in seen:
            seen.add(m.id)
            members.append(m)
    
    if not members:
        await interaction.followup.send("❌ No members specified.")
        return
    
    # Move members
    moved = []
    not_in_voice = []
    failed = []
    
    for member in members:
        if not member.voice or not member.voice.channel:
            not_in_voice.append(member.display_name)
            continue
        
        try:
            await member.move_to(channel, reason=f"Mass moved by {interaction.user}")
            moved.append(member.display_name)
        except (discord.Forbidden, discord.HTTPException):
            failed.append(member.display_name)
    
    # Build response
    response_parts = []
    if moved:
        response_parts.append(f"✅ Moved to {channel.mention}: {', '.join(moved)}")
    if not_in_voice:
        response_parts.append(f"⚪ Not in voice: {', '.join(not_in_voice)}")
    if failed:
        response_parts.append(f"❌ Move failed: {', '.join(failed)}")
    
    await interaction.followup.send("\n".join(response_parts) or "Nothing to do.")
    
    if moved:
        await bot.logging_service.log_action(
            interaction.guild,
            f"➡️ **{interaction.user.mention}** mass-moved to **{channel.mention}**: {', '.join(moved)}"
        )

@bot.tree.command(name="say", description="Send a message through the bot")
@app_commands.describe(
    message="What the bot should say",
    channel="Channel to post in (defaults to this channel)",
)
@app_commands.guild_only()
async def say(interaction: discord.Interaction, message: str, channel: discord.TextChannel = None):
    """Send a message as the bot"""
    if not await bot.permission_service.require_staff(interaction):
        return
    
    target = channel or interaction.channel
    if not isinstance(target, discord.abc.Messageable):
        await interaction.response.send_message("❌ That channel can't receive messages.", ephemeral=True)
        return
    
    try:
        sent = await target.send(message)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Missing permission to send messages there.", ephemeral=True)
        return
    except discord.HTTPException as e:
        await interaction.response.send_message(f"❌ Discord error: {e}", ephemeral=True)
        return
    
    await interaction.response.send_message(
        f"✅ Sent to {target.mention}. Message ID: `{sent.id}`",
        ephemeral=True
    )
    await bot.logging_service.log_action(
        interaction.guild,
        f"💬 **{interaction.user.mention}** used /say in {target.mention} — message ID `{sent.id}`"
    )

@bot.tree.command(name="edit", description="Edit a previous message sent by the bot")
@app_commands.describe(
    message_id="The ID of the bot's message to edit",
    new_content="The new message content",
    channel="Channel the message is in (defaults to this channel)",
)
@app_commands.guild_only()
async def edit(interaction: discord.Interaction, message_id: str, new_content: str, channel: discord.TextChannel = None):
    """Edit a bot message"""
    if not await bot.permission_service.require_staff(interaction):
        return
    
    target = channel or interaction.channel
    if not isinstance(target, discord.abc.Messageable):
        await interaction.response.send_message("❌ That channel can't be read.", ephemeral=True)
        return
    
    try:
        message_id_int = int(message_id)
    except ValueError:
        await interaction.response.send_message("❌ Invalid message ID.", ephemeral=True)
        return
    
    try:
        target_message = await target.fetch_message(message_id_int)
    except discord.NotFound:
        await interaction.response.send_message("❌ Message not found in that channel.", ephemeral=True)
        return
    except (discord.Forbidden, discord.HTTPException) as e:
        await interaction.response.send_message(f"❌ Error: {e}", ephemeral=True)
        return
    
    if target_message.author.id != bot.user.id:
        await interaction.response.send_message("❌ That message wasn't sent by this bot.", ephemeral=True)
        return
    
    try:
        await target_message.edit(content=new_content)
    except (discord.Forbidden, discord.HTTPException) as e:
        await interaction.response.send_message(f"❌ Failed to edit: {e}", ephemeral=True)
        return
    
    await interaction.response.send_message(
        f"✅ Edited message `{target_message.id}` in {target.mention}.",
        ephemeral=True
    )
    await bot.logging_service.log_action(
        interaction.guild,
        f"✏️ **{interaction.user.mention}** edited bot message `{target_message.id}` in {target.mention}"
    )

# ==========================
# Self-Destruct (deliberately kept away from the other mod commands, above /daily,
# so it's not visually adjacent to routine tools when someone's scrolling the file)
# ==========================

class SelfDestructConfirmView(discord.ui.View):
    """One confirm, one cancel. Times out in 30s so it can't sit around as a loaded gun."""

    def __init__(self, author_id: int):
        super().__init__(timeout=30)
        self.author_id = author_id
        self.confirmed = False

    @discord.ui.button(label="Confirm Deletion", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("not your button.", ephemeral=True)
            return
        self.confirmed = True
        self.stop()
        await interaction.response.defer()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("not your button.", ephemeral=True)
            return
        self.stop()
        await interaction.response.defer()


@bot.tree.command(name="self-destruct", description="Last Resort. You'll know when to use this. (DO NOT USE)")
@app_commands.guild_only()
async def self_destruct(interaction: discord.Interaction):
    """Deletes every channel and every deletable role in the guild. No undo."""
    if not bot.permission_service.can_use_selfdestruct(interaction.user):
        await interaction.response.send_message("❌ you cannot use this.", ephemeral=True)
        return

    guild = interaction.guild
    view = SelfDestructConfirmView(interaction.user.id)

    await interaction.response.send_message(
        f"⚠️ this deletes every channel and role in **{guild.name}**. permanently. "
        "confirm or don't.",
        view=view,
        ephemeral=True
    )
    await view.wait()

    if not view.confirmed:
        await interaction.edit_original_response(content="cancelled. nothing touched.", view=None)
        return

    # log channel is about to stop existing, so log to the invoker directly, before anything dies
    logger.warning(
        f"SELF-DESTRUCT triggered on {guild.name} ({guild.id}) by "
        f"{interaction.user} ({interaction.user.id})"
    )
    try:
        await interaction.user.send(
            f"self-destruct executed on {guild.name} ({guild.id}) — "
            f"{datetime.now(timezone.utc).isoformat()}"
        )
    except (discord.Forbidden, discord.HTTPException):
        pass

    await interaction.edit_original_response(content="executing.", view=None)

    try:
        await guild.edit(
            name="StayGeeked's Remains",
            reason=f"self-destruct invoked by {interaction.user}"
        )
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.error(f"failed to rename guild: {e}")

    for channel in list(guild.channels):
        try:
            await channel.delete(reason=f"self-destruct invoked by {interaction.user}")
        except (discord.Forbidden, discord.HTTPException) as e:
            logger.error(f"failed to delete channel {channel.name}: {e}")

    bot_top_role = guild.me.top_role
    for role in list(guild.roles):
        if role.is_default() or role.managed or role >= bot_top_role:
            continue
        try:
            await role.delete(reason=f"self-destruct invoked by {interaction.user}")
        except (discord.Forbidden, discord.HTTPException) as e:
            logger.error(f"failed to delete role {role.name}: {e}")

@bot.tree.command(name="daily", description="Claim your once-a-day XP bonus")
@app_commands.guild_only()
async def daily(interaction: discord.Interaction):
    """Claim daily XP bonus"""
    levels = await bot.data_manager.load("levels.json", {})
    user_id = str(interaction.user.id)
    
    if user_id not in levels:
        levels[user_id] = {"xp": 0, "last_message_time": None}
    
    entry = levels[user_id]
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    
    if entry.get("last_daily_bonus") == today:
        await interaction.response.send_message(
            "❌ Already claimed today's bonus. Resets at 00:00 UTC.",
            ephemeral=True
        )
        return
    
    old_level, _ = bot.xp_system.get_level_from_xp(entry["xp"])
    entry["xp"] += Config.DAILY_BONUS_XP
    entry["last_daily_bonus"] = today
    new_level, _ = bot.xp_system.get_level_from_xp(entry["xp"])
    
    await bot.data_manager.save("levels.json", levels)
    
    embed = discord.Embed(
        description=f"🎉 Claimed **+{Config.DAILY_BONUS_XP} XP**\nTotal XP: **{entry['xp']:,}**",
        color=discord.Color.green()
    )
    embed.set_author(
        name=f"{interaction.user.display_name}'s Daily Bonus",
        icon_url=interaction.user.display_avatar.url
    )
    
    await interaction.response.send_message(embed=embed)
    
    if new_level > old_level:
        await bot.logging_service.announce_level_up(
            interaction.guild,
            interaction.user,
            new_level,
            interaction.channel
        )

@bot.tree.command(name="rank", description="Check your level and XP, or someone else's")
@app_commands.describe(member="Member to check (defaults to you)")
@app_commands.guild_only()
async def rank(interaction: discord.Interaction, member: discord.Member = None):
    """Check rank and XP"""
    target = member or interaction.user
    rank, level, xp = await bot.xp_system.get_rank(str(target.id))
    
    if xp == 0:
        await interaction.response.send_message(
            f"{target.display_name} hasn't earned any XP yet."
        )
        return
    
    # Get progress to next level
    _, progress = bot.xp_system.get_level_from_xp(xp)
    needed = bot.xp_system.xp_for_level(level)
    bar = bot.format_progress_bar(progress, needed)
    
    embed = discord.Embed(color=discord.Color.blurple())
    embed.set_author(
        name=f"{target.display_name}'s Rank",
        icon_url=target.display_avatar.url
    )
    embed.set_thumbnail(url=target.display_avatar.url)
    embed.add_field(name="Level", value=str(level), inline=True)
    embed.add_field(name="Server Rank", value=f"#{rank or '?'}", inline=True)
    embed.add_field(name="Total XP", value=f"{xp:,}", inline=True)
    embed.add_field(
        name="Progress to Next Level",
        value=f"{bar}\n{progress:,} / {needed:,} XP",
        inline=False
    )
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="Show the top members by XP")
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction):
    """Show XP leaderboard"""
    levels = await bot.data_manager.load("levels.json", {})
    
    if not levels:
        await interaction.response.send_message("Nobody has earned XP yet.")
        return
    
    # Get top 10
    sorted_users = sorted(
        levels.items(),
        key=lambda kv: kv[1].get("xp", 0),
        reverse=True
    )[:10]
    
    medals = {0: "🥇", 1: "🥈", 2: "🥉"}
    lines = []
    
    for i, (user_id, entry) in enumerate(sorted_users):
        member = interaction.guild.get_member(int(user_id))
        name = member.display_name if member else f"Unknown ({user_id})"
        
        rank, level, _ = await bot.xp_system.get_rank(user_id)
        medal = medals.get(i, f"**{i+1}.**")
        
        lines.append(
            f"{medal} **{name}**\n"
            f"  Level {level} · {entry.get('xp', 0):,} XP"
        )
    
    embed = discord.Embed(
        title="🏆 Leaderboard",
        description="\n\n".join(lines),
        color=discord.Color.gold()
    )
    embed.set_footer(text=f"Top {len(sorted_users)} by total XP")
    
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="help", description="Show available commands")
@app_commands.guild_only()
async def help_command(interaction: discord.Interaction):
    """Show help menu"""
    commands_by_category = {
        "🎮 XP & Levels": ["/daily", "/rank", "/leaderboard"],
        "🔨 Moderation": ["/fakeban", "/lockdown", "/unlock", "/move", "/mass-move"],
        "📝 Utility": ["/say", "/edit", "/help"]
    }
    
    embed = discord.Embed(
        title="🤖 Bot Commands",
        description="Here are all available commands:",
        color=discord.Color.blue()
    )
    
    for category, cmd_list in commands_by_category.items():
        if any(cmd in [f"/{cmd.name}" for cmd in bot.tree.get_commands()] 
               or cmd.strip("/") in [cmd.name for cmd in bot.tree.get_commands()] 
               for cmd in cmd_list):
            embed.add_field(
                name=category,
                value="\n".join(cmd_list),
                inline=False
            )
    
    embed.set_footer(text="Use / for all slash commands")
    
    await interaction.response.send_message(embed=embed)

# ==========================
# Error Handling
# ==========================

@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    """Handle command errors"""
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission for that command.")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing argument: {error.param}")
    else:
        logger.error(f"Command error: {error}")
        await ctx.send("❌ An error occurred while executing that command.")

# ==========================
# Main Entry Point
# ==========================

if __name__ == "__main__":
    try:
        bot.run(Config.TOKEN)
    except KeyboardInterrupt:
        logger.info("Bot shutting down...")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        raise
