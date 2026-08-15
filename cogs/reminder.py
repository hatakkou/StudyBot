"""リマインダー: 自然言語パース / 定期 / スヌーズ"""
from __future__ import annotations
import time
import discord
from discord.ext import commands, tasks
from discord import app_commands
from utils import db
from utils.helpers import parse_remind, next_trigger_for_repeat, now_jst, ts_to_jst, format_duration

class ReminderView(discord.ui.View):
    def __init__(self, reminder_id: int):
        super().__init__(timeout=None)
        self.reminder_id = reminder_id

    @discord.ui.button(label="完了", style=discord.ButtonStyle.success, custom_id="remind:done")
    async def done(self, interaction: discord.Interaction, button: discord.ui.Button):
        rid = self.reminder_id
        # custom_id に埋め込まれたIDではなくViewインスタンスのIDを使う。永続化のためcustom_idにIDを埋める運用にする
        # ただしこのViewは送信時にcustom_idを上書きするため、interaction側のcustom_idを参照する
        try:
            cid = interaction.data.get("custom_id", "") if interaction.data else ""  # type: ignore
            if ":" in cid:
                rid = int(cid.split(":")[-1])
        except Exception:
            pass
        await db.deactivate_reminder(rid)
        await interaction.response.send_message("完了にしました。", ephemeral=True)
        # 元メッセージのボタンを無効化（試みる）
        try:
            await interaction.message.edit(view=None)  # type: ignore
        except Exception:
            pass

    @discord.ui.button(label="スヌーズ 10分", style=discord.ButtonStyle.secondary, custom_id="remind:snooze")
    async def snooze(self, interaction: discord.Interaction, button: discord.ui.Button):
        rid = self.reminder_id
        try:
            cid = interaction.data.get("custom_id", "") if interaction.data else ""  # type: ignore
            if ":" in cid:
                rid = int(cid.split(":")[-1])
        except Exception:
            pass
        await db.snooze_reminder(rid, 600)
        await interaction.response.send_message("10分後に再通知します。", ephemeral=True)

def _make_reminder_view(rid: int) -> discord.ui.View:
    v = discord.ui.View(timeout=None)
    b_done = discord.ui.Button(label="完了", style=discord.ButtonStyle.success, custom_id=f"remind:done:{rid}")
    b_snooze = discord.ui.Button(label="スヌーズ 10分", style=discord.ButtonStyle.secondary, custom_id=f"remind:snooze:{rid}")

    async def done_cb(inter: discord.Interaction):
        await db.deactivate_reminder(rid)
        await inter.response.send_message("完了にしました。", ephemeral=True)
        try:
            await inter.message.edit(view=None)  # type: ignore
        except Exception:
            pass

    async def snooze_cb(inter: discord.Interaction):
        await db.snooze_reminder(rid, 600)
        await inter.response.send_message("10分後に再通知します。", ephemeral=True)

    b_done.callback = done_cb  # type: ignore
    b_snooze.callback = snooze_cb  # type: ignore
    v.add_item(b_done)
    v.add_item(b_snooze)
    return v

class ReminderCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.poll.start()

    def cog_unload(self):
        self.poll.cancel()

    async def cog_load(self):
        # 永続Viewのダミー登録（custom_idパターンで拾うため on_interaction で処理）
        pass

    @commands.Cog.listener()
    async def on_interaction(self, interaction: discord.Interaction):
        # 永続ボタンのフォールバック（View再登録なしでも動くように）
        if interaction.type != discord.InteractionType.component:
            return
        data = interaction.data or {}  # type: ignore
        cid = data.get("custom_id", "")  # type: ignore
        if not isinstance(cid, str) or not cid.startswith("remind:"):
            return
        # View側で処理済みなら二重応答を避ける
        if interaction.response.is_done():
            return
        parts = cid.split(":")
        if len(parts) < 3:
            return
        action = parts[1]
        try:
            rid = int(parts[2])
        except ValueError:
            return
        if action == "done":
            await db.deactivate_reminder(rid)
            try:
                await interaction.response.send_message("完了にしました。", ephemeral=True)
            except discord.errors.InteractionResponded:
                pass
            try:
                if interaction.message:
                    await interaction.message.edit(view=None)
            except Exception:
                pass
        elif action == "snooze":
            await db.snooze_reminder(rid, 600)
            try:
                await interaction.response.send_message("10分後に再通知します。", ephemeral=True)
            except discord.errors.InteractionResponded:
                pass

    @tasks.loop(seconds=30)
    async def poll(self):
        rows = await db.fetch_due_reminders()
        for r in rows:
            ch = self.bot.get_channel(r["channel_id"])
            # DM的な代替: チャンネルが見つからなければスキップ
            if not isinstance(ch, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
                # チャンネルが取れない場合はギルドのシステムチャンネルや作成者へのDMはしない（安全側）
                if r["repeat_rule"]:
                    nxt = next_trigger_for_repeat(r["repeat_rule"], r["trigger_at"])
                    if nxt:
                        await db.reschedule_repeating(r["id"], nxt)
                    else:
                        await db.deactivate_reminder(r["id"])
                else:
                    await db.deactivate_reminder(r["id"])
                continue
            view = _make_reminder_view(r["id"])
            when = ts_to_jst(r["trigger_at"]).strftime("%m/%d %H:%M")
            try:
                await ch.send(f"⏰ <@{r['user_id']}> リマインダー ({when}): **{r['message']}**", view=view, allowed_mentions=discord.AllowedMentions(users=True))
            except Exception:
                pass
            if r["repeat_rule"]:
                nxt = next_trigger_for_repeat(r["repeat_rule"], r["trigger_at"])
                if nxt:
                    await db.reschedule_repeating(r["id"], nxt)
                else:
                    await db.deactivate_reminder(r["id"])
            else:
                await db.deactivate_reminder(r["id"])

    @poll.before_loop
    async def before_poll(self):
        await self.bot.wait_until_ready()

    # ---- スラッシュ ----
    @app_commands.command(name="remind", description="リマインダーを設定（例: あと30分 / 22:00 / 毎週月曜19時 / 毎日23時）")
    @app_commands.describe(when="いつ（例: あと30分 / 22:00 ねる / 毎日23時 寝よう）", message="メッセージ（whenに含めなかった場合）")
    async def remind(self, interaction: discord.Interaction, when: str, message: str | None = None):
        # when と message を結合してパース（messageが別引数なら末尾に付与）
        raw = when.strip()
        if message:
            raw = f"{raw} {message.strip()}"
        ts, repeat, msg = parse_remind(raw, base=now_jst())
        if ts is None or msg is None:
            await interaction.response.send_message(
                "読み取れませんでした。例:\n"
                "• `/remind when:あと30分 休憩終わり`\n"
                "• `/remind when:22:00 寝る`\n"
                "• `/remind when:毎日23時 そろそろ寝よう`\n"
                "• `/remind when:毎週月曜19時 勉強会`",
                ephemeral=True,
            )
            return
        # ギルド外からの実行も考慮
        gid = interaction.guild.id if interaction.guild else None
        ch_id = interaction.channel.id if interaction.channel else 0  # type: ignore
        rid = await db.add_reminder(interaction.user.id, gid, ch_id, msg, ts, repeat)
        when_s = ts_to_jst(ts).strftime("%Y/%m/%d %H:%M")
        rep_s = f"（繰り返し: {repeat}）" if repeat else ""
        await interaction.response.send_message(f"セットしました: **{msg}** → {when_s} {rep_s} (ID:{rid})", ephemeral=True)

    @app_commands.command(name="remind_list", description="自分のリマインダー一覧")
    async def remind_list(self, interaction: discord.Interaction):
        gid = interaction.guild.id if interaction.guild else None
        rows = await db.list_reminders(interaction.user.id, gid)
        if not rows:
            await interaction.response.send_message("リマインダーはありません。", ephemeral=True)
            return
        lines = []
        for r in rows[:20]:
            when = ts_to_jst(r["trigger_at"]).strftime("%m/%d %H:%M")
            rep = f" 🔁{r['repeat_rule']}" if r["repeat_rule"] else ""
            lines.append(f"`{r['id']}` {when}{rep} — {r['message']}")
        await interaction.response.send_message("\n".join(lines), ephemeral=True)

    @app_commands.command(name="remind_cancel", description="リマインダーをキャンセル")
    @app_commands.describe(id="ID（/remind_list で確認）")
    async def remind_cancel(self, interaction: discord.Interaction, id: int):
        # 本人のものだけ消せる
        rows = await db.list_reminders(interaction.user.id, None)
        ids = {r["id"] for r in rows}
        if id not in ids:
            await interaction.response.send_message("そのIDは見つからないか、あなたのリマインダーではありません。", ephemeral=True)
            return
        await db.deactivate_reminder(id)
        await interaction.response.send_message(f"ID:{id} をキャンセルしました。", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(ReminderCog(bot))
