"""Stats and profile cog.

Thin layer over PlayerStatsService: resolve who and which season, then either
format an embed or hand the same dataclass to the card renderer.
"""

import asyncio

import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import Button, View
from sqlalchemy import select

from bot.rendering import asset_path
from bot.rendering.playercard import render_player_card
from bot.services.match import MatchService
from bot.services.player_stats import PlayerStats, PlayerStatsService
from bot.services.season import SeasonService
from bot.db import get_db_manager, PlayerRepository
from bot.db.models import Player as DbPlayer

CHAMPION_COLOR = discord.Color.from_rgb(245, 196, 70)


async def season_autocomplete(
    interaction: discord.Interaction, current: str
) -> list[app_commands.Choice[int]]:
    """Seasons that actually have tournament data, newest first."""
    seasons = await PlayerStatsService().available_seasons()
    return [
        app_commands.Choice(name=f"Season {number}", value=number)
        for number in seasons
        if current in str(number)
    ][:25]


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

    # ── resolution ────────────────────────────────────────────

    async def username_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        if not current:
            return []
        async with self.db.get_session() as session:
            stmt = select(DbPlayer).where(DbPlayer.name.ilike(f"%{current}%")).limit(25)
            result = await session.execute(stmt)
            players = result.scalars().all()
            return [app_commands.Choice(name=p.name, value=p.name) for p in players]

    async def _resolve(self, interaction, username, season):
        """Return (player, season_number), or None after explaining why not."""
        season_num = season or await self.season_service.get_current_season_number()
        if not season_num:
            await interaction.followup.send(
                "No active season. Create one with `!season create N`.",
                ephemeral=True,
            )
            return None

        async with self.db.get_session() as session:
            repo = PlayerRepository(session)
            if username:
                player = await repo.get_by_name(username)
            else:
                player = await repo.get_by_discord_id(interaction.user.id)

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
            name="Play-in",
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
        username="Player's registered name (leave blank for yourself)",
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
        username="Player's registered name (leave blank for yourself)",
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

    @app_commands.command(name="history", description="View a player's match history")
    @app_commands.describe(
        username="Player's registered name (leave blank for yourself)",
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


async def setup(bot):
    await bot.add_cog(StatsCog(bot))
