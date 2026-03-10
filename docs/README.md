# YouTube英語学習アプリ

YouTube動画の字幕を取得し、LLM（OpenRouter API）でフレーズ抽出・解説・語源・ゴロ合わせを生成して学習できる、Flaskベースの英語学習Webアプリです。

---

## 主な特徴

- **YouTube字幕の自動取得**: yt-dlpを使い、英語・日本語字幕を自動DL
- **LLMによるフレーズ解説**: 語源・言語学的考察・ゴロ合わせ・例文をOpenRouter経由で生成
- **2列字幕表示**: 英語と日本語を左右に並べてリーディング・シャドーイング学習
- **インラインYouTubeプレイヤー**: 字幕クリックで該当箇所に自動シーク
- **ボキャブラリー管理**: フレーズをDB保存し、タグ・お気に入りで整理
- **ネットワーク可視化**: フレーズ間の関連をforce-directedグラフで表示
- **コメント機能**: 動画コメントを取得し、気になる単語をその場で調べて登録
- **マルチLLMモデル対応**: Claude・GPT-4o・Gemini・DeepSeek・Llamaを切り替えて使用

---

## 必要環境

| 要件 | バージョン / 備考 |
|------|-----------------|
| Python | 3.11 以上 |
| yt-dlp | 最新版推奨 |
| FFmpeg | システムにインストール済みであること |
| OpenRouter APIキー | [openrouter.ai](https://openrouter.ai) で取得 |
| SQLite | Python標準ライブラリに同梱 |
| OS | Linux / WSL2 推奨 |

---

## セットアップ手順

### 1. リポジトリのクローン

```bash
git clone <repository-url>
cd dl-youtube
```

### 2. 依存パッケージのインストール

```bash
pip install flask flask-cors yt-dlp requests python-dotenv openai
```

主要依存パッケージ:

```
flask
flask-cors
yt-dlp
requests
python-dotenv
openai
```

### 3. 環境変数の設定

プロジェクトルートに `.env` ファイルを作成します。

```env
OPENROUTER_API_KEY=sk-or-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx
```

OpenRouter APIキーは [https://openrouter.ai/keys](https://openrouter.ai/keys) から取得してください。

### 4. アプリの起動

```bash
python app.py
```

デフォルトでは `0.0.0.0:5000` で起動します。ブラウザで以下にアクセスしてください。

```
http://localhost:5000
```

WSL2環境の場合は、WSL2のIPアドレス（例: `172.26.101.177:5000`）でもアクセスできます。

---

## 基本的な使い方

1. **URLを入力してダウンロード**: サイドバーの入力欄にYouTube URLを貼り付けて「DL」ボタンを押す
2. **字幕を確認**: 英語字幕・日本語字幕の取得ボタンでそれぞれの字幕をDL
3. **字幕をクリック**: 対応する動画箇所に自動シーク（2回目クリックで再生）
4. **フレーズをクリック**: LLMが語源・ゴロ合わせ・例文を即時生成
5. **フレーズを登録**: 「★登録」ボタンでボキャブラリーDBに保存
6. **ボキャブラリー画面**: 登録語をネットワーク・カード形式で一覧・復習

---

## ディレクトリ構成

```
dl-youtube/
├── app.py                    # Flask APIサーバー（メインエントリーポイント）
├── dl_youtube_sub_llm.py     # yt-dlp字幕DL・LLM処理ライブラリ
├── templates/
│   └── index.html            # SPA フロントエンド（単一HTMLファイル）
├── static/                   # 静的ファイル（CSS・JSなど）
├── docs/
│   ├── README.md             # このファイル
│   ├── SPEC.md               # 技術仕様書
│   └── MANUAL.md             # 操作マニュアル
├── YYYYMMDD/                 # ダウンロード日付別出力フォルダ
│   ├── <video_id>.mp4        # 動画ファイル
│   ├── <video_id>.en.srt     # 英語字幕
│   ├── <video_id>.ja.srt     # 日本語字幕
│   └── <video_id>.md         # LLM生成フレーズ（Markdown）
├── .env                      # 環境変数（APIキー）※gitignore対象
└── ~/youtube_phrases.db      # SQLiteデータベース（ホームディレクトリ）
```

---

## 対応LLMモデル

| モデル | 提供元 | 備考 |
|--------|--------|------|
| `anthropic/claude-3-haiku` | Anthropic | デフォルト・低コスト |
| `anthropic/claude-sonnet-4-6` | Anthropic | 高品質 |
| `deepseek/deepseek-chat` | DeepSeek | コスト効率良好 |
| `google/gemini-2.0-flash-exp` | Google | 無料枠あり |
| `meta-llama/llama-3.3-70b-instruct` | Meta | 無料枠あり |
| `openai/gpt-4o-mini` | OpenAI | バランス型 |

---

## ライセンス

このプロジェクトは個人学習用途を想定しています。
