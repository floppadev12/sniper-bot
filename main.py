import os
import asyncio
import logging
from datetime import datetime, timezone

import asyncpg
import discord
from discord import app_commands
from discord.ext import tasks
from dotenv import load_dotenv

from tiktok_source import get_latest_videos, get_video_stats, TikTokVideo


load_dotenv()

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
DISCORD_GUILD_ID = os.getenv("DISCORD_GUILD_ID")

CHECK_INTERVAL_HOURS = 1
DAILY_TRACKING_CHECK_HOURS = 24

FIXED_KEYWORD = "challenge"
VIEW_MILESTONE = 1_000_000
MAX_ACTIVE_TRACKED_VIDEOS = 100
MILESTONE_DESCRIPTION_LIMIT = 160

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)

logger = logging.getLogger("tiktok-challenge-bot")


class ChallengeBot(discord.Client):
    def __init__(self):
        intents = discord.Intents.default()
        super().__init__(intents=intents)

        self.tree = app_commands.CommandTree(self)
        self.db_pool: asyncpg.Pool | None = None

        self.creator_group = app_commands.Group(
            name="creator",
            description="Manage TikTok creators to monitor",
        )

    async def setup_hook(self):
        if not DATABASE_URL:
            raise RuntimeError("DATABASE_URL is missing.")

        self.db_pool = await asyncpg.create_pool(DATABASE_URL)
        await self.create_tables()

        self.register_commands()

        if DISCORD_GUILD_ID:
            guild = discord.Object(id=int(DISCORD_GUILD_ID))
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            logger.info("Slash commands synced to guild %s", DISCORD_GUILD_ID)
        else:
            await self.tree.sync()
            logger.info("Slash commands synced globally")

        self.hourly_checker.start()
        self.daily_tracker.start()

    async def close(self):
        if self.hourly_checker.is_running():
            self.hourly_checker.cancel()

        if self.daily_tracker.is_running():
            self.daily_tracker.cancel()

        if self.db_pool:
            await self.db_pool.close()

        await super().close()

    async def create_tables(self):
        assert self.db_pool is not None

        await self.db_pool.execute(
            """
            CREATE TABLE IF NOT EXISTS guild_settings (
                guild_id TEXT PRIMARY KEY,
                alert_channel_id TEXT,
                milestone_channel_id TEXT,
                daily_report_channel_id TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        await self.db_pool.execute(
            """
            CREATE TABLE IF NOT EXISTS creators (
                id SERIAL PRIMARY KEY,
                guild_id TEXT NOT NULL,
                username TEXT NOT NULL,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                UNIQUE (guild_id, username)
            );
            """
        )

        await self.db_pool.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_videos (
                id SERIAL PRIMARY KEY,
                guild_id TEXT NOT NULL,
                creator_username TEXT NOT NULL,
                video_id TEXT NOT NULL,
                video_url TEXT NOT NULL,
                description TEXT,
                posted_at TIMESTAMPTZ,
                detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                matched_keyword TEXT,
                alerted BOOLEAN NOT NULL DEFAULT FALSE,
                view_count BIGINT,
                tracking_status TEXT NOT NULL DEFAULT 'none',
                tracked_at TIMESTAMPTZ,
                archived_at TIMESTAMPTZ,
                reached_1m_at TIMESTAMPTZ,
                UNIQUE (guild_id, video_id)
            );
            """
        )

        # Safe migrations for existing databases.
        await self.db_pool.execute(
            """
            ALTER TABLE guild_settings
            ADD COLUMN IF NOT EXISTS milestone_channel_id TEXT;
            """
        )

        await self.db_pool.execute(
            """
            ALTER TABLE guild_settings
            ADD COLUMN IF NOT EXISTS daily_report_channel_id TEXT;
            """
        )

        await self.db_pool.execute(
            """
            ALTER TABLE seen_videos
            ADD COLUMN IF NOT EXISTS view_count BIGINT;
            """
        )

        await self.db_pool.execute(
            """
            ALTER TABLE seen_videos
            ADD COLUMN IF NOT EXISTS tracking_status TEXT NOT NULL DEFAULT 'none';
            """
        )

        await self.db_pool.execute(
            """
            ALTER TABLE seen_videos
            ADD COLUMN IF NOT EXISTS tracked_at TIMESTAMPTZ;
            """
        )

        await self.db_pool.execute(
            """
            ALTER TABLE seen_videos
            ADD COLUMN IF NOT EXISTS archived_at TIMESTAMPTZ;
            """
        )

        await self.db_pool.execute(
            """
            ALTER TABLE seen_videos
            ADD COLUMN IF NOT EXISTS reached_1m_at TIMESTAMPTZ;
            """
        )

    def register_commands(self):
        bot = self

        @self.tree.command(
            name="setchannel",
            description="Set the channel where TikTok challenge alerts will be sent",
        )
        @app_commands.describe(channel="The Discord channel for challenge alerts")
        async def setchannel(
            interaction: discord.Interaction,
            channel: discord.TextChannel,
        ):
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command must be used inside a Discord server.",
                    ephemeral=True,
                )
                return

            assert bot.db_pool is not None

            await bot.db_pool.execute(
                """
                INSERT INTO guild_settings (guild_id, alert_channel_id, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (guild_id)
                DO UPDATE SET alert_channel_id = $2, updated_at = NOW();
                """,
                str(interaction.guild.id),
                str(channel.id),
            )

            await interaction.response.send_message(
                f"✅ TikTok challenge alerts will be sent to {channel.mention}.",
                ephemeral=True,
            )

        @self.tree.command(
            name="setmilestonechannel",
            description="Set the channel where 1M view hit alerts will be sent",
        )
        @app_commands.describe(channel="The Discord channel for 1M hit alerts")
        async def setmilestonechannel(
            interaction: discord.Interaction,
            channel: discord.TextChannel,
        ):
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command must be used inside a Discord server.",
                    ephemeral=True,
                )
                return

            assert bot.db_pool is not None

            await bot.db_pool.execute(
                """
                INSERT INTO guild_settings (guild_id, milestone_channel_id, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (guild_id)
                DO UPDATE SET milestone_channel_id = $2, updated_at = NOW();
                """,
                str(interaction.guild.id),
                str(channel.id),
            )

            await interaction.response.send_message(
                f"✅ 1M hit alerts will be sent to {channel.mention}.",
                ephemeral=True,
            )

        @self.tree.command(
            name="setdailyreportchannel",
            description="Set the channel where daily tracking reports will be sent",
        )
        @app_commands.describe(channel="The Discord channel for daily tracking reports")
        async def setdailyreportchannel(
            interaction: discord.Interaction,
            channel: discord.TextChannel,
        ):
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command must be used inside a Discord server.",
                    ephemeral=True,
                )
                return

            assert bot.db_pool is not None

            await bot.db_pool.execute(
                """
                INSERT INTO guild_settings (guild_id, daily_report_channel_id, updated_at)
                VALUES ($1, $2, NOW())
                ON CONFLICT (guild_id)
                DO UPDATE SET daily_report_channel_id = $2, updated_at = NOW();
                """,
                str(interaction.guild.id),
                str(channel.id),
            )

            await interaction.response.send_message(
                f"✅ Daily tracking reports will be sent to {channel.mention}.",
                ephemeral=True,
            )

        @self.creator_group.command(
            name="add",
            description="Add a TikTok creator to monitor",
        )
        @app_commands.describe(username="TikTok username, with or without @")
        async def creator_add(
            interaction: discord.Interaction,
            username: str,
        ):
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command must be used inside a Discord server.",
                    ephemeral=True,
                )
                return

            assert bot.db_pool is not None

            clean_username = normalize_username(username)

            try:
                await bot.db_pool.execute(
                    """
                    INSERT INTO creators (guild_id, username)
                    VALUES ($1, $2);
                    """,
                    str(interaction.guild.id),
                    clean_username,
                )
            except asyncpg.UniqueViolationError:
                await interaction.response.send_message(
                    f"⚠️ `@{clean_username}` is already being monitored.",
                    ephemeral=True,
                )
                return

            await interaction.response.send_message(
                f"✅ Added `@{clean_username}` to the monitoring list.",
                ephemeral=True,
            )

        @self.creator_group.command(
            name="remove",
            description="Remove a TikTok creator from monitoring",
        )
        @app_commands.describe(username="TikTok username, with or without @")
        async def creator_remove(
            interaction: discord.Interaction,
            username: str,
        ):
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command must be used inside a Discord server.",
                    ephemeral=True,
                )
                return

            assert bot.db_pool is not None

            clean_username = normalize_username(username)

            result = await bot.db_pool.execute(
                """
                DELETE FROM creators
                WHERE guild_id = $1 AND username = $2;
                """,
                str(interaction.guild.id),
                clean_username,
            )

            deleted_count = int(result.split(" ")[-1])

            if deleted_count == 0:
                await interaction.response.send_message(
                    f"⚠️ `@{clean_username}` was not in the monitoring list.",
                    ephemeral=True,
                )
                return

            await interaction.response.send_message(
                f"✅ Removed `@{clean_username}`.",
                ephemeral=True,
            )

        @self.creator_group.command(
            name="list",
            description="List all TikTok creators being monitored",
        )
        async def creator_list(interaction: discord.Interaction):
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command must be used inside a Discord server.",
                    ephemeral=True,
                )
                return

            assert bot.db_pool is not None

            rows = await bot.db_pool.fetch(
                """
                SELECT username
                FROM creators
                WHERE guild_id = $1
                ORDER BY username ASC;
                """,
                str(interaction.guild.id),
            )

            if not rows:
                await interaction.response.send_message(
                    "No creators are being monitored yet. Add one with `/creator add`.",
                    ephemeral=True,
                )
                return

            creator_text = "\n".join(f"- `@{row['username']}`" for row in rows)

            await interaction.response.send_message(
                f"Monitoring these TikTok creators for `{FIXED_KEYWORD}`:\n{creator_text}",
                ephemeral=True,
            )

        @self.tree.command(
            name="checknow",
            description="Manually check all creators now",
        )
        async def checknow(interaction: discord.Interaction):
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command must be used inside a Discord server.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True)

            result = await bot.check_guild(interaction.guild.id)

            note = ""
            if not result["alert_channel_set"]:
                note = "\n⚠️ No challenge alert channel is set. Use `/setchannel` first."

            await interaction.followup.send(
                (
                    "✅ Manual check finished.\n"
                    f"Creators checked: `{result['creators_checked']}`\n"
                    f"New videos found: `{result['new_videos']}`\n"
                    f"Challenge hits sent: `{result['alerts_sent']}`\n"
                    f"Videos added to active tracking: `{result['tracking_activated']}`"
                    f"{note}"
                ),
                ephemeral=True,
            )

        @self.tree.command(
            name="tracknow",
            description="Manually run the daily 1M view tracking check now",
        )
        async def tracknow(interaction: discord.Interaction):
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command must be used inside a Discord server.",
                    ephemeral=True,
                )
                return

            await interaction.response.defer(ephemeral=True)

            result = await bot.check_tracked_videos_for_guild(interaction.guild.id)

            note = ""
            if not result["milestone_channel_set"]:
                note += "\n⚠️ No 1M hit channel is set. Use `/setmilestonechannel` first."

            if not result["daily_report_channel_set"]:
                note += "\n⚠️ No daily report channel is set. Use `/setdailyreportchannel` if you want daily reports."

            await interaction.followup.send(
                (
                    "✅ Tracking check finished.\n"
                    f"Active videos checked: `{result['active_checked']}`\n"
                    f"1M hits found: `{result['milestones_hit']}`\n"
                    f"Still active: `{result['active_remaining']}`"
                    f"{note}"
                ),
                ephemeral=True,
            )

        @self.tree.command(
            name="usage",
            description="Show active TikTok tracking usage",
        )
        async def usage(interaction: discord.Interaction):
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command must be used inside a Discord server.",
                    ephemeral=True,
                )
                return

            assert bot.db_pool is not None

            active_count = await bot.get_active_tracking_count(str(interaction.guild.id))
            bar = build_usage_bar(active_count, MAX_ACTIVE_TRACKED_VIDEOS)

            await interaction.response.send_message(
                (
                    f"Usage: `{active_count}/{MAX_ACTIVE_TRACKED_VIDEOS}`\n"
                    f"{bar}"
                ),
                ephemeral=True,
            )

        @self.tree.command(
            name="activelist",
            description="Show active videos currently being tracked for 1M views",
        )
        async def activelist(interaction: discord.Interaction):
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command must be used inside a Discord server.",
                    ephemeral=True,
                )
                return

            assert bot.db_pool is not None

            rows = await bot.db_pool.fetch(
                """
                SELECT creator_username, video_url, description, view_count, tracked_at
                FROM seen_videos
                WHERE guild_id = $1
                  AND tracking_status = 'active'
                ORDER BY tracked_at ASC NULLS LAST, detected_at ASC
                LIMIT 25;
                """,
                str(interaction.guild.id),
            )

            active_count = await bot.get_active_tracking_count(str(interaction.guild.id))

            if not rows:
                await interaction.response.send_message(
                    "No active videos are currently being tracked for 1M views.",
                    ephemeral=True,
                )
                return

            lines = [
                f"Active videos tracking for 1M: `{active_count}/{MAX_ACTIVE_TRACKED_VIDEOS}`",
                "",
            ]

            for index, row in enumerate(rows, start=1):
                creator = row["creator_username"] or "unknown"
                description = shorten_description(row["description"], 60)
                view_count = row["view_count"]
                views = f"{int(view_count):,}" if view_count is not None else "Unknown"
                url = row["video_url"]

                lines.append(
                    f'{index}. **@{creator}** | `{views}` views | "{description}" | [VIEW HERE]({url})'
                )

            if active_count > len(rows):
                lines.append("")
                lines.append(f"Showing first {len(rows)} active videos.")

            await interaction.response.send_message(
                "\n".join(lines)[:1900],
                ephemeral=True,
            )

        class TestPanelSelect(discord.ui.Select):
            def __init__(self):
                options = [
                    discord.SelectOption(
                        label="Usage",
                        value="usage",
                        description="Show active tracking capacity",
                    ),
                    discord.SelectOption(
                        label="Active List",
                        value="active_list",
                        description="Show videos tracking toward 1M",
                    ),
                    discord.SelectOption(
                        label="Creator List",
                        value="creator_list",
                        description="Show monitored TikTok creators",
                    ),
                    discord.SelectOption(
                        label="Debug State",
                        value="debug",
                        description="Show channel IDs and database counts",
                    ),
                    discord.SelectOption(
                        label="Check Creators Now",
                        value="check_now",
                        description="Run the creator scan manually",
                    ),
                    discord.SelectOption(
                        label="Check 1M Tracking Now",
                        value="track_now",
                        description="Run the active video 1M check",
                    ),
                ]

                super().__init__(
                    placeholder="Choose a bot function to test",
                    min_values=1,
                    max_values=1,
                    options=options,
                )

            async def callback(self, interaction: discord.Interaction):
                if not interaction.guild:
                    await interaction.response.send_message(
                        "This panel must be used inside a Discord server.",
                        ephemeral=True,
                    )
                    return

                assert bot.db_pool is not None

                await interaction.response.defer(ephemeral=True, thinking=True)

                guild_id = interaction.guild.id
                guild_id_str = str(guild_id)
                selected = self.values[0]

                try:
                    if selected == "usage":
                        active_count = await bot.get_active_tracking_count(guild_id_str)
                        bar = build_usage_bar(active_count, MAX_ACTIVE_TRACKED_VIDEOS)
                        message = (
                            f"Usage: `{active_count}/{MAX_ACTIVE_TRACKED_VIDEOS}`\n"
                            f"{bar}"
                        )

                    elif selected == "active_list":
                        rows = await bot.db_pool.fetch(
                            """
                            SELECT creator_username, video_url, description, view_count, tracked_at
                            FROM seen_videos
                            WHERE guild_id = $1
                              AND tracking_status = 'active'
                            ORDER BY tracked_at ASC NULLS LAST, detected_at ASC
                            LIMIT 25;
                            """,
                            guild_id_str,
                        )

                        active_count = await bot.get_active_tracking_count(guild_id_str)

                        if not rows:
                            message = "No active videos are currently being tracked for 1M views."
                        else:
                            lines = [
                                f"Active videos tracking for 1M: `{active_count}/{MAX_ACTIVE_TRACKED_VIDEOS}`",
                                "",
                            ]

                            for index, row in enumerate(rows, start=1):
                                creator = row["creator_username"] or "unknown"
                                description = shorten_description(row["description"], 60)
                                view_count = row["view_count"]
                                views = f"{int(view_count):,}" if view_count is not None else "Unknown"
                                url = row["video_url"]

                                lines.append(
                                    f'{index}. **@{creator}** | `{views}` views | "{description}" | [VIEW HERE]({url})'
                                )

                            if active_count > len(rows):
                                lines.append("")
                                lines.append(f"Showing first {len(rows)} active videos.")

                            message = "\n".join(lines)

                    elif selected == "creator_list":
                        rows = await bot.db_pool.fetch(
                            """
                            SELECT username
                            FROM creators
                            WHERE guild_id = $1
                            ORDER BY username ASC;
                            """,
                            guild_id_str,
                        )

                        if not rows:
                            message = "No creators are being monitored yet. Add one with `/creator add`."
                        else:
                            creator_text = "\n".join(f"- `@{row['username']}`" for row in rows)
                            message = (
                                f"Monitoring these TikTok creators for `{FIXED_KEYWORD}`:\n"
                                f"{creator_text}"
                            )

                    elif selected == "debug":
                        creators = await bot.db_pool.fetch(
                            """
                            SELECT username
                            FROM creators
                            WHERE guild_id = $1
                            ORDER BY username ASC;
                            """,
                            guild_id_str,
                        )

                        settings = await bot.db_pool.fetchrow(
                            """
                            SELECT alert_channel_id, milestone_channel_id, daily_report_channel_id
                            FROM guild_settings
                            WHERE guild_id = $1;
                            """,
                            guild_id_str,
                        )

                        active_count = await bot.get_active_tracking_count(guild_id_str)
                        archived_count = await bot.db_pool.fetchval(
                            """
                            SELECT COUNT(*)
                            FROM seen_videos
                            WHERE guild_id = $1
                              AND tracking_status = 'archived';
                            """,
                            guild_id_str,
                        )

                        creator_text = "\n".join(f"- @{row['username']}" for row in creators)
                        if not creator_text:
                            creator_text = "No creators found."

                        alert_channel_id = settings["alert_channel_id"] if settings else "Not set"
                        milestone_channel_id = settings["milestone_channel_id"] if settings else "Not set"
                        daily_report_channel_id = settings["daily_report_channel_id"] if settings else "Not set"

                        message = (
                            f"**Guild ID:** `{guild_id_str}`\n"
                            f"**Challenge alert channel ID:** `{alert_channel_id}`\n"
                            f"**1M hit channel ID:** `{milestone_channel_id}`\n"
                            f"**Daily report channel ID:** `{daily_report_channel_id}`\n"
                            f"**Active tracked videos:** `{active_count}`\n"
                            f"**Archived tracked videos:** `{int(archived_count or 0)}`\n"
                            f"**Creators:**\n{creator_text}"
                        )

                    elif selected == "check_now":
                        result = await bot.check_guild(guild_id)
                        note = ""
                        if not result["alert_channel_set"]:
                            note = "\n⚠️ No challenge alert channel is set. Use `/setchannel` first."

                        message = (
                            "✅ Manual check finished.\n"
                            f"Creators checked: `{result['creators_checked']}`\n"
                            f"New videos found: `{result['new_videos']}`\n"
                            f"Challenge hits sent: `{result['alerts_sent']}`\n"
                            f"Videos added to active tracking: `{result['tracking_activated']}`"
                            f"{note}"
                        )

                    elif selected == "track_now":
                        result = await bot.check_tracked_videos_for_guild(guild_id)
                        note = ""
                        if not result["milestone_channel_set"]:
                            note += "\n⚠️ No 1M hit channel is set. Use `/setmilestonechannel` first."

                        if not result["daily_report_channel_set"]:
                            note += "\n⚠️ No daily report channel is set. Use `/setdailyreportchannel` if you want daily reports."

                        message = (
                            "✅ Tracking check finished.\n"
                            f"Active videos checked: `{result['active_checked']}`\n"
                            f"1M hits found: `{result['milestones_hit']}`\n"
                            f"Still active: `{result['active_remaining']}`"
                            f"{note}"
                        )

                    else:
                        message = "Unknown test option."

                except Exception as exc:
                    logger.exception("Test panel option failed: %s", selected)
                    message = f"❌ Test failed: `{exc}`"

                await interaction.followup.send(message[:1900], ephemeral=True)

        class TestPanelView(discord.ui.View):
            def __init__(self):
                super().__init__(timeout=300)
                self.add_item(TestPanelSelect())

        @self.tree.command(
            name="testpanel",
            description="Open a panel for testing bot functions",
        )
        async def testpanel(interaction: discord.Interaction):
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command must be used inside a Discord server.",
                    ephemeral=True,
                )
                return

            await interaction.response.send_message(
                "Choose a function to test.",
                view=TestPanelView(),
                ephemeral=True,
            )

        @self.tree.command(
            name="debug",
            description="Debug bot database state",
        )
        async def debug(interaction: discord.Interaction):
            if not interaction.guild:
                await interaction.response.send_message(
                    "This command must be used inside a Discord server.",
                    ephemeral=True,
                )
                return

            assert bot.db_pool is not None

            guild_id_str = str(interaction.guild.id)

            creators = await bot.db_pool.fetch(
                """
                SELECT username
                FROM creators
                WHERE guild_id = $1
                ORDER BY username ASC;
                """,
                guild_id_str,
            )

            settings = await bot.db_pool.fetchrow(
                """
                SELECT alert_channel_id, milestone_channel_id, daily_report_channel_id
                FROM guild_settings
                WHERE guild_id = $1;
                """,
                guild_id_str,
            )

            active_count = await bot.get_active_tracking_count(guild_id_str)
            archived_count = await bot.db_pool.fetchval(
                """
                SELECT COUNT(*)
                FROM seen_videos
                WHERE guild_id = $1
                  AND tracking_status = 'archived';
                """,
                guild_id_str,
            )

            creator_text = "\n".join(f"- @{row['username']}" for row in creators)
            if not creator_text:
                creator_text = "No creators found."

            alert_channel_id = settings["alert_channel_id"] if settings else "Not set"
            milestone_channel_id = settings["milestone_channel_id"] if settings else "Not set"
            daily_report_channel_id = settings["daily_report_channel_id"] if settings else "Not set"

            await interaction.response.send_message(
                (
                    f"**Guild ID:** `{guild_id_str}`\n"
                    f"**Challenge alert channel ID:** `{alert_channel_id}`\n"
                    f"**1M hit channel ID:** `{milestone_channel_id}`\n"
                    f"**Daily report channel ID:** `{daily_report_channel_id}`\n"
                    f"**Active tracked videos:** `{active_count}`\n"
                    f"**Archived tracked videos:** `{int(archived_count or 0)}`\n"
                    f"**Creators:**\n{creator_text}"
                ),
                ephemeral=True,
            )

        self.tree.add_command(self.creator_group)

    async def resolve_text_channel(self, channel_id: str | int) -> discord.TextChannel | None:
        try:
            channel_id_int = int(channel_id)
        except Exception:
            return None

        channel = self.get_channel(channel_id_int)

        if channel is None:
            try:
                channel = await self.fetch_channel(channel_id_int)
            except Exception:
                logger.exception("Failed fetching channel %s", channel_id)
                return None

        if isinstance(channel, discord.TextChannel):
            return channel

        return None

    @tasks.loop(hours=CHECK_INTERVAL_HOURS)
    async def hourly_checker(self):
        assert self.db_pool is not None

        guild_rows = await self.db_pool.fetch(
            """
            SELECT DISTINCT guild_id
            FROM creators;
            """
        )

        for row in guild_rows:
            guild_id = int(row["guild_id"])

            try:
                result = await self.check_guild(guild_id)
                logger.info("Checked guild %s: %s", guild_id, result)
            except Exception:
                logger.exception("Failed checking guild %s", guild_id)

            await asyncio.sleep(2)

    @hourly_checker.before_loop
    async def before_hourly_checker(self):
        await self.wait_until_ready()
        logger.info("Hourly checker started")

    @tasks.loop(hours=DAILY_TRACKING_CHECK_HOURS)
    async def daily_tracker(self):
        assert self.db_pool is not None

        guild_rows = await self.db_pool.fetch(
            """
            SELECT DISTINCT guild_id
            FROM seen_videos
            WHERE tracking_status = 'active';
            """
        )

        for row in guild_rows:
            guild_id = int(row["guild_id"])

            try:
                result = await self.check_tracked_videos_for_guild(guild_id)
                logger.info("Daily tracking check for guild %s: %s", guild_id, result)
            except Exception:
                logger.exception("Failed daily tracking check for guild %s", guild_id)

            await asyncio.sleep(2)

    @daily_tracker.before_loop
    async def before_daily_tracker(self):
        await self.wait_until_ready()
        logger.info("Daily tracker started")

    async def check_guild(self, guild_id: int) -> dict:
        assert self.db_pool is not None

        guild_id_str = str(guild_id)

        creators = await self.db_pool.fetch(
            """
            SELECT username
            FROM creators
            WHERE guild_id = $1
            ORDER BY username ASC;
            """,
            guild_id_str,
        )

        settings = await self.db_pool.fetchrow(
            """
            SELECT alert_channel_id
            FROM guild_settings
            WHERE guild_id = $1;
            """,
            guild_id_str,
        )

        alert_channel_set = bool(settings and settings["alert_channel_id"])

        if not alert_channel_set:
            logger.info("Guild %s has no challenge alert channel set", guild_id)
            return {
                "alert_channel_set": False,
                "creators_checked": len(creators),
                "new_videos": 0,
                "alerts_sent": 0,
                "tracking_activated": 0,
            }

        channel = await self.resolve_text_channel(settings["alert_channel_id"])

        if not channel:
            logger.warning("Challenge alert channel not found for guild %s", guild_id)
            return {
                "alert_channel_set": False,
                "creators_checked": len(creators),
                "new_videos": 0,
                "alerts_sent": 0,
                "tracking_activated": 0,
            }

        creators_checked = 0
        new_videos = 0
        alerts_sent = 0
        tracking_activated = 0

        for creator in creators:
            username = creator["username"]
            creators_checked += 1

            try:
                latest_videos = await get_latest_videos(username)
            except Exception:
                logger.exception("Failed fetching TikTok videos for @%s", username)
                continue

            logger.info("Fetched %s videos for @%s", len(latest_videos), username)

            for video in latest_videos:
                was_inserted = await self.save_seen_video(guild_id_str, video)

                if not was_inserted:
                    continue

                new_videos += 1

                if video_matches_challenge(video):
                    await self.mark_video_as_alerted(
                        guild_id_str=guild_id_str,
                        video_id=video.video_id,
                        matched_keyword=FIXED_KEYWORD,
                    )

                    activated = await self.activate_video_tracking(
                        guild_id_str=guild_id_str,
                        video_id=video.video_id,
                    )

                    if activated:
                        tracking_activated += 1

                    await send_challenge_alert(channel, video)
                    alerts_sent += 1

                await asyncio.sleep(1)

            await asyncio.sleep(3)

        return {
            "alert_channel_set": True,
            "creators_checked": creators_checked,
            "new_videos": new_videos,
            "alerts_sent": alerts_sent,
            "tracking_activated": tracking_activated,
        }

    async def check_tracked_videos_for_guild(self, guild_id: int) -> dict:
        assert self.db_pool is not None

        guild_id_str = str(guild_id)

        settings = await self.db_pool.fetchrow(
            """
            SELECT milestone_channel_id, daily_report_channel_id
            FROM guild_settings
            WHERE guild_id = $1;
            """,
            guild_id_str,
        )

        milestone_channel = None
        daily_report_channel = None

        if settings and settings["milestone_channel_id"]:
            milestone_channel = await self.resolve_text_channel(settings["milestone_channel_id"])

        if settings and settings["daily_report_channel_id"]:
            daily_report_channel = await self.resolve_text_channel(settings["daily_report_channel_id"])

        rows = await self.db_pool.fetch(
            """
            SELECT video_id, creator_username, video_url, description, posted_at
            FROM seen_videos
            WHERE guild_id = $1
              AND tracking_status = 'active'
            ORDER BY tracked_at ASC NULLS LAST, detected_at ASC;
            """,
            guild_id_str,
        )

        active_checked = 0
        milestones_hit = 0

        for row in rows:
            active_checked += 1
            video_url = row["video_url"]

            try:
                stats = await get_video_stats(video_url)
            except Exception:
                logger.exception("Failed fetching stats for %s", video_url)
                continue

            if not stats:
                continue

            view_count = stats.get("view_count")

            if view_count is not None:
                await self.update_video_view_count(
                    guild_id_str=guild_id_str,
                    video_id=row["video_id"],
                    view_count=view_count,
                )

            if view_count is not None and view_count >= VIEW_MILESTONE:
                if not milestone_channel:
                    logger.warning(
                        "Video %s reached 1M but guild %s has no valid milestone channel.",
                        row["video_id"],
                        guild_id_str,
                    )
                    continue

                try:
                    await send_milestone_alert(
                        channel=milestone_channel,
                        creator_username=row["creator_username"],
                        description=row["description"] or "",
                        video_url=row["video_url"],
                    )

                    await self.archive_tracked_video(
                        guild_id_str=guild_id_str,
                        video_id=row["video_id"],
                        view_count=view_count,
                    )

                    milestones_hit += 1
                except Exception:
                    logger.exception(
                        "Failed sending/archive milestone alert for video %s",
                        row["video_id"],
                    )

            await asyncio.sleep(3)

        active_remaining = await self.get_active_tracking_count(guild_id_str)

        result = {
            "milestone_channel_set": milestone_channel is not None,
            "daily_report_channel_set": daily_report_channel is not None,
            "active_checked": active_checked,
            "milestones_hit": milestones_hit,
            "active_remaining": active_remaining,
        }

        if daily_report_channel:
            try:
                await send_daily_tracking_report(
                    channel=daily_report_channel,
                    active_checked=active_checked,
                    milestones_hit=milestones_hit,
                    active_remaining=active_remaining,
                )
            except Exception:
                logger.exception("Failed sending daily tracking report for guild %s", guild_id)

        return result

    async def save_seen_video(self, guild_id_str: str, video: TikTokVideo) -> bool:
        assert self.db_pool is not None

        result = await self.db_pool.execute(
            """
            INSERT INTO seen_videos (
                guild_id,
                creator_username,
                video_id,
                video_url,
                description,
                posted_at,
                view_count
            )
            VALUES ($1, $2, $3, $4, $5, $6, $7)
            ON CONFLICT (guild_id, video_id)
            DO NOTHING;
            """,
            guild_id_str,
            video.creator_username,
            video.video_id,
            video.video_url,
            video.description,
            video.posted_at,
            video.view_count,
        )

        inserted_count = int(result.split(" ")[-1])
        return inserted_count == 1

    async def mark_video_as_alerted(
        self,
        guild_id_str: str,
        video_id: str,
        matched_keyword: str,
    ):
        assert self.db_pool is not None

        await self.db_pool.execute(
            """
            UPDATE seen_videos
            SET alerted = TRUE,
                matched_keyword = $3
            WHERE guild_id = $1
              AND video_id = $2;
            """,
            guild_id_str,
            video_id,
            matched_keyword,
        )

    async def activate_video_tracking(self, guild_id_str: str, video_id: str) -> bool:
        assert self.db_pool is not None

        active_count = await self.get_active_tracking_count(guild_id_str)

        if active_count >= MAX_ACTIVE_TRACKED_VIDEOS:
            logger.warning(
                "Guild %s has reached max active tracked videos: %s/%s",
                guild_id_str,
                active_count,
                MAX_ACTIVE_TRACKED_VIDEOS,
            )
            return False

        result = await self.db_pool.execute(
            """
            UPDATE seen_videos
            SET tracking_status = 'active',
                tracked_at = NOW()
            WHERE guild_id = $1
              AND video_id = $2
              AND tracking_status != 'archived';
            """,
            guild_id_str,
            video_id,
        )

        updated_count = int(result.split(" ")[-1])
        return updated_count == 1

    async def archive_tracked_video(
        self,
        guild_id_str: str,
        video_id: str,
        view_count: int,
    ):
        assert self.db_pool is not None

        await self.db_pool.execute(
            """
            UPDATE seen_videos
            SET tracking_status = 'archived',
                archived_at = NOW(),
                reached_1m_at = NOW(),
                view_count = $3
            WHERE guild_id = $1
              AND video_id = $2;
            """,
            guild_id_str,
            video_id,
            view_count,
        )

    async def update_video_view_count(
        self,
        guild_id_str: str,
        video_id: str,
        view_count: int,
    ):
        assert self.db_pool is not None

        await self.db_pool.execute(
            """
            UPDATE seen_videos
            SET view_count = $3
            WHERE guild_id = $1
              AND video_id = $2;
            """,
            guild_id_str,
            video_id,
            view_count,
        )

    async def get_active_tracking_count(self, guild_id_str: str) -> int:
        assert self.db_pool is not None

        active_count = await self.db_pool.fetchval(
            """
            SELECT COUNT(*)
            FROM seen_videos
            WHERE guild_id = $1
              AND tracking_status = 'active';
            """,
            guild_id_str,
        )

        return int(active_count or 0)


def normalize_username(username: str) -> str:
    username = username.strip()

    if username.startswith("@"):
        username = username[1:]

    return username.lower()


def video_matches_challenge(video: TikTokVideo) -> bool:
    description = video.description or ""
    return FIXED_KEYWORD.lower() in description.lower()


def format_datetime(dt: datetime | None) -> str:
    if not dt:
        return "Unknown"

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)

    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def shorten_description(description: str, limit: int = MILESTONE_DESCRIPTION_LIMIT) -> str:
    description = (description or "").strip()

    if not description:
        return "No description found."

    description = " ".join(description.split())

    if len(description) > limit:
        return description[:limit].rstrip() + "..."

    return description


def build_usage_bar(active_count: int, max_count: int) -> str:
    green_square = "\U0001f7e9"
    black_square = "\u2b1b"

    if max_count <= 0:
        return " ".join([black_square] * 10)

    filled_blocks = min(10, max(0, active_count * 10 // max_count))
    if active_count > 0 and filled_blocks == 0:
        filled_blocks = 1

    empty_blocks = 10 - filled_blocks

    return " ".join([green_square] * filled_blocks + [black_square] * empty_blocks)


async def send_challenge_alert(channel: discord.TextChannel, video: TikTokVideo):
    description = video.description or ""

    if len(description) > 3500:
        description = description[:3500] + "..."

    embed = discord.Embed(
        title="🔥 TikTok challenge hit detected",
        url=video.video_url,
        color=discord.Color.orange(),
    )

    embed.add_field(
        name="Creator",
        value=f"@{video.creator_username}",
        inline=True,
    )

    embed.add_field(
        name="Matched keyword",
        value=f"`{FIXED_KEYWORD}`",
        inline=True,
    )

    embed.add_field(
        name="Date posted",
        value=format_datetime(video.posted_at),
        inline=False,
    )

    embed.add_field(
        name="Exact description",
        value=description if description else "No description found.",
        inline=False,
    )

    embed.add_field(
        name="Video link",
        value=video.video_url,
        inline=False,
    )

    await channel.send(embed=embed)


async def send_milestone_alert(
    channel: discord.TextChannel,
    creator_username: str,
    description: str,
    video_url: str,
):
    clean_description = shorten_description(description)

    await channel.send(
        (
            f'🎯 1M Hit | @{creator_username} | '
            f'"{clean_description}" | '
            f'[VIEW HERE]({video_url})'
        )
    )


async def send_daily_tracking_report(
    channel: discord.TextChannel,
    active_checked: int,
    milestones_hit: int,
    active_remaining: int,
):
    await channel.send(
        (
            f"📅 Daily Tracking | "
            f"Checked: {active_checked} | "
            f"1M Hits: {milestones_hit} | "
            f"Active: {active_remaining}"
        )
    )


def validate_env():
    missing = []

    if not DISCORD_TOKEN:
        missing.append("DISCORD_TOKEN")

    if not DATABASE_URL:
        missing.append("DATABASE_URL")

    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}"
        )


if __name__ == "__main__":
    validate_env()
    bot = ChallengeBot()
    bot.run(DISCORD_TOKEN)
