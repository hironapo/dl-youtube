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
│  │ サイドバー   │  │ プレイヤー   │  │ タブエリア         │  │
│  │ (動画ツリー) │  │ (YouTube     │  │ (字幕/フレーズ/    │  │
│  │             │  │  Embed)      │  │  解説/コメント)    │  │
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
│     <video_id>.mp4          │
│     <video_id>.en.srt       │
│     <video_id>.ja.srt       │
│     <video_id>.md           │
└─────────────────────────────┘
```

---

## APIエンドポイント一覧

### 日付・動画管理

#### `GET /api/dates`

登録済み動画の日付一覧を返す。

**レスポンス例:**
```json
["20260310", "20260309", "20260308"]
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
    "title": "Rick Astley - Never Gonna Give You Up",
    "channel": "RickAstleyVEVO",
    "duration": 212,
    "created_at": "2026-03-10T12:00:00"
  }
]
```

---

#### `DELETE /api/video/<video_id>`

動画をDBおよびファイルシステムから完全削除する（関連フレーズ・コメントも削除）。

**パスパラメータ:**
- `video_id`: YouTube動画ID

**レスポンス例:**
```json
{ "status": "deleted" }
```

---

### フレーズ管理

#### `GET /api/video/<video_id>/phrases`

指定動画に紐づくフレーズ一覧を返す。

**レスポンス例:**
```json
[
  {
    "id": 1,
    "phrase_en": "give up",
    "phrase_ja": "諦める",
    "note": "",
    "tags": "動詞句",
    "is_top": true,
    "created_at": "2026-03-10T12:00:00"
  }
]
```

---

#### `GET /api/phrases`

全フレーズ一覧を返す。クエリパラメータでフィルタリング可能。

**クエリパラメータ（任意）:**
- `tag`: タグでフィルタ
- `video_id`: 動画IDでフィルタ
- `q`: キーワード検索

---

#### `POST /api/phrases`

フレーズを新規登録する。

**リクエストボディ:**
```json
{
  "video_id": "dQw4w9WgXcQ",
  "phrase_en": "give up",
  "phrase_ja": "諦める",
  "note": "Never gonna give you up の give up",
  "tags": "動詞句"
}
```

---

#### `GET /api/phrases/tags`

登録済みタグ一覧を返す。

**レスポンス例:**
```json
["動詞句", "名詞", "形容詞", "イディオム"]
```

---

#### `POST /api/phrases/prefetch`

指定フレーズの解説をLLMで事前生成・DBにキャッシュする。

**リクエストボディ:**
```json
{ "phrase_ids": [1, 2, 3] }
```

---

#### `POST /api/explain`

フレーズの解説をLLMでリアルタイム生成する。

**リクエストボディ:**
```json
{
  "phrase": "give up",
  "context": "Never gonna give you up, never gonna let you down",
  "model": "anthropic/claude-3-haiku"
}
```

**レスポンス例:**
```json
{
  "definition": "To stop trying; to abandon an effort",
  "definition_ja": "努力をやめる、諦める",
  "etymology": "古英語 'giefan'（与える）+ 'up'（上へ）から転じて「手放す」の意",
  "linguistics_note": "句動詞。give up + 名詞 / give + 名詞 + up の2通りの語順が可能",
  "story": "大切なものを「上（up）に向かって差し出す（give）」イメージ",
  "mnemonic": "ギブアップ＝ギブ（給）して上（up）に逃げる",
  "example": "Don't give up on your dreams.",
  "example_ja": "夢を諦めないで。",
  "related": ["give in", "give out", "surrender"],
  "eiken_note": "英検1級では surrender / abandon と同義で出題されることが多い"
}
```

---

### 字幕管理

#### `GET /api/srt/<video_id>/<lang>`

指定動画・言語の字幕をJSON配列で返す。

**パスパラメータ:**
- `video_id`: YouTube動画ID
- `lang`: `en` または `ja`

**レスポンス例:**
```json
[
  { "index": 1, "start": "00:00:01,000", "end": "00:00:04,000", "text": "Never gonna give you up" },
  { "index": 2, "start": "00:00:04,500", "end": "00:00:07,000", "text": "Never gonna let you down" }
]
```

---

#### `POST /api/video/<video_id>/fetch_en_sub`

指定動画の英語字幕をyt-dlpで取得・DB保存する。

---

#### `POST /api/video/<video_id>/fetch_ja_sub`

指定動画の日本語字幕をyt-dlpで取得・DB保存する。

---

#### `POST /api/fetch_all_ja_subs`

DB登録済みの全動画に対して日本語字幕を一括取得する（バッチ処理）。

---

### コメント管理

#### `GET /api/video/<video_id>/comments`

指定動画のコメント一覧を返す。

**レスポンス例:**
```json
[
  {
    "id": 1,
    "author": "User123",
    "text": "This song is timeless!",
    "likes": 42,
    "explanation": null
  }
]
```

---

#### `POST /api/video/<video_id>/comments/refresh`

指定動画のコメントをyt-dlpで再取得・DB更新する。

---

#### `POST /api/comments/fetch_all`

全動画のコメントを一括取得する（バッチ処理）。

---

### ネットワーク管理

#### `GET /api/network`

フレーズネットワークのノードとリンクを返す。

**レスポンス例:**
```json
{
  "nodes": [
    { "id": "give up", "group": 1, "count": 3 }
  ],
  "links": [
    { "source": "give up", "target": "give in", "type": "synonym", "weight": 0.8 }
  ]
}
```

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
  "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
  "model": "anthropic/claude-3-haiku"
}
```

**レスポンス例:**
```json
{ "job_id": "abc123" }
```

---

#### `GET /api/download/stream/<job_id>`

Server-Sent Events (SSE) でダウンロード進捗をリアルタイムストリーミング配信する。

**イベント形式:**
```
data: {"type": "log", "message": "Downloading video..."}
data: {"type": "progress", "percent": 45}
data: {"type": "done", "video_id": "dQw4w9WgXcQ"}
```

---

### メディア配信

#### `GET /media/<path>`

動画・字幕ファイルをローカルファイルシステムから配信する。

---

---

## DBスキーマ詳細

DBファイルパス: `~/youtube_phrases.db`（SQLite）

---

### テーブル: `videos`

動画メタデータと字幕・LLM結果を格納する。

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
| `created_at` | DATETIME | 登録日時 |

---

### テーブル: `phrases`

抽出・登録されたフレーズを格納する。

| カラム名 | 型 | 説明 |
|---------|-----|------|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | フレーズID |
| `video_id` | TEXT | 関連動画ID（videos.video_id 参照） |
| `phrase_en` | TEXT | 英語フレーズ |
| `phrase_ja` | TEXT | 日本語訳 |
| `note` | TEXT | メモ・補足 |
| `explanation` | TEXT | LLM生成解説（JSON文字列） |
| `tags` | TEXT | タグ（カンマ区切り） |
| `is_top` | BOOLEAN | お気に入りフラグ |
| `created_at` | DATETIME | 登録日時 |

---

### テーブル: `phrase_links`

フレーズ間の関連を格納する（ネットワーク構築用）。

| カラム名 | 型 | 説明 |
|---------|-----|------|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | リンクID |
| `phrase_a` | TEXT | フレーズA（英語） |
| `phrase_b` | TEXT | フレーズB（英語） |
| `link_type` | TEXT | 関連タイプ（synonym/antonym/related等） |
| `weight` | REAL | 関連強度（0.0〜1.0） |

---

### テーブル: `video_comments`

動画コメントと解説を格納する。

| カラム名 | 型 | 説明 |
|---------|-----|------|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | コメントID |
| `video_id` | TEXT | 関連動画ID |
| `author` | TEXT | コメント投稿者名 |
| `text` | TEXT | コメント本文 |
| `likes` | INTEGER | いいね数 |
| `explanation` | TEXT | コメント内フレーズの解説（JSON） |

---

### テーブル: `topics`

動画のトピック・カテゴリを格納する。

| カラム名 | 型 | 説明 |
|---------|-----|------|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | トピックID |
| `video_id` | TEXT | 関連動画ID |
| `topic` | TEXT | トピック名 |

---

### テーブル: `video_links`

動画間の関連を格納する。

| カラム名 | 型 | 説明 |
|---------|-----|------|
| `id` | INTEGER PRIMARY KEY AUTOINCREMENT | リンクID |
| `video_a` | TEXT | 動画AのID |
| `video_b` | TEXT | 動画BのID |
| `link_type` | TEXT | 関連タイプ |

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
     │         YYYYMMDD/<video_id>.mp4
     │
     ├─→ yt-dlp で英語字幕取得
     │         YYYYMMDD/<video_id>.en.srt
     │
     ├─→ LLM (OpenRouter) でフレーズ抽出
     │         字幕テキスト → プロンプト → フレーズリスト
     │         YYYYMMDD/<video_id>.md
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
[GET /api/srt/<video_id>/ja]  ← 日本語字幕JSON取得（別途取得必要）
     │
     ▼
[ユーザー] 字幕クリック → YouTubeプレイヤーシーク
     │
     ▼
[POST /api/explain]  ← フレーズクリック時
     │
     ├─→ OpenRouter API 呼び出し
     │
     └─→ explanation JSON をDBに保存・ブラウザに返却

[POST /api/phrases]  ← 「★登録」ボタン
     │
     └─→ phrases テーブルに保存
```

---

## explanation JSONフィールド構造

フレーズの解説データは `phrases.explanation` カラムにJSON文字列として格納される。

```json
{
  "definition": "英語による定義・意味",
  "definition_ja": "日本語による意味・訳",
  "etymology": "語源（ラテン語・ギリシャ語・古英語等）",
  "linguistics_note": "言語学的考察（句動詞・コロケーション・文法的特徴等）",
  "story": "記憶に残る覚え方ストーリー（日本語）",
  "mnemonic": "ゴロ合わせ（日本語）",
  "example": "例文（英語）",
  "example_ja": "例文の日本語訳",
  "related": ["関連語1", "関連語2", "関連語3"],
  "eiken_note": "英検1級・TOEIC等試験対策メモ"
}
```

| フィールド | 型 | 説明 |
|-----------|-----|------|
| `definition` | string | 英語辞書的定義 |
| `definition_ja` | string | 日本語訳 |
| `etymology` | string | 語源解説 |
| `linguistics_note` | string | 言語学的・文法的考察 |
| `story` | string | 記憶ストーリー（日本語） |
| `mnemonic` | string | ゴロ合わせ（日本語） |
| `example` | string | 使用例文（英語） |
| `example_ja` | string | 例文の日本語訳 |
| `related` | string[] | 関連語・類義語・反義語のリスト |
| `eiken_note` | string | 試験対策メモ |

---

## フロントエンド状態管理

`templates/index.html` はVanilla JS + fetch APIで実装されたSPA。
グローバルな `state` オブジェクトでUIの状態を管理する。

### 主要 state プロパティ

| プロパティ | 型 | 説明 |
|-----------|----|------|
| `currentVideoId` | string \| null | 現在選択中の動画ID |
| `currentTab` | string | アクティブタブ（`subtitles` / `phrases` / `explain` / `comments`） |
| `subtitlesEn` | array | 英語字幕データ配列 |
| `subtitlesJa` | array | 日本語字幕データ配列 |
| `phrases` | array | 現在動画のフレーズ一覧 |
| `selectedPhrase` | object \| null | 解説表示中のフレーズオブジェクト |
| `player` | YT.Player | YouTube IFrame Player APIインスタンス |
| `currentModel` | string | 選択中のLLMモデル識別子 |
| `filterMode` | string | フィルター（`all` / `top` / `video`） |
| `networkData` | object | ネットワーク用 nodes・links データ |
| `sseSource` | EventSource \| null | SSEコネクション（DL進捗用） |

### 主要UIイベント

| イベント | 処理 |
|---------|------|
| 字幕行クリック（1回目） | `player.seekTo(startTime)` でシーク |
| 字幕行クリック（2回目） | `player.playVideo()` で再生開始 |
| フレーズクリック | `POST /api/explain` → 解説パネル表示 |
| 関連語クリック | 新フレーズとして解説起動（再帰的に呼び出し） |
| DLボタン | `POST /api/download` → SSEで進捗受信 |
| ★登録ボタン | `POST /api/phrases` → フレーズ保存 |
| ネットワーク表示 | D3.js force-simulationで描画 |
