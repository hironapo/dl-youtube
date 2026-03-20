# 技術仕様書 (SPEC)

## 目次

1. [アーキテクチャ概要](#アーキテクチャ概要)
2. [APIエンドポイント一覧](#apiエンドポイント一覧)
3. [DBスキーマ詳細](#dbスキーマ詳細)
4. [データフロー](#データフロー)
5. [explanation JSONフィールド構造](#explanation-jsonフィールド構造)
6. [フロントエンド状態管理](#フロントエンド状態管理)

---

## アーキテクチャ概要

```
┌─────────────────────────────────────────────────────────────┐
│                     ブラウザ (SPA)                           │
│  templates/index.html                                        │
│  ┌─────────────┐  ┌──────────────┐  ┌────────────────────┐  │
│  │ サイドバー   │  │ プレイヤー   │  │ 右パネル(タブ)     │  │
│  │ (動画ツリー) │  │ (YouTube     │  │ 字幕/フレーズ/     │  │
│  │             │  │  Embed)      │  │ 解説/学習/コメント  │  │
│  └─────────────┘  └──────────────┘  └────────────────────┘  │
└───────────────────────────┬─────────────────────────────────┘
                            │ HTTP / SSE (Server-Sent Events)
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                    Flask APIサーバー                         │
│  app.py (0.0.0.0:5000)                                       │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │ REST API     │  │ SSE Stream   │  │ Static / Media    │  │
│  │ /api/*       │  │ /api/download│  │ /media/<path>     │  │
│  │              │  │ /stream/<id> │  │                   │  │
│  └──────┬───────┘  └──────┬───────┘  └───────────────────┘  │
│         │                 │                                   │
│  ┌──────▼───────────────────────────────────────────────┐    │
│  │           dl_youtube_sub_llm.py                       │    │
│  │   yt-dlp字幕DL / LLM呼び出し / ファイル保存          │    │
│  └──────┬────────────────────┬──────────────────────────┘    │
└─────────┼────────────────────┼────────────────────────────────┘
          │                    │
          ▼                    ▼
┌─────────────────┐   ┌────────────────────────┐
│ SQLite DB        │   │ OpenRouter API          │
│ ~/youtube_       │   │ (Claude/GPT/Gemini等)   │
│ phrases.db       │   └────────────────────────┘
│                 │
│ - videos        │
│ - phrases       │
│ - phrase_links  │
│ - video_comments│
│ - topics        │
│ - video_links   │
└─────────────────┘
          │
          ▼
┌─────────────────────────────┐
│ ローカルファイルシステム      │
│ ~/python/dl-youtube/        │
│   YYYYMMDD/                 │
│     <title>.mp4             │
│     <title>.en.srt          │
│     <title>.ja.srt          │
│     <title>_phrases.md      │
└─────────────────────────────┘
```

---

## APIエンドポイント一覧

### 日付・動画管理

#### `GET /api/dates`

登録済み動画の日付一覧を返す。DBとファイルシステムの両方を参照する。

**レスポンス例:**
```json
[
  { "date": "20260321", "display": "2026/03/21", "count": 4 },
  { "date": "20260310", "display": "2026/03/10", "count": 3 }
]
```

---

#### `GET /api/dates/<date>/videos`

指定日付の動画一覧を返す。

**パスパラメータ:**
- `date`: `YYYYMMDD` 形式の日付文字列

**レスポンス例:**
```json
[
  {
    "video_id": "dQw4w9WgXcQ",
    "title": "Video Title",
    "channel": "ChannelName",
    "duration": 212,
    "mp4": "20260321/title.mp4",
    "en_srt": "srt/dQw4w9WgXcQ/en",
    "ja_srt": "srt/dQw4w9WgXcQ/ja"
  }
]
```

---

#### `DELETE /api/video/<video_id>`

動画をDBおよびファイルシステムから完全削除する（関連フレーズ・コメントも削除）。

---

### フレーズ管理

#### `GET /api/video/<video_id>/phrases`

指定動画に紐づくフレーズ一覧を返す。

**レスポンス例:**
```json
[
  {
    "id": 1,
    "video_id": "dQw4w9WgXcQ",
    "phrase_en": "**give up**",
    "phrase_ja": "諦める",
    "note": "",
    "tags": "動詞句,英検1級",
    "is_top": 1,
    "explanation": "{...json...}",
    "level": "英検1級",
    "created_at": "2026-03-21 12:00:00"
  }
]
```

---

#### `GET /api/phrases`

全フレーズ一覧を返す。ページネーション・フィルタリング対応。

**クエリパラメータ（任意）:**
- `page`: ページ番号（デフォルト: 1）
- `per_page`: 1ページあたりの件数（デフォルト: 200）
- `q`: キーワード検索
- `tag`: タグでフィルタ
- `top_only`: `1` でお気に入りのみ
- `level`: レベルでフィルタ（例: `英検1級`）

**レスポンス例:**
```json
{
  "phrases": [...],
  "total": 128
}
```

---

#### `GET /api/phrases/tags`

登録済みタグ一覧を返す。

---

#### `POST /api/video/<video_id>/register_phrases`

フレーズを一括登録する。

**リクエストボディ:**
```json
{
  "phrases": [
    { "en": "give up", "ja": "諦める", "note": "", "is_top": 0, "level": "" }
  ]
}
```

---

#### `PATCH /api/phrases/<id>`

フレーズを更新する（phrase_ja, note, tags, level）。

---

#### `DELETE /api/phrases/<id>`

フレーズを1件削除する。

---

#### `DELETE /api/phrases/all`

全フレーズを削除する。

---

#### `POST /api/phrases/<id>/toggle_top`

フレーズのお気に入りフラグをトグルする。

---

#### `POST /api/phrases/prefetch`

解説が未取得のフレーズに対してLLMで一括プリフェッチを開始する（バックグラウンド実行）。

**レスポンス例:**
```json
{ "job_id": "abc123", "total": 15 }
```

---

#### `GET /api/phrases/prefetch/<job_id>`

プリフェッチジョブの進捗を返す。

---

#### `POST /api/explain`

フレーズ・センテンスの解説をLLMでリアルタイム生成する。DBキャッシュを優先参照し、ヒットしない場合のみOpenRouterを呼び出してDBに保存する。

**リクエストボディ:**
```json
{
  "text": "give up",
  "context": "Never gonna give you up",
  "model": "anthropic/claude-3-haiku"
}
```

**レスポンス例:**
```json
{
  "meaning": "努力をやめる、諦める。手放す意味。",
  "usage": "give up + 名詞 / give + 名詞 + up の2通りの語順が可能",
  "etymology": "古英語 giefan（与える）+ up（上へ）から転じて「手放す」",
  "linguistics_note": "句動詞。認知言語学的に「上方向への解放」イメージスキーマ",
  "story": "大切なものを「上（up）に向かって差し出す（give）」イメージ",
  "mnemonic": "ギブアップ＝ギブ（給）して上（up）に逃げる",
  "example": "Despite adversity, she refused to give up her aspirations.",
  "example_ja": "逆境にもかかわらず、彼女は夢を諦めなかった。",
  "related": ["relinquish", "forsake", "capitulate", "abdicate"],
  "eiken_note": "英検1級では surrender / abandon と同義で出題されることが多い",
  "level": "上級"
}
```

---

### 字幕管理

#### `GET /api/srt/<video_id>/<lang>`

指定動画・言語の字幕をJSON配列で返す。DBのvideo_path→SRTファイル→DBカラムの順でフォールバック。

**パスパラメータ:**
- `video_id`: YouTube動画ID
- `lang`: `en` または `ja`

**レスポンス例:**
```json
[
  { "start": 1.0, "end": 4.0, "text": "Never gonna give you up" },
  { "start": 4.5, "end": 7.0, "text": "Never gonna let you down" }
]
```

---

#### `GET /api/subtitle/<path:filepath>`

OUTDIRからの相対パスでSRTファイルを取得する。

---

#### `POST /api/video/<video_id>/fetch_en_sub`

英語字幕をyt-dlpで取得・DB保存する。

---

#### `POST /api/video/<video_id>/fetch_ja_sub`

日本語字幕をyt-dlpで取得・DB保存する。

---

#### `POST /api/fetch_all_ja_subs`

DB登録済みの全動画に対して日本語字幕を一括取得する。

---

### コメント管理

#### `GET /api/video/<video_id>/comments`

指定動画のコメント一覧を返す。

---

#### `POST /api/video/<video_id>/comments/refresh`

コメントをyt-dlpで再取得・DB更新する。

---

### ネットワーク管理

#### `GET /api/network`

フレーズネットワークのノードとリンクを返す。

---

#### `POST /api/network/build`

LLMを使いフレーズ間の関連を自動解析してネットワークを構築する。

---

### ダウンロード

#### `POST /api/download`

YouTube動画のダウンロードジョブを開始する。

**リクエストボディ:**
```json
{
  "urls": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "mode": "both",
  "model": "anthropic/claude-3-haiku",
  "lang": "en",
  "top_n": 20
}
```

**レスポンス例:**
```json
{ "job_id": "abc123" }
```

---

#### `GET /api/download/stream/<job_id>`

Server-Sent Events (SSE) でダウンロード進捗をリアルタイム配信する。

---

#### `POST /api/download/cancel/<job_id>`

実行中のダウンロードジョブをキャンセルする。

---

### メディア配信

#### `GET /media/<path>`

動画・字幕ファイルをローカルファイルシステムから配信する。

---

## DBスキーマ詳細

DBファイルパス: `~/youtube_phrases.db`（SQLite）

---

### テーブル: `videos`

| カラム名 | 型 | 説明 |
|---------|-----|------|
| `video_id` | TEXT PRIMARY KEY | YouTube動画ID |
| `url` | TEXT | YouTube URL |
| `title` | TEXT | 動画タイトル |
| `channel` | TEXT | チャンネル名 |
| `duration` | INTEGER | 動画長さ（秒） |
| `description` | TEXT | 動画説明文 |
| `subtitle_en` | TEXT | 英語字幕（生SRT形式） |
| `subtitle_ja` | TEXT | 日本語字幕（生SRT形式） |
| `video_path` | TEXT | ローカル動画ファイルパス |
| `md_path` | TEXT | LLM生成Markdownファイルパス |
| `llm_result` | TEXT | LLM生成フレーズ結果（JSON） |
| `llm_model` | TEXT | 使用LLMモデル名 |
| `tags` | TEXT | タグ（カンマ区切り） |
| `created_at` | DATETIME | 登録日時 |
| `updated_at` | DATETIME | 更新日時 |

---

### テーブル: `phrases`

| カラム名 | 型 | 説明 |
|---------|-----|------|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | フレーズID |
| `video_id` | TEXT | 関連動画ID（videos.video_id 参照） |
| `phrase_en` | TEXT | 英語フレーズ（markdown記法含む場合あり） |
| `phrase_ja` | TEXT | 日本語訳 |
| `note` | TEXT | メモ・補足 |
| `explanation` | TEXT | LLM生成解説（JSON文字列） |
| `tags` | TEXT | タグ（カンマ区切り） |
| `level` | TEXT | レベル（例: 英検1級） |
| `is_top` | INTEGER | お気に入りフラグ（0/1） |
| `created_at` | DATETIME | 登録日時 |

---

### テーブル: `phrase_links`

フレーズ間の関連を格納する（ネットワーク構築用）。

| カラム名 | 型 | 説明 |
|---------|-----|------|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | リンクID |
| `phrase_a` | INTEGER | フレーズAのID（phrases.id参照） |
| `phrase_b` | INTEGER | フレーズBのID（phrases.id参照） |
| `link_type` | TEXT | 関連タイプ（synonym/antonym/related等） |
| `weight` | REAL | 関連強度（0.0〜1.0） |

---

### テーブル: `video_comments`

| カラム名 | 型 | 説明 |
|---------|-----|------|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | コメントID |
| `video_id` | TEXT | 関連動画ID |
| `author` | TEXT | コメント投稿者名 |
| `text` | TEXT | コメント本文 |
| `likes` | INTEGER | いいね数 |
| `explanation` | TEXT | コメント内フレーズの解説（JSON） |
| `created_at` | DATETIME | 取得日時 |

---

## データフロー

### 動画DL→学習までの流れ

```
[ユーザー] URL入力
     │
     ▼
[POST /api/download]
     │
     ├─→ yt-dlp で動画ダウンロード
     │         YYYYMMDD/<title>.mp4
     │
     ├─→ yt-dlp で英語字幕取得
     │         YYYYMMDD/<title>.en.srt
     │
     ├─→ LLM (OpenRouter) でフレーズ抽出
     │         字幕テキスト → プロンプト → フレーズリスト
     │         YYYYMMDD/<title>_phrases.md
     │
     └─→ SQLite に保存
               videos テーブル: メタデータ・字幕・LLM結果
               phrases テーブル: 抽出フレーズ

[SSE /api/download/stream/<job_id>]
     │
     └─→ 進捗をリアルタイムでブラウザに配信

[ブラウザ] 完了後、動画リストを自動更新
     │
     ▼
[GET /api/srt/<video_id>/en]  ← 英語字幕JSON取得
[GET /api/srt/<video_id>/ja]  ← 日本語字幕JSON取得
     │
     ▼
[ユーザー] 字幕クリック → YouTubeプレイヤーシーク
[ユーザー] テキスト選択 → ポップアップ → 解説/登録
     │
     ▼
[POST /api/explain]  ← フレーズクリック・選択時
     │
     ├─→ DBキャッシュ確認（phrases.explanation）
     │     ヒット → DBから返却
     │     ミス  → OpenRouter API 呼び出し
     │               → explanation をDBに保存
     │               → ブラウザに返却
     │
     └─→ renderDetail() で語源・ゴロ合わせ等を表示

[POST /api/video/<video_id>/register_phrases]  ← 「登録」ボタン
     │
     └─→ phrases テーブルに保存

[📄 全文DL ボタン]
     │
     ├─→ GET /api/video/<video_id>/phrases  ← 登録フレーズ+解説取得
     ├─→ state.subtitlesEN / state.subtitlesJA を参照
     └─→ EN+JA+解説付き Markdown を生成・ダウンロード
```

---

## explanation JSONフィールド構造

フレーズの解説データは `phrases.explanation` カラムにJSON文字列として格納される。

```json
{
  "meaning": "意味・説明（日本語・詳しく）",
  "usage": "使い方・ニュアンス・語法（日本語）",
  "example": "英検1級レベルの例文（英語）",
  "example_ja": "例文の日本語訳",
  "etymology": "語源・成り立ち（ラテン語/ギリシャ語/古英語の語根・接頭辞・接尾辞の分析）",
  "linguistics_note": "言語学的考察（音象徴・認知言語学的メタファー・形態素分析・イメージスキーマ）",
  "story": "覚え方ストーリー（日本語・情景が浮かぶ具体的なエピソード）",
  "mnemonic": "ゴロ合わせや記憶術（日本語）",
  "related": ["英検1級レベルの類語・対義語・派生語 最大4つ"],
  "eiken_note": "英検1級での出題傾向・注意点",
  "level": "初級/中級/上級"
}
```

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `meaning` | string | 意味・詳細説明（日本語） |
| `usage` | string | 使い方・ニュアンス・語法 |
| `example` | string | 英検1級レベルの例文（英語） |
| `example_ja` | string | 例文の日本語訳 |
| `etymology` | string | 語源解説（ラテン語・ギリシャ語・古英語等） |
| `linguistics_note` | string | 言語学的考察（音象徴・認知言語学・形態素分析） |
| `story` | string | 記憶ストーリー（日本語） |
| `mnemonic` | string | ゴロ合わせ・記憶術（日本語） |
| `related` | string[] | 関連語・類義語・反義語（英検1級以上レベル） |
| `eiken_note` | string | 英検1級試験対策メモ |
| `level` | string | 難易度（初級/中級/上級） |

---

## フロントエンド状態管理

`templates/index.html` はVanilla JS + fetch APIで実装されたSPA。
グローバルな `state` オブジェクトでUIの状態を管理する。

### 主要 state プロパティ

| プロパティ | 型 | 説明 |
|-----------|----|------|
| `currentVideo` | object \| null | 現在選択中の動画オブジェクト |
| `subtitlesEN` | array | 英語字幕データ配列 `{start, end, text}` |
| `subtitlesJA` | array | 日本語字幕データ配列 |
| `currentSubIdx` | number | 現在ハイライト中の字幕インデックス |
| `currentSubText` | string | 現在表示中の字幕テキスト |
| `vocabFilter` | string | ボキャブラリーフィルター（`all`/`top`/`current`/`words`/`phrases`/`eiken1`） |
| `vocabQuery` | string | ボキャブラリー検索キーワード |
| `vocabTag` | string | ボキャブラリータグフィルター |
| `vocabGroupView` | boolean | タグ別グループ表示モード |
| `vocabDateView` | boolean | 日付別グループ表示モード |
| `loopState` | object | A-Bループ設定 `{loopAll, loopStart, loopEnd}` |

### 主要UIイベント

| イベント | 処理 |
|---------|------|
| 字幕行クリック（1回目） | `video.currentTime = s.start` でシーク＋一時停止 |
| 字幕行クリック（2回目） | `video.play()` で再生開始 |
| `.sw` 単語クリック | `showWordPopup()` → 解説/登録ポップアップ |
| 字幕テキストドラッグ選択 | `window.getSelection()` → `showWordPopup()` |
| 解説ボタン | `POST /api/explain` → `renderDetail()` で表示 |
| 関連語クリック | 新フレーズとして `explainText()` 再帰呼び出し |
| DLボタン | `POST /api/download` → SSEで進捗受信 |
| 📄 全文ボタン | EN+JA+解説付きMarkdownを生成・DL |
| ★トグル | `POST /api/phrases/<id>/toggle_top` |
| 📅 日付別 | `vocabDateView` トグル → 日付グループ表示 |
| ネットワーク表示 | D3.js force-simulationで描画 |
| 学習モード（← →） | `studyPrev()` / `studyNext()` でセンテンス移動 |
| A-Bループ | `setLoopA()` / `setLoopB()` で区間設定 |
