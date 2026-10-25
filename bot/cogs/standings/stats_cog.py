"""Stats and profile cog.

Thin layer over PlayerStatsService: resolve who and which season, then either
format an embed or hand the same dataclass to the card renderer.
"""

import asyncio

import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import Button, View

from bot.rendering import asset_path
from bot.rendering.playercard import render_player_card
from bot.rendering.seasonsummary import render_season_summary
from bot.services.match import MatchService
from bot.services.player_stats import PlayerStats, PlayerStatsService
from bot.services.season import SeasonService
from bot.services.season_summary import SeasonSummaryService
from bot.db import get_db_manager, PlayerRepository
from bot.ui.autocomplete import season_autocomplete, username_autocomplete

CHAMPION_COLOR = discord.Color.from_rgb(245, 196, 70)


class HistoryView(View):
    def __init__(self, pages: list[str], author_id: int):
        super().__init__(timeout=60)
        self.pages = pages
        self.author_id = author_id
        self.current = 0
        self._update_buttons()

    def _update_buttons(self):
        self.prev_btn.disabled = self.current <= 0
        self.next_btn.disabled = self.current >= len(self.pages) - 1

    @discord.ui.button(label="◀", style=discord.ButtonStyle.grey)
    async def prev_btn(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.author_id:
            return
        self.current = max(0, self.current - 1)
        self._update_buttons()
        await interaction.response.edit_message(
            content=self.pages[self.current],
            view=self,
        )

    @discord.ui.button(label="▶", style=discord.ButtonStyle.grey)
    async def next_btn(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.author_id:
            return
        self.current = min(len(self.pages) - 1, self.current + 1)
        self._update_buttons()
        await interaction.response.edit_message(
            content=self.pages[self.current],
            view=self,
        )


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
                "No active season. Create one with `!season create N`.",
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

    def _profile_embed(self, stats: PlayerStats) -> discord.Embed:
        embed = discord.Embed(
            title=stats.name,
            description=(
                f"Season {stats.season} · "
                f"{'Active' if stats.season_active else 'Archived'}"
            ),
            color=CHAMPION_COLOR if stats.champion else discord.Color.green(),
        )
        if stats.country:
            embed.add_field(
                name="Country",
                value=f":flag_{stats.country.lower()}: {stats.country.upper()}",
                inline=True,
            )
        if stats.hp is not None:
            embed.add_field(name="HP", value=str(stats.hp), inline=True)

        embed.add_field(name="Open", value=stats.open_summary, inline=False)
        embed.add_field(
            name="Playin",
            value=self._stage_line(stats, "PLAYIN", stats.playin),
            inline=True,
        )
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

        total = stats.total
        embed.add_field(
            name="Totals",
            value=(
                f"**Games:** {total.games} · **W-L:** {total.wins}-{total.losses}\n"
                f"**Goals:** {total.goals_for}-{total.goals_against} · "
                f"**GD:** {total.goal_diff:+d} · "
                f"**Win rate:** {total.win_rate:.0%}"
            ),
            inline=False,
        )
        embed.set_footer(text="Use /playercard for the image version")
        return embed

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
        await interaction.followup.send(embed=self._profile_embed(stats), ephemeral=True)

    @app_commands.command(name="playercard", description="Render a player card image")
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
    async def season_summary_cmd(
        self,
        interaction: discord.Interaction,
        season: int = None,
    ):
        await interaction.response.defer(ephemeral=True)
        season_num = season or await self.season_service.get_current_season_number()
        if not season_num:
            await interaction.followup.send(
                "No active season. Create one with `!season create N`.",
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

        lines = []
        for m in matches:
            icon = "✅" if m["won"] else "❌"
            slot_str = f" [{m['slot']} G{m['series_game']}]" if m["slot"] else ""
            lines.append(
                f"{icon} `#{m['match_id']}` {m['stage']}{slot_str} — "
                f"**{m['my_score']}** - {m['opp_score']} vs {m['opponent']}"
            )

        per_page = 10
        pages = []
        for i in range(0, len(lines), per_page):
            chunk = lines[i:i + per_page]
            header = f"**Match History for {player.name}** (Season {season_num})\n"
            header += f"Page {i // per_page + 1}/{(len(lines) - 1) // per_page + 1}\n\n"
            pages.append(header + "\n".join(chunk))

        view = HistoryView(pages, interaction.user.id) if len(pages) > 1 else None
        await interaction.followup.send(content=pages[0], view=view, ephemeral=True)

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
            title="Seasons",
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
