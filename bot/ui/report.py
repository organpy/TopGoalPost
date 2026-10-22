"""Score reporting modal, preset buttons, and confirmation for match reporting."""

import discord

from bot.db import get_db_manager, MatchRepository
from bot.services.match import MatchService
from bot.config import Config


class ConfirmView(discord.ui.View):
    def __init__(self, match_id: int, p1_goals: int, p2_goals: int, winner_side: int, target_discord_id: int):
        super().__init__(timeout=86400)  # 24 hour timeout
        self.match_id = match_id
        self.p1_goals = p1_goals
        self.p2_goals = p2_goals
        self.winner_side = winner_side
        self.target_discord_id = target_discord_id
        self.value = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        # Check if user is opponent or has mod_role
        if interaction.user.id == self.target_discord_id:
            return True
        cfg = Config()
        mod_role_id = await cfg.get("moderator_role")
        if mod_role_id:
            try:
                mod_role_id = int(mod_role_id)
                if isinstance(interaction.user, discord.Member) and interaction.user.get_role(mod_role_id):
                    return True
            except ValueError:
                pass

        # Also check admin permission as fallback
        if interaction.user.guild_permissions.administrator:
            return True

        await interaction.response.send_message("Only the opponent or a moderator can confirm this score.", ephemeral=True)
        return False

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.success)
    async def confirm_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        await interaction.response.defer()

        async with get_db_manager().get_session() as session:
            current = await MatchRepository(session).get_by_id(self.match_id)
            if current and current.completed_at:
                await interaction.message.edit(
                    content=(
                        f"Match {self.match_id} was already completed while this "
                        "confirmation was open. Ask an admin to correct it with `/set_score`."
                    ),
                    view=None,
                )
                self.stop()
                return

        result = await MatchService().report_score(self.match_id, self.p1_goals, self.p2_goals, self.winner_side)
        if "error" in result:
            await interaction.message.edit(content=f"Error reporting score: {result['error']}", view=None)
        else:
            summary = await MatchService().format_result_summary(self.match_id)
            await interaction.message.edit(
                content=f"Score confirmed and submitted successfully!\n{summary}",
                view=None,
            )
        self.stop()

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger)
    async def reject_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.defer()
        await interaction.message.edit(content=f"Score submission was rejected for Match {self.match_id}.", view=None)
        self.stop()

    async def on_timeout(self):
        self.value = None


async def _send_confirmation(
    interaction: discord.Interaction,
    match_id: int,
    reporter_id: int,
    p1_goals: int,
    p2_goals: int,
    winner_side: int,
    my_goals: int,
    opponent_goals: int,
) -> None:
    """Find the opponent and post a confirmation view for a reported score."""
    db = get_db_manager()
    opponent_discord_id = None
    async with db.get_session() as session:
        match = await MatchRepository(session).get_by_id(match_id)
        if match:
            for mp in match.match_players:
                if mp.player and mp.player.discord_id != reporter_id:
                    opponent_discord_id = mp.player.discord_id
                    break

    if not opponent_discord_id:
        await interaction.followup.send(
            "Could not find your opponent to confirm the score.", ephemeral=True
        )
        return

    cfg = Config()
    score_channel_id = await cfg.get_channel_id("score_channel")
    view = ConfirmView(
        match_id, p1_goals, p2_goals, winner_side, opponent_discord_id
    )
    msg_content = (
        f"<@{opponent_discord_id}>, please confirm the score for Match {match_id}:\n"
        f"**<@{reporter_id}> reported {my_goals} - {opponent_goals}**"
    )

    if score_channel_id and interaction.guild:
        channel = interaction.guild.get_channel(score_channel_id)
        if channel:
            await channel.send(content=msg_content, view=view)
            await interaction.followup.send(
                "Score submitted for confirmation. Please wait for your opponent to confirm.",
                ephemeral=True,
            )
            return

    # Fallback to sending in the same channel (e.g. the match thread)
    await interaction.channel.send(content=msg_content, view=view)
    await interaction.followup.send(
        "Score submitted for confirmation in this channel.", ephemeral=True
    )


class ScoreReportModal(discord.ui.Modal, title="Report Match Score"):
    def __init__(self, match_id: int, reporter_side: int, reporter_id: int):
        super().__init__()
        self.match_id = match_id
        self.reporter_side = reporter_side
        self.reporter_id = reporter_id

        self.my_goals = discord.ui.TextInput(
            label="Your Goals",
            placeholder="e.g. 3",
            min_length=1,
            max_length=2,
            required=True,
        )
        self.opponent_goals = discord.ui.TextInput(
            label="Opponent's Goals",
            placeholder="e.g. 1",
            min_length=1,
            max_length=2,
            required=True,
        )
        self.add_item(self.my_goals)
        self.add_item(self.opponent_goals)

    async def on_submit(self, interaction: discord.Interaction):
        try:
            my_g = int(self.my_goals.value)
            opp_g = int(self.opponent_goals.value)
        except ValueError:
            await interaction.response.send_message("Scores must be numbers.", ephemeral=True)
            return

        if my_g < 0 or opp_g < 0:
            await interaction.response.send_message("Scores cannot be negative.", ephemeral=True)
            return

        if my_g == opp_g:
            await interaction.response.send_message("Ties are not allowed.", ephemeral=True)
            return

        p1_goals = my_g if self.reporter_side == 0 else opp_g
        p2_goals = my_g if self.reporter_side == 1 else opp_g
        winner_side = 0 if p1_goals > p2_goals else 1

        await interaction.response.defer()
        await _send_confirmation(
            interaction,
            self.match_id,
            self.reporter_id,
            p1_goals,
            p2_goals,
            winner_side,
            my_g,
            opp_g,
        )


class ScoreButton(discord.ui.Button):
    def __init__(self, score_l: int, score_r: int, row: int):
        super().__init__(
            label=f"{score_l}-{score_r}",
            row=row,
            style=(
                discord.ButtonStyle.green
                if score_l > score_r
                else discord.ButtonStyle.red
            ),
        )
        self.score_l = score_l
        self.score_r = score_r

    async def callback(self, interaction: discord.Interaction):
        await self.view.report_score(interaction, self.score_l, self.score_r)


class CustomScoreButton(discord.ui.Button):
    def __init__(self, row: int):
        super().__init__(
            label="Custom score",
            row=row,
            style=discord.ButtonStyle.secondary,
        )

    async def callback(self, interaction: discord.Interaction):
        await self.view.custom_score(interaction)


class ReportMatchSelect(discord.ui.Select):
    def __init__(self, options: list[discord.SelectOption]):
        super().__init__(placeholder="Select your match", options=options, row=0)

    async def callback(self, interaction: discord.Interaction):
        await self.view.select_match(interaction, int(self.values[0]))


class ReportScoreView(discord.ui.View):
    """Old-style report flow: pick match, then a preset or custom score."""

    def __init__(self, matches: list[dict], reporter_id: int):
        super().__init__(timeout=900)
        self.reporter_id = reporter_id
        self.matches = {m["match_id"]: m for m in matches}
        self.match_id = None
        self.reporter_side = None

        options = [
            discord.SelectOption(
                label=m["label"][:100],
                value=str(m["match_id"]),
                default=(len(matches) == 1),
            )
            for m in matches[:25]
        ]
        self.add_item(ReportMatchSelect(options))
        for i in range(5):
            self.add_item(ScoreButton(5, i, row=1))
        for i in range(5):
            self.add_item(ScoreButton(i, 5, row=2))
        self.add_item(CustomScoreButton(row=3))

        if len(matches) == 1:
            self.match_id = matches[0]["match_id"]
            self.reporter_side = matches[0]["side"]

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.reporter_id:
            return True
        await interaction.response.send_message(
            "This score reporter belongs to someone else.", ephemeral=True
        )
        return False

    async def select_match(self, interaction: discord.Interaction, match_id: int):
        self.match_id = match_id
        self.reporter_side = self.matches[match_id]["side"]
        await interaction.response.defer()

    async def report_score(
        self, interaction: discord.Interaction, my_goals: int, opponent_goals: int
    ):
        if self.match_id is None:
            await interaction.response.send_message(
                "Select your match first.", ephemeral=True
            )
            return

        p1_goals = my_goals if self.reporter_side == 0 else opponent_goals
        p2_goals = opponent_goals if self.reporter_side == 0 else my_goals
        winner_side = 0 if p1_goals > p2_goals else 1

        await interaction.response.defer()
        await _send_confirmation(
            interaction,
            self.match_id,
            self.reporter_id,
            p1_goals,
            p2_goals,
            winner_side,
            my_goals,
            opponent_goals,
        )

    async def custom_score(self, interaction: discord.Interaction):
        if self.match_id is None:
            await interaction.response.send_message(
                "Select your match first.", ephemeral=True
            )
            return
        await interaction.response.send_modal(
            ScoreReportModal(
                self.match_id, self.reporter_side, self.reporter_id
            )
        )
