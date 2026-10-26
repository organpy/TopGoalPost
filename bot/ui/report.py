"""Score reporting modal, preset buttons, and confirmation for match reporting."""

import discord

from bot.utils.errors import ReportingView

from bot.cogs.checks import is_moderator
from bot.config import Config
from bot.services.match_report import MatchReportService


REPORT_ACTIONS = {
    "confirm": ("Confirm", discord.ButtonStyle.success),
    "reject": ("Reject", discord.ButtonStyle.danger),
    "dispute": ("Dispute", discord.ButtonStyle.secondary),
}


class ReportActionButton(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"matchreport:(?P<action>confirm|reject|dispute):(?P<report_id>\d+)",
):
    """Stateless confirm/reject/dispute button; the report lives in the DB.

    Registered once with ``bot.add_dynamic_items`` so buttons on old messages
    keep working after a restart. Never change the custom_id format.
    """

    def __init__(self, action: str, report_id: int):
        label, style = REPORT_ACTIONS[action]
        super().__init__(
            discord.ui.Button(
                label=label, style=style, custom_id=f"matchreport:{action}:{report_id}"
            )
        )
        self.action = action
        self.report_id = report_id

    @classmethod
    async def from_custom_id(cls, interaction, item, match):
        return cls(match["action"], int(match["report_id"]))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        service = MatchReportService()
        report = await service.get(self.report_id)
        if report is None:
            await interaction.response.send_message("This report no longer exists.", ephemeral=True)
            return False
        is_mod = await is_moderator(interaction.user, interaction.guild)
        if await service.can_resolve(report, interaction.user.id, is_mod):
            return True
        await interaction.response.send_message(
            "Only the opponent or a moderator can resolve this score.", ephemeral=True
        )
        return False

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.defer()
        service = MatchReportService()
        report = await service.get(self.report_id)
        result = await getattr(service, self.action)(self.report_id, interaction.user.id)
        if "error" in result:
            await interaction.followup.send(result["error"], ephemeral=True)
            if self.action == "confirm":
                await interaction.message.edit(
                    content=f"Report #{self.report_id} for Match {report.match_id} could not be applied: {result['error']}",
                    view=None,
                )
            return

        reporter = f"<@{report.reporter_discord_id}>"
        if self.action == "confirm":
            content = f"Score confirmed and submitted successfully!\n{result['summary']}"
            notice = f"{reporter}, your score for Match {report.match_id} was confirmed."
        elif self.action == "reject":
            content = f"Score submission was rejected for Match {report.match_id}."
            notice = f"{reporter}, your score for Match {report.match_id} was rejected. Report again with `/report`."
        else:
            content = (
                f"Score for Match {report.match_id} is **disputed** and waiting for a moderator."
            )
            notice = f"{reporter}, your score for Match {report.match_id} was disputed; a moderator will review it."
        await interaction.message.edit(content=content, view=None)
        await interaction.followup.send(notice)


def score_confirm_view(report_id: int) -> discord.ui.View:
    view = ReportingView(timeout=None)
    for action in REPORT_ACTIONS:
        view.add_item(ReportActionButton(action, report_id))
    return view


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
    """Store the report and post persistent confirmation buttons for it."""
    created = await MatchReportService().create_report(
        match_id, reporter_id, p1_goals, p2_goals, winner_side
    )
    if "error" in created:
        await interaction.followup.send(created["error"], ephemeral=True)
        return

    report = created["report"]
    opponent_discord_id = created["opponent_discord_id"]
    view = score_confirm_view(report.id)
    msg_content = (
        f"<@{opponent_discord_id}>, please confirm the score for Match {match_id}:\n"
        f"**<@{reporter_id}> reported {my_goals} - {opponent_goals}**"
    )

    channel = None
    score_channel_id = await Config().get_channel_id("score_channel")
    if score_channel_id and interaction.guild:
        channel = interaction.guild.get_channel(score_channel_id)
    if channel:
        reply = "Score submitted for confirmation. Please wait for your opponent to confirm."
    else:
        # Fallback to the current channel (e.g. the match thread)
        channel = interaction.channel
        reply = "Score submitted for confirmation in this channel."

    message = await channel.send(content=msg_content, view=view)
    await MatchReportService().set_message(report.id, channel.id, message.id)
    await interaction.followup.send(reply, ephemeral=True)


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


class ReportScoreView(ReportingView):
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
