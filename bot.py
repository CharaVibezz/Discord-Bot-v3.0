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
    LOG_CHANNEL_ID = 1525377874868305940
    LEVEL_UP_CHANNEL_ID = 1525392046989246525
    
    # Role IDs
    STAFF_ROLES = {
        1478214213292785825,
        1478212575908073482,
        1478212776588607670,
        1478212883253825657,
    }
    
    FAKEBAN_ALLOWED_ROLES = STAFF_ROLES | {1478127021828604075}

    # Separate, deliberately not reusing FAKEBAN_ALLOWED_ROLES — this command has a
    # bigger blast radius than a prank timeout. Left empty on purpose: an empty
    # whitelist means the command is inert until someone explicitly staffs it.
    SELFDESTRUCT_ALLOWED_ROLES = {
        1478210342524944447,
        1478238526330900552
    }

    # role sync: anyone holding VERIFIED_ROLE_ID has UNVERIFIED_ROLE_ID stripped
    VERIFIED_ROLE_ID = 1478213211307380838
    UNVERIFIED_ROLE_ID = 1478213220408889384

    # roles that survive a /strip-roles call regardless of who holds them.
    # empty by default — same reasoning as SELFDESTRUCT_ALLOWED_ROLES: an
    # unconfigured whitelist means the command strips everything, not that
    # it quietly protects roles nobody told it to protect
    KEEP_ROLES_WHITELIST: set = {
        1478213157804572722,
        1478212910735032482,
        1478212776588607670,
        1478213318177980426,
    }
    
    # XP Settings — level curve: xp_for_level(n) = LEVEL_A*n^2 + LEVEL_B*n + LEVEL_C
    LEVEL_A = 35
    LEVEL_B = 120
    LEVEL_C = 200

    # Message XP — tiered by length, plus small quality bonuses
    MESSAGE_XP_SHORT = (10, 20)      # content < 10 chars
    MESSAGE_XP_MEDIUM = (25, 35)     # content 10-49 chars
    MESSAGE_XP_LONG = (35, 50)       # content 50+ chars
    MESSAGE_XP_COOLDOWN_SECONDS = 30
    QUALITY_BONUS_LONG_MESSAGE = 5   # 20+ words
    QUALITY_BONUS_LINK = 3           # contains a link
    QUALITY_BONUS_REPLY = 2          # is a reply

    DAILY_BONUS_XP = 200

    # Voice XP — tiered rate, resets every join/leave (same behavior as the old
    # ramp model). a short-hop farmer just keeps re-collecting the top tier, so
    # VOICE_XP_COOLDOWN_SECONDS below is what actually stops that, not this curve.
    VOICE_RATE_OPTIMAL = 20     # xp/min, first 30 min
    VOICE_RATE_MODERATE = 12    # xp/min, next 30 min (30-60)
    VOICE_RATE_MINIMAL = 6      # xp/min, everything past 60 min
    VOICE_MINIMUM_MINUTES = 1
    # per-user cooldown between paid voice sessions. in-memory only — resets on
    # restart — but that's fine, its job is stopping rapid join/leave cycling
    # within a single runtime, not surviving deploys.
    VOICE_XP_COOLDOWN_SECONDS = 300
    # flat bonus for sessions that clear 60 min, doubled past 90 min
    CONTENT_CREATOR_BONUS = 30

    # Streak bonus — consecutive days (ending today) with 50+ voice XP
    STREAK_THRESHOLD = 3
    STREAK_BONUS_PER_DAY = 5
    STREAK_BONUS_MAX = 50

    # date (YYYY-MM-DD, UTC) -> multiplier. set via /doublexp, persisted to
    # data/config.json so it survives restarts.
    DOUBLE_XP_EVENTS: Dict[str, float] = {}

    # if True, every voice XP award posts a breakdown embed to LOG_CHANNEL_ID.
    # useful while you're debugging XP totals, noisy in a busy server long-term —
    # turn it off once you trust the numbers again.
    VOICE_XP_DETAILED_LOGGING = True
    
    # Warning Settings
    WARNING_EXPIRY_DAYS = 30
    
    # File paths
    WARNING_FILE = DATA_DIR / "warnings.json"
    LEVELS_FILE = DATA_DIR / "levels.json"
    VOICE_SESSIONS_FILE = DATA_DIR / "voice_sessions.json"
    # user_id -> [role_id, ...] removed by the most recent /strip-roles call.
    # overwritten on every strip — restore always undoes the latest one, not
    # a history of every strip that's ever happened
    STRIPPED_ROLES_FILE = DATA_DIR / "stripped_roles.json"
    
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

    @classmethod
    def is_double_xp(cls) -> float:
        """Multiplier for today, UTC. 1.0 unless a /doublexp event is set."""
        today = datetime.now(timezone.utc).date().isoformat()
        return cls.DOUBLE_XP_EVENTS.get(today, 1.0)

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
    """XP, levels, streaks, and achievements. No daily caps — progression is
    unlimited, rate-limited only by the message cooldown and the voice cooldown."""

    def __init__(self, data_manager: DataManager):
        self.data_manager = data_manager
        # per-user cooldown tracking for voice xp payouts, in-memory only
        self.last_voice_xp_time: Dict[str, datetime] = {}

    @staticmethod
    def xp_for_level(level: int) -> int:
        """XP required to clear `level` and reach level+1."""
        return Config.LEVEL_A * (level ** 2) + Config.LEVEL_B * level + Config.LEVEL_C

    @staticmethod
    def get_level_from_xp(total_xp: int) -> Tuple[int, int]:
        """Calculate level and progress into that level from total XP"""
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
    def _new_entry() -> Dict[str, Any]:
        """Default shape for a fresh user record."""
        return {
            "xp": 0,
            "last_message_time": None,
            "last_daily_bonus": None,
            "daily_voice_xp": {},
            "daily_message_xp": {},
            "voice_days": [],
            "total_voice_time": 0,
            "total_messages": 0,
            "achievements": [],
        }

    @staticmethod
    def _ensure_entry(entry: Dict[str, Any]) -> Dict[str, Any]:
        """Backfill any fields missing from an older or partially-built entry,
        in place. Every function below calls this before touching an entry so
        it doesn't matter which command created the record first."""
        for key, default in XPSystem._new_entry().items():
            entry.setdefault(key, default)
        return entry

    @staticmethod
    def _voice_streak(entry: Dict[str, Any]) -> int:
        """Consecutive days, ending today, with 50+ voice XP earned."""
        daily_voice = entry.get("daily_voice_xp", {})
        if not daily_voice:
            return 0
        today = datetime.now(timezone.utc).date()
        streak = 0
        for i in range(30):
            check_date = (today - timedelta(days=i)).isoformat()
            if daily_voice.get(check_date, 0) > 50:
                streak += 1
            else:
                break
        return streak

    @staticmethod
    def calculate_voice_xp(elapsed_seconds: float, streak_days: int = 0) -> Tuple[int, Dict[str, Any]]:
        """XP for a single voice session. Tiered rate — the first 30 minutes
        pay the most per minute, tapering off after that so a session left
        open all day doesn't dwarf normal use.

        NOTE — this resets on every join/leave, same as the ramp model it
        replaced. That means a short-hop farmer just keeps re-collecting the
        top tier; VOICE_XP_COOLDOWN_SECONDS (checked in add_voice_xp) is what
        actually stops that, not this curve. If you want the curve itself to
        resist hopping, track cumulative daily minutes instead of per-session
        — bigger change, say so if you want it.
        """
        minutes = elapsed_seconds / 60
        if minutes < Config.VOICE_MINIMUM_MINUTES:
            return 0, {"minutes": minutes, "reason": "too_short"}

        if minutes <= 30:
            tier, rate = "optimal", Config.VOICE_RATE_OPTIMAL
            base_xp = int(minutes * rate)
        elif minutes <= 60:
            tier, rate = "moderate", Config.VOICE_RATE_MODERATE
            base_xp = int(30 * Config.VOICE_RATE_OPTIMAL + (minutes - 30) * rate)
        else:
            tier, rate = "minimal", Config.VOICE_RATE_MINIMAL
            base_xp = int(
                30 * Config.VOICE_RATE_OPTIMAL
                + 30 * Config.VOICE_RATE_MODERATE
                + (minutes - 60) * rate
            )

        content_creator_bonus = 0
        if minutes >= 90:
            content_creator_bonus = Config.CONTENT_CREATOR_BONUS * 2
        elif minutes >= 60:
            content_creator_bonus = Config.CONTENT_CREATOR_BONUS

        streak_bonus = 0
        if streak_days >= Config.STREAK_THRESHOLD:
            streak_bonus = min(streak_days * Config.STREAK_BONUS_PER_DAY, Config.STREAK_BONUS_MAX)

        double_multiplier = Config.is_double_xp()
        total_xp = int((base_xp + content_creator_bonus) * double_multiplier) + streak_bonus

        breakdown = {
            "minutes": minutes,
            "tier": tier,
            "rate": rate,
            "base_xp": base_xp,
            "content_creator_bonus": content_creator_bonus,
            "streak_bonus": streak_bonus,
            "streak_days": streak_days,
            "double_xp": double_multiplier,
        }
        return total_xp, breakdown

    async def add_voice_xp(self, member: discord.Member, elapsed_seconds: float) -> Dict[str, Any]:
        """Add XP from a voice session. Subject to a per-user cooldown so rapid
        join/leave cycling can't re-trigger payout."""
        user_id = str(member.id)
        now = datetime.now(timezone.utc)

        last_time = self.last_voice_xp_time.get(user_id)
        if last_time:
            remaining = Config.VOICE_XP_COOLDOWN_SECONDS - (now - last_time).total_seconds()
            if remaining > 0:
                return {
                    "xp_gained": 0,
                    "leveled_up": False,
                    "old_level": None,
                    "new_level": None,
                    "total_xp": None,
                    "breakdown": {"reason": "cooldown", "cooldown_remaining": remaining},
                }

        levels = await self.data_manager.load("levels.json", {})
        entry = self._ensure_entry(levels.get(user_id, self._new_entry()))
        levels[user_id] = entry

        streak_days = self._voice_streak(entry)
        xp_gained, breakdown = self.calculate_voice_xp(elapsed_seconds, streak_days)

        result: Dict[str, Any] = {
            "xp_gained": xp_gained,
            "leveled_up": False,
            "old_level": None,
            "new_level": None,
            "total_xp": None,
            "breakdown": breakdown,
        }
        if xp_gained <= 0:
            return result

        today = now.date().isoformat()
        entry["daily_voice_xp"][today] = entry["daily_voice_xp"].get(today, 0) + xp_gained
        entry["total_voice_time"] += elapsed_seconds
        if today not in entry["voice_days"]:
            entry["voice_days"].append(today)
            entry["voice_days"] = entry["voice_days"][-30:]

        old_level, _ = self.get_level_from_xp(entry["xp"])
        entry["xp"] += xp_gained
        new_level, _ = self.get_level_from_xp(entry["xp"])

        await self.data_manager.save("levels.json", levels)
        self.last_voice_xp_time[user_id] = now

        result.update({
            "leveled_up": new_level > old_level,
            "old_level": old_level,
            "new_level": new_level,
            "total_xp": entry["xp"],
        })
        return result

    async def award_message_xp(self, message: discord.Message) -> bool:
        """Award XP for a message, tiered by length with small quality bonuses.
        Returns True if this message leveled the author up."""
        if not message.guild or message.author.bot:
            return False

        levels = await self.data_manager.load("levels.json", {})
        user_id = str(message.author.id)
        entry = self._ensure_entry(levels.get(user_id, self._new_entry()))
        levels[user_id] = entry

        now = datetime.now(timezone.utc)
        if entry.get("last_message_time"):
            try:
                last_time = datetime.fromisoformat(entry["last_message_time"])
                if (now - last_time).total_seconds() < Config.MESSAGE_XP_COOLDOWN_SECONDS:
                    return False
            except ValueError:
                pass

        content = message.content.strip()
        content_length = len(content)
        word_count = len(content.split())

        if content_length < 10:
            base_min, base_max = Config.MESSAGE_XP_SHORT
        elif content_length < 50:
            base_min, base_max = Config.MESSAGE_XP_MEDIUM
        else:
            base_min, base_max = Config.MESSAGE_XP_LONG
        base_xp = random.randint(base_min, base_max)

        quality_bonus = 0
        if word_count > 20:
            quality_bonus += Config.QUALITY_BONUS_LONG_MESSAGE
        if "http" in content.lower():
            quality_bonus += Config.QUALITY_BONUS_LINK
        if message.reference is not None:
            quality_bonus += Config.QUALITY_BONUS_REPLY

        gained = int((base_xp + quality_bonus) * Config.is_double_xp())

        today = now.date().isoformat()
        entry["last_message_time"] = now.isoformat()
        entry["daily_message_xp"][today] = entry["daily_message_xp"].get(today, 0) + gained
        entry["total_messages"] += 1

        old_level, _ = self.get_level_from_xp(entry["xp"])
        entry["xp"] += gained
        new_level, _ = self.get_level_from_xp(entry["xp"])

        await self.data_manager.save("levels.json", levels)
        return new_level > old_level

    async def get_rank(self, user_id: str) -> Tuple[Optional[int], int, int]:
        """Get user's rank, level, and total XP"""
        levels = await self.data_manager.load("levels.json", {})
        entry = levels.get(user_id, {"xp": 0})
        xp = entry.get("xp", 0)
        level, _ = self.get_level_from_xp(xp)

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

    async def get_daily_stats(self, user_id: str) -> Dict[str, Any]:
        """Today's activity plus lifetime totals, for /stats."""
        levels = await self.data_manager.load("levels.json", {})
        entry = self._ensure_entry(levels.get(user_id, self._new_entry()))
        today = datetime.now(timezone.utc).date().isoformat()

        return {
            "daily_voice_xp": entry["daily_voice_xp"].get(today, 0),
            "daily_message_xp": entry["daily_message_xp"].get(today, 0),
            "voice_streak": self._voice_streak(entry),
            "total_xp": entry.get("xp", 0),
            "total_voice_time": entry.get("total_voice_time", 0),
            "total_messages": entry.get("total_messages", 0),
            "achievements": entry.get("achievements", []),
        }

    async def check_achievements(self, member: discord.Member) -> List[str]:
        """Check thresholds and award any newly-unlocked achievements.
        Membership is checked against the stored label text itself, not a
        separate short key — comparing a key like "voice_veteran" against a
        list of formatted labels never matches, which silently re-awards
        every achievement on every check. Don't reintroduce that."""
        user_id = str(member.id)
        levels = await self.data_manager.load("levels.json", {})
        entry = self._ensure_entry(levels.get(user_id, self._new_entry()))
        levels[user_id] = entry

        unlocked = entry["achievements"]
        new_achievements = []

        def maybe_add(label: str, condition: bool):
            if condition and label not in unlocked:
                new_achievements.append(label)

        voice_hours = entry.get("total_voice_time", 0) / 3600
        maybe_add("🎙️ Voice Veteran (10 hours)", voice_hours >= 10)
        maybe_add("🎙️ Voice Master (50 hours)", voice_hours >= 50)
        maybe_add("🎙️ Voice Legend (100 hours)", voice_hours >= 100)
        maybe_add("🎙️ Voice God (500 hours)", voice_hours >= 500)
        maybe_add("🎙️ Voice Immortal (1000 hours)", voice_hours >= 1000)

        streak = self._voice_streak(entry)
        maybe_add("🔥 Weekly Warrior (7-day streak)", streak >= 7)
        maybe_add("🔥 Monthly Monster (30-day streak)", streak >= 30)
        maybe_add("🔥 Streak Legend (100-day streak)", streak >= 100)

        total_messages = entry.get("total_messages", 0)
        maybe_add("💬 Chatty Cathy (100 messages)", total_messages >= 100)
        maybe_add("💬 Message Master (1,000 messages)", total_messages >= 1000)
        maybe_add("💬 Message Legend (10,000 messages)", total_messages >= 10000)

        level, _ = self.get_level_from_xp(entry.get("xp", 0))
        maybe_add("⭐ Level Enthusiast (Level 25)", level >= 25)
        maybe_add("⭐ Level Expert (Level 50)", level >= 50)
        maybe_add("⭐ Level Legend (Level 100)", level >= 100)

        if new_achievements:
            entry["achievements"].extend(new_achievements)
            await self.data_manager.save("levels.json", levels)

        return new_achievements

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

    async def log_voice_xp(
        self,
        guild: discord.Guild,
        member: discord.Member,
        result: Dict[str, Any]
    ):
        """Log a voice XP award with the full rate breakdown — this is the
        detail you need to see *why* two members with similar voice time
        ended up with different totals."""
        breakdown = result["breakdown"]

        if breakdown.get("reason") == "cooldown":
            remaining = breakdown.get("cooldown_remaining", 0)
            await self.log_action(
                guild,
                f"⏳ **{member.mention}** voice XP on cooldown ({remaining:.0f}s remaining)",
                discord.Color.greyple()
            )
            return

        if breakdown.get("reason") == "too_short":
            return  # below the 1-minute floor, nothing worth logging

        minutes = breakdown.get("minutes", 0)
        tier = breakdown.get("tier", "unknown")

        lines = [f"🎧 **{member.mention}** — {minutes:.1f} min in voice → +{result['xp_gained']} XP"]

        detail = f"`{breakdown['base_xp']} base ({tier} tier, {breakdown['rate']}/min)`"
        if breakdown.get("content_creator_bonus"):
            detail += f" + `{breakdown['content_creator_bonus']} content-creator bonus`"
        lines.append(detail)

        lines.append(
            f"total XP: **{result['total_xp']:,}** — level {result['old_level']} → {result['new_level']}"
        )

        if breakdown.get("streak_days", 0) >= Config.STREAK_THRESHOLD:
            lines.append(f"🔥 {breakdown['streak_days']}-day streak (+{breakdown['streak_bonus']} bonus)")

        if breakdown.get("double_xp", 1.0) > 1.0:
            lines.append(f"⚡ double xp event active ({breakdown['double_xp']}x)")

        await self.log_action(guild, "\n".join(lines), discord.Color.teal())

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
        self.verified_role_sweep.start()
        
        # Sync slash commands
        try:
            await self.tree.sync()
            logger.info("Slash commands synced")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")
    
    async def on_ready(self):
        """Called when bot is ready"""
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")

        # log the voice xp settings once at startup — first thing to check when
        # someone says the numbers look wrong
        logger.info(
            f"voice xp settings — {Config.VOICE_RATE_OPTIMAL}/min (0-30m), "
            f"{Config.VOICE_RATE_MODERATE}/min (30-60m), {Config.VOICE_RATE_MINIMAL}/min (60m+), "
            f"{Config.VOICE_XP_COOLDOWN_SECONDS}s cooldown between paid sessions "
            f"(resets per session — see XPSystem.calculate_voice_xp docstring)"
        )

        # double xp events are set at runtime via /doublexp, so they don't
        # survive a restart unless we reload them from disk here
        config_data = await self.data_manager.load("config.json", {})
        if config_data.get("double_xp_events"):
            Config.DOUBLE_XP_EVENTS.update(config_data["double_xp_events"])
            logger.info(f"loaded {len(config_data['double_xp_events'])} double xp event(s) from disk")

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

        # catch anyone who already holds both roles — role changes made while the
        # bot was offline don't fire on_member_update, so this is the safety net
        for guild in self.guilds:
            await self.sweep_verified_roles(guild)

    async def sync_verified_role(self, member: discord.Member) -> bool:
        """Strip the unverified role from a member who holds the verified role.
        Returns True if a role was actually removed."""
        if not Config.VERIFIED_ROLE_ID or not Config.UNVERIFIED_ROLE_ID:
            return False  # not configured — no-op rather than guessing at IDs

        role_ids = {r.id for r in member.roles}
        if Config.VERIFIED_ROLE_ID not in role_ids or Config.UNVERIFIED_ROLE_ID not in role_ids:
            return False

        unverified_role = member.guild.get_role(Config.UNVERIFIED_ROLE_ID)
        if not unverified_role:
            logger.warning(f"unverified role {Config.UNVERIFIED_ROLE_ID} not found in {member.guild.name}")
            return False

        try:
            await member.remove_roles(unverified_role, reason="Verified role present — removing unverified")
        except (discord.Forbidden, discord.HTTPException) as e:
            logger.error(f"failed to remove unverified role from {member}: {e}")
            return False

        logger.info(f"removed unverified from {member} ({member.id}) in {member.guild.name}")
        return True

    async def sweep_verified_roles(self, guild: discord.Guild):
        """Check every member for the verified+unverified overlap. Run on startup
        and periodically as a safety net for anything on_member_update missed."""
        if not Config.VERIFIED_ROLE_ID or not Config.UNVERIFIED_ROLE_ID:
            return

        verified_role = guild.get_role(Config.VERIFIED_ROLE_ID)
        if not verified_role:
            return

        cleaned = 0
        for member in verified_role.members:
            if await self.sync_verified_role(member):
                cleaned += 1

        if cleaned:
            logger.info(f"verified-role sweep cleaned {cleaned} member(s) in {guild.name}")
    
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
    # Verified Role Sweep Task
    # ==========================

    @tasks.loop(minutes=10)
    async def verified_role_sweep(self):
        """Periodic safety net — catches anything on_member_update missed
        (role added via external tool, audit gaps, etc)."""
        try:
            await self.wait_until_ready()
            for guild in self.guilds:
                await self.sweep_verified_roles(guild)
        except Exception as e:
            logger.error(f"verified role sweep failed: {e}")

    @verified_role_sweep.before_loop
    async def before_verified_role_sweep(self):
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
async def on_member_update(before: discord.Member, after: discord.Member):
    """Catch role changes — specifically, verified being granted while
    unverified is still present."""
    if before.roles == after.roles:
        return  # something else changed (nickname, etc), not our concern

    before_ids = {r.id for r in before.roles}
    after_ids = {r.id for r in after.roles}

    verified_just_added = (
        Config.VERIFIED_ROLE_ID in after_ids and Config.VERIFIED_ROLE_ID not in before_ids
    )
    verified_already_present = Config.VERIFIED_ROLE_ID in after_ids

    if not verified_already_present:
        return

    if await bot.sync_verified_role(after):
        await bot.logging_service.log_action(
            after.guild,
            f"✅ **{after.mention}** was verified — removed unverified role"
            + (" (granted this update)" if verified_just_added else ""),
            discord.Color.green()
        )

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
        logger.info(f"voice session start: {member} ({member.id}) in {guild.name} at {now.isoformat()}")
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
                result = await bot.xp_system.add_voice_xp(member, elapsed)
                breakdown = result["breakdown"]

                if breakdown.get("reason") == "cooldown":
                    logger.info(
                        f"voice xp on cooldown: {member} ({member.id}) in {guild.name} — "
                        f"{breakdown.get('cooldown_remaining', 0):.0f}s remaining"
                    )
                else:
                    logger.info(
                        f"voice xp award: {member} ({member.id}) in {guild.name} — "
                        f"session {start_time.isoformat()} -> {now.isoformat()} "
                        f"({breakdown.get('minutes', 0):.1f} min elapsed, {breakdown.get('tier')} tier), "
                        f"+{result['xp_gained']} xp, total {result['total_xp']}, "
                        f"level {result['old_level']} -> {result['new_level']}, "
                        f"breakdown={breakdown}"
                    )

                    if result["leveled_up"]:
                        await bot.logging_service.announce_level_up(guild, member, result["new_level"])

                if Config.VOICE_XP_DETAILED_LOGGING:
                    await bot.logging_service.log_voice_xp(guild, member, result)
            else:
                logger.info(
                    f"voice xp skipped: {member} ({member.id}) in {guild.name} — "
                    f"session only {elapsed:.0f}s, below 60s minimum"
                )
            
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
    """Fake ban command with animation.

    member.timeout() is a real API call, and Discord enforces role hierarchy
    on it same as every other moderation action: a bot cannot time out a
    member whose top role is at or above the bot's own top role, and can
    never time out the guild owner. That's a platform restriction, not
    something fixable from this side — if fakeban needs to reach a
    moderator, the bot's role has to sit above that moderator's role in
    Server Settings > Roles. What IS fixable in code is checking for that
    up front instead of quietly eating the 403 and telling the channel it
    worked anyway.
    """
    # Permission check
    if not bot.permission_service.can_use_fakeban(interaction.user):
        await interaction.response.send_message("❌ You cannot use this command.", ephemeral=True)
        return

    if bot.permission_service.is_admin(member):
        await interaction.response.send_message("❌ You cannot fakeban an administrator.", ephemeral=True)
        return

    if member.id == interaction.guild.owner_id:
        await interaction.response.send_message("❌ You cannot fakeban the server owner.", ephemeral=True)
        return

    bot_top_role = interaction.guild.me.top_role
    if member.top_role >= bot_top_role:
        await interaction.response.send_message(
            f"❌ Cannot fakeban {member.mention} — their top role (**{member.top_role.name}**) "
            f"is at or above my top role (**{bot_top_role.name}**). Discord won't let a bot "
            f"time out a member ranked at or above it, no matter what this command does. "
            f"Move my role higher in Server Settings > Roles to fix it.",
            ephemeral=True
        )
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
    timed_out = False
    try:
        await member.timeout(timedelta(seconds=10), reason="Fake ban prank")
        timed_out = True
    except (discord.Forbidden, discord.HTTPException) as e:
        logger.warning(f"Failed to timeout {member}: {e}")

    # Final message — only claim success if the timeout actually landed
    if timed_out:
        await interaction.edit_original_response(
            content=f"🔨 {member.mention} has been banned.\nReason: Breaking the rules."
        )
    else:
        await interaction.edit_original_response(
            content=f"🔨 {member.mention} has been banned.\nReason: Breaking the rules.\n"
            f"-# (the timeout itself failed — Discord rejected it, see logs)"
        )

    await bot.logging_service.log_action(
        interaction.guild,
        f"🔨 **{interaction.user.mention}** fakebanned **{member.mention}**"
        + ("" if timed_out else " *(timeout failed — role hierarchy)*")
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

    # move_to() is a real API call — mass-move already defers for this reason,
    # this one didn't. defer up front so a slow response can't outlive the
    # 3-second interaction token (same bug that hit strip-roles).
    await interaction.response.defer()

    try:
        await member.move_to(channel, reason=f"Moved by {interaction.user}")
    except discord.Forbidden:
        await interaction.followup.send(
            "❌ Missing Move Members permission or role order issue.",
            ephemeral=True
        )
        return
    except discord.HTTPException as e:
        await interaction.followup.send(
            f"❌ Discord error: {e}",
            ephemeral=True
        )
        return
    
    await interaction.followup.send(
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

@bot.tree.command(name="voice-xp-logging", description="Toggle detailed voice XP logging to the log channel")
@app_commands.describe(enabled="Turn detailed voice XP embed logging on or off")
@app_commands.guild_only()
async def voice_xp_logging(interaction: discord.Interaction, enabled: bool):
    """Staff toggle for Config.VOICE_XP_DETAILED_LOGGING.

    In-memory only — resets to the hardcoded default in Config on restart.
    If you want it to stick permanently, change the default in Config instead
    of relying on this every time the bot redeploys.
    """
    if not await bot.permission_service.require_staff(interaction):
        return

    Config.VOICE_XP_DETAILED_LOGGING = enabled
    state = "on" if enabled else "off"

    await interaction.response.send_message(
        f"✅ voice xp detailed logging is now **{state}**. "
        f"(resets to the code default on next restart)",
        ephemeral=True
    )
    await bot.logging_service.log_action(
        interaction.guild,
        f"⚙️ **{interaction.user.mention}** turned voice xp detailed logging **{state}**"
    )

@bot.tree.command(name="strip-roles", description="Remove all roles from a member except the configured whitelist")
@app_commands.describe(member="The member to strip")
@app_commands.guild_only()
async def strip_roles(interaction: discord.Interaction, member: discord.Member):
    """Remove every role from member except Config.KEEP_ROLES_WHITELIST, managed
    roles (bot integrations), and anything at or above the bot's top role —
    those last two can't be touched regardless of whitelist."""
    if not await bot.permission_service.require_staff(interaction):
        return

    # remove_roles() below is a real API call and can occasionally take long
    # enough that the 3-second interaction token expires before we get to
    # our final send_message — defer immediately so the token stays alive
    # regardless of how long the role edit takes.
    await interaction.response.defer()

    bot_top_role = interaction.guild.me.top_role
    to_remove = []

    for role in member.roles:
        if role.is_default():
            continue  # @everyone, nothing to remove
        if role.id in Config.KEEP_ROLES_WHITELIST:
            continue
        if role.managed:
            continue  # bot/integration role, removal would just fail
        if role >= bot_top_role:
            continue  # above the bot, would 403
        to_remove.append(role)

    if not to_remove:
        await interaction.followup.send(
            f"❌ nothing to remove — {member.display_name} has no roles outside the whitelist.",
            ephemeral=True
        )
        return

    try:
        await member.remove_roles(*to_remove, reason=f"role strip by {interaction.user}")
    except (discord.Forbidden, discord.HTTPException) as e:
        await interaction.followup.send(f"❌ failed: {e}", ephemeral=True)
        return

    # remember what was removed so /restore-roles can put it back. keyed by
    # member id, overwritten each strip — see STRIPPED_ROLES_FILE comment.
    stripped = await bot.data_manager.load("stripped_roles.json", {})
    stripped[str(member.id)] = [r.id for r in to_remove]
    await bot.data_manager.save("stripped_roles.json", stripped)

    removed_names = ", ".join(r.name for r in to_remove)
    await interaction.followup.send(
        f"✅ stripped {len(to_remove)} role(s) from {member.mention}: {removed_names}"
    )
    await bot.logging_service.log_action(
        interaction.guild,
        f"🧹 **{interaction.user.mention}** stripped roles from **{member.mention}**: {removed_names}",
        discord.Color.orange()
    )

@bot.tree.command(name="restore-roles", description="Add back the roles removed by the most recent /strip-roles on a member")
@app_commands.describe(member="The member to restore")
@app_commands.guild_only()
async def restore_roles(interaction: discord.Interaction, member: discord.Member):
    """Re-adds whatever /strip-roles most recently took from member. Not a
    general role backup — only covers strips, and only the latest one per
    member. Clears the stored record on success so a second restore-roles
    call is a no-op instead of re-adding roles the staff member re-removed
    on purpose in between."""
    if not await bot.permission_service.require_staff(interaction):
        return

    # same reasoning as strip-roles: add_roles() is a network call, defer up
    # front so a slow response doesn't burn the 3-second interaction token.
    await interaction.response.defer()

    stripped = await bot.data_manager.load("stripped_roles.json", {})
    role_ids = stripped.get(str(member.id))

    if not role_ids:
        await interaction.followup.send(
            f"❌ no recorded strip for {member.display_name} — nothing to restore.",
            ephemeral=True
        )
        return

    bot_top_role = interaction.guild.me.top_role
    to_add = []
    skipped = []

    for role_id in role_ids:
        role = interaction.guild.get_role(role_id)
        if not role:
            skipped.append(f"`{role_id}` (deleted)")
            continue
        if role in member.roles:
            continue  # already has it, nothing to do
        if role.managed or role >= bot_top_role:
            skipped.append(f"{role.name} (can't assign)")
            continue
        to_add.append(role)

    if not to_add and not skipped:
        await interaction.followup.send(
            f"❌ {member.display_name} already has every recorded role back.",
            ephemeral=True
        )
        stripped.pop(str(member.id), None)
        await bot.data_manager.save("stripped_roles.json", stripped)
        return

    if to_add:
        try:
            await member.add_roles(*to_add, reason=f"role restore by {interaction.user}")
        except (discord.Forbidden, discord.HTTPException) as e:
            await interaction.followup.send(f"❌ failed: {e}", ephemeral=True)
            return

    stripped.pop(str(member.id), None)
    await bot.data_manager.save("stripped_roles.json", stripped)

    parts = []
    if to_add:
        parts.append(f"✅ restored {len(to_add)} role(s) to {member.mention}: " + ", ".join(r.name for r in to_add))
    if skipped:
        parts.append(f"⚪ skipped: {', '.join(skipped)}")

    await interaction.followup.send("\n".join(parts))
    await bot.logging_service.log_action(
        interaction.guild,
        f"↩️ **{interaction.user.mention}** restored roles to **{member.mention}**: "
        + (", ".join(r.name for r in to_add) if to_add else "(none — all skipped)"),
        discord.Color.blurple()
    )

@bot.tree.command(name="daily", description="Claim your once-a-day XP bonus")
@app_commands.guild_only()
async def daily(interaction: discord.Interaction):
    """Claim daily XP bonus"""
    levels = await bot.data_manager.load("levels.json", {})
    user_id = str(interaction.user.id)
    entry = bot.xp_system._ensure_entry(levels.get(user_id, bot.xp_system._new_entry()))
    levels[user_id] = entry

    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    
    if entry.get("last_daily_bonus") == today:
        await interaction.response.send_message(
            "❌ Already claimed today's bonus. Resets at 00:00 UTC.",
            ephemeral=True
        )
        return
    
    old_level, _ = bot.xp_system.get_level_from_xp(entry["xp"])
    gained = int(Config.DAILY_BONUS_XP * Config.is_double_xp())
    entry["xp"] += gained
    entry["last_daily_bonus"] = today
    new_level, _ = bot.xp_system.get_level_from_xp(entry["xp"])
    
    await bot.data_manager.save("levels.json", levels)
    
    embed = discord.Embed(
        description=f"🎉 Claimed **+{gained} XP**\nTotal XP: **{entry['xp']:,}**",
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
@app_commands.describe(limit="Number of users to show (1-20, default 10)")
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction, limit: int = 10):
    """Show XP leaderboard"""
    if limit < 1 or limit > 20:
        await interaction.response.send_message("❌ Limit must be between 1 and 20.", ephemeral=True)
        return

    levels = await bot.data_manager.load("levels.json", {})
    
    if not levels:
        await interaction.response.send_message("Nobody has earned XP yet.")
        return
    
    sorted_users = sorted(
        levels.items(),
        key=lambda kv: kv[1].get("xp", 0),
        reverse=True
    )[:limit]
    
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

@bot.tree.command(name="stats", description="View your detailed XP statistics")
@app_commands.guild_only()
async def stats(interaction: discord.Interaction):
    """Detailed breakdown: today's activity, lifetime totals, achievements"""
    user_id = str(interaction.user.id)
    rank, level, xp = await bot.xp_system.get_rank(user_id)
    daily_stats = await bot.xp_system.get_daily_stats(user_id)

    next_level_xp = bot.xp_system.xp_for_level(level)
    _, progress = bot.xp_system.get_level_from_xp(xp)

    embed = discord.Embed(
        title=f"📊 {interaction.user.display_name}'s Stats",
        color=discord.Color.blue(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.add_field(
        name="📈 Level & XP",
        value=f"**Level {level}**\nTotal XP: {xp:,}\nRank: #{rank if rank else 'N/A'}",
        inline=True
    )
    embed.add_field(
        name="📅 Today",
        value=f"📝 Message XP: {daily_stats['daily_message_xp']:,}\n"
              f"🎧 Voice XP: {daily_stats['daily_voice_xp']:,}\n"
              f"🔥 Voice Streak: {daily_stats['voice_streak']} day(s)",
        inline=True
    )
    voice_hours = daily_stats["total_voice_time"] / 3600
    embed.add_field(
        name="🏆 Lifetime",
        value=f"🎙️ Voice Time: {voice_hours:.1f} hours\n"
              f"💬 Messages: {daily_stats['total_messages']:,}\n"
              f"🏅 Achievements: {len(daily_stats['achievements'])}",
        inline=True
    )

    if next_level_xp > 0:
        bar = bot.format_progress_bar(progress, next_level_xp)
        embed.add_field(
            name="Progress to Next Level",
            value=f"{bar}\n{progress:,} / {next_level_xp:,} XP",
            inline=False
        )

    if Config.is_double_xp() > 1.0:
        embed.set_footer(text=f"⚡ Double XP event active ({Config.is_double_xp()}x)")

    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="achievements", description="View your unlocked achievements")
@app_commands.guild_only()
async def achievements(interaction: discord.Interaction):
    """List unlocked achievements"""
    daily_stats = await bot.xp_system.get_daily_stats(str(interaction.user.id))
    unlocked = daily_stats["achievements"]

    if not unlocked:
        embed = discord.Embed(
            description="You haven't unlocked any achievements yet. Keep being active!",
            color=discord.Color.blurple()
        )
        embed.set_author(name=f"{interaction.user.display_name}'s Achievements", icon_url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)
        return

    embed = discord.Embed(
        title=f"🏅 {interaction.user.display_name}'s Achievements",
        description="\n".join(unlocked),
        color=discord.Color.gold(),
        timestamp=datetime.now(timezone.utc)
    )
    embed.set_footer(text=f"{len(unlocked)} unlocked")
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="doublexp", description="Activate a double XP day (Admin only)")
@app_commands.describe(date="Date in UTC, format YYYY-MM-DD", multiplier="Multiplier, e.g. 2.0 for double")
@app_commands.guild_only()
async def doublexp(interaction: discord.Interaction, date: str, multiplier: float = 2.0):
    """Admin-only: set a double XP day, persisted to disk"""
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Only administrators can use this command.", ephemeral=True)
        return

    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        await interaction.response.send_message("❌ Invalid date format. Use YYYY-MM-DD.", ephemeral=True)
        return

    if multiplier <= 0 or multiplier > 10:
        await interaction.response.send_message("❌ Multiplier must be between 0.1 and 10.0.", ephemeral=True)
        return

    Config.DOUBLE_XP_EVENTS[date] = multiplier

    config_data = await bot.data_manager.load("config.json", {})
    config_data["double_xp_events"] = Config.DOUBLE_XP_EVENTS
    await bot.data_manager.save("config.json", config_data)

    await interaction.response.send_message(
        f"✅ Double XP activated for **{date}** at **{multiplier}x**."
    )
    await bot.logging_service.log_action(
        interaction.guild,
        f"⚡ **{interaction.user.mention}** activated double XP for {date} ({multiplier}x)",
        discord.Color.gold()
    )

@bot.tree.command(name="help", description="Show available commands")
@app_commands.guild_only()
async def help_command(interaction: discord.Interaction):
    """Show help menu"""
    commands_by_category = {
        "🎮 XP & Levels": ["/daily", "/rank", "/stats", "/achievements", "/leaderboard", "/doublexp"],
        "🔨 Moderation": ["/fakeban", "/lockdown", "/unlock", "/move", "/mass-move", "/strip-roles", "/restore-roles"],
        "📝 Utility": ["/say", "/edit", "/help", "/voice-xp-logging"]
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
