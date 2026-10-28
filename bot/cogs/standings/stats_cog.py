"""Stats and profile cog.

Thin layer over PlayerStatsService: resolve who and which season, then either
format an embed or hand the same dataclass to the card renderer.
"""

import asyncio

import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import Button

from bot.rendering import asset_path
from bot.rendering.playercard import render_player_card
from bot.rendering.seasonsummary import render_season_summary
from bot.services.match import MatchService
from bot.services.player_stats import PlayerStats, PlayerStatsService
from bot.services.season import SeasonService
from bot.services.season_summary import SeasonSummaryService
from bot.db import get_db_manager, PlayerRepository
from bot.ui.autocomplete import season_autocomplete, username_autocomplete
from bot.utils.countries import COUNTRIES
from bot.utils.errors import ReportingView

CHAMPION_COLOR = discord.Color.from_rgb(245, 196, 70)
PROFILE_COLOR = discord.Color.from_rgb(29, 89, 212)
COUNTRY_NAMES = {code.upper(): name for name, code in COUNTRIES.items()}
STAGE_LABELS = {"OPEN": "Open", "PLAYIN": "Play-in", "SWISS": "Swiss", "PLAYOFF": "Playoff"}
RECENT_MATCHES = 5
HISTORY_PER_PAGE = 8

# Rendering images is expensive; one card/summary per user per window.
IMAGE_COOLDOWN_SECONDS = 20.0

def _result_line(match: dict) -> str:
    """One history row: result square, score, opponent, where it was played."""
    icon = "🟩" if match["won"] else "🟥"
    where = STAGE_LABELS.get(match["stage"], match["stage"].title())
    if match["slot"]:
        where += f" {match['slot']}"
        if match["series_game"]:
            where += f" G{match['series_game']}"
    return (
        f"{icon} **{match['my_score']}–{match['opp_score']}** vs {match['opponent']}"
        f" · {where} `#{match['match_id']}`"
    )


def _form(matches: list[dict]) -> str:
    """Last few results, oldest first so it reads left to right."""
    recent = matches[:RECENT_MATCHES]
    return "".join("🟩" if m["won"] else "🟥" for m in reversed(recent))


def _history_pages(name: str, season: int, matches: list[dict]) -> list[discord.Embed]:
    wins = sum(1 for m in matches if m["won"])
    pages = []
    total = (len(matches) - 1) // HISTORY_PER_PAGE + 1
    for index in range(0, len(matches), HISTORY_PER_PAGE):
        chunk = matches[index:index + HISTORY_PER_PAGE]
        embed = discord.Embed(
            title=f"Match history · {name}",
            description="\n".join(_result_line(m) for m in chunk),
            color=PROFILE_COLOR,
        )
        embed.set_footer(
            text=(
                f"Season {season} · {wins}-{len(matches) - wins} in {len(matches)} matches"
                f" · Page {index // HISTORY_PER_PAGE + 1}/{total}"
            )
        )
        pages.append(embed)
    return pages


class ProfileView(ReportingView):
    """Profile and match-history tabs on one message, history paginated."""

    def __init__(
        self,
        author_id: int,
        profile: discord.Embed | None,
        history: list[discord.Embed],
        start_on_history: bool = False,
    ):
        super().__init__(timeout=180)
        self.author_id = author_id
        self.profile = profile
        self.history = history
        self.tab = "history" if start_on_history or profile is None else "profile"
        self.page = 0
        if profile is None:
            self.remove_item(self.profile_btn)
        if not history:
            self.remove_item(self.history_btn)
        if len(history) <= 1:
            self.remove_item(self.prev_btn)
            self.remove_item(self.next_btn)
        self._sync()

    @property
    def needed(self) -> bool:
        """A view is only worth sending when there is something to switch to."""
        return len(self.children) > 1 or len(self.history) > 1

    def current(self) -> discord.Embed:
        return self.history[self.page] if self.tab == "history" else self.profile

    def _sync(self) -> None:
        on_history = self.tab == "history"
        self.profile_btn.disabled = not on_history
        self.history_btn.disabled = on_history
        self.prev_btn.disabled = not on_history or self.page <= 0
        self.next_btn.disabled = not on_history or self.page >= len(self.history) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        await super().interaction_check(interaction)
        return interaction.user.id == self.author_id

    async def _show(self, interaction: discord.Interaction) -> None:
        self._sync()
        await interaction.response.edit_message(embed=self.current(), view=self)

    @discord.ui.button(label="Profile", emoji="👤", style=discord.ButtonStyle.primary)
    async def profile_btn(self, interaction: discord.Interaction, button: Button):
        self.tab = "profile"
        await self._show(interaction)

    @discord.ui.button(label="History", emoji="📜", style=discord.ButtonStyle.secondary)
    async def history_btn(self, interaction: discord.Interaction, button: Button):
        self.tab = "history"
        await self._show(interaction)

    @discord.ui.button(emoji="◀", style=discord.ButtonStyle.grey)
    async def prev_btn(self, interaction: discord.Interaction, button: Button):
        self.page = max(0, self.page - 1)
        await self._show(interaction)

    @discord.ui.button(emoji="▶", style=discord.ButtonStyle.grey)
    async def next_btn(self, interaction: discord.Interaction, button: Button):
        self.page = min(len(self.history) - 1, self.page + 1)
        await self._show(interaction)


class StatsCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.db = get_db_manager()
        self.season_service = SeasonService()
        self.match_service = MatchService()
        self.stats_service = PlayerStatsService()
        self.summary_service = SeasonSummaryService()

    # ── resolution ────────────────────────────────────────────

    async def _resolve(self, interaction, username, season):
        """Return (player, season_number), or None after explaining why not."""
        season_num = season or await self.season_service.get_current_season_number()
        if not season_num:
            await interaction.followup.send(
                "There's no active season right now. Check back once the next one starts!",
                ephemeral=True,
            )
            return None

        if username:
            player = await self.stats_service.resolve_player(username, season_num)
        else:
            async with self.db.get_session() as session:
                player = await PlayerRepository(session).get_by_discord_id(
                    interaction.user.id
                )

        if player is None:
            message = (
                f"Player '{username}' not found."
                if username
                else "You are not registered. Use /signup first."
            )
            await interaction.followup.send(message, ephemeral=True)
            return None

        return player, season_num

    async def _load_stats(self, interaction, username, season):
        resolved = await self._resolve(interaction, username, season)
        if resolved is None:
            return None
        player, season_num = resolved

        stats = await self.stats_service.get_stats(player.id, season_num)
        if stats is None:
            await interaction.followup.send(
                f"Player '{player.name}' not found.", ephemeral=True
            )
            return None
        if not stats.has_data:
            await interaction.followup.send(
                f"{player.name} did not play Season {season_num}.", ephemeral=True
            )
            return None
        return stats

    # ── formatting ────────────────────────────────────────────

    @staticmethod
    def _stage_line(stats: PlayerStats, stage: str, record) -> str:
        champion = stage == "PLAYOFF" and stats.champion
        if not record.played:
            return "Champion" if champion else "Did not play"
        text = f"{record.wins}-{record.losses}"
        if stage == "PLAYIN" and stats.playin_rank:
            text += f" · Rank #{stats.playin_rank}"
        if stage == "SWISS" and stats.swiss_seed:
            text += f" · Seed #{stats.swiss_seed}"
        if champion:
            text += " · Champion"
        return text

    def _avatar_url(self, guild, discord_id: int) -> str | None:
        member = guild.get_member(discord_id) if guild else None
        return member.display_avatar.url if member else None

    def _profile_embed(
        self,
        stats: PlayerStats,
        matches: list[dict] | None = None,
        avatar_url: str | None = None,
    ) -> discord.Embed:
        matches = matches or []
        lines = []
        if stats.champion:
            lines.append(f"🏆 **Season {stats.season} Champion**")
        where = []
        if stats.country:
            code = stats.country.upper()
            where.append(f":flag_{code.lower()}: {COUNTRY_NAMES.get(code, code)}")
        where.append(f"Season {stats.season}")
        where.append("🟢 Active" if stats.season_active else "⚪ Archived")
        lines.append(" · ".join(where))
        if stats.championship_seasons:
            seasons = " · ".join(f"S{n}" for n in stats.championship_seasons)
            titles = "title" if stats.circuits_won == 1 else "titles"
            lines.append(f"🎖️ **{stats.circuits_won}** career {titles} ({seasons})")

        embed = discord.Embed(
            title=stats.name,
            description="\n".join(lines),
            color=CHAMPION_COLOR if stats.champion else PROFILE_COLOR,
        )
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)

        total = stats.total
        embed.add_field(name="Record", value=f"**{total.wins}-{total.losses}**", inline=True)
        embed.add_field(name="Win rate", value=f"**{total.win_rate:.0%}**", inline=True)
        embed.add_field(
            name="Goals",
            value=f"**{total.goals_for}-{total.goals_against}** ({total.goal_diff:+d})",
            inline=True,
        )

        embed.add_field(name="Open", value=stats.open_summary, inline=True)
        embed.add_field(
            name="Playin",
            value=self._stage_line(stats, "PLAYIN", stats.playin),
            inline=True,
        )
        if stats.hp is not None:
            embed.add_field(name="HP", value=str(stats.hp), inline=True)
        embed.add_field(
            name="Swiss",
            value=self._stage_line(stats, "SWISS", stats.swiss),
            inline=True,
        )
        embed.add_field(
            name="Playoff",
            value=self._stage_line(stats, "PLAYOFF", stats.playoff),
            inline=True,
        )

        if matches:
            embed.add_field(name="Form", value=_form(matches), inline=False)
            embed.add_field(
                name="Recent matches",
                value="\n".join(_result_line(m) for m in matches[:RECENT_MATCHES]),
                inline=False,
            )
        embed.set_footer(text="History tab for every match · /playercard for the image")
        return embed

    async def _send_profile(
        self, interaction, stats: PlayerStats, start_on_history: bool
    ) -> None:
        """Send the profile/history message; both tabs share one match query."""
        matches = await self.match_service.get_match_history(stats.player_id, stats.season)
        profile = None
        if stats.has_data:
            profile = self._profile_embed(
                stats, matches, self._avatar_url(interaction.guild, stats.discord_id)
            )
        history = _history_pages(stats.name, stats.season, matches)
        view = ProfileView(interaction.user.id, profile, history, start_on_history)
        kwargs = {"embed": view.current(), "ephemeral": True}
        if view.needed:
            kwargs["view"] = view
        await interaction.followup.send(**kwargs)

    # ── commands ──────────────────────────────────────────────

    @app_commands.command(name="profile", description="View a player's season stats")
    @app_commands.describe(
        username="Player's registered name or ID (leave blank for yourself)",
        season="Season number (defaults to the current one)",
    )
    @app_commands.autocomplete(
        username=username_autocomplete, season=season_autocomplete
    )
    async def profile_cmd(
        self,
        interaction: discord.Interaction,
        username: str = None,
        season: int = None,
    ):
        await interaction.response.defer(ephemeral=True)
        stats = await self._load_stats(interaction, username, season)
        if stats is None:
            return
        await self._send_profile(interaction, stats, start_on_history=False)

    @app_commands.command(name="playercard", description="Render a player card image")
    @app_commands.checks.cooldown(1, IMAGE_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    @app_commands.describe(
        username="Player's registered name or ID (leave blank for yourself)",
        season="Season number (defaults to the current one)",
    )
    @app_commands.autocomplete(
        username=username_autocomplete, season=season_autocomplete
    )
    async def playercard_cmd(
        self,
        interaction: discord.Interaction,
        username: str = None,
        season: int = None,
    ):
        await interaction.response.defer(ephemeral=True)
        stats = await self._load_stats(interaction, username, season)
        if stats is None:
            return

        filename = asset_path(
            "tables", f"card_{stats.discord_id}_s{stats.season}.png"
        )
        try:
            await asyncio.to_thread(render_player_card, stats, filename)
        except Exception as exc:  # pragma: no cover - defensive
            await interaction.followup.send(f"Failed to render card: {exc}", ephemeral=True)
            return

        await interaction.followup.send(
            file=discord.File(filename, filename=f"card_s{stats.season}.png"),
            ephemeral=True,
        )

    @app_commands.command(
        name="season_summary",
        description="Render a season summary image with placements and leaders",
    )
    @app_commands.describe(
        season="Season number (defaults to the current one)",
    )
    @app_commands.autocomplete(season=season_autocomplete)
    @app_commands.checks.cooldown(1, IMAGE_COOLDOWN_SECONDS, key=lambda i: i.user.id)
    async def season_summary_cmd(
        self,
        interaction: discord.Interaction,
        season: int = None,
    ):
        await interaction.response.defer(ephemeral=True)
        season_num = season or await self.season_service.get_current_season_number()
        if not season_num:
            await interaction.followup.send(
                "There's no active season right now. Check back once the next one starts!",
                ephemeral=True,
            )
            return

        summary = await self.summary_service.get_summary(season_num)
        if not summary.has_data:
            await interaction.followup.send(
                f"No completed matches in Season {season_num} yet.",
                ephemeral=True,
            )
            return

        filename = asset_path(
            "tables", f"season_summary_s{season_num}.png"
        )
        try:
            await asyncio.to_thread(
                render_season_summary, summary, filename
            )
        except Exception as exc:  # pragma: no cover - defensive
            await interaction.followup.send(
                f"Failed to render season summary: {exc}", ephemeral=True
            )
            return

        await interaction.followup.send(
            file=discord.File(
                filename, filename=f"season_summary_s{season_num}.png"
            ),
            ephemeral=True,
        )

    @app_commands.command(name="history", description="View a player's match history")
    @app_commands.describe(
        username="Player's registered name or ID (leave blank for yourself)",
        season="Season number (defaults to the current one)",
    )
    @app_commands.autocomplete(
        username=username_autocomplete, season=season_autocomplete
    )
    async def history_cmd(
        self,
        interaction: discord.Interaction,
        username: str = None,
        season: int = None,
    ):
        await interaction.response.defer(ephemeral=True)
        resolved = await self._resolve(interaction, username, season)
        if resolved is None:
            return
        player, season_num = resolved

        matches = await self.match_service.get_match_history(player.id, season_num)
        if not matches:
            await interaction.followup.send(
                f"{player.name} has no completed matches in Season {season_num}.",
                ephemeral=True,
            )
            return

        stats = await self.stats_service.get_stats(player.id, season_num)
        await self._send_profile(interaction, stats, start_on_history=True)

    @app_commands.command(name="seasons", description="List seasons and their champions")
    async def seasons_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        seasons = await self.stats_service.season_overview()
        if not seasons:
            await interaction.followup.send("No seasons with data yet.", ephemeral=True)
            return

        lines = []
        for info in seasons[:20]:
            line = f"**Season {info.number}** · {'Active' if info.active else 'Archived'}"
            if info.champion:
                line += f" · 🏆 {info.champion}"
            if info.ended_at:
                line += f" · ended {info.ended_at:%Y-%m-%d}"
            lines.append(line)

        embed = discord.Embed(
            title="📚 Every season so far",
            description="\n".join(lines),
            color=discord.Color.blurple(),
        )
        embed.set_footer(
            text="Use /season_summary for the full picture · "
            "Add season:N to /open, /swiss, /playoff, /profile or /playercard"
        )
        await interaction.followup.send(embed=embed, ephemeral=True)


async def setup(bot):
    await bot.add_cog(StatsCog(bot))
