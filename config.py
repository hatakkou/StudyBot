"""設定一元管理"""
import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_TOKEN: str | None = os.getenv("DISCORD_TOKEN")
try:
    GUILD_ID: int | None = int(os.getenv("GUILD_ID", "")) if os.getenv("GUILD_ID") else None
except ValueError:
    GUILD_ID = None

def _parse_ids(raw: str | None) -> set[int]:
    if not raw:
        return set()
    out: set[int] = set()
    for p in raw.split(","):
        p = p.strip()
        if p:
            try:
                out.add(int(p))
            except ValueError:
                pass
    return out

STUDY_VOICE_CHANNEL_IDS: set[int] = _parse_ids(os.getenv("STUDY_VOICE_CHANNEL_IDS"))
try:
    STUDY_PANEL_CHANNEL_ID: int | None = int(os.getenv("STUDY_PANEL_CHANNEL_ID", "")) if os.getenv("STUDY_PANEL_CHANNEL_ID") else None
except ValueError:
    STUDY_PANEL_CHANNEL_ID = None
try:
    REPORT_CHANNEL_ID: int | None = int(os.getenv("REPORT_CHANNEL_ID", "")) if os.getenv("REPORT_CHANNEL_ID") else None
except ValueError:
    REPORT_CHANNEL_ID = None
try:
    STUDY_NOTIFY_CHANNEL_ID: int | None = int(os.getenv("STUDY_NOTIFY_CHANNEL_ID", "")) if os.getenv("STUDY_NOTIFY_CHANNEL_ID") else None
except ValueError:
    STUDY_NOTIFY_CHANNEL_ID = None

TZ_NAME: str = os.getenv("TZ", "Asia/Tokyo")

# DB path
DB_PATH: str = os.getenv("DB_PATH", "studybot.db")

# ポモドーロ既定
POMODORO_WORK_MIN = 25
POMODORO_BREAK_MIN = 5
POMODORO_CYCLES = 4

# 科目リスト（任意・ボタン/セレクトで出す）
SUBJECTS: list[str] = ["数学", "英語", "理科", "社会", "国語", "情報", "その他"]
