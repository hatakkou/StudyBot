"""勉強記録Cog: ボタン常設パネル / スラッシュ / VC自動検知 / 再起動復元
- 特定VC入室で自動で勉強開始
- 別テキストチャンネルにメンション通知を送り、10秒後に自動削除
- すべてのコマンド/ボタン応答は ephemeral=True (本人のみに見える)
- ★ライブボード: 常設パネルを編集して「ユーザー名 + 現在の状態」を常時表示（状態変化で即時編集 + 60秒ごとの定期更新）
"""
from __future__ import annotations
import asyncio
import logging
import datetime as dt
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

# 板の定期更新間隔（秒）。頻度が高すぎるとレート制限に触れるので60秒が目安
_BOARD_REFRESH_SEC = 60
_BOARD_MAX_LINES = 20


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
            # ボード即時更新
            if inter.guild:
                asyncio.create_task(self.cog.refresh_panel(inter.guild.id))

        select.callback = callback  # type: ignore
        self.add_item(select)


class StudyCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._board_lock = asyncio.Lock()
        self._refresh_tasks: set[asyncio.Task] = set()

    async def cog_load(self):
        # 永続View登録（再起動してもボタンが生きる）
        self.bot.add_view(StudyPanelView(self))
        # 定期更新ループ開始
        if not self._board_auto_refresh.is_running():
            self._board_auto_refresh.start()

    async def cog_unload(self):
        for task in list(self._refresh_tasks):
            task.cancel()
        self._refresh_tasks.clear()
        if self._board_auto_refresh.is_running():
            self._board_auto_refresh.cancel()

    # ---- ライブボード構築 ----
    async def _build_panel_embed(self, guild: discord.Guild, sessions: list | None = None) -> discord.Embed:
        """現在のセッション一覧を埋め込んだパネルEmbedを生成"""
        if sessions is None:
            try:
                sessions = await db.get_open_sessions_for_guild(guild.id)
            except Exception as e:
                log.warning("ボード用セッション取得失敗 guild=%s: %s", guild.id, e)
                sessions = []

        # 使い方説明は常に上部に表示
        description = (
            "**使い方**\n"
            "• 「勉強開始」→ 科目を選んで記録開始\n"
            "• 「休憩 / 再開」→ 休憩時間を除外\n"
            "• 「終了」→ その日の記録に加算\n"
            "• VCで **ミュート=集中 / ミュート解除=休憩** の自動記録も対応\n"
            "• スラッシュ `/study_start` `/study_end` `/study_pause` `/study_resume` も使えます"
        )

        embed = discord.Embed(
            title="📚 勉強パネル",
            description=description,
            color=0x5865F2,
            timestamp=now_jst(),
        )

        if not sessions:
            embed.add_field(
                name="📊 現在の勉強状況 — 誰も勉強していません",
                value="ボタンを押すかVCに入室して開始しよう！\n` /study_status ` で自分の状況も確認できます。",
                inline=False,
            )
        else:
            # ユーザー名 + 状態を常に表示（Discordのフィールド値は1024文字制限）
            lines: list[str] = []
            overflow_line: str | None = None
            if len(sessions) > _BOARD_MAX_LINES:
                overflow_line = f"… 他 {len(sessions) - _BOARD_MAX_LINES} 人"
            for sess in sessions:
                if len(lines) >= _BOARD_MAX_LINES:
                    break
                uid = sess["user_id"]
                member = guild.get_member(uid)
                if member:
                    # メンションはEmbed内ではpingしない（表示だけ）
                    name_part = f"{member.mention} `{member.display_name}`"
                else:
                    # キャッシュに居なければ mention 文字列でフォールバック（表示は <@id>）
                    # 可能なら fetch せずに軽量に済ます。必要なら次の定期更新で解決される
                    name_part = f"<@{uid}>"
                is_paused = bool(sess["is_paused"])
                subject = sess["subject"] or "未設定"
                from utils.db import _effective_seconds
                eff = _effective_seconds(sess)
                started = dt.datetime.fromtimestamp(sess["started_at"], TZ)
                # 状態表示
                if is_paused:
                    state = "☕ 休憩中"
                else:
                    # 完全集中判定はVC状態が必要だが、ここでは勉強中として統一（必要ならVCで補足）
                    state = "🟢 勉強中"
                # 経過（休憩除外）
                dur = format_duration(eff)
                candidate = f"• {name_part} — **{state}** ｜ {subject} ｜ {dur} (開始 {started:%H:%M})"
                # 1024文字制限を超過しないか検証（overflow表示も考慮）
                test_lines = lines + [candidate]
                if overflow_line:
                    test_value = "\n".join(test_lines + [overflow_line])
                else:
                    # まだoverflow未確定だが、残り人数がある場合はoverflowを想定
                    remaining = len(sessions) - len(test_lines)
                    if remaining > 0 and len(test_lines) >= _BOARD_MAX_LINES:
                        test_value = "\n".join(test_lines + [f"… 他 {remaining} 人"])
                    else:
                        test_value = "\n".join(test_lines)
                if len(test_value) > 1024:
                    # overflowを入れても収まらない場合はoverflowを付けて打ち止め
                    if overflow_line and len("\n".join(lines + [overflow_line])) <= 1024:
                        # 既にoverflowがあればそれで打ち止め
                        break
                    # overflow無しでも超過なら追加せず打ち止め
                    break
                lines.append(candidate)

            # overflow調整: 1024に収まるか再チェック
            if overflow_line:
                if len("\n".join(lines + [overflow_line])) > 1024:
                    # overflowが入らないほど逼迫していたら最後の1行を削ってoverflowを優先
                    while lines and len("\n".join(lines + [overflow_line])) > 1024:
                        lines.pop()
                # 実際に表示する人数差分がlines未表示分と一致するか再計算
                if len(lines) < len(sessions):
                    overflow_line = f"… 他 {len(sessions) - len(lines)} 人"
                    # 再度1024チェック
                    while lines and len("\n".join(lines + [overflow_line])) > 1024:
                        lines.pop()
                        overflow_line = f"… 他 {len(sessions) - len(lines)} 人"
                    lines.append(overflow_line)
            else:
                # _BOARD_MAX_LINES未満でも1024で打ち切られた場合のoverflow
                if len(lines) < len(sessions):
                    overflow_line = f"… 他 {len(sessions) - len(lines)} 人"
                    if len("\n".join(lines + [overflow_line])) <= 1024:
                        lines.append(overflow_line)
                    else:
                        # overflowも入らない場合は最後の行を削る
                        while lines and len("\n".join(lines + [overflow_line])) > 1024:
                            lines.pop()
                            overflow_line = f"… 他 {len(sessions) - len(lines)} 人"
                        if len("\n".join(lines + [overflow_line])) <= 1024:
                            lines.append(overflow_line)

            # 人数ヘッダ
            studying = sum(1 for s in sessions if not s["is_paused"])
            pausing = len(sessions) - studying
            header = f"📊 現在の勉強状況 — {len(sessions)}人 (勉強中 {studying} / 休憩中 {pausing})"
            field_value = "\n".join(lines) if lines else "表示できるセッションがありません。"
            # 最終安全策: 1024を超過していたら切り詰め
            if len(field_value) > 1024:
                field_value = field_value[:1021] + "…"
            embed.add_field(name=header, value=field_value, inline=False)

        embed.set_footer(text=f"最終更新: {now_jst():%Y/%m/%d %H:%M:%S} JST ｜ 自動更新 { _BOARD_REFRESH_SEC }秒ごと")
        return embed

    async def refresh_panel(self, guild_id: int):
        """指定ギルドのパネルメッセージを現在の状態で編集（存在しなければ何もしない）"""
        async with self._board_lock:
            try:
                panel = await db.get_panel(guild_id)
                if not panel:
                    return
                guild = self.bot.get_guild(guild_id)
                if guild is None:
                    try:
                        guild = await self.bot.fetch_guild(guild_id)
                    except Exception:
                        return
                # チャンネル解決
                ch_id = panel["channel_id"]
                msg_id = panel["message_id"]
                channel = self.bot.get_channel(ch_id)
                if channel is None:
                    try:
                        channel = await self.bot.fetch_channel(ch_id)
                    except Exception as e:
                        log.debug("パネルチャンネル取得失敗 guild=%s ch=%s: %s", guild_id, ch_id, e)
                        return
                # メッセージ取得
                try:
                    msg = await channel.fetch_message(msg_id)  # type: ignore
                except discord.NotFound:
                    log.warning("パネルメッセージが見つからない guild=%s ch=%s msg=%s", guild_id, ch_id, msg_id)
                    try:
                        await db.clear_panel(guild_id)
                    except Exception as e:
                        log.debug("clear_panel失敗 guild=%s: %s", guild_id, e)
                    return
                except discord.Forbidden:
                    log.warning("パネルメッセージ取得権限なし guild=%s ch=%s", guild_id, ch_id)
                    return

                embed = await self._build_panel_embed(guild)  # type: ignore
                view = StudyPanelView(self)
                try:
                    await msg.edit(embed=embed, view=view)
                except discord.NotFound:
                    log.warning("パネル編集時メッセージ消失 guild=%s", guild_id)
                except discord.Forbidden:
                    log.warning("パネル編集権限なし guild=%s", guild_id)
                except discord.HTTPException as e:
                    log.warning("パネル編集失敗 guild=%s: %s", guild_id, e)
            except Exception as e:
                log.debug("refresh_panel 例外 guild=%s: %s", guild_id, e)

    @tasks.loop(seconds=_BOARD_REFRESH_SEC)
    async def _board_auto_refresh(self):
        """全ギルドのパネルを定期的に編集して経過時間を更新"""
        try:
            panels = await db.get_all_panels()
        except Exception as e:
            log.debug("定期更新: パネル一覧取得失敗: %s", e)
            return
        for p in panels:
            try:
                gid = p["guild_id"]
                await self.refresh_panel(gid)
                # レート制限配慮で少し待つ
                await asyncio.sleep(0.8)
            except Exception as e:
                # guild_id取得失敗も含めて個別に処理
                try:
                    gid_str = p["guild_id"] if "guild_id" in p else "unknown"  # type: ignore
                except Exception:
                    gid_str = "unknown"
                log.debug("定期更新失敗 guild=%s: %s", gid_str, e)

    @_board_auto_refresh.before_loop
    async def _before_board_loop(self):
        await self.bot.wait_until_ready()

    @_board_auto_refresh.error
    async def _board_auto_refresh_error(self, exc: BaseException):
        log.error("定期更新ループで例外発生: %s", exc, exc_info=exc)
        if not self._board_auto_refresh.is_running():
            try:
                self._board_auto_refresh.start()
            except Exception as e:
                log.debug("定期更新ループ再起動失敗: %s", e)

    def _schedule_refresh(self, guild_id: int):
        """状態変化後に即時ボード更新をスケジュール（ブロックしない）"""
        task = asyncio.create_task(self.refresh_panel(guild_id))
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)

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
            self._schedule_refresh(inter.guild.id)
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
            self._schedule_refresh(inter.guild.id)
        else:
            await inter.response.send_message("休憩中ではありません。", ephemeral=True)

    async def handle_button_end(self, inter: discord.Interaction):
        if not inter.guild:
            await inter.response.send_message("サーバー内で押してください。", ephemeral=True)
            return
        ok, msg = await self._end(inter.user.id, inter.guild.id)
        await inter.response.send_message(msg, ephemeral=True)
        # 終了時も即時反映
        if ok:
            self._schedule_refresh(inter.guild.id)

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
        if ok:
            self._schedule_refresh(interaction.guild.id)

    @app_commands.command(name="study_end", description="勉強を終了する")
    async def study_end(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        ok, msg = await self._end(interaction.user.id, interaction.guild.id)
        await interaction.response.send_message(msg, ephemeral=True)
        if ok:
            self._schedule_refresh(interaction.guild.id)

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
        if ok:
            self._schedule_refresh(interaction.guild.id)

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
        if ok:
            self._schedule_refresh(interaction.guild.id)

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
        await interaction.response.defer(ephemeral=True)
        # ライブ埋め込みを先に生成（既存セッションがあれば即反映）
        embed = await self._build_panel_embed(interaction.guild)
        view = StudyPanelView(self)

        channel = interaction.channel  # TextChannel | VoiceChannel | Thread | None
        # ---- 事前権限チェック & 丁寧なエラーハンドリング (403 Missing Access 対策) ----
        # 403/50001 はほぼ「Botがそのチャンネルで View Channel / Send Messages を持っていない」が原因
        try:
            if channel is None:
                await interaction.followup.send(
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
                        await interaction.followup.send(
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

            # 既存パネルがあれば古いメッセージを削除して二重表示を防ぐ
            try:
                old = await db.get_panel(interaction.guild.id)
                old_msg_id = None
                if old is not None:
                    old_ch_id = old["channel_id"]
                    old_msg_id = old["message_id"]
                    try:
                        old_ch = self.bot.get_channel(old_ch_id)
                        if old_ch is None:
                            old_ch = await self.bot.fetch_channel(old_ch_id)
                        old_msg = await old_ch.fetch_message(old_msg_id)  # type: ignore
                        if old_msg.author.id == self.bot.user.id:  # type: ignore
                            await old_msg.delete()
                            log.info("旧パネルを削除 guild=%s ch=%s msg=%s", interaction.guild.id, old_ch_id, old_msg_id)
                    except discord.NotFound:
                        pass
                    except discord.Forbidden:
                        log.warning("旧パネル削除権限なし guild=%s ch=%s", interaction.guild.id, old_ch_id)
                    except Exception as e:
                        log.debug("旧パネル削除失敗: %s", e)
                # 同一チャンネル内の取り残されたパネル重複も掃除（直近50件） — old有無に関わらず実行
                try:
                    ch_for_scan = self.bot.get_channel(channel.id) or await self.bot.fetch_channel(channel.id)  # type: ignore
                    if hasattr(ch_for_scan, "history"):
                        async for m in ch_for_scan.history(limit=50):  # type: ignore
                            if m.author.id != self.bot.user.id:  # type: ignore
                                continue
                            if not m.embeds:
                                continue
                            # パネルEmbedのタイトルで判定
                            if m.embeds[0].title and "勉強パネル" in m.embeds[0].title:
                                # これから送るメッセージ以外で、旧IDでもない重複があれば削除
                                if m.id != old_msg_id:
                                    try:
                                        await m.delete()
                                        log.info("重複パネルを削除 guild=%s msg=%s", interaction.guild.id, m.id)
                                    except Exception:
                                        pass
                                    await asyncio.sleep(0.3)
                except Exception as e:
                    log.debug("重複スキャン失敗: %s", e)
            except Exception as e:
                log.debug("旧パネル事前削除失敗: %s", e)

            # 本命: チャンネルに常設メッセージとして送る（従来どおり）
            msg = await channel.send(embed=embed, view=view)  # type: ignore[union-attr]
            await db.set_panel(interaction.guild.id, channel.id, msg.id)  # type: ignore
            await interaction.followup.send("パネルを設置しました。ライブボードは自動で更新されます。（旧パネルは自動削除済み）", ephemeral=True)
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
            # フォールバック: defer後は followup 経由でパネルを公開
            try:
                fb_msg = await interaction.followup.send(embed=embed, view=view)
                try:
                    await db.set_panel(interaction.guild.id, fb_msg.channel.id, fb_msg.id)  # type: ignore
                except Exception:
                    pass
                await interaction.followup.send(detail + "\n※ フォールバックで応答メッセージとしてパネルを設置しました。", ephemeral=True)
            except discord.Forbidden:
                try:
                    await interaction.followup.send(detail, ephemeral=True)
                except Exception:
                    pass
            return
        except discord.HTTPException as e:
            await interaction.followup.send(f"パネル送信に失敗しました: `{e}`", ephemeral=True)
            return

    @app_commands.command(name="study_board_refresh", description="ライブボードを手動で更新する（管理者）")
    @app_commands.default_permissions(manage_guild=True)
    async def study_board_refresh(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        await self.refresh_panel(interaction.guild.id)
        await interaction.followup.send("ボードを更新しました。", ephemeral=True)

    @app_commands.command(name="study_panel_cleanup", description="重複した勉強パネルを削除する（管理者）")
    @app_commands.default_permissions(manage_guild=True)
    async def study_panel_cleanup(self, interaction: discord.Interaction):
        """現在チャンネルの重複パネルを削除。DBが指す1件だけ残す"""
        if not interaction.guild:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        if not isinstance(interaction.channel, (discord.TextChannel, discord.Thread, discord.VoiceChannel)):
            await interaction.response.send_message("テキストチャンネルで実行してください。", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True)
        panel = await db.get_panel(interaction.guild.id)
        keep_id = panel["message_id"] if panel else None
        channel = interaction.channel  # type: ignore
        deleted = 0
        forbidden_count = 0
        try:
            async for m in channel.history(limit=100):  # type: ignore
                if m.author.id != self.bot.user.id:  # type: ignore
                    continue
                if not m.embeds or not m.embeds[0].title or "勉強パネル" not in m.embeds[0].title:
                    continue
                if keep_id is not None and m.id == keep_id:
                    continue
                try:
                    await m.delete()
                    deleted += 1
                except discord.Forbidden:
                    forbidden_count += 1
                    log.warning("重複パネル削除権限なし guild=%s msg=%s", interaction.guild.id, m.id)
                except Exception:
                    pass
                finally:
                    await asyncio.sleep(0.4)
        except Exception as e:
            await interaction.followup.send(f"スキャン失敗: {e}", ephemeral=True)
            return
        msg = f"クリーンアップ完了: {deleted}件の重複パネルを削除しました。" + ("" if keep_id else "（DBにパネルが無いため全て削除対象でした）")
        if forbidden_count:
            msg += f"（権限不足で {forbidden_count}件削除できませんでした）"
        await interaction.followup.send(msg, ephemeral=True)

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
                self._schedule_refresh(gid)
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
                self._schedule_refresh(gid)
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
                    self._schedule_refresh(gid)
                else:
                    # 休憩中なら再開
                    if sess["is_paused"]:
                        await db.resume_session(sess["id"])
                        self._schedule_refresh(gid)
                return

            if was_muted and not now_muted:
                sess = await db.get_open_session(member.id, gid)
                if sess and not sess["is_paused"]:
                    await db.pause_session(sess["id"])
                    self._schedule_refresh(gid)
                return


async def setup(bot: commands.Bot):
    await bot.add_cog(StudyCog(bot))
