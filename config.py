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

# ---- 年組オンボーディング ----
try:
    ONBOARDING_CHANNEL_ID: int | None = int(os.getenv("ONBOARDING_CHANNEL_ID", "")) if os.getenv("ONBOARDING_CHANNEL_ID") else None
except ValueError:
    ONBOARDING_CHANNEL_ID = None

# 年組ロール設定: 既定は「1年/2年/3年」「1組〜7組」のロール名で解決する。
# 別名のロールを使いたい場合は .env でマッピングを指定:
#   ONBOARDING_YEAR_ROLES="1年:123456,2年:234567,3年:34567"
#   ONBOARDING_CLASS_ROLES="1組:111,2組:222,..."
def _parse_role_map(raw: str | None) -> dict[str, int]:
    if not raw:
        return {}
    out: dict[str, int] = {}
    for p in raw.split(","):
        p = p.strip()
        if not p or ":" not in p:
            continue
        k, v = p.split(":", 1)
        k = k.strip()
        v = v.strip()
        try:
            out[k] = int(v)
        except ValueError:
            pass
    return out

ONBOARDING_YEAR_ROLES: dict[str, int] = _parse_role_map(os.getenv("ONBOARDING_YEAR_ROLES"))
ONBOARDING_CLASS_ROLES: dict[str, int] = _parse_role_map(os.getenv("ONBOARDING_CLASS_ROLES"))
# 年の選択肢 / 組の選択肢（カンマ区切りで上書き可）
# 例: ONBOARDING_YEARS="1年,2年,3年"  ONBOARDING_CLASSES="1組,2組,3組,4組,5組,6組"
def _parse_list(raw: str | None, default: list[str]) -> list[str]:
    if not raw:
        return default
    items = [x.strip() for x in raw.split(",") if x.strip()]
    return items if items else default

ONBOARDING_YEARS: list[str] = _parse_list(os.getenv("ONBOARDING_YEARS"), ["1年", "2年", "3年"])
ONBOARDING_CLASSES: list[str] = _parse_list(os.getenv("ONBOARDING_CLASSES"), [f"{i}組" for i in range(1, 8)])
# 年組チャンネルで「自由入力メッセージ」を受け付けるか
ONBOARDING_ALLOW_TEXT_INPUT: bool = os.getenv("ONBOARDING_ALLOW_TEXT_INPUT", "1").lower() not in ("0", "false", "no")
# テキスト入力メッセージを何秒後に自動削除するか（0で削除しない）
try:
    ONBOARDING_AUTO_DELETE_SEC: int = int(os.getenv("ONBOARDING_AUTO_DELETE_SEC", "10"))
except ValueError:
    ONBOARDING_AUTO_DELETE_SEC = 10
# 年組登録時にニックネーム先頭に「年組2桁」を付与するか（例: 2年3組 → "23 名前"）
ONBOARDING_SET_NICKNAME: bool = os.getenv("ONBOARDING_SET_NICKNAME", "1").lower() not in ("0", "false", "no")
# ニックネームの区切り文字（既定: 半角スペース）。クォートで囲まれていても剥がす
_raw_sep = os.getenv("ONBOARDING_NICKNAME_SEPARATOR", " ")
if _raw_sep is not None and len(_raw_sep) >= 2 and _raw_sep[0] == _raw_sep[-1] and _raw_sep[0] in ('"', "'"):
    _raw_sep = _raw_sep[1:-1]
ONBOARDING_NICKNAME_SEPARATOR: str = _raw_sep if _raw_sep is not None else " "

# 科目リスト（任意・ボタン/セレクトで出す）
SUBJECTS: list[str] = ["数学", "英語", "理科", "社会", "国語", "情報", "その他"]
