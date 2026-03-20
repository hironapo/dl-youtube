# YouTube英語学習アプリ

YouTube動画の字幕を取得し、LLM（OpenRouter API）でフレーズ抽出・解説・語源・ゴロ合わせを生成して学習できる、Flaskベースの英語学習Webアプリです。

---

## 主な特徴

- **YouTube字幕の自動取得**: yt-dlpを使い、英語・日本語字幕を自動DL
- **LLMによるフレーズ解説**: 語源・言語学的考察・ゴロ合わせ・例文をOpenRouter経由で生成（結果をDBに自動キャッシュ）
- **2列字幕表示**: 英語と日本語を左右に並べてリーディング・シャドーイング学習
- **字幕テキスト選択**: 字幕テキストをマウスでドラッグ選択すると登録・解説ポップアップを表示
- **全文スクリプトDL**: 動画の全センテンスを EN+JA 対応・解説付き Markdown でダウンロード
- **インラインYouTubeプレイヤー**: 字幕クリックで該当箇所に自動シーク
- **A-B区間ループ**: 字幕リスト上部のループコントロールで任意区間を繰り返し再生
- **学習モード**: Canvasスクリーンショット＋センテンス一覧＋キーボードナビによる精聴練習
- **ボキャブラリー管理**: フレーズをDB保存し、タグ・お気に入り・日付別・英検1級でグループ表示
- **英検1級フィルター**: 英検1級レベルのフレーズを自動タグ付けして絞り込み表示
- **単語帳モード**: フリップカードで英単語を表裏練習
- **ネットワーク可視化**: フレーズ間の関連をforce-directedグラフで表示
- **コメント機能**: 動画コメントを取得し、気になる単語をその場で調べて登録
- **解説一括取得**: 登録済みフレーズの解説をバックグラウンドで一括プリフェッチ
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

1. **URLを入力してダウンロード**: ヘッダーの「📥 DL・登録」ボタンからYouTube URLを貼り付けてDL
2. **字幕を確認**: 動画選択後、字幕タブでEN/JAの2列表示
3. **字幕をクリック**: 対応する動画箇所に自動シーク（2回目クリックで再生）
4. **単語をクリック or テキスト選択**: ポップアップからフレーズ解説・登録が可能
5. **フレーズをクリック**: LLMが語源・ゴロ合わせ・例文を即時生成（DBに自動保存）
6. **全文DL**: 字幕ヘッダーの「📄 全文」からEN+JA+解説付きMarkdownをダウンロード
7. **ボキャブラリー画面**: 登録語をネットワーク・カード形式で一覧・復習
8. **学習モード**: 右パネルの「📷 学習」タブでスクリーンショット＋精聴練習

---

## ディレクトリ構成

```
dl-youtube/
├── app.py                    # Flask APIサーバー（メインエントリーポイント）
├── dl_youtube_sub_llm.py     # yt-dlp字幕DL・LLM処理ライブラリ
├── start.sh                  # 自動再起動スクリプト
├── templates/
│   └── index.html            # SPA フロントエンド（単一HTMLファイル）
├── static/                   # 静的ファイル（CSS・JSなど）
├── docs/
│   ├── README.md             # このファイル
│   ├── SPEC.md               # 技術仕様書
│   └── MANUAL.md             # 操作マニュアル
├── YYYYMMDD/                 # ダウンロード日付別出力フォルダ
│   ├── <title>.mp4           # 動画ファイル
│   ├── <title>.en.srt        # 英語字幕
│   ├── <title>.ja.srt        # 日本語字幕
│   └── <title>_phrases.md    # LLM生成フレーズ（Markdown）
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
