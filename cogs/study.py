"""勉強記録Cog: ボタン常設パネル / スラッシュ / VC自動検知 / 再起動復元
- 特定VC入室で自動で勉強開始
- 別テキストチャンネルにメンション通知を送り、10秒後に自動削除
- すべてのコマンド/ボタン応答は ephemeral=True (本人のみに見える)
"""
from __future__ import annotations
import asyncio
import logging
import discord
from discord.ext import commands, tasks
from discord import app_commands
import config
from utils import db
from utils.helpers import format_duration, is_study_channel, TZ, now_jst

log = logging.getLogger(__name__)

SUBJECT_CHOICES = [app_commands.Choice(name=s, value=s) for s in config.SUBJECTS]

def _embed(title: str, desc: str, color: int = 0x5865F2) -> discord.Embed:
    return discord.Embed(title=title, description=desc, color=color, timestamp=now_jst())



# ---------- 常設パネル View ----------
class StudyPanelView(discord.ui.View):
    def __init__(self, cog: "StudyCog"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(label="勉強開始", style=discord.ButtonStyle.success, custom_id="study:start", emoji="▶️")
    async def btn_start(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_button_start(interaction)

    @discord.ui.button(label="休憩", style=discord.ButtonStyle.secondary, custom_id="study:pause", emoji="☕")
    async def btn_pause(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_button_pause(interaction)

    @discord.ui.button(label="再開", style=discord.ButtonStyle.primary, custom_id="study:resume", emoji="🔔")
    async def btn_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_button_resume(interaction)

    @discord.ui.button(label="終了", style=discord.ButtonStyle.danger, custom_id="study:end", emoji="⏹️")
    async def btn_end(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_button_end(interaction)

    @discord.ui.button(label="科目変更", style=discord.ButtonStyle.secondary, custom_id="study:subject", emoji="📚")
    async def btn_subject(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.cog.handle_button_subject(interaction)


class SubjectSelectView(discord.ui.View):
    def __init__(self, cog: "StudyCog", user_id: int):
        super().__init__(timeout=60)
        self.cog = cog
        self.user_id = user_id
        options = [discord.SelectOption(label=s, value=s) for s in config.SUBJECTS]
        select = discord.ui.Select(placeholder="科目を選択", options=options, custom_id="study:subject_select")

        async def callback(inter: discord.Interaction):
            if inter.user.id != self.user_id:
                await inter.response.send_message("自分用の選択です。", ephemeral=True)
                return
            subject = select.values[0]
            sess = await db.get_open_session(inter.user.id, inter.guild.id if inter.guild else 0)  # type: ignore
            if sess:
                await db.update_session_subject(sess["id"], subject)
                await inter.response.send_message(f"科目を **{subject}** に変更しました。", ephemeral=True)
            else:
                # セッションが無い場合は新規作成して科目付きで開始
                gid = inter.guild.id if inter.guild else 0  # type: ignore
                ch_id = inter.user.voice.channel.id if inter.user.voice and inter.user.voice.channel else None  # type: ignore
                await db.create_session(inter.user.id, gid, ch_id, subject, auto_started=False)
                await inter.response.send_message(f"**{subject}** で勉強を開始しました！", ephemeral=True)
            self.stop()

        select.callback = callback  # type: ignore
        self.add_item(select)


class StudyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # 永続View登録（再起動してもボタンが生きる）
        self.bot.add_view(StudyPanelView(self))

    # ---- 通知ヘルパー: テキストチャンネルにメンションを送って10秒後に自動削除 ----
    async def _notify_channel(self, guild: discord.Guild) -> discord.TextChannel | None:
        """通知先テキストチャンネルを解決。STUDY_NOTIFY_CHANNEL_ID優先、未設定ならSTUDY_PANEL_CHANNEL_ID、両方なければNone"""
        cid = config.STUDY_NOTIFY_CHANNEL_ID or config.STUDY_PANEL_CHANNEL_ID
        if not cid:
            return None
        ch = self.bot.get_channel(cid)
        if isinstance(ch, discord.TextChannel) and ch.guild.id == guild.id:
            return ch
        try:
            fetched = await self.bot.fetch_channel(cid)
            if isinstance(fetched, discord.TextChannel) and fetched.guild.id == guild.id:
                return fetched
        except Exception:
            pass
        return None

    async def _send_auto_delete_mention(self, member: discord.Member, title: str, desc: str, color: int = 0x5865F2, delete_after: float = 10.0):
        """
        通知チャンネルに <@user> + Embed を送り、delete_after秒後に自動削除する。
        Botの「メッセージの管理」権限が必要。削除はバックグラウンドで行うため呼び出し元をブロックしない。
        """
        ch = await self._notify_channel(member.guild)
        if ch is None:
            log.warning("通知チャンネル未設定のためスキップ (guild=%s user=%s)", member.guild.id, member.id)
            return
        embed = _embed(title, desc, color=color)
        try:
            msg = await ch.send(
                content=f"{member.mention}",
                embed=embed,
                allowed_mentions=discord.AllowedMentions(users=[member], everyone=False, roles=False, replied_user=False),
            )
        except (discord.Forbidden, discord.HTTPException) as e:
            log.warning("通知チャンネル送信失敗 ch=%s: %s", ch.id if ch else "?", e)
            return

        async def _delete_later():
            try:
                await asyncio.sleep(delete_after)
                await msg.delete()
            except discord.NotFound:
                pass
            except discord.Forbidden:
                log.warning("自動削除に失敗（権限不足） ch=%s msg=%s", ch.id, msg.id)
            except Exception as e:
                log.debug("自動削除失敗: %s", e)

        # ブロックせずに削除タスクを走らせる
        asyncio.create_task(_delete_later())

    # ---- 共通ロジック ----
    async def _start(self, user: discord.abc.User | discord.Member, guild_id: int, channel_id: int | None, subject: str | None, auto: bool = False) -> tuple[bool, str]:
        sess = await db.get_open_session(user.id, guild_id)
        if sess:
            return False, "すでに勉強セッションが進行中です。`/study_end` かパネルの「終了」で先に終了してください。"
        await db.create_session(user.id, guild_id, channel_id, subject, auto_started=auto)
        label = f"（科目: {subject}）" if subject else ""
        prefix = "自動記録: " if auto else ""
        return True, f"{prefix}勉強を開始しました！{label}"

    async def _end(self, user_id: int, guild_id: int) -> tuple[bool, str]:
        sess = await db.get_open_session(user_id, guild_id)
        if not sess:
            return False, "進行中のセッションがありません。"
        # 有効秒を計算してから終了
        from utils.db import _effective_seconds
        # 終了前に有効秒を計算（pause中も考慮）
        eff_before = _effective_seconds(sess)
        await db.end_session(sess["id"])
        subj = f"（{sess['subject']}）" if sess["subject"] else ""
        return True, f"お疲れさま！今回の勉強時間: **{format_duration(eff_before)}** {subj}"

    # ---- ボタン handlers ----
    async def handle_button_start(self, inter: discord.Interaction):
        if not inter.guild:
            await inter.response.send_message("サーバー内で押してください。", ephemeral=True)
            return
        sess = await db.get_open_session(inter.user.id, inter.guild.id)
        if sess:
            await inter.response.send_message("すでに勉強中です。", ephemeral=True)
            return
        # 科目選択を促す
        view = SubjectSelectView(self, inter.user.id)
        await inter.response.send_message("科目を選んで開始（スキップする場合は下の「スキップ」ボタン）:", view=view, ephemeral=True)

    async def handle_button_pause(self, inter: discord.Interaction):
        if not inter.guild:
            await inter.response.send_message("サーバー内で押してください。", ephemeral=True)
            return
        sess = await db.get_open_session(inter.user.id, inter.guild.id)
        if not sess:
            await inter.response.send_message("開始していません。", ephemeral=True)
            return
        ok = await db.pause_session(sess["id"])
        if ok:
            await inter.response.send_message("休憩に入りました。戻ったら「再開」を押してね。", ephemeral=True)
        else:
            await inter.response.send_message("すでに休憩中です。", ephemeral=True)

    async def handle_button_resume(self, inter: discord.Interaction):
        if not inter.guild:
            await inter.response.send_message("サーバー内で押してください。", ephemeral=True)
            return
        sess = await db.get_open_session(inter.user.id, inter.guild.id)
        if not sess:
            await inter.response.send_message("開始していません。", ephemeral=True)
            return
        ok = await db.resume_session(sess["id"])
        if ok:
            await inter.response.send_message("再開！がんばろう。", ephemeral=True)
        else:
            await inter.response.send_message("休憩中ではありません。", ephemeral=True)

    async def handle_button_end(self, inter: discord.Interaction):
        if not inter.guild:
            await inter.response.send_message("サーバー内で押してください。", ephemeral=True)
            return
        ok, msg = await self._end(inter.user.id, inter.guild.id)
        await inter.response.send_message(msg, ephemeral=True)

    async def handle_button_subject(self, inter: discord.Interaction):
        if not inter.guild:
            await inter.response.send_message("サーバー内で押してください。", ephemeral=True)
            return
        view = SubjectSelectView(self, inter.user.id)
        await inter.response.send_message("科目を選択:", view=view, ephemeral=True)

    # ---- スラッシュ ----
    @app_commands.command(name="study_start", description="勉強を開始する")
    @app_commands.describe(subject="科目（任意）")
    @app_commands.choices(subject=SUBJECT_CHOICES)
    async def study_start(self, interaction: discord.Interaction, subject: str | None = None):
        if not interaction.guild:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        ch_id = interaction.user.voice.channel.id if isinstance(interaction.user, discord.Member) and interaction.user.voice and interaction.user.voice.channel else None  # type: ignore
        ok, msg = await self._start(interaction.user, interaction.guild.id, ch_id, subject)
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="study_end", description="勉強を終了する")
    async def study_end(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        ok, msg = await self._end(interaction.user.id, interaction.guild.id)
        await interaction.response.send_message(msg, ephemeral=True)

    @app_commands.command(name="study_pause", description="休憩する")
    async def study_pause(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        sess = await db.get_open_session(interaction.user.id, interaction.guild.id)
        if not sess:
            await interaction.response.send_message("開始していません。", ephemeral=True)
            return
        ok = await db.pause_session(sess["id"])
        await interaction.response.send_message("休憩に入りました。" if ok else "すでに休憩中です。", ephemeral=True)

    @app_commands.command(name="study_resume", description="休憩から再開する")
    async def study_resume(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        sess = await db.get_open_session(interaction.user.id, interaction.guild.id)
        if not sess:
            await interaction.response.send_message("開始していません。", ephemeral=True)
            return
        ok = await db.resume_session(sess["id"])
        await interaction.response.send_message("再開しました。" if ok else "休憩中ではありません。", ephemeral=True)

    @app_commands.command(name="study_status", description="現在の勉強状況を見る")
    async def study_status(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        sess = await db.get_open_session(interaction.user.id, interaction.guild.id)
        if not sess:
            await interaction.response.send_message("現在セッションはありません。", ephemeral=True)
            return
        from utils.db import _effective_seconds
        eff = _effective_seconds(sess)
        import datetime as dt, zoneinfo
        started = dt.datetime.fromtimestamp(sess["started_at"], TZ)
        state = "休憩中" if sess["is_paused"] else "勉強中"
        subj = sess["subject"] or "未設定"
        await interaction.response.send_message(
            embed=_embed("勉強状況", f"状態: **{state}**\n科目: **{subj}**\n開始: {started:%Y/%m/%d %H:%M}\n経過(休憩除外): **{format_duration(eff)}**"),
            ephemeral=True,
        )

    @app_commands.command(name="study_panel", description="勉強パネル（ボタン常設メッセージ）を設置する（管理者）")
    @app_commands.default_permissions(manage_guild=True)
    async def study_panel(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        embed = discord.Embed(
            title="📚 勉強パネル",
            description=(
                "**使い方**\n"
                "• 「勉強開始」→ 科目を選んで記録開始\n"
                "• 「休憩 / 再開」→ 休憩時間を除外\n"
                "• 「終了」→ その日の記録に加算\n"
                "• VCで **ミュート=集中 / ミュート解除=休憩** の自動記録も対応\n"
                "• スラッシュ `/study_start` `/study_end` `/study_pause` `/study_resume` も使えます"
            ),
            color=0x5865F2,
        )
        view = StudyPanelView(self)

        channel = interaction.channel  # TextChannel | VoiceChannel | Thread | None
        # ---- 事前権限チェック & 丁寧なエラーハンドリング (403 Missing Access 対策) ----
        # 403/50001 はほぼ「Botがそのチャンネルで View Channel / Send Messages を持っていない」が原因
        try:
            if channel is None:
                await interaction.response.send_message(
                    "チャンネル情報が取得できませんでした。テキストチャンネル内で実行してください。",
                    ephemeral=True,
                )
                return

            # Bot自身のメンバー取得 & 権限確認
            me: discord.Member | None = interaction.guild.me  # cache
            if me is None:
                try:
                    me = await interaction.guild.fetch_member(self.bot.user.id)  # type: ignore
                except Exception:
                    me = None
            if me is not None:
                try:
                    perms = channel.permissions_for(me)  # type: ignore[arg-type]
                    missing: list[str] = []
                    if not perms.view_channel:
                        missing.append("チャンネルを見る")
                    if not perms.send_messages:
                        missing.append("メッセージを送信")
                    if not perms.embed_links:
                        missing.append("埋め込みリンク")
                    # View Channel / Send Messages が無い場合は channel.send は必ず 403 になるので先に教える
                    if missing:
                        await interaction.response.send_message(
                            f"❌ Botに権限が足りないためパネルを設置できません: **{' / '.join(missing)}**\n"
                            f"対象チャンネル: {channel.mention if hasattr(channel, 'mention') else channel}\n\n"
                            "**直し方**\n"
                            "サーバー設定 → ロール / チャンネル権限でBotのロール（またはBotメンバー）に\n"
                            "「チャンネルを見る」「メッセージを送信」「埋め込みリンク」をONにしてください。\n"
                            "プライベートチャンネルなら「メンバー」にBotを追加してください。",
                            ephemeral=True,
                        )
                        return
                except Exception:
                    # 権限チェック自体が失敗しても送信トライは続行（後段の Forbidden で拾う）
                    pass

            # 本命: チャンネルに常設メッセージとして送る（従来どおり）
            msg = await channel.send(embed=embed, view=view)  # type: ignore[union-attr]
            await db.set_panel(interaction.guild.id, channel.id, msg.id)  # type: ignore
            await interaction.response.send_message("パネルを設置しました。", ephemeral=True)
            return

        except discord.Forbidden as e:
            # ここが今回の 403 Missing Access (50001) の受け皿
            detail = (
                "❌ `403 Forbidden (50001: Missing Access)` でパネルを送信できませんでした。\n"
                f"Discord詳細: `{e}`\n\n"
                "**チェックリスト**\n"
                "1. **Bot招待URL**に `bot` と `applications.commands` 両方のスコープが入っていますか？\n"
                "2. このチャンネルでBotに **チャンネルを見る / メッセージを送信 / 埋め込みリンク** がありますか？\n"
                "   → サーバー設定 > チャンネル > 歯車 > 権限 でBotロールを確認\n"
                "3. プライベート/アナウンス/スレッドなら、Botをメンバーに追加していますか？\n"
                "4. チャンネルを間違えていませんか？（VCのテキストチャットや権限が絞られたチャンネルで実行していませんか？）\n"
                "権限を付与してから `/study_panel` を再実行してください。\n"
            )
            # フォールバック: interaction応答として送る方法は webhook経由なので channel.send より通りやすいことがある
            try:
                if not interaction.response.is_done():
                    # フォールバックを試す: 応答自体をパネルにする
                    await interaction.response.send_message(embed=embed, view=view)
                    try:
                        orig = await interaction.original_response()
                        await db.set_panel(interaction.guild.id, orig.channel.id, orig.id)  # type: ignore
                    except Exception:
                        pass
                    # 追加で ephemeral で案内
                    await interaction.followup.send(detail + "\n※ フォールバックで応答メッセージとしてパネルを設置しました。", ephemeral=True)
                else:
                    await interaction.followup.send(detail, ephemeral=True)
            except discord.Forbidden:
                # フォールバックもダメならエラーのみを ephemeral で返す
                try:
                    if not interaction.response.is_done():
                        await interaction.response.send_message(detail, ephemeral=True)
                    else:
                        await interaction.followup.send(detail, ephemeral=True)
                except Exception:
                    pass
            return
        except discord.HTTPException as e:
            msg = f"パネル送信に失敗しました: `{e}`"
            if not interaction.response.is_done():
                await interaction.response.send_message(msg, ephemeral=True)
            else:
                await interaction.followup.send(msg, ephemeral=True)
            return

    # ---- VC自動検知: 入退室 + ミュート連動 ----
    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return
        gid = member.guild.id

        # チャンネル移動/入退室
        before_ch = before.channel.id if before.channel else None
        after_ch = after.channel.id if after.channel else None

        # 入室（未接続→接続 ＋ チャンネル移動で勉強VCに入った場合も含む）
        is_enter_study = (
            after_ch is not None
            and is_study_channel(after_ch)
            and (before_ch is None or before_ch != after_ch)
        )
        if is_enter_study:
            # 自動開始（既にセッションがあれば何もしない + 通知も出さない）
            sess = await db.get_open_session(member.id, gid)
            if not sess:
                await db.create_session(member.id, gid, after_ch, None, auto_started=True)
                # 通知チャンネルにメンション + 10秒後に自動削除
                try:
                    vc_name = after.channel.name if after.channel else "VC"  # type: ignore
                except Exception:
                    vc_name = "VC"
                await self._send_auto_delete_mention(
                    member,
                    "▶️ 勉強開始！",
                    f"{member.mention} **{vc_name}** に入室したので勉強を開始しました。\n"
                    f"集中してがんばろう！ `/study_status` で経過を確認できます。\n"
                    f"（このメッセージは10秒後に自動で削除されます）",
                    color=0x00CC88,
                    delete_after=10.0,
                )
            return

        # 退室（接続→未接続 ＋ 勉強VCから別VC/切断への移動も含む）
        is_leave_study = (
            before_ch is not None
            and is_study_channel(before_ch)
            and (after_ch is None or before_ch != after_ch)
        )
        if is_leave_study:
            sess = await db.get_open_session(member.id, gid)
            if sess:
                from utils.db import _effective_seconds
                eff = _effective_seconds(sess)
                await db.end_session(sess["id"])
                try:
                    await self._send_auto_delete_mention(
                        member,
                        "⏹️ 勉強終了",
                        f"お疲れさま！今回の勉強時間: **{format_duration(eff)}**\n"
                        f"VCから退出したので自動で終了しました。\n"
                        f"（このメッセージは10秒後に自動で削除されます）",
                        color=0x5865F2,
                        delete_after=10.0,
                    )
                except Exception:
                    pass
            return

        # 同じVC内でのミュート変化 → 休憩/再開の自動判定
        if before_ch is not None and after_ch is not None and before_ch == after_ch:
            if not is_study_channel(after_ch):
                return
            # 自己ミュート or サーバーミュート or ヘッドホンミュート(self_deaf) のいずれかがONなら集中=再開、OFFなら休憩
            # 計画: ミュート=勉強中、ミュート解除=休憩/雑談
            # 完全集中: self_mute & self_deaf 両方ON
            was_muted = bool(before.self_mute or before.mute or before.self_deaf or before.deaf)
            now_muted = bool(after.self_mute or after.mute or after.self_deaf or after.deaf)

            # セッションが無ければ、ミュートONをきっかけに自動開始
            if not was_muted and now_muted:
                sess = await db.get_open_session(member.id, gid)
                if not sess:
                    await db.create_session(member.id, gid, after_ch, None, auto_started=True)
                else:
                    # 休憩中なら再開
                    if sess["is_paused"]:
                        await db.resume_session(sess["id"])
                return

            if was_muted and not now_muted:
                sess = await db.get_open_session(member.id, gid)
                if sess and not sess["is_paused"]:
                    await db.pause_session(sess["id"])
                return


async def setup(bot: commands.Bot):
    await bot.add_cog(StudyCog(bot))
