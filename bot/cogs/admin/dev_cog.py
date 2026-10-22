"""Dev utility commands for testing / staging.

All commands require administrator permissions.
Text-only (`!` prefix) — no slash commands.
"""

import random

from discord.ext import commands

from bot.cogs.checks import require_host

from bot.services.dev import DevService, FLAG_POOL
from bot.services.season import SeasonService


class DevCog(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.dev_service = DevService()
        self.season_service = SeasonService()

    @commands.command(name="dummy_add")
    @require_host()
    async def dummy_add(self, ctx: commands.Context, num: int = 12):
        """Add N dummy players to the current season.

        Usage: !dummy_add [num]   (default 12)
        """
        season_num = await self.season_service.get_current_season_number()
        if not season_num:
            await ctx.send("No active season. Create one first: `!season create N`")
            return

        start_id = await self.dev_service.next_dummy_start()
        msg = await ctx.send(f"Adding {num} dummy players...")
        added = 0
        skipped = 0

        for i in range(num):
            discord_id = start_id + i
            name = f"Dummy{start_id - 9_000_000 + i + 1:04d}"
            country = random.choice(FLAG_POOL)
            ok, message = await self.dev_service.dummy_register(
                season_num, discord_id, name, country,
            )
            if ok:
                added += 1
            else:
                skipped += 1

        await msg.edit(content=
            f"Added **{added}** dummy players to season **{season_num}**."
            + (f" ({skipped} skipped — already registered)" if skipped else "")
        )

    @commands.command(name="dummy_score")
    @require_host()
    async def dummy_score(self, ctx: commands.Context):
        """Score all unplayed matches with random results.

        Usage: !dummy_score
        """
        msg = await ctx.send("Scoring all unplayed matches...")
        try:
            scored = await self.dev_service.dummy_score_all()
            await msg.edit(content=f"Scored **{scored}** matches.")
        except Exception as e:
            await msg.edit(content=f"Error scoring matches: {e}")

    @commands.command(name="dummy_replace")
    @require_host()
    async def dummy_replace(
        self, ctx: commands.Context, old_id: int, new_id: int
    ):
        """Replace a player's Discord ID.

        Usage: !dummy_replace <old_discord_id> <new_discord_id>
        """
        msg = await ctx.send(f"Replacing `{old_id}` → `{new_id}`...")
        ok, message = await self.dev_service.dummy_replace_id(old_id, new_id)
        await msg.edit(content=f"{'✅' if ok else '❌'} {message}")


async def setup(bot):
    await bot.add_cog(DevCog(bot))
