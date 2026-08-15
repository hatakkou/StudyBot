"""集計・リーダーボード・レポート"""
from __future__ import annotations
import datetime as dt
import discord
from discord.ext import commands, tasks
from discord import app_commands
import config
from utils import db
from utils.helpers import format_duration, day_bounds, week_bounds_monday, now_jst, ts_to_jst, TZ

def _embed(title: str, desc: str) -> discord.Embed:
    return discord.Embed(title=title, description=desc, color=0x5865F2, timestamp=now_jst())

class StatsCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.daily_report.start()

    def cog_unload(self):
        self.daily_report.cancel()

    # ---- スラッシュ: 集計 ----
    @app_commands.command(name="study_today", description="今日の勉強時間を見る")
    async def study_today(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        since, _ = day_bounds()
        total, by_subject, rows = await db.aggregate_user(interaction.user.id, interaction.guild.id, since_ts=since)
        # 進行中セッションがあれば経過を表示
        sess = await db.get_open_session(interaction.user.id, interaction.guild.id)
        extra = ""
        if sess:
            from utils.db import _effective_seconds
            extra = f"\n進行中: **{format_duration(_effective_seconds(sess))}** （{'休憩中' if sess['is_paused'] else '勉強中'}）"
        desc = f"合計: **{format_duration(total)}**{extra}\n"
        if by_subject:
            desc += "\n".join([f"• {k}: {format_duration(v)}" for k, v in sorted(by_subject.items(), key=lambda x: x[1], reverse=True)])
        else:
            desc += "（まだ記録がありません）"
        await interaction.response.send_message(embed=_embed("📊 今日の勉強時間", desc), ephemeral=True)

    @app_commands.command(name="study_week", description="今週の勉強時間を見る")
    async def study_week(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        since, _ = week_bounds_monday()
        total, by_subject, rows = await db.aggregate_user(interaction.user.id, interaction.guild.id, since_ts=since)
        desc = f"合計: **{format_duration(total)}**\n"
        if by_subject:
            desc += "\n".join([f"• {k}: {format_duration(v)}" for k, v in sorted(by_subject.items(), key=lambda x: x[1], reverse=True)])
        else:
            desc += "（まだ記録がありません）"
        await interaction.response.send_message(embed=_embed("📊 今週の勉強時間", desc), ephemeral=True)

    @app_commands.command(name="study_all", description="全期間の勉強時間を見る")
    async def study_all(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        total, by_subject, rows = await db.aggregate_user(interaction.user.id, interaction.guild.id)
        desc = f"合計: **{format_duration(total)}** （{len([r for r in rows if r['ended_at'] is not None])}セッション）\n"
        if by_subject:
            desc += "\n".join([f"• {k}: {format_duration(v)}" for k, v in sorted(by_subject.items(), key=lambda x: x[1], reverse=True)])
        await interaction.response.send_message(embed=_embed("📊 全期間の勉強時間", desc), ephemeral=True)

    @app_commands.command(name="study_history", description="最近の勉強履歴を見る")
    @app_commands.describe(limit="表示件数（既定10）")
    async def study_history(self, interaction: discord.Interaction, limit: int = 10):
        if not interaction.guild:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        limit = max(1, min(25, limit))
        rows = await db.fetch_sessions(interaction.user.id, interaction.guild.id)
        # 新しい順に limit
        rows = list(reversed(rows))[:limit]
        if not rows:
            await interaction.response.send_message("履歴がありません。", ephemeral=True)
            return
        lines = []
        for r in rows:
            from utils.db import _effective_seconds
            eff = _effective_seconds(r)
            s = ts_to_jst(r["started_at"]).strftime("%m/%d %H:%M")
            e = ts_to_jst(r["ended_at"]).strftime("%H:%M") if r["ended_at"] else "進行中"
            subj = r["subject"] or "未設定"
            state = "休憩中" if r["is_paused"] and r["ended_at"] is None else ""
            lines.append(f"`{s}–{e}` {subj} **{format_duration(eff)}** {state}")
        await interaction.response.send_message(embed=_embed("📜 勉強履歴", "\n".join(lines)), ephemeral=True)

    @app_commands.command(name="leaderboard", description="週間ランキングを見る")
    async def leaderboard(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        since, _ = week_bounds_monday()
        totals = await db.leaderboard(interaction.guild.id, since)
        if not totals:
            await interaction.response.send_message("今週の記録はまだありません。", ephemeral=True)
            return
        lines = []
        for i, (uid, sec) in enumerate(totals[:10], 1):
            medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, f"{i}.")
            member = interaction.guild.get_member(uid)
            name = member.display_name if member else f"<@{uid}>"
            lines.append(f"{medal} {name} — **{format_duration(sec)}**")
        await interaction.response.send_message(embed=_embed("🏆 週間ランキング", "\n".join(lines)), ephemeral=True)

    @app_commands.command(name="study_report", description="週報/日報を手動で投稿する")
    @app_commands.describe(period="today / week")
    @app_commands.choices(period=[app_commands.Choice(name="今日", value="today"), app_commands.Choice(name="今週", value="week")])
    async def study_report(self, interaction: discord.Interaction, period: str = "today"):
        if not interaction.guild:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        if period == "week":
            since, _ = week_bounds_monday()
            title = "📊 今週の勉強レポート"
        else:
            since, _ = day_bounds()
            title = "📊 今日の勉強レポート"
        totals = await db.leaderboard(interaction.guild.id, since)
        if not totals:
            await interaction.followup.send("まだ記録がありません。", ephemeral=True)
            return
        lines = []
        for i, (uid, sec) in enumerate(totals[:10], 1):
            member = interaction.guild.get_member(uid)
            name = member.display_name if member else f"<@{uid}>"
            lines.append(f"{i}. {name} — {format_duration(sec)}")
        embed = _embed(title, "\n".join(lines))
        await interaction.followup.send(embed=embed, ephemeral=True)
        # 投稿先が指定されていればそちらにも転送
        if config.REPORT_CHANNEL_ID:
            ch = self.bot.get_channel(config.REPORT_CHANNEL_ID)
            if isinstance(ch, discord.TextChannel):
                try:
                    await ch.send(embed=embed)
                except Exception:
                    pass

    # ---- 日報タスク: 毎日 23:00 JST に投稿（JSTの23時は運用で「そろそろ寝よう」含む） ----
    @tasks.loop(minutes=1)
    async def daily_report(self):
        now = now_jst()
        # 23:00ちょうどだけ発火（分まで一致、秒はループ誤差で59秒以内ならOKだが分一致で判定）
        if now.hour != 23 or now.minute != 0:
            return
        for guild in self.bot.guilds:
            ch_id = config.REPORT_CHANNEL_ID
            # ギルド別のレポートチャンネルがなければ最初のテキストチャンネルにフォールバックしない（誤爆防止のため設定時のみ投稿）
            if not ch_id:
                continue
            ch = self.bot.get_channel(ch_id)
            if not isinstance(ch, discord.TextChannel):
                continue
            if ch.guild.id != guild.id:
                continue
            since, _ = day_bounds(now.date())
            totals = await db.leaderboard(guild.id, since)
            if not totals:
                try:
                    await ch.send("📊 今日の勉強記録はありませんでした。お疲れさま！")
                except Exception:
                    pass
                continue
            lines = []
            for i, (uid, sec) in enumerate(totals[:10], 1):
                member = guild.get_member(uid)
                name = member.display_name if member else f"<@{uid}>"
                lines.append(f"{i}. {name} — {format_duration(sec)}")
            embed = _embed("📊 今日の勉強レポート", "\n".join(lines))
            embed.set_footer(text="明日もがんばろう！")
            try:
                await ch.send(embed=embed)
            except Exception:
                pass

    @daily_report.before_loop
    async def before_daily(self):
        await self.bot.wait_until_ready()


async def setup(bot: commands.Bot):
    await bot.add_cog(StatsCog(bot))
