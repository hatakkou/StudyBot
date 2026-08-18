"""年組オンボーディング: 最初に年組を入力してロールを付与するチャンネル

対応経路:
  1) 常設パネル（Select: 学年 / 組） — 永続View
  2) 専用チャンネルへのテキスト入力（例: "1年3組" "2-3" "3組"）— 自動パース→付与
  3) 「テキストで入力」ボタン → モーダル
  4) /my_class スラッシュ

ロール解決:
  - .env で ONBOARDING_YEAR_ROLES / ONBOARDING_CLASS_ROLES に "表示名:ロールID" を書けばIDで解決
  - 未指定ならロール名が表示名と完全一致するものを探す
  - 付与時に同カテゴリの別ロールは自動で外す（1年→2年に変更など）
"""
from __future__ import annotations

import asyncio
import logging
import re

import discord
from discord.ext import commands
from discord import app_commands

import config
from utils import db

log = logging.getLogger(__name__)

# ---- パースヘルパー ----

# 全角数字→半角 正規化用
_ZEN2HAN = str.maketrans("０１２３４５６７８９", "0123456789")


def _normalize_digits(s: str) -> str:
    return s.translate(_ZEN2HAN)


def _parse_nenkumi_text(raw: str) -> tuple[str | None, str | None]:
    """テキストから (year_label, class_label) を抽出。見つからなければ None。
    例: "1年3組"→("1年","3組"), "2-3"→("2年","3組"), "3組"→(None,"3組"), "2年"→("2年",None)
    """
    s = _normalize_digits(raw).strip()
    if not s:
        return None, None

    # 1) "1年3組" / "1年 3組" / "1年-3組"
    m = re.search(r"([1-3])\s*年\s*[-ー]?\s*([1-9]\d*)\s*組", s)
    if m:
        return f"{m.group(1)}年", f"{m.group(2)}組"

    # 2) "1-3" / "1ー3" / "1 3" （ハイフン/空白区切りで年-組とみなす） — 文中に年/組が無い場合のみ
    #    誤爆を避けるため、短いメッセージで数字2つが並ぶ場合だけ採用
    if "年" not in s and "組" not in s:
        # 例: "2-3", "2 3", "1年3組" は上で拾われるのでここはハイフン型のみ
        m = re.search(r"\b([1-3])\s*[-ー]\s*([1-9]\d*)\b", s)
        if m:
            # ハイフン型は短い入力全体がパターンに近い場合のみ採用（"1-3です"も拾うが"12-345"は除外で既に1桁制限）
            return f"{m.group(1)}年", f"{m.group(2)}組"

    # 3) 単独 "X年"
    m = re.search(r"([1-3])\s*年", s)
    year = f"{m.group(1)}年" if m else None
    # 4) 単独 "Y組"
    m2 = re.search(r"([1-9]\d*)\s*組", s)
    klass = f"{m2.group(1)}組" if m2 else None

    # 上記2)でなく、片方だけでも返す
    if year or klass:
        return year, klass

    return None, None


# ---- ロール解決 ----

def _resolve_role(guild: discord.Guild, label: str) -> discord.Role | None:
    """label("1年"や"3組")に対応するRoleを返す。IDマップ優先、なければ名前一致。"""
    # IDマップを統合して探す
    role_id = config.ONBOARDING_YEAR_ROLES.get(label) or config.ONBOARDING_CLASS_ROLES.get(label)
    if role_id is not None:
        r = guild.get_role(role_id)
        if r:
            return r
        # キャッシュに無ければfetchを試みる（get_roleがNoneでもID自体は正しい可能性があるが、Guild.get_roleはキャッシュ依存）
        # RoleはGuild.rolesから探すのが最も確実
        for role in guild.roles:
            if role.id == role_id:
                return role
        return None
    # 名前一致
    for role in guild.roles:
        if role.name == label:
            return role
    return None


def _collect_all_labeled_roles(guild: discord.Guild, labels: list[str]) -> list[discord.Role]:
    out: list[discord.Role] = []
    for lb in labels:
        r = _resolve_role(guild, lb)
        if r and r not in out:
            out.append(r)
    return out


def _member_current_labels(member: discord.Member) -> tuple[list[str], list[str]]:
    """メンバーの現在の年/組ラベルを返す（表示用）"""
    years: list[str] = []
    klasses: list[str] = []
    role_ids = {r.id for r in member.roles}
    role_names = {r.name for r in member.roles}
    for lb in config.ONBOARDING_YEARS:
        r = _resolve_role(member.guild, lb)
        if r and r.id in role_ids:
            years.append(lb)
        elif lb in role_names and r is None:
            # _resolve_roleが見つけられなかったケース（キャッシュ不整合）のフォールバック
            years.append(lb)
    for lb in config.ONBOARDING_CLASSES:
        r = _resolve_role(member.guild, lb)
        if r and r.id in role_ids:
            klasses.append(lb)
        elif lb in role_names and r is None:
            klasses.append(lb)
    return years, klasses


# ---- ニックネーム（年組2桁を先頭に） ----
_prefix_re = re.compile(r"^\s*[0-9０-９]{2,3}\s*")


def _label_to_number(label: str) -> str | None:
    m = re.search(r"(\d+)", _normalize_digits(label))
    return m.group(1) if m else None


def _strip_nenkumi_prefix(name: str) -> str:
    """先頭の2桁（年組）プレフィックスを除去。例: '23 田中' -> '田中'"""
    return _prefix_re.sub("", name, count=1).lstrip()


async def _maybe_update_nickname(
    member: discord.Member,
    year_label: str | None,
    class_label: str | None,
) -> str | None:
    """年組が両方揃っていればニックネーム先頭を '年組2桁 名前' に更新。戻り値は追記用メッセージ or None。"""
    if not config.ONBOARDING_SET_NICKNAME:
        return None
    # 権限・ヒエラルキー事前チェックは edit 時の例外で拾うが、早期に分かるものはスキップ
    guild = member.guild
    if member.id == guild.owner_id:
        log.info("nickname skip: target is owner %s", member.id)
        return "（オーナーのためニックネームは変更できません）"

    # 有効な年/組を決定（付与直後の roles から取得。部分更新でも既存の片方が補完される）
    years, klasses = _member_current_labels(member)
    eff_year = years[0] if years else year_label
    eff_class = klasses[0] if klasses else class_label
    # year_label/class_label が今回指定されたものなら優先（_member_current_labels がまだ反映されていない場合の保険）
    if year_label:
        eff_year = year_label
    if class_label:
        eff_class = class_label
    # 再度 roles から補完: 片方だけ指定された場合に既存rolesを活かす
    if not eff_year and years:
        eff_year = years[0]
    if not eff_class and klasses:
        eff_class = klasses[0]
    # 典型ケース: 両方揃っていないならまだ付けない
    if not eff_year or not eff_class:
        return None
    y_num = _label_to_number(eff_year)
    k_num = _label_to_number(eff_class)
    if not y_num or not k_num:
        return None
    prefix = f"{y_num}{k_num}"
    sep = config.ONBOARDING_NICKNAME_SEPARATOR

    # ベース名決定（既存prefixを剥がしたもの）
    current_display = member.display_name
    base = _strip_nenkumi_prefix(current_display)
    if not base.strip():
        # display_name が "23" だけ等だった場合のフォールバック
        base = member.global_name or member.name
        base = _strip_nenkumi_prefix(base)
        if not base.strip():
            base = member.name
    new_nick = f"{prefix}{sep}{base}"
    # Discord ニックネーム上限 32文字
    if len(new_nick) > 32:
        max_base = 32 - len(prefix) - len(sep)
        if max_base < 1:
            max_base = 1
        base = base[:max_base].rstrip()
        new_nick = f"{prefix}{sep}{base}"
    # 既に同じなら何もしない
    if member.nick == new_nick or (member.nick is None and current_display == new_nick):
        return f"（ニックネームは既に `{new_nick}` です）"
    # Bot側権限チェック
    me = guild.me
    if me is None:
        try:
            me = await guild.fetch_member(guild._state.self_id)  # type: ignore[attr-defined]
        except Exception:
            me = None
    if me is not None:
        if not me.guild_permissions.manage_nicknames:
            return "（Botに「ニックネームの管理」権限がないためニックネームは変更できませんでした）"
        # ヒエラルキー: Botの最上位ロールが対象より上である必要
        if member.top_role >= me.top_role and member.id != me.id:
            return "（Botのロールが対象より下のためニックネームを変更できません。Botのロールを上げてください）"
    try:
        await member.edit(nick=new_nick, reason="年組登録: ニックネームに年組2桁を付与")
        log.info("nickname updated: %s -> %s", current_display, new_nick)
        return f"（ニックネームを `{new_nick}` に変更しました）"
    except discord.Forbidden:
        log.warning("nickname forbidden for %s", member.id)
        return "（権限不足でニックネームを変更できませんでした。Botに「ニックネームの管理」権限があるか確認してください）"
    except discord.HTTPException as e:
        log.warning("nickname http error for %s: %s", member.id, e)
        return f"（ニックネーム変更に失敗: {e}）"


async def _apply_roles(
    member: discord.Member,
    year_label: str | None,
    class_label: str | None,
) -> tuple[bool, str]:
    """ロール付与の共通処理。成功時 (True, msg)、失敗時 (False, error_msg)"""
    guild = member.guild
    me = guild.me
    if me is None:
        try:
            me = await guild.fetch_member(guild._state.self_id)  # type: ignore[attr-defined]
        except Exception:
            me = None

    # Bot権限チェック
    bot_perms_missing: list[str] = []
    if me is not None:
        if not me.guild_permissions.manage_roles:
            bot_perms_missing.append("ロールの管理")
    # ヒエラルキーは個別ロールで後でチェック

    to_add: list[discord.Role] = []
    to_remove: list[discord.Role] = []
    not_found: list[str] = []

    # 年
    if year_label is not None:
        r = _resolve_role(guild, year_label)
        if not r:
            not_found.append(f"{year_label} ロールが見つかりません（サーバーに「{year_label}」というロールを作成してください）")
        else:
            # ヒエラルキーチェック
            if me and r >= me.top_role:
                return False, f"Botのロールが「{r.name}」より下にあるため付与できません。Botのロールを一番上に上げてください。"
            to_add.append(r)
            # 同カテゴリの別ロールは外す
            for other in _collect_all_labeled_roles(guild, config.ONBOARDING_YEARS):
                if other.id != r.id and other in member.roles:
                    to_remove.append(other)

    # 組
    if class_label is not None:
        r = _resolve_role(guild, class_label)
        if not r:
            not_found.append(f"{class_label} ロールが見つかりません（サーバーに「{class_label}」というロールを作成してください）")
        else:
            if me and r >= me.top_role:
                return False, f"Botのロールが「{r.name}」より下にあるため付与できません。Botのロールを一番上に上げてください。"
            to_add.append(r)
            for other in _collect_all_labeled_roles(guild, config.ONBOARDING_CLASSES):
                if other.id != r.id and other in member.roles:
                    to_remove.append(other)

    if not_found:
        return False, "\n".join(not_found)
    if not to_add and not to_remove:
        # ロール変更なしだが、ニックネームだけ更新できる場合がある（既に年組ロールを持っている等）
        nick_msg = await _maybe_update_nickname(member, year_label, class_label)
        if nick_msg is not None:
            # ニックネームの結果を含めて成功として返す（ロールは既に持っている）
            if "変更しました" in nick_msg or "既に" in nick_msg:
                return True, f"年組は既に登録済みです {nick_msg}".strip()
            # 権限不足などで変更できなかった場合も、ロール自体は保持している旨を伝える
            return True, f"年組は既に登録済みです {nick_msg}".strip()
        return False, "変更するロールがありません。学年または組を選択してください。"

    # 実際に付与/剥奪
    # remove→add の順で実行（一度にやると競合しにくいが分けて実行）
    try:
        if to_remove:
            await member.remove_roles(*to_remove, reason="年組変更: 旧ロール除去")
        if to_add:
            # 既に持っているものはaddしても害はないが、重複を除く
            add_filtered = [r for r in to_add if r not in member.roles]
            if add_filtered:
                await member.add_roles(*add_filtered, reason="年組登録")
    except discord.Forbidden:
        if bot_perms_missing:
            return False, f"Botに権限がありません: {' / '.join(bot_perms_missing)} を付与してください。"
        return False, "権限不足でロールを付与できませんでした。Botに「ロールの管理」権限があるか、Botのロールが対象ロールより上にあるか確認してください。"
    except discord.HTTPException as e:
        return False, f"ロール付与に失敗しました: {e}"

    # 結果メッセージ
    parts: list[str] = []
    if year_label:
        parts.append(year_label)
    if class_label:
        parts.append(class_label)
    label_str = "・".join(parts) if parts else "—"
    removed_str = f"（{', '.join(r.name for r in to_remove)} を解除）" if to_remove else ""
    base_msg = f"{label_str} を付与しました {removed_str}".strip()
    # 年組2桁をニックネーム先頭に付与（例: 2年3組 → "23 名前"）
    try:
        nick_msg = await _maybe_update_nickname(member, year_label, class_label)
    except Exception as e:
        log.warning("nickname update error: %s", e)
        nick_msg = None
    if nick_msg:
        base_msg = f"{base_msg} {nick_msg}"
    return True, base_msg


# ---- モーダル ----

class NenkumiModal(discord.ui.Modal, title="年組を入力"):
    combined = discord.ui.TextInput(
        label="年組（例: 1年3組 / 2-3 / 3組）",
        placeholder="例: 2年3組",
        required=True,
        max_length=20,
    )

    def __init__(self, cog: "OnboardingCog"):
        super().__init__()
        self.cog = cog

    async def on_submit(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        raw = str(self.combined.value).strip()
        year, klass = _parse_nenkumi_text(raw)
        if not year and not klass:
            await interaction.response.send_message(
                f"読み取れませんでした: `{raw}`\n例: `1年3組` / `2-3` / `3組` / `2年` のように入力してください。",
                ephemeral=True,
            )
            return
        ok, msg = await _apply_roles(interaction.user, year, klass)
        if ok:
            await interaction.response.send_message(f"✅ {msg}", ephemeral=True)
        else:
            # ロールが見つからない等の案内＋有効な選択肢を提示
            hint = f"\n有効な学年: {', '.join(config.ONBOARDING_YEARS)}\n有効な組: {', '.join(config.ONBOARDING_CLASSES)}"
            await interaction.response.send_message(f"❌ {msg}{hint}", ephemeral=True)


# ---- 常設パネル View（永続） ----

class OnboardingPanelView(discord.ui.View):
    def __init__(self, cog: "OnboardingCog"):
        super().__init__(timeout=None)
        self.cog = cog

        # 年セレクト
        year_opts = [discord.SelectOption(label=y, value=y) for y in config.ONBOARDING_YEARS]
        self.year_select = discord.ui.Select(
            placeholder="学年を選択",
            options=year_opts,
            custom_id="onboarding:select_year",
            min_values=1,
            max_values=1,
        )
        self.year_select.callback = self._on_year_select  # type: ignore[method-assign]
        self.add_item(self.year_select)

        # 組セレクト
        class_opts = [discord.SelectOption(label=c, value=c) for c in config.ONBOARDING_CLASSES]
        self.class_select = discord.ui.Select(
            placeholder="組を選択",
            options=class_opts,
            custom_id="onboarding:select_class",
            min_values=1,
            max_values=1,
        )
        self.class_select.callback = self._on_class_select  # type: ignore[method-assign]
        self.add_item(self.class_select)

        # テキスト入力ボタン
        btn = discord.ui.Button(
            label="テキストで入力",
            style=discord.ButtonStyle.secondary,
            custom_id="onboarding:modal_open",
            emoji="✏️",
        )
        btn.callback = self._on_modal_open  # type: ignore[method-assign]
        self.add_item(btn)

    async def _on_year_select(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        year = self.year_select.values[0] if self.year_select.values else None
        if not year:
            await interaction.response.send_message("学年を選択してください。", ephemeral=True)
            return
        ok, msg = await _apply_roles(interaction.user, year, None)
        if ok:
            await interaction.response.send_message(f"✅ {msg}", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {msg}", ephemeral=True)

    async def _on_class_select(self, interaction: discord.Interaction):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        klass = self.class_select.values[0] if self.class_select.values else None
        if not klass:
            await interaction.response.send_message("組を選択してください。", ephemeral=True)
            return
        ok, msg = await _apply_roles(interaction.user, None, klass)
        if ok:
            await interaction.response.send_message(f"✅ {msg}", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {msg}", ephemeral=True)

    async def _on_modal_open(self, interaction: discord.Interaction):
        await interaction.response.send_modal(NenkumiModal(self.cog))


def _build_panel_embed(guild: discord.Guild) -> discord.Embed:
    years = ", ".join(config.ONBOARDING_YEARS)
    klasses = ", ".join(config.ONBOARDING_CLASSES)
    nick_note = "（年組が揃うとニックネームの先頭に `23 名前` のように2桁が自動付与されます）\n" if config.ONBOARDING_SET_NICKNAME else ""
    desc = (
        "最初に **年組** を登録すると、対応するロールが自動で付きます。\n"
        + nick_note
        + "このチャンネルで年組を登録してください。\n\n"
        "**登録方法（どれでもOK）**\n"
        "• 下の **学年 / 組 セレクト** から選ぶ\n"
        "• **テキストで入力** ボタン → 例: `1年3組`\n"
        "• このチャンネルに直接メッセージで `1年3組` / `2-3` / `3組` と送る\n\n"
        f"学年: {years}\n"
        f"組: {klasses}\n\n"
        "変更したいときはもう一度選び直せばOK（古い年/組ロールは自動で外れます／ニックネームの2桁も自動更新）。\n"
        "うまくいかない場合は管理者にロール名を確認してください。"
    )
    emb = discord.Embed(title="📝 年組登録パネル", description=desc, color=0x5865F2)
    # ロール存在チェックのヒント
    missing: list[str] = []
    for lb in config.ONBOARDING_YEARS + config.ONBOARDING_CLASSES:
        if not _resolve_role(guild, lb):
            missing.append(lb)
    if missing:
        emb.add_field(
            name="⚠️ 未作成のロール",
            value="`" + "`, `".join(missing[:12]) + "`" + (" …他" if len(missing) > 12 else "") + "\nサーバー設定→ロールでこの名前のロールを作ってください。",
            inline=False,
        )
        if len(missing) <= 6:
            emb.set_footer(text="IDマッピングを使う場合は .env の ONBOARDING_YEAR_ROLES / ONBOARDING_CLASS_ROLES を設定")
    return emb


# ---- Cog ----

YEAR_CHOICES = [app_commands.Choice(name=y, value=y) for y in config.ONBOARDING_YEARS]
CLASS_CHOICES = [app_commands.Choice(name=c, value=c) for c in config.ONBOARDING_CLASSES]


class OnboardingCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def cog_load(self):
        # 永続View登録（再起動後もボタン/セレクトが生きる）
        self.bot.add_view(OnboardingPanelView(self))

    # ---- テキスト入力の自動処理（専用チャンネル限定） ----
    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.author.bot or not message.guild:
            return
        # 専用チャンネルの判定: config優先、未設定ならDBのonboarding_panelのchannel_id
        target_id: int | None = config.ONBOARDING_CHANNEL_ID
        if target_id is None:
            row = await db.get_onboarding_panel(message.guild.id)
            if row:
                target_id = int(row["channel_id"])
        if target_id is None or message.channel.id != target_id:
            return
        if not config.ONBOARDING_ALLOW_TEXT_INPUT:
            return
        # スラッシュ/ボットのコマンドっぽいものは無視
        if message.content.startswith("/") or message.content.startswith("!"):
            return
        year, klass = _parse_nenkumi_text(message.content)
        if not year and not klass:
            return  # パネル操作以外の雑談は無視（スパム防止）

        # Member取得（キャッシュ優先）
        member = message.author if isinstance(message.author, discord.Member) else message.guild.get_member(message.author.id)
        if not isinstance(member, discord.Member):
            try:
                member = await message.guild.fetch_member(message.author.id)
            except Exception:
                return

        ok, msg = await _apply_roles(member, year, klass)
        # 返信して一定秒後に自動削除（設定0なら残す）
        try:
            if ok:
                reply = await message.reply(f"✅ {member.mention} {msg}", allowed_mentions=discord.AllowedMentions(users=True))
            else:
                reply = await message.reply(f"❌ {msg}", allowed_mentions=discord.AllowedMentions.none())
        except (discord.Forbidden, discord.HTTPException):
            return

        # 自動削除
        delay = int(config.ONBOARDING_AUTO_DELETE_SEC)
        if delay > 0:
            async def _auto_delete():
                try:
                    await asyncio.sleep(delay)
                    # 元の入力メッセージも消す（チャンネルを綺麗に保つ）
                    try:
                        await message.delete()
                    except (discord.Forbidden, discord.NotFound):
                        pass
                    await reply.delete()
                except Exception:
                    pass

            asyncio.create_task(_auto_delete())

    # ---- スラッシュ: パネル設置（管理者） ----
    @app_commands.command(name="onboarding_panel", description="年組登録パネルを設置する（管理者）")
    @app_commands.describe(channel="設置先チャンネル（省略で現在のチャンネル）")
    @app_commands.default_permissions(manage_guild=True)
    async def onboarding_panel(self, interaction: discord.Interaction, channel: discord.TextChannel | None = None):
        if not interaction.guild:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        # 権限の二重チェック（default_permissionsはUI上の制限なので）
        if not interaction.user.guild_permissions.manage_guild:  # type: ignore[union-attr]
            await interaction.response.send_message("このコマンドはサーバー管理者のみ使えます。", ephemeral=True)
            return

        target: discord.abc.GuildChannel | discord.TextChannel | None = channel or interaction.channel  # type: ignore[assignment]
        if target is None or not isinstance(target, discord.TextChannel):
            await interaction.response.send_message("テキストチャンネルで実行するか、channelを指定してください。", ephemeral=True)
            return

        # Bot権限チェック
        me = interaction.guild.me
        if me is None:
            try:
                me = await interaction.guild.fetch_member(self.bot.user.id)  # type: ignore[arg-type]
            except Exception:
                me = None
        if me is not None:
            try:
                perms = target.permissions_for(me)
                missing: list[str] = []
                if not perms.view_channel:
                    missing.append("チャンネルを見る")
                if not perms.send_messages:
                    missing.append("メッセージを送信")
                if not perms.embed_links:
                    missing.append("埋め込みリンク")
                if not perms.manage_roles:
                    missing.append("ロールの管理（付与に必要）")
                if missing:
                    await interaction.response.send_message(
                        f"❌ Botに権限が足りません: **{' / '.join(missing)}**\n"
                        f"対象: {target.mention}\n"
                        "サーバー設定→ロール/チャンネル権限で付与してください。",
                        ephemeral=True,
                    )
                    return
            except Exception:
                pass

        embed = _build_panel_embed(interaction.guild)
        view = OnboardingPanelView(self)

        try:
            msg = await target.send(embed=embed, view=view)
            await db.set_onboarding_panel(interaction.guild.id, target.id, msg.id)
            # configで固定IDを使う運用ならそちらも案内
            extra = ""
            if config.ONBOARDING_CHANNEL_ID and config.ONBOARDING_CHANNEL_ID != target.id:
                extra = f"\n※ .env の ONBOARDING_CHANNEL_ID は {config.ONBOARDING_CHANNEL_ID} になっています。テキスト入力の自動付与はそちらが優先されます。パネル設置先と揃えるならIDを {target.id} に変更してください。"
            await interaction.response.send_message(f"パネルを {target.mention} に設置しました。{extra}", ephemeral=True)
        except discord.Forbidden as e:
            detail = (
                f"❌ 送信できませんでした (403): `{e}`\n"
                "Botに **チャンネルを見る / メッセージを送信 / 埋め込みリンク / ロールの管理** があるか確認してください。\n"
                "プライベートチャンネルならBotをメンバーに追加してください。"
            )
            # フォールバック: 応答自体をパネルにする
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(embed=embed, view=view)
                    try:
                        orig = await interaction.original_response()
                        await db.set_onboarding_panel(interaction.guild.id, orig.channel.id, orig.id)  # type: ignore
                    except Exception:
                        pass
                    await interaction.followup.send(detail + "\n※ フォールバックで応答メッセージとして設置しました。", ephemeral=True)
                else:
                    await interaction.followup.send(detail, ephemeral=True)
            except Exception:
                try:
                    if not interaction.response.is_done():
                        await interaction.response.send_message(detail, ephemeral=True)
                    else:
                        await interaction.followup.send(detail, ephemeral=True)
                except Exception:
                    pass
        except discord.HTTPException as e:
            await interaction.response.send_message(f"送信に失敗: `{e}`", ephemeral=True) if not interaction.response.is_done() else await interaction.followup.send(f"送信に失敗: `{e}`", ephemeral=True)

    @app_commands.command(name="onboarding_refresh", description="年組パネルの表示を更新する（ロール追加後に）")
    @app_commands.default_permissions(manage_guild=True)
    async def onboarding_refresh(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        row = await db.get_onboarding_panel(interaction.guild.id)
        if not row:
            await interaction.response.send_message("パネルがまだ設置されていません。`/onboarding_panel` で設置してください。", ephemeral=True)
            return
        ch = self.bot.get_channel(int(row["channel_id"]))
        if not isinstance(ch, discord.TextChannel):
            try:
                ch = await self.bot.fetch_channel(int(row["channel_id"]))  # type: ignore[assignment]
            except Exception:
                ch = None
        if not isinstance(ch, discord.TextChannel):
            await interaction.response.send_message("パネルのチャンネルが見つかりません。`/onboarding_panel` で再設置してください。", ephemeral=True)
            return
        try:
            msg = await ch.fetch_message(int(row["message_id"]))
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as e:
            await interaction.response.send_message(f"パネルメッセージが取得できません: `{e}` — `/onboarding_panel` で再設置してください。", ephemeral=True)
            return
        try:
            embed = _build_panel_embed(interaction.guild)
            view = OnboardingPanelView(self)
            await msg.edit(embed=embed, view=view)
            await interaction.response.send_message("パネルを更新しました。", ephemeral=True)
        except Exception as e:
            await interaction.response.send_message(f"更新に失敗: `{e}`", ephemeral=True)

    # ---- スラッシュ: 自分の年組確認・変更 ----
    @app_commands.command(name="my_class", description="自分の年組ロールを確認・変更する")
    @app_commands.describe(year="学年", klass="組")
    @app_commands.choices(year=YEAR_CHOICES, klass=CLASS_CHOICES)
    async def my_class(self, interaction: discord.Interaction, year: str | None = None, klass: str | None = None):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        if year is None and klass is None:
            years, klasses = _member_current_labels(interaction.user)
            y_str = "・".join(years) if years else "未登録"
            k_str = "・".join(klasses) if klasses else "未登録"
            await interaction.response.send_message(
                f"あなたの年組: **{y_str} {k_str}**\n"
                f"変更するなら `/my_class year:1年 klass:3組` のように指定するか、年組チャンネルのパネルを使ってください。",
                ephemeral=True,
            )
            return
        ok, msg = await _apply_roles(interaction.user, year, klass)
        if ok:
            await interaction.response.send_message(f"✅ {msg}", ephemeral=True)
        else:
            await interaction.response.send_message(f"❌ {msg}", ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(OnboardingCog(bot))
