# StudyBot — 勉強用 Discord Bot

`plan.md` に基づく discord.py 実装。

## 機能
- **自動勉強時間記録（メイン）**
  - 常設パネルのボタン: 勉強開始 / 休憩 / 再開 / 終了 / 科目変更（` /study_panel` で設置、永続View）
  - スラッシュ: `/study_start` `/study_end` `/study_pause` `/study_resume` `/study_status`
  - VC自動: 入室で自動開始 / 退室で自動終了 / ミュート=集中→再開、ミュート解除=休憩（`voiceStateUpdate`）
  - 完全集中: `self_mute` + `self_deaf` で判定（ヘッドホンミュート込み）
  - 科目タグ（任意）: 数学/英語/理科/社会/国語/情報/その他（`config.SUBJECTS`）
  - **イベント都度DB書き込み**（aiosqlite/WAL）でbot落ちても記録保持
- **ポモドーロ**: `/pomodoro_start` `/pomodoro_stop` `/pomodoro_status`（25/5分、サーバー同期、テキスト通知）
- **集計・レポート**: `/study_today` `/study_week` `/study_all` `/study_history` `/leaderboard` `/study_report`、毎日23:00 JSTに日報自動投稿（`REPORT_CHANNEL_ID` 設定時）
- **リマインダー**: `/remind`（自然言語: `あと30分` / `22:00` / `毎日23時` / `毎週月曜19時` / `明日9時`）、`/remind_list` `/remind_cancel`、通知に「完了 / スヌーズ10分」ボタン、毎日/毎週の繰り返し
- **BGM**: `/bgm_play` `/bgm_stop` `/bgm_help`（要 FFmpeg + PyNaCl、無ければ代替Bot推奨の案内）

## セットアップ
```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env  # DISCORD_TOKEN を入れる
python bot.py
```

### .env
```
DISCORD_TOKEN=xxx
GUILD_ID=123...              # 開発時は入れると同期が速い
STUDY_VOICE_CHANNEL_IDS=     # 勉強部屋VCのID（カンマ区切り）。空なら全VC対象
STUDY_PANEL_CHANNEL_ID=      # パネル設置先（任意）
REPORT_CHANNEL_ID=           # 日報投稿先（任意）
TZ=Asia/Tokyo
```

### Discord Developer Portal
- Bot → Privileged Intents: **Server Members Intent** と **Message Content Intent** は不要だが、Voice States は必要（Intentsで有効化）
- OAuth2 → URL Generator: `bot` + `applications.commands`、Bot Permissions: 適宜（Send Messages, Connect, Speak 等）

## 運用メモ
- ミュート運用: サーバー内で「ミュート=集中、ミュート解除=雑談」の合図を共有する
- 記録対象VCを絞りたい場合は `STUDY_VOICE_CHANNEL_IDS` を設定
- ポモドーロの分数は `config.py` の `POMODORO_WORK_MIN` / `POMODORO_BREAK_MIN` / `POMODORO_CYCLES`
- DBは `studybot.db`（`DB_PATH` で変更可）

## 構成
```
bot.py
config.py
utils/db.py        # aiosqlite / スキーマ / セッション等のCRUD
utils/helpers.py   # JTZ/集計/リマインダーパーサー
cogs/study.py      # 記録Cog
cogs/stats.py      # 集計/ランキング/レポート
cogs/pomodoro.py   # ポモドーロ
cogs/reminder.py   # リマインダー
cogs/bgm.py        # BGM
```
