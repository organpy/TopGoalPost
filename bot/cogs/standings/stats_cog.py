"""Stats and profile cog."""

import discord
from discord.ext import commands
from discord import app_commands
from discord.ui import Button, View
from sqlalchemy import select, text

from bot.services.season import SeasonService
from bot.services.match import MatchService
from bot.db import get_db_manager, PlayerRepository, PlayerSeasonRepository
from bot.db.models import Player as DbPlayer


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

    async def username_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        if not current:
            return []
        async with self.db.get_session() as session:
            stmt = select(DbPlayer).where(DbPlayer.name.ilike(f"%{current}%")).limit(25)
            result = await session.execute(stmt)
            players = result.scalars().all()
            return [app_commands.Choice(name=p.name, value=p.name) for p in players]

    @app_commands.command(name="profile", description="View a player's profile and stats")
    @app_commands.describe(username="Player's registered name (leave blank for yourself)")
    @app_commands.autocomplete(username=username_autocomplete)
    async def profile_cmd(self, interaction: discord.Interaction, username: str = None):
        season_num = await self.season_service.get_current_season_number()

        async with self.db.get_session() as session:
            player_repo = PlayerRepository(session)
            if username:
                player = await player_repo.get_by_name(username)
                if not player:
                    await interaction.response.send_message(f"Player '{username}' not found.", ephemeral=True)
                    return
            else:
                player = await player_repo.get_by_discord_id(interaction.user.id)
                if not player:
                    await interaction.response.send_message("You are not registered. Use /signup first.", ephemeral=True)
                    return

            embed = discord.Embed(title=f"Profile: {player.name}", color=discord.Color.green())
            if player.country:
                embed.description = f"Flag: :flag_{player.country.lower()}:"

            if season_num:
                ps_repo = PlayerSeasonRepository(session)
                ps = await ps_repo.get(player_id=player.id, season_number=season_num)
                if ps:
                    if ps.swiss_seed and ps.swiss_seed > 0:
                        # Skippers have their Open record zeroed, so showing
                        # "0 - 0" would read as two losses' worth of nothing.
                        embed.add_field(
                            name="Open",
                            value=f"Skipped Open (Swiss seed #{ps.swiss_seed})",
                            inline=False,
                        )
                        embed.add_field(name="HP", value=str(ps.hp))
                    else:
                        embed.add_field(name="Current Season Stats", value=f"**Open Record:** {ps.open_wins} - {ps.open_losses}\n**Goal Difference:** {ps.open_gd}\n**HP:** {ps.hp}", inline=False)
                    if ps.playin_rank:
                        embed.add_field(name="Playin Rank", value=str(ps.playin_rank))

            stmt = text(
                "SELECT COUNT(*), SUM(is_winner), SUM(goals) "
                "FROM match_players WHERE player_id = :pid"
            )
            result = await session.execute(stmt, {"pid": player.id})
            row = result.first()
            if row and row[0] > 0:
                total, wins, goals = row
                losses = total - wins
                embed.add_field(name="All-Time Record", value=f"**Matches:** {total}\n**W-L:** {wins} - {losses}\n**Goals Scored:** {goals}", inline=False)

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(name="history", description="View a player's match history")
    @app_commands.describe(username="Player's registered name (leave blank for yourself)")
    @app_commands.autocomplete(username=username_autocomplete)
    async def history_cmd(self, interaction: discord.Interaction, username: str = None):
        season_num = await self.season_service.get_current_season_number()
        if not season_num:
            await interaction.response.send_message("No active season.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        async with self.db.get_session() as session:
            player_repo = PlayerRepository(session)
            if username:
                player = await player_repo.get_by_name(username)
                if not player:
                    await interaction.followup.send(f"Player '{username}' not found.", ephemeral=True)
                    return
            else:
                player = await player_repo.get_by_discord_id(interaction.user.id)
                if not player:
                    await interaction.followup.send("You are not registered. Use /signup first.", ephemeral=True)
                    return

            display_name = player.name

        matches = await self.match_service.get_match_history(player.id, season_num)

        if not matches:
            await interaction.followup.send(f"{display_name} has no completed matches this season.", ephemeral=True)
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
            header = f"**Match History for {display_name}** (Season {season_num})\n"
            header += f"Page {i // per_page + 1}/{(len(lines) - 1) // per_page + 1}\n\n"
            pages.append(header + "\n".join(chunk))

        view = HistoryView(pages, interaction.user.id) if len(pages) > 1 else None
        await interaction.followup.send(content=pages[0], view=view, ephemeral=True)


async def setup(bot):
    await bot.add_cog(StatsCog(bot))
