"""ポモドーロ: 25分集中+5分休憩をサーバー同期。VC移動/アンミュート通知はテキスト通知"""
from __future__ import annotations
import discord
from discord.ext import commands, tasks
from discord import app_commands
import time
import config
from utils import db
from utils.helpers import now_jst

class PomodoroCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.tick.start()

    def cog_unload(self):
        self.tick.cancel()

    @tasks.loop(seconds=15)
    async def tick(self):
        for guild in self.bot.guilds:
            row = await db.get_pomodoro(guild.id)
            if not row or not row["is_running"] or not row["ends_at"]:
                continue
            now = int(time.time())
            if now < row["ends_at"]:
                continue
            # 期限到来 → 切替
            ch = self.bot.get_channel(row["channel_id"]) if row["channel_id"] else None
            is_break = bool(row["is_break"])
            cycle = row["cycle"] or 0
            if is_break:
                # 休憩終了 → 次の作業
                cycle += 1
                if cycle >= config.POMODORO_CYCLES:
                    await db.clear_pomodoro(guild.id)
                    if isinstance(ch, discord.TextChannel):
                        try:
                            await ch.send("✅ ポモドーロ完了！4セットやり切ったよ。お疲れさま！")
                        except Exception:
                            pass
                    continue
                ends_at = now + config.POMODORO_WORK_MIN * 60
                await db.upsert_pomodoro(guild.id, channel_id=row["channel_id"], is_running=1, is_break=0, cycle=cycle, ends_at=ends_at, started_at=now)
                if isinstance(ch, discord.TextChannel):
                    try:
                        await ch.send(f"🔔 休憩終了！ {cycle+1}セット目 — **{config.POMODORO_WORK_MIN}分 集中** スタート。ミュートして集中しよう。")
                    except Exception:
                        pass
            else:
                # 作業終了 → 休憩
                ends_at = now + config.POMODORO_BREAK_MIN * 60
                await db.upsert_pomodoro(guild.id, channel_id=row["channel_id"], is_running=1, is_break=1, cycle=cycle, ends_at=ends_at, started_at=now)
                if isinstance(ch, discord.TextChannel):
                    try:
                        await ch.send(f"☕ 集中終了！ **{config.POMODORO_BREAK_MIN}分 休憩** — ミュート解除して雑談OK。")
                    except Exception:
                        pass

    @tick.before_loop
    async def before_tick(self):
        await self.bot.wait_until_ready()

    @app_commands.command(name="pomodoro_start", description="ポモドーロを開始（25分/5分）")
    @app_commands.describe(work="集中分数", break_min="休憩分数")
    async def pomodoro_start(self, interaction: discord.Interaction, work: int | None = None, break_min: int | None = None):
        if not interaction.guild or not isinstance(interaction.channel, discord.TextChannel):
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        wm = work or config.POMODORO_WORK_MIN
        bm = break_min or config.POMODORO_BREAK_MIN
        now = int(time.time())
        ends_at = now + wm * 60
        await db.upsert_pomodoro(interaction.guild.id, channel_id=interaction.channel.id, is_running=1, is_break=0, cycle=0, ends_at=ends_at, started_at=now)
        await interaction.response.send_message(f"🍅 ポモドーロ開始！ **{wm}分 集中** → 終わったら **{bm}分 休憩** を {config.POMODORO_CYCLES}セット。", ephemeral=True)

    @app_commands.command(name="pomodoro_stop", description="ポモドーロを停止")
    async def pomodoro_stop(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        await db.clear_pomodoro(interaction.guild.id)
        await interaction.response.send_message("ポモドーロを停止しました。", ephemeral=True)

    @app_commands.command(name="pomodoro_status", description="ポモドーロの状況を見る")
    async def pomodoro_status(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        row = await db.get_pomodoro(interaction.guild.id)
        if not row or not row["is_running"]:
            await interaction.response.send_message("ポモドーロは動いていません。", ephemeral=True)
            return
        remain = max(0, (row["ends_at"] or 0) - int(time.time()))
        m, s = divmod(remain, 60)
        phase = "休憩中" if row["is_break"] else "集中中"
        await interaction.response.send_message(f"{phase} — 残り **{m}分{s}秒**（{row['cycle']+1}/{config.POMODORO_CYCLES}セット目）", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(PomodoroCog(bot))
