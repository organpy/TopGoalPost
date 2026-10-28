"""Match reporting cog."""

from datetime import timezone
from typing import Optional

import discord
from discord.ext import commands

from bot.cogs.checks import require_host_app, require_moderator_app
from discord import app_commands

from bot.services.match import MatchService
from bot.services.season import SeasonService
from bot.db import get_db_manager, MatchPlayerRepository, PlayerRepository
from bot.ui.autocomplete import username_autocomplete
from bot.ui.report import ReportScoreView, report_prompt_embed, score_confirm_view
from bot.services.match_report import MatchReportService



def _scheduled_suffix(scheduled_at) -> str:
    """` · 🗓 <t:...>` for matches with an agreed time (stored as naive UTC)."""
    if not scheduled_at:
        return ""
    ts = int(scheduled_at.replace(tzinfo=timezone.utc).timestamp())
    return f" · 🗓 <t:{ts}:f>"


class MatchCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.match_service = MatchService()
        self.db = get_db_manager()

    async def match_autocomplete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        """Autocomplete for a user's active matches in the current season."""
        discord_id = interaction.user.id
        choices = []
        async with self.db.get_session() as session:
            from bot.db import Match as M
            from sqlalchemy import select, and_
            from sqlalchemy.orm import selectinload
            from bot.db import MatchPlayer as MP, Event
            from bot.services.season import SeasonService

            season = await SeasonService().get_current_season_number()
            if not season:
                return []

            pr = PlayerRepository(session)
            player = await pr.get_by_discord_id(discord_id)
            if not player:
                return []
            stmt = (
                select(M)
                .options(selectinload(M.match_players).selectinload(MP.player))
                .join(MP)
                .join(Event, M.event_id == Event.id)
                .where(
                    and_(
                        MP.player_id == player.id,
                        M.completed_at.is_(None),
                        Event.season == season,
                    )
                )
                .order_by(M.id.desc())
                .limit(25)
            )
            r = await session.execute(stmt)
            matches = r.scalars().all()
            for m in matches:
                if len(m.match_players) != 2 or any(
                    mp.player_id is None for mp in m.match_players
                ):
                    continue
                label = f"Match #{m.id}"
                for mp in m.match_players:
                    if mp.player_id != player.id and mp.player:
                        label += f" vs {mp.player.name}"
                if current.lower() in label.lower():
                    choices.append(app_commands.Choice(name=label[:100], value=str(m.id)))
        return choices

    async def admin_match_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete active and completed matches (admin set_score)."""
        choices = []
        matches = await self.match_service.list_incomplete_matches(
            include_completed=True
        )
        for m in matches:
            label = m["label"]
            if current.lower() in label.lower():
                choices.append(
                    app_commands.Choice(name=label[:100], value=str(m["match_id"]))
                )
            if len(choices) >= 25:
                break
        return choices

    async def _active_player_matches(self, player_id: int) -> list[dict]:
        """Active current-season matches for the reporter, with side and label."""
        season = await SeasonService().get_current_season_number()
        if not season:
            return []
        return await self.match_service.active_matches_for_player(player_id, season)

    async def playoff_match_autocomplete(
        self, interaction: discord.Interaction, current: str
    ) -> list[app_commands.Choice[str]]:
        """Autocomplete playoff series/games for set_score_playoff."""
        choices = []
        matches = await self.match_service.list_incomplete_matches(
            include_completed=True, stage="PLAYOFF"
        )
        for m in matches:
            label = m["label"]
            if current.lower() in label.lower():
                choices.append(
                    app_commands.Choice(name=label[:100], value=str(m["match_id"]))
                )
            if len(choices) >= 25:
                break
        return choices

    @app_commands.command(name="report", description="Report a match score")
    async def report_cmd(self, interaction: discord.Interaction):
        discord_id = interaction.user.id

        async with self.db.get_session() as session:
            player = await PlayerRepository(session).get_by_discord_id(discord_id)
            if not player:
                await interaction.response.send_message(
                    "You are not a registered player.", ephemeral=True
                )
                return
            player_id = player.id

        matches = await self._active_player_matches(player_id)
        if not matches:
            await interaction.response.send_message(
                "You have no active matches.", ephemeral=True
            )
            return

        await interaction.response.send_message(
            embed=report_prompt_embed(),
            view=ReportScoreView(matches, interaction.user.id),
            ephemeral=True,
        )

    @app_commands.command(name="forfeit", description="Forfeit a match")
    @app_commands.describe(match_id="The ID of the match to forfeit")
    @app_commands.autocomplete(match_id=match_autocomplete)
    async def forfeit_cmd(self, interaction: discord.Interaction, match_id: int):
        discord_id = interaction.user.id
        
        async with self.db.get_session() as session:
            player_repo = PlayerRepository(session)
            player = await player_repo.get_by_discord_id(discord_id)
            if not player:
                await interaction.response.send_message("You are not a registered player.", ephemeral=True)
                return
                
            mp_repo = MatchPlayerRepository(session)
            match_players = await mp_repo.get_by_match(match_id)
            if not match_players:
                await interaction.response.send_message("Match not found.", ephemeral=True)
                return
                
            is_participant = any(mp.player_id == player.id for mp in match_players)
            if not is_participant:
                await interaction.response.send_message("You are not part of this match.", ephemeral=True)
                return
                
        await interaction.response.defer(ephemeral=True)
        result = await self.match_service.forfeit(match_id, player.id)

        if result and result.get("error"):
            await interaction.followup.send(result["error"], ephemeral=True)
        elif result:
            await interaction.followup.send(f"Match {match_id} forfeited.", ephemeral=True)
        else:
            await interaction.followup.send(
                "Failed to forfeit match. It might already be completed or "
                "waiting for an opponent.",
                ephemeral=True,
            )

    @app_commands.command(name="list_matches", description="List matches with optional filters (admin)")
    @app_commands.describe(
        stage="Filter by stage: OPEN, PLAYIN, SWISS, PLAYOFF",
        status="Filter by active or completed",
        round_num="Filter by round / game number",
        player="Filter by player name or ID",
        page="Page number (20 matches per page)",
    )
    @app_commands.autocomplete(player=username_autocomplete)
    @require_host_app()
    async def list_matches_cmd(
        self,
        interaction: discord.Interaction,
        stage: Optional[str] = None,
        status: Optional[str] = None,
        round_num: Optional[int] = None,
        player: Optional[str] = None,
        page: int = 1,
    ):
        await interaction.response.defer(ephemeral=True)
        matches = await self.match_service.list_incomplete_matches(
            include_completed=True,
            stage=stage,
            status=status,
            round_num=round_num,
            player=player,
        )
        if not matches:
            await interaction.followup.send("No matches match those filters.", ephemeral=True)
            return

        page = max(1, page)
        page_size = 20
        start = (page - 1) * page_size
        page_matches = matches[start:start + page_size]
        if not page_matches:
            await interaction.followup.send(
                f"Page {page} is out of range ({len(matches)} matches).",
                ephemeral=True,
            )
            return

        lines = [m["label"] + _scheduled_suffix(m.get("scheduled_at")) for m in page_matches]
        total_pages = (len(matches) + page_size - 1) // page_size
        body = "\n".join(lines)
        await interaction.followup.send(
            f"**Matches** ({len(matches)} total, page {page}/{total_pages}):\n"
            f"{body}\n\nUse `/set_score` with autocomplete.",
            ephemeral=True,
        )

    @app_commands.command(name="set_score", description="Admin override for a match score")
    @app_commands.describe(
        match_id="Match to score (pick from list)",
        p1_goals="Goals for player 1 (left / side 0)",
        p2_goals="Goals for player 2 (right / side 1)",
    )
    @app_commands.autocomplete(match_id=admin_match_autocomplete)
    @require_host_app()
    async def set_score_cmd(self, interaction: discord.Interaction, match_id: int, p1_goals: int, p2_goals: int):
        if p1_goals == p2_goals:
            await interaction.response.send_message("Ties are not allowed.", ephemeral=True)
            return
            
        winner_side = 0 if p1_goals > p2_goals else 1
        await interaction.response.defer(ephemeral=True)
        result = await self.match_service.report_score(match_id, p1_goals, p2_goals, winner_side)
        
        if "error" in result:
            await interaction.followup.send(f"Failed to set score: {result['error']}", ephemeral=True)
        else:
            summary = await self.match_service.format_result_summary(match_id)
            suffix = (
                "\nDependent downstream matches were reset for rescoring."
                if result.get("edited")
                else ""
            )
            await interaction.followup.send(
                f"Admin override: {summary}{suffix}",
                ephemeral=True,
            )

    @app_commands.command(
        name="set_score_playoff",
        description="Admin override for a playoff BO3 game",
    )
    @app_commands.describe(
        match_id="Any game in the playoff series",
        game="Which game to score",
        p1_score="Score for player 1 (left / side 0)",
        p2_score="Score for player 2 (right / side 1)",
    )
    @app_commands.choices(
        game=[
            app_commands.Choice(name="Game 1", value="g1"),
            app_commands.Choice(name="Game 2", value="g2"),
            app_commands.Choice(name="Game 3", value="g3"),
        ]
    )
    @app_commands.autocomplete(match_id=playoff_match_autocomplete)
    @require_host_app()
    async def set_score_playoff_cmd(
        self,
        interaction: discord.Interaction,
        match_id: int,
        game: app_commands.Choice[str],
        p1_score: int,
        p2_score: int,
    ):
        if p1_score == p2_score:
            await interaction.response.send_message(
                "Ties are not allowed.", ephemeral=True
            )
            return

        from sqlalchemy import select, and_
        from sqlalchemy.orm import selectinload
        from bot.db import Match as M, EventType

        game_num = int(game.value[1])
        async with self.db.get_session() as session:
            stmt = (
                select(M)
                .options(selectinload(M.event))
                .where(M.id == match_id)
            )
            match = (await session.execute(stmt)).scalars().first()
            if not match or not match.event or match.event.type != EventType.PLAYOFF:
                await interaction.response.send_message(
                    "That is not a playoff match.", ephemeral=True
                )
                return
            if not match.bracket_slot:
                await interaction.response.send_message(
                    "That playoff match has no bracket slot.", ephemeral=True
                )
                return

            stmt = (
                select(M)
                .options(selectinload(M.match_players))
                .where(
                    and_(
                        M.event_id == match.event_id,
                        M.bracket_slot == match.bracket_slot,
                        M.series_game == game_num,
                    )
                )
            )
            target = (await session.execute(stmt)).scalars().first()
            if not target:
                await interaction.response.send_message(
                    f"{game.name} does not exist for this series yet.",
                    ephemeral=True,
                )
                return
            target_id = target.id
            slot = match.bracket_slot

        winner_side = 0 if p1_score > p2_score else 1
        await interaction.response.defer(ephemeral=True)
        result = await self.match_service.report_score(
            target_id, p1_score, p2_score, winner_side
        )

        if "error" in result:
            await interaction.followup.send(
                f"Failed to set playoff score: {result['error']}", ephemeral=True
            )
            return

        summary = await self.match_service.format_result_summary(target_id)
        suffix = (
            "\nDependent downstream matches were reset for rescoring."
            if result.get("edited")
            else ""
        )
        await interaction.followup.send(
            f"Admin override: {summary}{suffix}",
            ephemeral=True,
        )

    @app_commands.command(name="reports", description="Pending and disputed score reports (moderators)")
    @require_moderator_app()
    async def reports_cmd(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)
        service = MatchReportService()
        reports = await service.list_open()
        if not reports:
            await interaction.followup.send("No pending or disputed reports.", ephemeral=True)
            return

        shown = reports[:10]
        await interaction.followup.send(
            f"**{len(reports)} open report(s)**"
            + (f" — showing the oldest {len(shown)}." if len(reports) > len(shown) else "."),
            ephemeral=True,
        )
        for report in shown:
            label = await service.describe(report)
            side = "P1" if report.winner_side == 0 else "P2"
            lines = [
                f"**Report #{report.id}** · {report.status.value.upper()} · Match {report.match_id}",
                label,
                f"Reported score: {report.p1_goals}-{report.p2_goals} ({side} wins) "
                f"by <@{report.reporter_discord_id}>",
            ]
            if report.reason:
                lines.append(f"Reason: {report.reason}")
            await interaction.followup.send(
                "\n".join(lines), view=score_confirm_view(report.id), ephemeral=True
            )


async def setup(bot):
    await bot.add_cog(MatchCog(bot))
