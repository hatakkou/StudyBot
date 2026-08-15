"""汎用ヘルパー"""
from __future__ import annotations
import datetime as dt
import zoneinfo
import re
import config

try:
    TZ = zoneinfo.ZoneInfo(config.TZ_NAME)
except zoneinfo.ZoneInfoNotFoundError:
    # Windows では tzdata パッケージが無いと ZoneInfo が見つからない。
    # 依存追加で直るが、フォールバックとして JST(UTC+9) 固定で起動を継続する。
    import datetime as _dt

    TZ = _dt.timezone(_dt.timedelta(hours=9))  # type: ignore[assignment]
    import warnings

    warnings.warn(
        f"ZoneInfo '{config.TZ_NAME}' not found. "
        f"Falling back to fixed UTC+9 (JST). "
        f"Install tzdata: pip install tzdata (or pip install -r requirements.txt)",
        UserWarning,
        stacklevel=2,
    )

def now_jst() -> dt.datetime:
    return dt.datetime.now(TZ)

def ts_to_jst(ts: int) -> dt.datetime:
    return dt.datetime.fromtimestamp(ts, TZ)

def format_duration(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds}秒"
    m, s = divmod(seconds, 60)
    h, m = divmod(m, 60)
    if h:
        return f"{h}時間 {m}分" if s == 0 else f"{h}時間 {m}分 {s}秒"
    return f"{m}分 {s}秒" if s else f"{m}分"

def day_bounds(d: dt.date | None = None) -> tuple[int, int]:
    """その日の 00:00~翌00:00 の epoch 秒"""
    if d is None:
        d = now_jst().date()
    start = dt.datetime.combine(d, dt.time.min, TZ)
    end = start + dt.timedelta(days=1)
    return int(start.timestamp()), int(end.timestamp())

def week_bounds_monday() -> tuple[int, int]:
    """月曜00:00〜翌月曜00:00"""
    now = now_jst()
    monday = now.date() - dt.timedelta(days=now.weekday())
    start = dt.datetime.combine(monday, dt.time.min, TZ)
    end = start + dt.timedelta(days=7)
    return int(start.timestamp()), int(end.timestamp())

def is_study_channel(channel_id: int | None) -> bool:
    if not channel_id:
        return True  # VC不明でも記録はする
    if not config.STUDY_VOICE_CHANNEL_IDS:
        return True  # 限定なしなら全VC対象
    return channel_id in config.STUDY_VOICE_CHANNEL_IDS

# ---- remind パーザー ----
# 対応: "あと30分", "あと2時間", "30分後", "22:00", "22時", "22時30分",
#       "毎週月曜19時", "毎日23時", "明日9時", "1時間後 買い物" の末尾メッセージ抽出は呼び出し側
_RE_AFTER = re.compile(r"あと\s*(\d+)\s*(秒|分|時間|時)")
_RE_AFTER2 = re.compile(r"(\d+)\s*(秒|分|時間|時)\s*後")
_RE_CLOCK = re.compile(r"(\d{1,2})(?:[:：時](\d{1,2})?)?\s*分?")
_RE_EVERY_WEEK = re.compile(r"毎週\s*([月火水木金土日])曜?\s*(\d{1,2})(?:[:：時](\d{1,2})?)?")
_RE_EVERY_DAY = re.compile(r"毎日\s*(\d{1,2})(?:[:：時](\d{1,2})?)?")
_RE_TOMORROW = re.compile(r"明日\s*(\d{1,2})(?:[:：時](\d{1,2})?)?")

WD_MAP = {"月": 0, "火": 1, "水": 2, "木": 3, "金": 4, "土": 5, "日": 6}

def _to_sec(n: int, unit: str) -> int:
    if unit in ("秒",):
        return n
    if unit in ("分",):
        return n * 60
    return n * 3600

def parse_remind(input_text: str, base: dt.datetime | None = None) -> tuple[int | None, str | None, str | None]:
    """
    自然言語をパースして (trigger_epoch, repeat_rule, cleaned_message) を返す。
    repeat_rule: None | "daily:HH:MM" | "weekly:W:HH:MM"  （W=0月〜6日）
    時刻指定が未来でない場合は翌日に回す。
    パーサーが時刻を抽出できなければ (None, None, None)
    input_text は "/remind 22:00 ねる" の引数部分想定。末尾の余りはメッセージとして扱う。
    実装簡易: 先頭の時刻表現を消費し、残りをメッセージとする。時刻表現がない場合は先頭をメッセージ扱いにしない。
    """
    if base is None:
        base = now_jst()
    s = input_text.strip()
    if not s:
        return None, None, None

    # 毎週
    m = _RE_EVERY_WEEK.search(s)
    if m:
        wd_jp, h, mi = m.group(1), m.group(2), m.group(3)
        wd = WD_MAP[wd_jp]
        hour = int(h)
        minute = int(mi) if mi else 0
        # 次の該当曜日
        days_ahead = (wd - base.weekday()) % 7
        cand = (base + dt.timedelta(days=days_ahead)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand <= base:
            cand += dt.timedelta(days=7)
        msg = _RE_EVERY_WEEK.sub("", s, count=1).strip() or "リマインダー"
        return int(cand.timestamp()), f"weekly:{wd}:{hour:02d}:{minute:02d}", msg

    # 毎日
    m = _RE_EVERY_DAY.search(s)
    if m:
        h, mi = m.group(1), m.group(2)
        hour = int(h)
        minute = int(mi) if mi else 0
        cand = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if cand <= base:
            cand += dt.timedelta(days=1)
        msg = _RE_EVERY_DAY.sub("", s, count=1).strip() or "リマインダー"
        return int(cand.timestamp()), f"daily:{hour:02d}:{minute:02d}", msg

    # 明日
    m = _RE_TOMORROW.search(s)
    if m:
        h, mi = m.group(1), m.group(2)
        hour = int(h)
        minute = int(mi) if mi else 0
        cand = (base + dt.timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
        msg = _RE_TOMORROW.sub("", s, count=1).strip() or "リマインダー"
        return int(cand.timestamp()), None, msg

    # あとN分 / N分後
    for pat in (_RE_AFTER, _RE_AFTER2):
        m = pat.search(s)
        if m:
            n, unit = int(m.group(1)), m.group(2)
            # 時 は 時間扱い
            if unit == "時":
                unit = "時間"
            cand = base + dt.timedelta(seconds=_to_sec(n, unit))
            msg = pat.sub("", s, count=1).strip() or "リマインダー"
            return int(cand.timestamp()), None, msg

    # 時刻（22:00 / 22時30分）
    # 先頭寄りを優先して拾うが、メッセージ中に時刻が含まれるケースも拾う
    m = _RE_CLOCK.search(s)
    if m:
        # 「あと30分」のようなパターンは上で除外済み。ここは純粋な時刻
        # ただし "30分後" も上で除外済み
        # 時刻として妥当か判定: 前後が "あと" "後" ならスキップ（念のため）
        raw = m.group(0)
        # キーワードが先頭にあるような "22時 寝る" は時刻とみなす
        hour = int(m.group(1))
        minute = int(m.group(2)) if m.group(2) else 0
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            # 時刻表現が文中にあってもOK。メッセージ分離は「時刻部分を除いた残りをメッセージ」
            # ただし数字だけの "30" は時刻とみなさないように、"時" ":" "分" のいずれかが必要
            if ":" in raw or "：" in raw or "時" in raw or "分" in raw:
                cand = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
                if cand <= base:
                    cand += dt.timedelta(days=1)
                msg = s.replace(raw, "", 1).strip()
                # "22:00に寝る" の "に" を除去
                if msg.startswith("に"):
                    msg = msg[1:].strip()
                msg = msg or "リマインダー"
                return int(cand.timestamp()), None, msg

    return None, None, None

def next_trigger_for_repeat(repeat_rule: str, after_ts: int) -> int | None:
    """repeat_rule から次回 trigger を計算"""
    base = dt.datetime.fromtimestamp(after_ts, TZ)
    if repeat_rule.startswith("daily:"):
        _, hm = repeat_rule.split(":", 1)  # daily:HH:MM
        h, mi = hm.split(":")
        cand = base.replace(hour=int(h), minute=int(mi), second=0, microsecond=0)
        if cand <= base:
            cand += dt.timedelta(days=1)
        return int(cand.timestamp())
    if repeat_rule.startswith("weekly:"):
        parts = repeat_rule.split(":")  # weekly:W:HH:MM
        wd, h, mi = int(parts[1]), int(parts[2]), int(parts[3])
        days_ahead = (wd - base.weekday()) % 7
        cand = (base + dt.timedelta(days=days_ahead)).replace(hour=int(h), minute=int(mi), second=0, microsecond=0)
        if cand <= base:
            cand += dt.timedelta(days=7)
        return int(cand.timestamp())
    return None
