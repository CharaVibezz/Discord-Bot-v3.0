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
    
    TOKEN = os.getenv("TOKEN")
    if not TOKEN:
        raise ValueError("Missing TOKEN environment variable")
    
    DATA_DIR = Path(os.getenv("DATA_DIR", "./data"))
    DATA_DIR.mkdir(exist_ok=True)
    
    # Channel IDs – UPDATE THESE!
    TARGET_CHANNEL_ID = 1525220657560817766
    LOG_CHANNEL_ID = 1525377874868305940
    LEVEL_UP_CHANNEL_ID = 1525392046989246525
    
    # Role IDs
    STAFF_ROLES = {
        1478214213292785825,
        1478212575908073482,
        1478212776588607670,
    }
    FAKEBAN_ALLOWED_ROLES = STAFF_ROLES | {1478127021828604075}
    SELFDESTRUCT_ALLOWED_ROLES = {
        1478210342524944447,
        1478238526330900552
    }
    
    VERIFIED_ROLE_ID = 1478213211307380838
    UNVERIFIED_ROLE_ID = 1478213220408889384
    
    # XP Settings – NO CAPS
    LEVEL_A = 35
    LEVEL_B = 120
    LEVEL_C = 200
    
    VOICE_RATE_OPTIMAL = 20
    VOICE_RATE_MODERATE = 12
    VOICE_RATE_MINIMAL = 6
    VOICE_MINIMUM_MINUTES = 1
    VOICE_XP_COOLDOWN_SECONDS = 300
    
    ACTIVITY_BONUS = 25
    STREAK_BONUS_PER_DAY = 5
    STREAK_BONUS_MAX = 50
    STREAK_THRESHOLD = 3
    CONTENT_CREATOR_BONUS = 30
    
    MESSAGE_XP_SHORT = (10, 20)
    MESSAGE_XP_MEDIUM = (25, 35)
    MESSAGE_XP_LONG = (35, 50)
    MESSAGE_XP_COOLDOWN_SECONDS = 30
    
    # Special events – date -> multiplier
    DOUBLE_XP_EVENTS = {}
    
    VOICE_XP_DETAILED_LOGGING = True
    WARNING_EXPIRY_DAYS = 30
    
    WARNING_FILE = DATA_DIR / "warnings.json"
    LEVELS_FILE = DATA_DIR / "levels.json"
    VOICE_SESSIONS_FILE = DATA_DIR / "voice_sessions.json"
    
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    
    @classmethod
    def validate(cls, guild: discord.Guild) -> List[str]:
        errors = []
        for cid in (cls.LOG_CHANNEL_ID, cls.TARGET_CHANNEL_ID, cls.LEVEL_UP_CHANNEL_ID):
            if not guild.get_channel(cid):
                errors.append(f"Channel {cid} not found")
        for rid in cls.STAFF_ROLES:
            if not guild.get_role(rid):
                errors.append(f"Staff role {rid} not found")
        return errors
    
    @classmethod
    def is_double_xp(cls) -> float:
        today = datetime.now(timezone.utc).date().isoformat()
        return cls.DOUBLE_XP_EVENTS.get(today, 1.0)
    
    @classmethod
    def get_all_events(cls) -> Dict[str, float]:
        return dict(sorted(cls.DOUBLE_XP_EVENTS.items()))
    
    @classmethod
    def add_event(cls, date: str, multiplier: float):
        cls.DOUBLE_XP_EVENTS[date] = multiplier
    
    @classmethod
    def remove_event(cls, date: str) -> bool:
        if date in cls.DOUBLE_XP_EVENTS:
            del cls.DOUBLE_XP_EVENTS[date]
            return True
        return False
    
    @classmethod
    def clear_events(cls):
        cls.DOUBLE_XP_EVENTS.clear()

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
    def __init__(self, data_dir: Path):
        self.data_dir = data_dir
        self._cache: Dict[str, Any] = {}
        self._lock = asyncio.Lock()
    
    async def load(self, filename: str, default: Any = None) -> Any:
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
                    backup = filepath.with_suffix('.json.bak')
                    filepath.rename(backup)
                    logger.warning(f"Corrupted {filename} backed up to {backup}")
                self._save_sync(filename, data)
            self._cache[filename] = data
            return data
    
    async def save(self, filename: str, data: Any):
        async with self._lock:
            self._cache[filename] = data
            self._save_sync(filename, data)
    
    def _save_sync(self, filename: str, data: Any):
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
        async with self._lock:
            self._cache.clear()

# ==========================
# XP System (with all fixes)
# ==========================

class XPSystem:
    def __init__(self, data_manager: DataManager):
        self.data_manager = data_manager
        self.voice_activity: Dict[str, Dict[str, Any]] = {}
        self.speaking_users: Dict[str, datetime] = {}
        self.last_voice_xp_time: Dict[str, datetime] = {}
        self._voice_activity_lock = asyncio.Lock()
        self._speaking_users_lock = asyncio.Lock()
        self._last_xp_lock = asyncio.Lock()
    
    @staticmethod
    def xp_for_level(level: int) -> int:
        return Config.LEVEL_A * (level ** 2) + Config.LEVEL_B * level + Config.LEVEL_C
    
    @staticmethod
    def get_level_from_xp(total_xp: int) -> Tuple[int, int]:
        level = 0
        remaining = total_xp
        while True:
            needed = XPSystem.xp_for_level(level)
            if remaining < needed:
                break
            remaining -= needed
            level += 1
        return level, remaining
    
    def calculate_voice_xp(self, member_id: str, elapsed_seconds: float, was_active: bool = False, streak_days: int = 0) -> Tuple[int, Dict[str, Any]]:
        minutes = elapsed_seconds / 60
        if minutes < 1:
            return 0, {"minutes": minutes, "reason": "too_short"}
        
        if minutes <= 30:
            base_rate = Config.VOICE_RATE_OPTIMAL
            base_xp = int(minutes * base_rate)
            tier = "optimal"
        elif minutes <= 60:
            base_rate = Config.VOICE_RATE_MODERATE
            base_xp = int(30 * Config.VOICE_RATE_OPTIMAL + (minutes - 30) * base_rate)
            tier = "moderate"
        else:
            base_rate = Config.VOICE_RATE_MINIMAL
            base_xp = int(30 * Config.VOICE_RATE_OPTIMAL + 30 * Config.VOICE_RATE_MODERATE + (minutes - 60) * base_rate)
            tier = "minimal"
        
        content_creator_bonus = 0
        if minutes >= 90 and was_active:
            content_creator_bonus = Config.CONTENT_CREATOR_BONUS * 2
        elif minutes >= 60 and was_active:
            content_creator_bonus = Config.CONTENT_CREATOR_BONUS
        
        activity_bonus = Config.ACTIVITY_BONUS if was_active else 0
        streak_bonus = 0
        if streak_days >= Config.STREAK_THRESHOLD:
            streak_bonus = min(streak_days * Config.STREAK_BONUS_PER_DAY, Config.STREAK_BONUS_MAX)
        
        total_xp = base_xp + activity_bonus + content_creator_bonus + streak_bonus
        double_multiplier = Config.is_double_xp()
        total_xp = int(total_xp * double_multiplier)
        
        breakdown = {
            "minutes": minutes,
            "tier": tier,
            "base_rate": base_rate,
            "base_xp": base_xp,
            "activity_bonus": activity_bonus,
            "content_creator_bonus": content_creator_bonus,
            "streak_bonus": streak_bonus,
            "streak_days": streak_days,
            "double_xp": double_multiplier,
            "total_xp": total_xp,
            "was_active": was_active,
            "capped": False
        }
        return total_xp, breakdown
    
    def _calculate_voice_streak(self, member_id: str, levels_data: Dict) -> int:
        user_data = levels_data.get(member_id, {})
        daily_voice = user_data.get("daily_voice_xp", {})
        if not daily_voice:
            return 0
        today = datetime.now(timezone.utc).date()
        streak = 0
        for i in range(30):
            check_date = (today - timedelta(days=i)).isoformat()
            if check_date in daily_voice and daily_voice[check_date] > 50:
                streak += 1
            else:
                break
        return streak
    
    async def add_voice_xp(self, member: discord.Member, elapsed_seconds: float, was_active: bool = False) -> Dict[str, Any]:
        user_id = str(member.id)
        now = datetime.now(timezone.utc)
        async with self._last_xp_lock:
            last_time = self.last_voice_xp_time.get(user_id)
        if last_time:
            cooldown_remaining = Config.VOICE_XP_COOLDOWN_SECONDS - (now - last_time).total_seconds()
            if cooldown_remaining > 0:
                return {
                    "xp_gained": 0,
                    "leveled_up": False,
                    "old_level": None,
                    "new_level": None,
                    "total_xp": None,
                    "breakdown": {"reason": "cooldown", "cooldown_remaining": cooldown_remaining},
                    "capped": False
                }
        levels = await self.data_manager.load("levels.json", {})
        streak_days = self._calculate_voice_streak(user_id, levels)
        xp_gained, breakdown = self.calculate_voice_xp(user_id, elapsed_seconds, was_active, streak_days)
        result = {
            "xp_gained": xp_gained,
            "leveled_up": False,
            "old_level": None,
            "new_level": None,
            "total_xp": None,
            "breakdown": breakdown,
            "capped": False
        }
        if xp_gained <= 0:
            return result
        if user_id not in levels:
            levels[user_id] = {
                "xp": 0,
                "last_message_time": None,
                "daily_voice_xp": {},
                "daily_message_xp": {},
                "voice_days": [],
                "total_voice_time": 0,
                "total_messages": 0,
                "achievements": []
            }
        entry = levels[user_id]
        today = datetime.now(timezone.utc).date().isoformat()
        if "daily_voice_xp" not in entry:
            entry["daily_voice_xp"] = {}
        entry["daily_voice_xp"][today] = entry["daily_voice_xp"].get(today, 0) + xp_gained
        entry["total_voice_time"] = entry.get("total_voice_time", 0) + elapsed_seconds
        if "voice_days" not in entry:
            entry["voice_days"] = []
        if today not in entry["voice_days"]:
            entry["voice_days"].append(today)
            entry["voice_days"] = entry["voice_days"][-30:]
        old_level, _ = self.get_level_from_xp(entry["xp"])
        entry["xp"] += xp_gained
        new_level, _ = self.get_level_from_xp(entry["xp"])
        await self.data_manager.save("levels.json", levels)
        async with self._last_xp_lock:
            self.last_voice_xp_time[user_id] = now
        result.update({
            "leveled_up": new_level > old_level,
            "old_level": old_level,
            "new_level": new_level,
            "total_xp": entry["xp"],
        })
        return result
    
    async def award_message_xp(self, message: discord.Message) -> bool:
        if not message.guild or message.author.bot:
            return False
        levels = await self.data_manager.load("levels.json", {})
        user_id = str(message.author.id)
        if user_id not in levels:
            levels[user_id] = {
                "xp": 0,
                "last_message_time": None,
                "daily_voice_xp": {},
                "daily_message_xp": {},
                "voice_days": [],
                "total_voice_time": 0,
                "total_messages": 0,
                "achievements": []
            }
        entry = levels[user_id]
        now = datetime.now(timezone.utc)
        today = now.date().isoformat()
        if entry.get("last_message_time"):
            try:
                last_time = datetime.fromisoformat(entry["last_message_time"])
                if (now - last_time).total_seconds() < Config.MESSAGE_XP_COOLDOWN_SECONDS:
                    return False
            except ValueError:
                pass
        if "daily_message_xp" not in entry:
            entry["daily_message_xp"] = {}
        content_length = len(message.content.strip())
        word_count = len(message.content.split())
        has_links = "http" in message.content.lower()
        is_reply = message.reference is not None
        quality_bonus = 0
        if word_count > 20:
            quality_bonus += 5
        if has_links:
            quality_bonus += 3
        if is_reply:
            quality_bonus += 2
        if content_length < 10:
            base_min, base_max = Config.MESSAGE_XP_SHORT
            base_xp = random.randint(base_min, base_max)
        elif content_length < 50:
            base_min, base_max = Config.MESSAGE_XP_MEDIUM
            base_xp = random.randint(base_min, base_max)
        else:
            base_min, base_max = Config.MESSAGE_XP_LONG
            base_xp = random.randint(base_min, base_max)
        gained = base_xp + quality_bonus
        double_multiplier = Config.is_double_xp()
        gained = int(gained * double_multiplier)
        entry["last_message_time"] = now.isoformat()
        entry["daily_message_xp"][today] = entry["daily_message_xp"].get(today, 0) + gained
        entry["total_messages"] = entry.get("total_messages", 0) + 1
        old_level, _ = self.get_level_from_xp(entry["xp"])
        entry["xp"] += gained
        new_level, _ = self.get_level_from_xp(entry["xp"])
        await self.data_manager.save("levels.json", levels)
        return new_level > old_level
    
    async def get_rank(self, user_id: str) -> Tuple[int, int, int]:
        levels = await self.data_manager.load("levels.json", {})
        entry = levels.get(user_id, {"xp": 0})
        xp = entry["xp"]
        level, _ = self.get_level_from_xp(xp)
        sorted_users = sorted(levels.items(), key=lambda kv: kv[1].get("xp", 0), reverse=True)
        rank = next((i for i, (uid, _) in enumerate(sorted_users, 1) if uid == user_id), None)
        return rank, level, xp
    
    async def get_daily_stats(self, user_id: str) -> Dict[str, Any]:
        levels = await self.data_manager.load("levels.json", {})
        entry = levels.get(user_id, {})
        today = datetime.now(timezone.utc).date().isoformat()
        streak = self._calculate_voice_streak(user_id, levels)
        return {
            "daily_voice_xp": entry.get("daily_voice_xp", {}).get(today, 0),
            "daily_message_xp": entry.get("daily_message_xp", {}).get(today, 0),
            "voice_streak": streak,
            "total_xp": entry.get("xp", 0),
            "total_voice_time": entry.get("total_voice_time", 0),
            "total_messages": entry.get("total_messages", 0),
            "achievements": entry.get("achievements", [])
        }
    
    async def check_achievements(self, member: discord.Member) -> List[str]:
        user_id = str(member.id)
        levels = await self.data_manager.load("levels.json", {})
        entry = levels.get(user_id, {})
        if "achievements" not in entry:
            entry["achievements"] = []
        new_achievements = []
        total_voice_hours = entry.get("total_voice_time", 0) / 3600
        if total_voice_hours >= 10 and "voice_veteran" not in entry["achievements"]:
            new_achievements.append("🎙️ Voice Veteran (10 hours)")
        if total_voice_hours >= 50 and "voice_master" not in entry["achievements"]:
            new_achievements.append("🎙️ Voice Master (50 hours)")
        if total_voice_hours >= 100 and "voice_legend" not in entry["achievements"]:
            new_achievements.append("🎙️ Voice Legend (100 hours)")
        if total_voice_hours >= 500 and "voice_god" not in entry["achievements"]:
            new_achievements.append("🎙️ Voice God (500 hours)")
        if total_voice_hours >= 1000 and "voice_immortal" not in entry["achievements"]:
            new_achievements.append("🎙️ Voice Immortal (1000 hours)")
        streak = self._calculate_voice_streak(user_id, levels)
        if streak >= 7 and "weekly_warrior" not in entry["achievements"]:
            new_achievements.append("🔥 Weekly Warrior (7-day streak)")
        if streak >= 30 and "monthly_monster" not in entry["achievements"]:
            new_achievements.append("🔥 Monthly Monster (30-day streak)")
        if streak >= 100 and "streak_legend" not in entry["achievements"]:
            new_achievements.append("🔥 Streak Legend (100-day streak)")
        total_messages = entry.get("total_messages", 0)
        if total_messages >= 100 and "chatty_cathy" not in entry["achievements"]:
            new_achievements.append("💬 Chatty Cathy (100 messages)")
        if total_messages >= 1000 and "message_master" not in entry["achievements"]:
            new_achievements.append("💬 Message Master (1000 messages)")
        if total_messages >= 10000 and "message_legend" not in entry["achievements"]:
            new_achievements.append("💬 Message Legend (10,000 messages)")
        _, level, _ = await self.get_rank(user_id)
        if level >= 25 and "level_enthusiast" not in entry["achievements"]:
            new_achievements.append("⭐ Level Enthusiast (Level 25)")
        if level >= 50 and "level_expert" not in entry["achievements"]:
            new_achievements.append("⭐ Level Expert (Level 50)")
        if level >= 100 and "level_legend" not in entry["achievements"]:
            new_achievements.append("⭐ Level Legend (Level 100)")
        if level >= 200 and "level_god" not in entry["achievements"]:
            new_achievements.append("⭐ Level God (Level 200)")
        if level >= 500 and "level_immortal" not in entry["achievements"]:
            new_achievements.append("⭐ Level Immortal (Level 500)")
        if new_achievements:
            entry["achievements"].extend(new_achievements)
            await self.data_manager.save("levels.json", levels)
        return new_achievements

# ==========================
# Logging Service
# ==========================

class LoggingService:
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._retry_count = 3
        self._retry_delay = 1
    
    async def log_action(self, guild: discord.Guild, description: str,
                          color: discord.Color = discord.Color.blurple(),
                          embed_kwargs: Optional[Dict] = None):
        channel = guild.get_channel(Config.LOG_CHANNEL_ID)
        if not channel:
            logger.error(f"Log channel {Config.LOG_CHANNEL_ID} not found")
            return
        embed_kwargs = embed_kwargs or {}
        embed = discord.Embed(description=description, color=color,
                              timestamp=datetime.now(timezone.utc), **embed_kwargs)
        for attempt in range(self._retry_count):
            try:
                await channel.send(embed=embed)
                return
            except (discord.Forbidden, discord.HTTPException) as e:
                if attempt == self._retry_count - 1:
                    logger.error(f"Failed to log action after {self._retry_count} attempts: {e}")
                else:
                    await asyncio.sleep(self._retry_delay * (attempt + 1))
    
    async def announce_level_up(self, guild: discord.Guild, member: discord.Member,
                                 new_level: int, fallback_channel: discord.TextChannel = None):
        channel = guild.get_channel(Config.LEVEL_UP_CHANNEL_ID) or fallback_channel or guild.get_channel(Config.LOG_CHANNEL_ID)
        if channel:
            embed = discord.Embed(
                description=f"{member.mention} just hit **level {new_level}**! 🎉",
                color=discord.Color.gold()
            )
            embed.set_author(name=f"{member.display_name} leveled up!", icon_url=member.display_avatar.url)
            embed.set_thumbnail(url=member.display_avatar.url)
            achievements = await bot.xp_system.check_achievements(member)
            if achievements:
                embed.add_field(name="🏆 Achievements Unlocked", value="\n".join(achievements[:5]), inline=False)
            try:
                await channel.send(embed=embed)
            except Exception as e:
                logger.warning(f"Failed to send level-up announcement: {e}")
        await self.log_action(guild, f"⬆️ **{member.mention}** leveled up to **level {new_level}**", discord.Color.gold())
    
    async def log_voice_xp(self, guild: discord.Guild, member: discord.Member, result: Dict[str, Any]):
        breakdown = result["breakdown"]
        if breakdown.get("reason") == "cooldown":
            remaining = breakdown.get("cooldown_remaining", 0)
            await self.log_action(guild, f"⏳ **{member.mention}** voice XP on cooldown ({remaining:.0f}s remaining)", discord.Color.greyple())
            return
        minutes = breakdown.get("minutes", 0)
        tier = breakdown.get("tier", "unknown")
        base_xp = breakdown.get("base_xp", 0)
        activity_bonus = breakdown.get("activity_bonus", 0)
        content_creator_bonus = breakdown.get("content_creator_bonus", 0)
        streak_bonus = breakdown.get("streak_bonus", 0)
        streak_days = breakdown.get("streak_days", 0)
        double_xp = breakdown.get("double_xp", 1.0)
        lines = [
            f"🎧 **{member.mention}** — {minutes:.1f} min in voice → +{result['xp_gained']} XP",
            f"`{base_xp} base ({tier} tier) + {activity_bonus} active + {content_creator_bonus} creator`",
            f"total XP: **{result['total_xp']:,}** — level {result['old_level']} → {result['new_level']}"
        ]
        if streak_days >= Config.STREAK_THRESHOLD:
            lines.append(f"🔥 {streak_days}-day streak! (+{streak_bonus} bonus)")
        if double_xp > 1.0:
            lines.append(f"⚡ DOUBLE XP EVENT! ({double_xp}x multiplier)")
        await self.log_action(guild, "\n".join(lines), discord.Color.teal())

# ==========================
# Permission Service
# ==========================

class PermissionService:
    @staticmethod
    def has_staff_role(member: discord.Member) -> bool:
        return any(role.id in Config.STAFF_ROLES for role in member.roles)
    
    @staticmethod
    def can_use_fakeban(member: discord.Member) -> bool:
        return any(role.id in Config.FAKEBAN_ALLOWED_ROLES for role in member.roles)
    
    @staticmethod
    def can_use_selfdestruct(member: discord.Member) -> bool:
        return any(role.id in Config.SELFDESTRUCT_ALLOWED_ROLES for role in member.roles)
    
    @staticmethod
    def is_admin(member: discord.Member) -> bool:
        return member.guild_permissions.administrator
    
    @staticmethod
    async def require_staff(interaction: discord.Interaction) -> bool:
        if PermissionService.has_staff_role(interaction.user):
            return True
        await interaction.response.send_message("❌ You don't have permission for that.", ephemeral=True)
        return False

# ==========================
# Discord Bot
# ==========================

class ModBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        intents.voice_states = True
        super().__init__(command_prefix="!", intents=intents, help_command=None)
        self.data_manager = DataManager(Config.DATA_DIR)
        self.xp_system = XPSystem(self.data_manager)
        self.logging_service = LoggingService(self)
        self.permission_service = PermissionService()
        self.voice_sessions: Dict[str, Dict[str, Any]] = {}
        self._voice_sessions_lock = asyncio.Lock()
    
    async def setup_hook(self):
        self.voice_cleanup.start()
        self.verified_role_sweep.start()
        try:
            await self.tree.sync()
            logger.info("Slash commands synced")
        except Exception as e:
            logger.error(f"Failed to sync commands: {e}")
    
    async def on_ready(self):
        logger.info(f"Logged in as {self.user} (ID: {self.user.id})")
        config = await self.data_manager.load("config.json", {})
        if "double_xp_events" in config:
            Config.DOUBLE_XP_EVENTS.update(config["double_xp_events"])
            logger.info(f"Loaded {len(config['double_xp_events'])} double XP events")
        for guild in self.guilds:
            errors = Config.validate(guild)
            if errors:
                logger.warning(f"Configuration errors in {guild.name}:")
                for error in errors:
                    logger.warning(f"  - {error}")
        sessions = await self.data_manager.load("voice_sessions.json", {})
        async with self._voice_sessions_lock:
            for user_id, data in sessions.items():
                try:
                    if isinstance(data, str):
                        start_time = datetime.fromisoformat(data)
                        self.voice_sessions[user_id] = {"start_time": start_time, "was_active": False, "speaking_time": 0}
                    else:
                        start_time = datetime.fromisoformat(data["start_time"])
                        self.voice_sessions[user_id] = {
                            "start_time": start_time,
                            "was_active": data.get("was_active", False),
                            "speaking_time": data.get("speaking_time", 0)
                        }
                except Exception as e:
                    logger.warning(f"Invalid voice session for {user_id}: {e}")
        logger.info(f"Bot is online in {len(self.guilds)} guilds")
        for guild in self.guilds:
            await self.sweep_verified_roles(guild)
    
    async def sync_verified_role(self, member: discord.Member) -> bool:
        if not Config.VERIFIED_ROLE_ID or not Config.UNVERIFIED_ROLE_ID:
            return False
        role_ids = {r.id for r in member.roles}
        if Config.VERIFIED_ROLE_ID not in role_ids or Config.UNVERIFIED_ROLE_ID not in role_ids:
            return False
        unverified_role = member.guild.get_role(Config.UNVERIFIED_ROLE_ID)
        if not unverified_role:
            logger.warning(f"Unverified role {Config.UNVERIFIED_ROLE_ID} not found in {member.guild.name}")
            return False
        try:
            await member.remove_roles(unverified_role, reason="Verified role present — removing unverified")
        except Exception as e:
            logger.error(f"Failed to remove unverified role from {member}: {e}")
            return False
        logger.info(f"Removed unverified from {member} ({member.id}) in {member.guild.name}")
        return True
    
    async def sweep_verified_roles(self, guild: discord.Guild):
        if not Config.VERIFIED_ROLE_ID or not Config.UNVERIFIED_ROLE_ID:
            return
        verified_role = guild.get_role(Config.VERIFIED_ROLE_ID)
        if not verified_role:
            return
        cleaned = 0
        for member in verified_role.members:
            if any(r.id == Config.UNVERIFIED_ROLE_ID for r in member.roles):
                if await self.sync_verified_role(member):
                    cleaned += 1
        if cleaned:
            logger.info(f"Verified-role sweep cleaned {cleaned} member(s) in {guild.name}")
    
    def format_progress_bar(self, progress: int, needed: int, length: int = 12) -> str:
        if needed <= 0:
            return "░" * length
        filled = min(length, int(length * progress / needed))
        return "█" * filled + "░" * (length - filled)
    
    @tasks.loop(minutes=5)
    async def voice_cleanup(self):
        try:
            await self.wait_until_ready()
            now = datetime.now(timezone.utc)
            active_users = set()
            for guild in self.guilds:
                afk_channel = guild.afk_channel
                for vc in guild.voice_channels:
                    if vc == afk_channel:
                        continue
                    for member in vc.members:
                        if not member.bot:
                            active_users.add(str(member.id))
            async with self._voice_sessions_lock:
                stale = [uid for uid in self.voice_sessions if uid not in active_users]
                for uid in stale:
                    del self.voice_sessions[uid]
            async with self.xp_system._voice_activity_lock:
                stale_act = [uid for uid in self.xp_system.voice_activity if uid not in active_users]
                for uid in stale_act:
                    del self.xp_system.voice_activity[uid]
            async with self.xp_system._speaking_users_lock:
                stale_speak = [uid for uid in self.xp_system.speaking_users if uid not in active_users]
                for uid in stale_speak:
                    del self.xp_system.speaking_users[uid]
            if stale or stale_act or stale_speak:
                logger.info(f"Cleaned up {len(stale)} sessions, {len(stale_act)} activity, {len(stale_speak)} speaking")
                async with self._voice_sessions_lock:
                    save_data = {
                        uid: {
                            "start_time": data["start_time"].isoformat(),
                            "was_active": data["was_active"],
                            "speaking_time": data["speaking_time"]
                        }
                        for uid, data in self.voice_sessions.items()
                    }
                await self.data_manager.save("voice_sessions.json", save_data)
        except Exception as e:
            logger.error(f"Voice cleanup failed: {e}")
    
    @voice_cleanup.before_loop
    async def before_voice_cleanup(self):
        await self.wait_until_ready()
    
    @tasks.loop(minutes=10)
    async def verified_role_sweep(self):
        try:
            await self.wait_until_ready()
            for guild in self.guilds:
                await self.sweep_verified_roles(guild)
        except Exception as e:
            logger.error(f"Verified role sweep failed: {e}")
    
    @verified_role_sweep.before_loop
    async def before_verified_role_sweep(self):
        await self.wait_until_ready()

bot = ModBot()

# ==========================
# Event Handlers
# ==========================

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    leveled_up = await bot.xp_system.award_message_xp(message)
    if leveled_up:
        _, new_level, _ = await bot.xp_system.get_rank(str(message.author.id))
        await bot.logging_service.announce_level_up(message.guild, message.author, new_level, message.channel)
    if bot.permission_service.has_staff_role(message.author):
        return
    if message.channel.id != Config.TARGET_CHANNEL_ID:
        return
    await handle_forbidden_message(message)

async def handle_forbidden_message(message: discord.Message):
    user_id = str(message.author.id)
    now = datetime.now(timezone.utc)
    try:
        await message.delete()
    except discord.Forbidden:
        logger.warning(f"Missing Manage Messages permission in {message.channel}")
        return
    warnings = await bot.data_manager.load("warnings.json", {})
    last_warning = warnings.get(user_id)
    warning_valid = False
    if last_warning:
        try:
            last_time = datetime.fromisoformat(last_warning)
            warning_valid = (now - last_time).total_seconds() < (Config.WARNING_EXPIRY_DAYS * 86400)
        except ValueError:
            pass
    if not warning_valid:
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
    if before.roles == after.roles:
        return
    before_ids = {r.id for r in before.roles}
    after_ids = {r.id for r in after.roles}
    verified_just_added = (Config.VERIFIED_ROLE_ID in after_ids and Config.VERIFIED_ROLE_ID not in before_ids)
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
async def on_speaking_update(member: discord.Member, speaking: bool):
    if member.bot:
        return
    user_id = str(member.id)
    async with bot.xp_system._voice_activity_lock:
        if user_id in bot.xp_system.voice_activity:
            if speaking:
                bot.xp_system.voice_activity[user_id]["was_active"] = True
                bot.xp_system.voice_activity[user_id]["speaking_time"] += 1
                async with bot._voice_sessions_lock:
                    if user_id in bot.voice_sessions:
                        bot.voice_sessions[user_id]["was_active"] = True
                        bot.voice_sessions[user_id]["speaking_time"] += 1
    async with bot.xp_system._speaking_users_lock:
        if speaking:
            bot.xp_system.speaking_users[user_id] = datetime.now(timezone.utc)
        else:
            if user_id in bot.xp_system.speaking_users:
                del bot.xp_system.speaking_users[user_id]

@bot.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    if member.bot:
        return
    guild = member.guild
    afk_channel = guild.afk_channel
    before_counts = before.channel and before.channel != afk_channel
    after_counts = after.channel and after.channel != afk_channel
    now = datetime.now(timezone.utc)
    if after_counts and not before_counts:
        async with bot._voice_sessions_lock:
            bot.voice_sessions[str(member.id)] = {"start_time": now, "was_active": False, "speaking_time": 0}
        async with bot.xp_system._voice_activity_lock:
            bot.xp_system.voice_activity[str(member.id)] = {"start_time": now, "last_check": now, "was_active": False, "speaking_time": 0}
        logger.info(f"Voice session start: {member} ({member.id}) in {guild.name} at {now.isoformat()}")
        async with bot._voice_sessions_lock:
            save_data = {
                uid: {
                    "start_time": data["start_time"].isoformat(),
                    "was_active": data["was_active"],
                    "speaking_time": data["speaking_time"]
                }
                for uid, data in bot.voice_sessions.items()
            }
        await bot.data_manager.save("voice_sessions.json", save_data)
    elif before_counts and not after_counts:
        user_id = str(member.id)
        session_data = None
        async with bot._voice_sessions_lock:
            session_data = bot.voice_sessions.pop(user_id, None)
        if session_data:
            start_time = session_data["start_time"]
            elapsed = (now - start_time).total_seconds()
            was_active = session_data["was_active"]
            speaking_time = session_data["speaking_time"]
            async with bot.xp_system._voice_activity_lock:
                bot.xp_system.voice_activity.pop(user_id, None)
            async with bot.xp_system._speaking_users_lock:
                if user_id in bot.xp_system.speaking_users:
                    del bot.xp_system.speaking_users[user_id]
            if elapsed > 60:
                result = await bot.xp_system.add_voice_xp(member, elapsed, was_active)
                breakdown = result["breakdown"]
                if breakdown.get("reason") == "cooldown":
                    logger.info(f"Voice XP on cooldown: {member} ({member.id}) – {breakdown.get('cooldown_remaining', 0):.0f}s remaining")
                    if Config.VOICE_XP_DETAILED_LOGGING:
                        await bot.logging_service.log_voice_xp(guild, member, result)
                else:
                    logger.info(f"Voice XP award: {member} ({member.id}) – {breakdown.get('minutes', 0):.1f} min, +{result['xp_gained']} XP, level {result['old_level']}->{result['new_level']}")
                    if Config.VOICE_XP_DETAILED_LOGGING:
                        await bot.logging_service.log_voice_xp(guild, member, result)
                    if result["leveled_up"]:
                        await bot.logging_service.announce_level_up(guild, member, result["new_level"])
            else:
                logger.info(f"Voice XP skipped: {member} ({member.id}) – session only {elapsed:.0f}s, below 60s minimum")
            async with bot._voice_sessions_lock:
                save_data = {
                    uid: {
                        "start_time": data["start_time"].isoformat(),
                        "was_active": data["was_active"],
                        "speaking_time": data["speaking_time"]
                    }
                    for uid, data in bot.voice_sessions.items()
                }
            await bot.data_manager.save("voice_sessions.json", save_data)

# ==========================
# Slash Commands
# ==========================

# ---------- XP Event Group ----------
xp_group = app_commands.Group(name="xp", description="Manage double XP events")

@xp_group.command(name="add", description="Add a double XP event (Admin only)")
@app_commands.describe(date="Date in YYYY-MM-DD format", multiplier="Multiplier (e.g. 2.0 for double)")
@app_commands.guild_only()
async def xp_add(interaction: discord.Interaction, date: str, multiplier: float = 2.0):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Only administrators can use this command.", ephemeral=True)
        return
    try:
        dt = datetime.strptime(date, "%Y-%m-%d")
        if dt.date() < datetime.now(timezone.utc).date():
            await interaction.response.send_message("❌ Date cannot be in the past.", ephemeral=True)
            return
    except ValueError:
        await interaction.response.send_message("❌ Invalid date format. Use YYYY-MM-DD.", ephemeral=True)
        return
    if multiplier <= 0 or multiplier > 10:
        await interaction.response.send_message("❌ Multiplier must be between 0.1 and 10.0.", ephemeral=True)
        return
    Config.add_event(date, multiplier)
    config = await bot.data_manager.load("config.json", {})
    config["double_xp_events"] = Config.DOUBLE_XP_EVENTS
    await bot.data_manager.save("config.json", config)
    await interaction.response.send_message(f"✅ Double XP event added for **{date}** with **{multiplier}x** multiplier!")
    await bot.logging_service.log_action(
        interaction.guild,
        f"⚡ **{interaction.user.mention}** added double XP for {date} ({multiplier}x)",
        discord.Color.gold()
    )

@xp_group.command(name="list", description="List all upcoming double XP events")
@app_commands.guild_only()
async def xp_list(interaction: discord.Interaction):
    if not await bot.permission_service.require_staff(interaction):
        return
    events = Config.get_all_events()
    today = datetime.now(timezone.utc).date().isoformat()
    upcoming = {d: m for d, m in events.items() if d >= today}
    if not upcoming:
        await interaction.response.send_message("📅 No upcoming double XP events scheduled.")
        return
    embed = discord.Embed(
        title="📅 Upcoming Double XP Events",
        description="\n".join([f"**{d}** – {m}x multiplier" for d, m in upcoming.items()]),
        color=discord.Color.gold()
    )
    embed.set_footer(text="Events are in UTC timezone.")
    await interaction.response.send_message(embed=embed)

@xp_group.command(name="remove", description="Remove a double XP event (Admin only)")
@app_commands.describe(date="Date of the event to remove (YYYY-MM-DD)")
@app_commands.guild_only()
async def xp_remove(interaction: discord.Interaction, date: str):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Only administrators can use this command.", ephemeral=True)
        return
    try:
        datetime.strptime(date, "%Y-%m-%d")
    except ValueError:
        await interaction.response.send_message("❌ Invalid date format. Use YYYY-MM-DD.", ephemeral=True)
        return
    if Config.remove_event(date):
        config = await bot.data_manager.load("config.json", {})
        config["double_xp_events"] = Config.DOUBLE_XP_EVENTS
        await bot.data_manager.save("config.json", config)
        await interaction.response.send_message(f"✅ Removed double XP event for **{date}**.")
        await bot.logging_service.log_action(
            interaction.guild,
            f"🗑️ **{interaction.user.mention}** removed double XP for {date}",
            discord.Color.orange()
        )
    else:
        await interaction.response.send_message(f"❌ No event found for **{date}**.", ephemeral=True)

@xp_group.command(name="clear", description="Clear all double XP events (Admin only)")
@app_commands.guild_only()
async def xp_clear(interaction: discord.Interaction):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Only administrators can use this command.", ephemeral=True)
        return
    if not Config.DOUBLE_XP_EVENTS:
        await interaction.response.send_message("ℹ️ No events to clear.", ephemeral=True)
        return
    Config.clear_events()
    config = await bot.data_manager.load("config.json", {})
    config["double_xp_events"] = {}
    await bot.data_manager.save("config.json", config)
    await interaction.response.send_message("✅ All double XP events have been cleared.")
    await bot.logging_service.log_action(
        interaction.guild,
        f"🗑️ **{interaction.user.mention}** cleared all double XP events",
        discord.Color.red()
    )

@xp_group.command(name="today", description="Check if double XP is active today")
@app_commands.guild_only()
async def xp_today(interaction: discord.Interaction):
    if not await bot.permission_service.require_staff(interaction):
        return
    today = datetime.now(timezone.utc).date().isoformat()
    mult = Config.is_double_xp()
    if mult == 1.0:
        await interaction.response.send_message("📅 No double XP active today. Normal XP rates apply.")
    else:
        await interaction.response.send_message(f"⚡ **Double XP is active today!** ({mult}x multiplier)")

@xp_group.command(name="toggle", description="Toggle double XP on/off for today (Admin only)")
@app_commands.describe(multiplier="Multiplier when turning on (default 2.0)")
@app_commands.guild_only()
async def xp_toggle(interaction: discord.Interaction, multiplier: float = 2.0):
    if not interaction.user.guild_permissions.administrator:
        await interaction.response.send_message("❌ Only administrators can use this command.", ephemeral=True)
        return
    if multiplier <= 0 or multiplier > 10:
        await interaction.response.send_message("❌ Multiplier must be between 0.1 and 10.0.", ephemeral=True)
        return
    today = datetime.now(timezone.utc).date().isoformat()
    if today in Config.DOUBLE_XP_EVENTS:
        # Turn off
        Config.remove_event(today)
        config = await bot.data_manager.load("config.json", {})
        config["double_xp_events"] = Config.DOUBLE_XP_EVENTS
        await bot.data_manager.save("config.json", config)
        await interaction.response.send_message(f"❌ Double XP deactivated for today ({today}).")
        await bot.logging_service.log_action(
            interaction.guild,
            f"❌ **{interaction.user.mention}** toggled OFF double XP for today",
            discord.Color.orange()
        )
    else:
        # Turn on
        Config.add_event(today, multiplier)
        config = await bot.data_manager.load("config.json", {})
        config["double_xp_events"] = Config.DOUBLE_XP_EVENTS
        await bot.data_manager.save("config.json", config)
        await interaction.response.send_message(f"✅ Double XP activated for **today** ({today}) with **{multiplier}x** multiplier!")
        await bot.logging_service.log_action(
            interaction.guild,
            f"⚡ **{interaction.user.mention}** toggled ON double XP for today ({multiplier}x)",
            discord.Color.gold()
        )

# Add the group to the tree
bot.tree.add_command(xp_group)

# ---------- Legacy /doublexp ----------
@bot.tree.command(name="doublexp", description="[Legacy] Add double XP for a specific date (Admin only)")
@app_commands.describe(date="Date (YYYY-MM-DD)", multiplier="Multiplier (e.g., 2.0)")
@app_commands.guild_only()
async def doublexp_legacy(interaction: discord.Interaction, date: str, multiplier: float = 2.0):
    await xp_add(interaction, date, multiplier)

# ---------- Other Staff Commands ----------
@bot.tree.command(name="fakeban", description="Prank-ban a member (timeout, not a real ban)")
@app_commands.describe(member="The member to fake ban")
@app_commands.guild_only()
async def fakeban(interaction: discord.Interaction, member: discord.Member):
    if not bot.permission_service.can_use_fakeban(interaction.user):
        await interaction.response.send_message("❌ You cannot use this command.", ephemeral=True)
        return
    if bot.permission_service.is_admin(member):
        await interaction.response.send_message("❌ You cannot fakeban an administrator.", ephemeral=True)
        return
    await interaction.response.send_message(f"🔨 Preparing ban for {member.mention}...")
    for i in range(5, 0, -1):
        await interaction.edit_original_response(content=f"🔨 Preparing ban for {member.mention}\nExecuting in **{i}**...")
        await asyncio.sleep(1)
    try:
        await member.send("You've been BANNED!! 🤯🪦 (joke)")
    except Exception:
        pass
    try:
        await member.timeout(timedelta(seconds=10), reason="Fake ban prank")
    except Exception as e:
        logger.warning(f"Failed to timeout {member}: {e}")
    await interaction.edit_original_response(content=f"🔨 {member.mention} has been banned.\nReason: Breaking the rules.")
    await bot.logging_service.log_action(interaction.guild, f"🔨 **{interaction.user.mention}** fakebanned **{member.mention}**")

@bot.tree.command(name="lockdown", description="Prevent members from joining a voice channel")
@app_commands.describe(channel="The voice channel to lock")
@app_commands.guild_only()
async def lockdown(interaction: discord.Interaction, channel: discord.VoiceChannel):
    if not await bot.permission_service.require_staff(interaction):
        return
    try:
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        overwrite.connect = False
        await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"Voice lockdown by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message("❌ Missing permission to edit that channel.", ephemeral=True)
        return
    await interaction.response.send_message(f"🔒 {channel.mention} is locked. Use `/unlock` to reopen.")
    await bot.logging_service.log_action(interaction.guild, f"🔒 **{interaction.user.mention}** locked voice channel **{channel.mention}**", discord.Color.red())

@bot.tree.command(name="unlock", description="Allow members to join a previously locked voice channel")
@app_commands.describe(channel="The voice channel to unlock")
@app_commands.guild_only()
async def unlock(interaction: discord.Interaction, channel: discord.VoiceChannel):
    if not await bot.permission_service.require_staff(interaction):
        return
    try:
        overwrite = channel.overwrites_for(interaction.guild.default_role)
        overwrite.connect = None
        await channel.set_permissions(interaction.guild.default_role, overwrite=overwrite, reason=f"Voice unlock by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message("❌ Missing permission to edit that channel.", ephemeral=True)
        return
    await interaction.response.send_message(f"🔓 {channel.mention} is unlocked.")
    await bot.logging_service.log_action(interaction.guild, f"🔓 **{interaction.user.mention}** unlocked voice channel **{channel.mention}**", discord.Color.green())

@bot.tree.command(name="move", description="Move a member into a specified voice channel")
@app_commands.describe(member="The member to move", channel="The destination voice channel")
@app_commands.guild_only()
async def move(interaction: discord.Interaction, member: discord.Member, channel: discord.VoiceChannel):
    if not await bot.permission_service.require_staff(interaction):
        return
    if not member.voice or not member.voice.channel:
        await interaction.response.send_message(f"❌ {member.display_name} isn't in a voice channel.", ephemeral=True)
        return
    try:
        await member.move_to(channel, reason=f"Moved by {interaction.user}")
    except discord.Forbidden:
        await interaction.response.send_message("❌ Missing permission to move members.", ephemeral=True)
        return
    await interaction.response.send_message(f"✅ Moved {member.mention} to {channel.mention}")
    await bot.logging_service.log_action(interaction.guild, f"🔀 **{interaction.user.mention}** moved **{member.mention}** to **{channel.mention}**", discord.Color.blue())

# ---------- Public Commands ----------
@bot.tree.command(name="stats", description="View your XP statistics - NO CAPS")
@app_commands.guild_only()
async def stats(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    rank, level, xp = await bot.xp_system.get_rank(user_id)
    daily_stats = await bot.xp_system.get_daily_stats(user_id)
    next_level_xp = XPSystem.xp_for_level(level)
    current_level_xp = XPSystem.xp_for_level(level - 1) if level > 0 else 0
    progress = xp - current_level_xp
    needed = next_level_xp - current_level_xp
    daily_voice = daily_stats["daily_voice_xp"]
    daily_message = daily_stats["daily_message_xp"]
    embed = discord.Embed(title=f"📊 {interaction.user.display_name}'s Stats", color=discord.Color.blue(), timestamp=datetime.now(timezone.utc))
    embed.add_field(name="📈 Level & XP", value=f"**Level {level}**\nTotal XP: {xp:,}\nProgress: {progress:,}/{needed:,}\nRank: #{rank if rank else 'N/A'}", inline=True)
    embed.add_field(name="📅 Today's Activity", value=f"📝 Message XP: {daily_message:,}\n🎧 Voice XP: {daily_voice:,}\n🔥 Voice Streak: {daily_stats['voice_streak']} day(s)\n📊 Total Today: {daily_voice + daily_message:,}", inline=True)
    voice_hours = daily_stats.get("total_voice_time", 0) / 3600
    embed.add_field(name="🏆 Lifetime Stats", value=f"🎙️ Voice Time: {voice_hours:.1f} hours\n💬 Messages: {daily_stats.get('total_messages', 0):,}\n🏅 Achievements: {len(daily_stats.get('achievements', []))}", inline=True)
    if needed > 0:
        progress_bar = bot.format_progress_bar(progress, needed)
        embed.add_field(name="Progress", value=f"```\n{progress_bar}\n{progress:,}/{needed:,} XP to next level\n```", inline=False)
    achievements = daily_stats.get("achievements", [])
    if achievements:
        embed.add_field(name="🏅 Achievements", value="\n".join(achievements[:5]) + (f"\n...and {len(achievements) - 5} more" if len(achievements) > 5 else ""), inline=False)
    if Config.is_double_xp() > 1.0:
        embed.set_footer(text=f"⚡ DOUBLE XP EVENT ACTIVE! ({Config.is_double_xp()}x multiplier)")
    else:
        embed.set_footer(text="📈 No daily caps! Unlimited XP progression!")
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="View the server leaderboard")
@app_commands.describe(limit="Number of users to show (max 20)")
@app_commands.guild_only()
async def leaderboard(interaction: discord.Interaction, limit: int = 10):
    if limit < 1 or limit > 20:
        await interaction.response.send_message("❌ Limit must be between 1 and 20.", ephemeral=True)
        return
    levels = await bot.data_manager.load("levels.json", {})
    sorted_users = sorted(levels.items(), key=lambda kv: kv[1].get("xp", 0), reverse=True)[:limit]
    if not sorted_users:
        await interaction.response.send_message("No XP data yet.")
        return
    embed = discord.Embed(title="🏆 Server Leaderboard", color=discord.Color.gold(), timestamp=datetime.now(timezone.utc))
    lines = []
    for i, (user_id, data) in enumerate(sorted_users, 1):
        xp = data.get("xp", 0)
        level, _ = XPSystem.get_level_from_xp(xp)
        member = interaction.guild.get_member(int(user_id))
        name = member.display_name if member else f"User {user_id[:6]}"
        emoji = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        lines.append(f"{emoji} **{name}** — Lv.{level} ({xp:,} XP)")
    embed.description = "\n".join(lines)
    if sorted_users:
        top_user_id = sorted_users[0][0]
        top_member = interaction.guild.get_member(int(top_user_id))
        top_xp = sorted_users[0][1].get("xp", 0)
        top_level, _ = XPSystem.get_level_from_xp(top_xp)
        embed.set_footer(text=f"👑 {top_member.display_name if top_member else 'User'} is #1 at Level {top_level} with {top_xp:,} XP!")
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="achievements", description="View your unlocked achievements")
@app_commands.guild_only()
async def achievements(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    daily_stats = await bot.xp_system.get_daily_stats(user_id)
    achievements = daily_stats.get("achievements", [])
    if not achievements:
        embed = discord.Embed(title="🏅 Achievements", description="You haven't unlocked any achievements yet. Keep being active!", color=discord.Color.blurple())
        embed.set_author(name=interaction.user.display_name, icon_url=interaction.user.display_avatar.url)
        await interaction.response.send_message(embed=embed)
        return
    embed = discord.Embed(title=f"🏅 {interaction.user.display_name}'s Achievements", color=discord.Color.gold(), timestamp=datetime.now(timezone.utc))
    voice_ach = [a for a in achievements if "🎙️" in a or "🔥" in a]
    msg_ach = [a for a in achievements if "💬" in a]
    lvl_ach = [a for a in achievements if "⭐" in a]
    if voice_ach:
        embed.add_field(name="🎙️ Voice Achievements", value="\n".join(voice_ach), inline=False)
    if msg_ach:
        embed.add_field(name="💬 Message Achievements", value="\n".join(msg_ach), inline=False)
    if lvl_ach:
        embed.add_field(name="⭐ Level Achievements", value="\n".join(lvl_ach), inline=False)
    embed.set_footer(text=f"Total: {len(achievements)} achievements unlocked!")
    embed.set_thumbnail(url=interaction.user.display_avatar.url)
    await interaction.response.send_message(embed=embed)

# ==========================
# Bot Run
# ==========================

if __name__ == "__main__":
    try:
        bot.run(Config.TOKEN)
    except discord.LoginFailure:
        logger.error("Invalid bot token")
    except Exception as e:
        logger.error(f"Bot startup failed: {e}")
