"""BGM: yt-dlp経由のURL再生 + ローカル brownnoise.mp3 のループ再生。PyNaCl+FFmpeg が無い環境では案内を返す。"""
from __future__ import annotations

import asyncio
import logging
import pathlib
import discord
from discord.ext import commands
from discord import app_commands

log = logging.getLogger("studybot.bgm")

HELP = (
    "BGMをVCで流すには、サーバーに **FFmpeg** と Python パッケージ **PyNaCl** が必要です。\n"
    "- `/bgm_play url:<YouTube等のURL>` : yt-dlp があれば YouTube/SoundCloud 等から音声を抽出して再生（無ければ直URLのみ）\n"
    "- `/bgm_brownnoise` : 同梱の `audio/brownnoise.mp3` をループ再生\n"
    "- `/bgm_stop` : 停止してVCから退出\n"
    "FFmpeg は `ffmpeg -version` で確認、PyNaCl は `pip install PyNaCl`、yt-dlp は `pip install yt-dlp` で導入できます。"
)

# brownnoise 探索候補（優先順）
_CANDIDATES = [
    pathlib.Path(__file__).resolve().parent.parent / "audio" / "brownnoise.mp3",
    pathlib.Path(__file__).resolve().parent.parent / "brownnoise.mp3",
    pathlib.Path.cwd() / "audio" / "brownnoise.mp3",
    pathlib.Path.cwd() / "brownnoise.mp3",
]


def _find_brownnoise() -> pathlib.Path | None:
    for p in _CANDIDATES:
        if p.is_file():
            return p
    return None


def _extract_with_ytdlp(url: str) -> tuple[str, str | None]:
    """同期的に yt-dlp で直接ストリームURLを取り出す。失敗時は元URLを返す。

    Returns: (stream_url, title)
    """
    try:
        import yt_dlp  # type: ignore
    except ImportError:
        return url, None

    # 既に直リンク(mp3/wav等)なら抽出不要に見えるが、yt-dlpに投げても無害なので
    # http(s) なら一度試す。失敗したらフォールバック。
    ydl_opts: dict = {
        "format": "bestaudio/best",
        "quiet": True,
        "noplaylist": True,
        "no_warnings": True,
        "extract_flat": False,
        "default_search": "auto",
        # 短時間で失敗させたい
        "socket_timeout": 15,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:  # type: ignore[attr-defined]
            info = ydl.extract_info(url, download=False)
            # プレイリスト/検索結果の場合
            if isinstance(info, dict) and "entries" in info:
                entries = list(info["entries"])
                if not entries:
                    return url, None
                info = entries[0]
            if not isinstance(info, dict):
                return url, None
            stream_url = info.get("url")
            title = info.get("title")
            if stream_url and isinstance(stream_url, str):
                return stream_url, title if isinstance(title, str) else None
            return url, title if isinstance(title, str) else None
    except Exception as e:
        log.info("yt-dlp extract failed for %s: %s", url, e)
        return url, None


def _is_http_url(s: str) -> bool:
    s = s.strip()
    return s.startswith("http://") or s.startswith("https://")


class BGMCog(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        # guild_id -> {"loop": bool, "source": str, "title": str|None, "volume": float, "is_brownnoise": bool}
        self._playing: dict[int, dict] = {}

    async def _ensure_voice(
        self, interaction: discord.Interaction
    ) -> discord.VoiceClient | None:
        """呼び出しユーザのVCに接続/移動して VoiceClient を返す。失敗時は followup済みで None。"""
        assert interaction.guild and isinstance(interaction.user, discord.Member)
        ch = interaction.user.voice.channel  # type: ignore[union-attr]
        vc = interaction.guild.voice_client
        if isinstance(vc, discord.VoiceClient) and vc.channel and vc.channel.id != ch.id:  # type: ignore[union-attr]
            try:
                await vc.move_to(ch)  # type: ignore[union-attr]
            except Exception as e:
                await interaction.followup.send(f"VC移動に失敗: {e}", ephemeral=True)
                return None
        elif not vc:
            try:
                vc = await ch.connect()  # type: ignore[union-attr]
            except Exception as e:
                await interaction.followup.send(f"VC接続に失敗: {e}", ephemeral=True)
                return None
        if isinstance(vc, discord.VoiceClient):
            return vc
        await interaction.followup.send("VoiceClientが取得できませんでした。", ephemeral=True)
        return None

    def _make_source(self, stream_url_or_path: str, *, is_local_file: bool) -> discord.AudioSource:
        """FFmpegPCMAudio (+ VolumeTransformer) を生成。"""
        if is_local_file:
            # ローカルファイルは reconnect 不要
            ffmpeg_audio = discord.FFmpegPCMAudio(
                stream_url_or_path,
                options="-vn",
            )
        else:
            ffmpeg_audio = discord.FFmpegPCMAudio(
                stream_url_or_path,
                before_options="-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5",
                options="-vn",
            )
        # 音量調整できるようにラップ（既定 0.5 相当で少し小さめに）
        return discord.PCMVolumeTransformer(ffmpeg_audio, volume=0.5)

    def _play_with_loop(
        self,
        guild: discord.Guild,
        vc: discord.VoiceClient,
        stream_url_or_path: str,
        *,
        is_local_file: bool,
        loop: bool,
        volume: float | None = None,
        title: str | None = None,
        is_brownnoise: bool = False,
    ) -> None:
        """vc.play をループ対応で開始。after で再帰再生する。"""
        gid = guild.id
        # 既存のループ状態を更新
        self._playing[gid] = {
            "loop": loop,
            "source": stream_url_or_path,
            "title": title,
            "is_local_file": is_local_file,
            "is_brownnoise": is_brownnoise,
            "volume": volume if volume is not None else 0.5,
        }

        def _after(err: Exception | None):
            if err:
                log.warning("playback error guild=%s err=%s", gid, err)
            # ギルドが消えていたら何もしない
            state = self._playing.get(gid)
            if not state:
                return
            if not state.get("loop"):
                # 単発再生なら状態をクリア
                self._playing.pop(gid, None)
                return
            # ループ再生: 同じソースで再生成して再play
            # voice_client がまだ有効かチェック
            g = self.bot.get_guild(gid)
            if not g:
                self._playing.pop(gid, None)
                return
            vcc = g.voice_client
            if not isinstance(vcc, discord.VoiceClient) or not vcc.is_connected():
                self._playing.pop(gid, None)
                return
            try:
                nxt = self._make_source(
                    state["source"], is_local_file=state["is_local_file"]
                )
                if volume is not None:
                    nxt.volume = state["volume"]  # type: ignore[attr-defined]
                # after は別スレッドから呼ばれるため run_coroutine は使わず直接 play
                vcc.play(nxt, after=_after)
                log.info("loop replay guild=%s source=%s", gid, state["source"])
            except Exception as e:
                log.exception("loop replay failed guild=%s: %s", gid, e)
                self._playing.pop(gid, None)

        source = self._make_source(stream_url_or_path, is_local_file=is_local_file)
        if volume is not None:
            source.volume = volume  # type: ignore[attr-defined]
        if vc.is_playing():
            vc.stop()
        vc.play(source, after=_after)

    # ---- commands ----

    @app_commands.command(name="bgm_play", description="VCでBGMを再生（yt-dlp対応）")
    @app_commands.describe(
        url="音声URL or 検索ワード（YouTube等はyt-dlpで抽出）",
        loop="ループ再生するか（既定: False）",
        volume="音量 0.0〜2.0（既定 0.5）",
    )
    async def bgm_play(
        self,
        interaction: discord.Interaction,
        url: str,
        loop: bool = False,
        volume: float | None = None,
    ):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("先にVCに入ってください。", ephemeral=True)
            return
        # 環境チェック
        try:
            import nacl  # noqa: F401
        except ImportError:
            await interaction.response.send_message(HELP, ephemeral=True)
            return
        if volume is not None and not (0.0 <= volume <= 2.0):
            await interaction.response.send_message("volume は 0.0〜2.0 で指定してください。", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True)

        # yt-dlp があれば抽出を試みる（ブロッキングなので to_thread）
        stream_url = url.strip()
        title: str | None = None
        # http(s) でも検索ワードでも yt-dlp に投げてOK。ただしローカルパスっぽいものは除外
        is_local_candidate = pathlib.Path(stream_url).is_file()
        if not is_local_candidate:
            # yt-dlp がインストールされていれば抽出
            try:
                import yt_dlp  # noqa: F401  # type: ignore
                has_ytdlp = True
            except ImportError:
                has_ytdlp = False
            if has_ytdlp:
                await interaction.followup.send("yt-dlpで音声を抽出中…", ephemeral=True)
                try:
                    stream_url, title = await asyncio.to_thread(_extract_with_ytdlp, url)
                except Exception as e:
                    log.warning("yt-dlp thread failed: %s", e)
                    stream_url, title = url, None
            else:
                # yt-dlp無し: http(s)以外はエラー案内
                if not _is_http_url(url) and not is_local_candidate:
                    await interaction.followup.send(
                        "yt-dlp が未導入のため、検索ワード/YouTube URL の直接再生はできません。\n"
                        "`pip install yt-dlp` を導入するか、直リンク(mp3等)を指定してください。\n" + HELP,
                        ephemeral=True,
                    )
                    return

        # 再生対象がローカルファイルか判定
        is_local_file = pathlib.Path(stream_url).is_file()
        # http URLでもローカル試行が失敗したらFFmpegに任せるので is_local_file=False のまま

        vc = await self._ensure_voice(interaction)
        if not vc:
            return

        try:
            vol = volume if volume is not None else 0.5
            self._play_with_loop(
                interaction.guild,  # type: ignore[arg-type]
                vc,
                stream_url,
                is_local_file=is_local_file,
                loop=loop,
                volume=vol,
                title=title,
                is_brownnoise=False,
            )
            label = title or stream_url
            # 長すぎるURLは省略
            if len(label) > 120:
                label = label[:117] + "..."
            msg = f"再生開始: {label}"
            if loop:
                msg += " (ループ: ON)"
            if has_ytdlp if 'has_ytdlp' in locals() else False:
                # yt-dlp経由だったことを明記（直URLでも害はない）
                pass
            await interaction.followup.send(msg, ephemeral=True)
        except Exception as e:
            log.exception("bgm_play failed: %s", e)
            await interaction.followup.send(f"再生に失敗: {e}\n{HELP}", ephemeral=True)

    @app_commands.command(name="bgm_brownnoise", description="ブラウンノイズをループ再生")
    @app_commands.describe(volume="音量 0.0〜2.0（既定 0.5）")
    async def bgm_brownnoise(
        self, interaction: discord.Interaction, volume: float | None = None
    ):
        if not interaction.guild or not isinstance(interaction.user, discord.Member):
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        if not interaction.user.voice or not interaction.user.voice.channel:
            await interaction.response.send_message("先にVCに入ってください。", ephemeral=True)
            return
        try:
            import nacl  # noqa: F401
        except ImportError:
            await interaction.response.send_message(HELP, ephemeral=True)
            return
        if volume is not None and not (0.0 <= volume <= 2.0):
            await interaction.response.send_message("volume は 0.0〜2.0 で指定してください。", ephemeral=True)
            return

        path = _find_brownnoise()
        if not path:
            await interaction.response.send_message(
                "ブラウンノイズ音源が見つかりません。`audio/brownnoise.mp3` を配置してください。\n"
                f"探索先: {', '.join(str(p) for p in _CANDIDATES)}",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)
        vc = await self._ensure_voice(interaction)
        if not vc:
            return
        try:
            vol = volume if volume is not None else 0.35  # ブラウンノイズは少し小さめ既定
            self._play_with_loop(
                interaction.guild,  # type: ignore[arg-type]
                vc,
                str(path),
                is_local_file=True,
                loop=True,
                volume=vol,
                title="Brown Noise",
                is_brownnoise=True,
            )
            await interaction.followup.send(
                f"ブラウンノイズをループ再生中: `{path.name}` (volume={vol}) — `/bgm_stop` で停止",
                ephemeral=True,
            )
        except Exception as e:
            log.exception("bgm_brownnoise failed: %s", e)
            await interaction.followup.send(f"再生に失敗: {e}\n{HELP}", ephemeral=True)

    @app_commands.command(name="bgm_stop", description="BGMを停止してVCから退出")
    async def bgm_stop(self, interaction: discord.Interaction):
        if not interaction.guild:
            await interaction.response.send_message("サーバー内で使ってください。", ephemeral=True)
            return
        vc = interaction.guild.voice_client
        # ループ状態を先にクリア（afterコールバックで再再生されないように）
        self._playing.pop(interaction.guild.id, None)
        if isinstance(vc, discord.VoiceClient):
            try:
                if vc.is_playing():
                    vc.stop()
                await vc.disconnect()
            except Exception:
                pass
            await interaction.response.send_message("停止して退出しました。", ephemeral=True)
        else:
            await interaction.response.send_message("VCに接続していません。", ephemeral=True)

    @app_commands.command(name="bgm_help", description="BGM機能の使い方")
    async def bgm_help(self, interaction: discord.Interaction):
        await interaction.response.send_message(HELP, ephemeral=True)


async def setup(bot: commands.Bot):
    await bot.add_cog(BGMCog(bot))
