"""StudyBot — エントリポイント"""
import asyncio
import logging
import discord
from discord.ext import commands
import config
from utils.db import init_db, get_open_session, end_session
from utils.helpers import TZ

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("studybot")

intents = discord.Intents.default()
intents.message_content = True  # 年組チャンネルのテキスト入力検知に必要（onboarding）
intents.voice_states = True
intents.guilds = True
intents.members = True

class StudyBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix=commands.when_mentioned, intents=intents, help_command=None)

    async def setup_hook(self):
        await init_db()
        for ext in ("cogs.study", "cogs.stats", "cogs.pomodoro", "cogs.reminder", "cogs.bgm", "cogs.onboarding"):
            try:
                await self.load_extension(ext)
                log.info("loaded %s", ext)
            except Exception as e:
                log.exception("failed to load %s: %s", ext, e)
        # スラッシュ同期
        if config.GUILD_ID:
            guild = discord.Object(id=config.GUILD_ID)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("synced commands to guild %s", config.GUILD_ID)
        else:
            await self.tree.sync()
            log.info("synced global commands")

    async def on_ready(self):
        log.info("Logged in as %s (%s) | guilds=%s", self.user, self.user.id if self.user else "?", len(self.guilds))
        # 再起動復元: VCにいるのにDB上でセッションが無い人を検知 → 自動セッションを作成しない（開始していない扱い）
        # ただし「VCに居て、DB上で開いているセッションがあるが、VCに居ないはずの人」の不整合は無し
        # ここでは「開いているセッションがあるが、該当ユーザーがVCに居ない」場合はそのまま保持（外出中扱い）
        # 逆に落ちている間にVCに入った人でセッションが無い人は、on_voice_state_update の次回イベントで拾われるためここでは何もしない
        # 必要なら再起動時にVCスキャンして自動開始するオプション:
        # for guild in self.guilds:
        #     for vs in guild.voice_states.values():
        #         ...

async def main():
    if not config.DISCORD_TOKEN:
        log.error("DISCORD_TOKEN が未設定です。.env を用意してください（.env.example参照）")
        raise SystemExit(1)
    bot = StudyBot()
    async with bot:
        await bot.start(config.DISCORD_TOKEN)

if __name__ == "__main__":
    asyncio.run(main())
