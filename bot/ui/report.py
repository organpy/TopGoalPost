"""Score reporting modal, preset buttons, and confirmation for match reporting."""

import logging

import discord

from bot.utils.errors import ReportingView

from bot.cogs.checks import is_moderator
from bot.config import Config
from bot.services.audit import current_actor
from bot.services.match_report import MatchReportService


logger = logging.getLogger(__name__)

REPORT_ACTIONS = {
    "confirm": ("Confirm", discord.ButtonStyle.success),
    "reject": ("Reject", discord.ButtonStyle.danger),
    "dispute": ("Dispute", discord.ButtonStyle.secondary),
}

REPORT_COLOR = discord.Color.from_rgb(241, 196, 15)
CONFIRMED_COLOR = discord.Color.from_rgb(46, 204, 113)
REJECTED_COLOR = discord.Color.from_rgb(231, 76, 60)
DISPUTED_COLOR = discord.Color.from_rgb(149, 165, 166)

REPORT_NEXT_STEPS = {
    "confirm": "It's in the books. Check the standings or bracket to see who's next.",
    "reject": "No worries. Send the correct score with `/report`.",
    "dispute": "A moderator will take a look and sort it out.",
    "error": "Give it another try. If it keeps happening, let a host know.",
}


def report_prompt_embed() -> discord.Embed:
    """Ephemeral /report picker header."""
    embed = discord.Embed(
        title="🎮 How did it go?",
        description=(
            "Pick your match from the menu, then tap the final score. Scores "
            "are shown as **your goals – their goals**. Use **Custom score** "
            "if it isn't listed.\n\n"
            "Your opponent will be asked to confirm it before it counts."
        ),
        color=REPORT_COLOR,
    )
    embed.set_footer(text="Only report once the match is over.")
    return embed


def score_report_embed(
    *,
    report_id: int,
    match_id: int,
    reporter_id: int,
    opponent_id: int,
    my_goals: int,
    opponent_goals: int,
) -> discord.Embed:
    """Public score-confirmation card posted after /report."""
    embed = discord.Embed(
        title="🎮 Final score check",
        description=(
            f"Hey <@{opponent_id}>, <@{reporter_id}> just reported your match. "
            "Does this look right?"
        ),
        color=REPORT_COLOR,
    )
    embed.add_field(
        name="Reported score",
        value=(
            f"<@{reporter_id}> **{my_goals} – {opponent_goals}** "
            f"<@{opponent_id}>"
        ),
        inline=False,
    )
    embed.add_field(
        name="What now?",
        value=(
            "Hit **Confirm** if it's correct. If something's off, use "
            "**Reject** or **Dispute** and tell us why."
        ),
        inline=False,
    )
    embed.set_footer(
        text=f"Match #{match_id} · Report #{report_id} · Only your opponent or a moderator can answer"
    )
    return embed


def score_outcome_embed(
    *,
    report_id: int,
    match_id: int,
    action: str,
    content: str,
) -> discord.Embed:
    """Result card reused when a report is confirmed, rejected or disputed."""
    meta = {
        "confirm": ("✅ Result locked in", CONFIRMED_COLOR),
        "reject": ("❌ Score rejected", REJECTED_COLOR),
        "dispute": ("⚠️ Score under review", DISPUTED_COLOR),
        "error": ("⚠️ That score couldn't be saved", REJECTED_COLOR),
    }
    title, color = meta.get(action, ("Score report updated", REPORT_COLOR))
    embed = discord.Embed(title=title, description=content, color=color)
    if next_step := REPORT_NEXT_STEPS.get(action):
        embed.add_field(name="What now?", value=next_step, inline=False)
    embed.set_footer(text=f"Match #{match_id} · Report #{report_id}")
    return embed


def score_notice_embed(
    *,
    report_id: int,
    match_id: int,
    action: str,
    notice: str,
    reason: str | None = None,
) -> discord.Embed:
    """Public notification posted after a report is resolved."""
    meta = {
        "confirm": ("✅ Result locked in", CONFIRMED_COLOR),
        "reject": ("❌ Score rejected", REJECTED_COLOR),
        "dispute": ("⚠️ Score under review", DISPUTED_COLOR),
    }
    title, color = meta.get(action, ("Score report updated", REPORT_COLOR))
    embed = discord.Embed(title=title, description=notice, color=color)
    if reason:
        embed.add_field(name="Reason given", value=f"> {reason}", inline=False)
    if next_step := REPORT_NEXT_STEPS.get(action):
        embed.add_field(name="What now?", value=next_step, inline=False)
    embed.set_footer(text=f"Match #{match_id} · Report #{report_id}")
    return embed


def report_submitted_embed(*, match_id: int) -> discord.Embed:
    """Ephemeral receipt shown to the reporter after /report."""
    embed = discord.Embed(
        title="📨 Score sent",
        description=(
            f"Thanks! Your opponent has been asked to confirm the result of "
            f"**Match #{match_id}**."
        ),
        color=REPORT_COLOR,
    )
    embed.set_footer(text="We'll let you know as soon as they answer.")
    return embed


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
        current_actor.set(interaction.user.id)
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
        if self.action == "confirm":
            await interaction.response.defer()
            await apply_report_action(interaction, self.action, self.report_id)
        else:
            await interaction.response.send_modal(ReportReasonModal(self.action, self.report_id))


class ReportReasonModal(discord.ui.Modal):
    def __init__(self, action: str, report_id: int):
        super().__init__(title=f"{REPORT_ACTIONS[action][0]} score report #{report_id}")
        self.action = action
        self.report_id = report_id
        self.reason = discord.ui.TextInput(
            label="Reason (optional)",
            style=discord.TextStyle.paragraph,
            required=False,
            max_length=500,
        )
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction):
        current_actor.set(interaction.user.id)
        await interaction.response.defer()
        await apply_report_action(
            interaction, self.action, self.report_id, self.reason.value or None
        )


PAST_TENSE = {"confirm": "confirmed", "reject": "rejected", "dispute": "disputed"}


async def apply_report_action(
    interaction: discord.Interaction, action: str, report_id: int, reason: str | None = None
) -> None:
    """Resolve a report, update its public message, and notify both players."""
    service = MatchReportService()
    report = await service.get(report_id)
    if action == "confirm":
        result = await service.confirm(report_id, interaction.user.id)
    else:
        result = await getattr(service, action)(report_id, interaction.user.id, reason)

    if "error" in result:
        await interaction.followup.send(result["error"], ephemeral=True)
        if action == "confirm" and report:
            error_text = (
                f"Something went wrong while saving this score.\n{result['error']}"
            )
            await publish_outcome(
                interaction.client,
                report,
                embed=score_outcome_embed(
                    report_id=report_id,
                    match_id=report.match_id,
                    action="error",
                    content=error_text,
                ),
            )
        return

    if action == "confirm":
        content = f"Both players agree, so it's official. GG!\n**{result['summary']}**"
    else:
        content = f"<@{interaction.user.id}> {PAST_TENSE[action]} this score."
    if reason:
        content += f"\n> {reason}"

    if report is not None:
        await publish_outcome(
            interaction.client,
            report,
            embed=score_outcome_embed(
                report_id=report_id,
                match_id=report.match_id,
                action=action,
                content=content,
            ),
        )

    # Confirmation already updates the original public message in place. Only
    # rejected/disputed reports need a separate notification ping.
    if action != "confirm":
        players = " ".join(f"<@{pid}>" for pid in await service.participants(report))
        notice = (
            f"Heads up, <@{interaction.user.id}> {PAST_TENSE[action]} the score "
            f"for Match #{report.match_id}."
        )
        await _notify(
            interaction.client,
            report,
            players,
            embed=score_notice_embed(
                report_id=report_id,
                match_id=report.match_id,
                action=action,
                notice=notice,
                reason=reason,
            ),
        )
    await interaction.followup.send(f"Report #{report_id} {PAST_TENSE[action]}.", ephemeral=True)


async def _report_channel(client: discord.Client, report):
    if not report.channel_id:
        return None
    channel = client.get_channel(report.channel_id)
    if channel is None:
        try:
            channel = await client.fetch_channel(report.channel_id)
        except discord.HTTPException:
            logger.warning("Report #%s channel %s unavailable", report.id, report.channel_id)
            return None
    return channel


async def publish_outcome(
    client: discord.Client,
    report,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
) -> None:
    """Replace the public confirmation message so stale buttons disappear."""
    channel = await _report_channel(client, report)
    if channel is None or not report.message_id:
        return
    try:
        message = await channel.fetch_message(report.message_id)
        await message.edit(content=content, embed=embed, view=None)
    except discord.HTTPException:
        logger.warning("Could not update report #%s message", report.id, exc_info=True)


async def _notify(
    client: discord.Client,
    report,
    content: str,
    *,
    embed: discord.Embed | None = None,
) -> None:
    channel = await _report_channel(client, report)
    if channel is None:
        return
    try:
        await channel.send(content=content, embed=embed)
    except discord.HTTPException:
        logger.warning("Could not notify players for report #%s", report.id, exc_info=True)


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
    embed = score_report_embed(
        report_id=report.id,
        match_id=match_id,
        reporter_id=reporter_id,
        opponent_id=opponent_discord_id,
        my_goals=my_goals,
        opponent_goals=opponent_goals,
    )

    channel = None
    score_channel_id = await Config().get_channel_id("score_channel")
    if score_channel_id and interaction.guild:
        channel = interaction.guild.get_channel(score_channel_id)
    if not channel:
        # Fallback to the current channel (e.g. the match thread)
        channel = interaction.channel

    message = await channel.send(
        content=f"<@{opponent_discord_id}>",
        embed=embed,
        view=view,
    )
    await MatchReportService().set_message(report.id, channel.id, message.id)
    await interaction.followup.send(
        embed=report_submitted_embed(match_id=match_id),
        ephemeral=True,
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
