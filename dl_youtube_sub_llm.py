#!/usr/bin/env python3
"""
YouTube 動画＆字幕ダウンロード / Web記事スクレイピング → DB登録 → OpenRouter LLMで英文フレーズ抽出・整理

特長:
  - YouTube動画の字幕 + Web記事（Bridge English等）の両方に対応
  - SQLiteで全データを蓄積（字幕・フレーズ・メタ情報・関連語）
  - LLM分析時に過去の関連データを自動参照
  - MDには元URL、会話の文脈、関連する過去データリンクを含めて出力
  - 複数URL/プレイリスト対応、YouTube + Web記事混在OK
  - 【改造】処理日付別フォルダに全ファイルを自動整理
            YYYYMMDD/
              ├── video_title.mp4
              ├── video_title.en.srt
              ├── video_title.ja.srt
              ├── video_title_phrases.md
              └── phrases_summary_YYYYMMDD_HHMMSS.md

使い方:
  python dl_youtube_sub_llm.py <URL1> [URL2 ...] [オプション]

  URLはYouTubeでもWebページでも自動判定されます。
  例:
    python dl_youtube_sub_llm.py https://www.youtube.com/watch?v=xxxxx
    python dl_youtube_sub_llm.py https://bridge-english.blogspot.com/2026/02/flue.html
    python dl_youtube_sub_llm.py YouTube_URL Blog_URL Blog_URL  ← 混在OK

モード（YouTube動画のみ有効）:
  --both      動画＋字幕＋LLM分析（デフォルト）
  --video     動画のみダウンロード
  --sub       字幕のみ＋LLM分析

DB管理:
  --history             過去に処理したコンテンツ一覧を表示
  --search <keyword>    DBからキーワード検索
  --links <phrase>      フレーズの関連語ネットワーク表示
  --topics              登録済みトピック一覧と紐付きデータ数
  --db <path>           DBファイルパス (デフォルト: ~/youtube_phrases.db)

オプション:
  --model <model>       OpenRouterモデル名 (デフォルト: deepseek/deepseek-chat)
  --lang <lang>         字幕言語 (デフォルト: en,ja)
  --outdir <dir>        出力先ルートディレクトリ（日付フォルダはここの下に作成）
  --top <n>             覚えるべきフレーズ数 (デフォルト: 3)
  --no-playlist         プレイリスト展開しない
  --list-models         おすすめモデル一覧
  -h, --help            ヘルプ

環境変数:
  OPENROUTER_API_KEY    OpenRouter APIキー (LLM分析時に必須)

例:
  python dl_youtube_sub_llm.py https://www.youtube.com/watch?v=xxxxx
  python dl_youtube_sub_llm.py https://bridge-english.blogspot.com/2026/02/b-8094.html
  python dl_youtube_sub_llm.py URL1 URL2 --model anthropic/claude-sonnet-4
  python dl_youtube_sub_llm.py --history
  python dl_youtube_sub_llm.py --search "pull one over on"
  python dl_youtube_sub_llm.py --links "falling-out"
"""

import sys
import os
import re
import json
import shutil
import sqlite3
import argparse
import tempfile
import time
from datetime import datetime
import requests
import yt_dlp

# ============================================================
# 定数
# ============================================================
RECOMMENDED_MODELS = {
    # --- 低コスト（$0.1～0.3/1Mトークン） ---
    "deepseek/deepseek-chat": "DeepSeek V3 ($0.10/$0.28) ★最安・高性能",
    "google/gemini-2.5-flash": "Gemini 2.5 Flash ($0.15/$0.60) 高速",
    "openai/gpt-4o-mini": "GPT-4o Mini ($0.15/$0.60) 安定",
    "meta-llama/llama-4-scout": "Llama 4 Scout ($0.15/$0.40) OSS",
    # --- 中コスト（$1～3/1Mトークン） ---
    "anthropic/claude-haiku-4": "Claude Haiku 4 ($0.80/$4.00) 高速",
    "google/gemini-2.5-pro": "Gemini 2.5 Pro ($1.25/$10.00) 高精度",
    "openai/gpt-4o": "GPT-4o ($2.50/$10.00) 高精度",
    # --- 高コスト（$3+/1Mトークン） ---
    "anthropic/claude-sonnet-4": "Claude Sonnet 4 ($3.00/$15.00) 最高精度",
}
DEFAULT_MODEL = "deepseek/deepseek-chat"


# ============================================================
# 【改造】日付別フォルダ管理
# ============================================================
def get_dated_outdir(base_outdir: str, date: datetime = None) -> str:
    """YYYYMMDD 形式の日付フォルダパスを返す（なければ作成）"""
    if date is None:
        date = datetime.now()
    folder_name = date.strftime("%Y%m%d")
    dated_dir = os.path.join(base_outdir, folder_name)
    os.makedirs(dated_dir, exist_ok=True)
    return dated_dir


# ============================================================
# DB管理
# ============================================================
class PhraseDB:
    """SQLiteで動画・字幕・フレーズデータを管理"""

    def __init__(self, db_path: str):
        self.db_path = db_path
        os.makedirs(os.path.dirname(db_path) or ".", exist_ok=True)
        self.conn = sqlite3.connect(db_path)
        self.conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS videos (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id    TEXT UNIQUE,
                url         TEXT NOT NULL,
                title       TEXT NOT NULL,
                channel     TEXT DEFAULT '',
                duration    INTEGER DEFAULT 0,
                description TEXT DEFAULT '',
                subtitle_en TEXT DEFAULT '',
                subtitle_ja TEXT DEFAULT '',
                llm_result  TEXT DEFAULT '',
                llm_model   TEXT DEFAULT '',
                video_path  TEXT DEFAULT '',
                md_path     TEXT DEFAULT '',
                tags        TEXT DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now', 'localtime')),
                updated_at  TEXT DEFAULT (datetime('now', 'localtime'))
            );

            CREATE TABLE IF NOT EXISTS phrases (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id    TEXT NOT NULL,
                phrase_en   TEXT NOT NULL,
                phrase_ja   TEXT DEFAULT '',
                note        TEXT DEFAULT '',
                is_top      INTEGER DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (video_id) REFERENCES videos(video_id)
            );

            /* ── フレーズ間の関連（類義語・対義語・語根共通など） ── */
            CREATE TABLE IF NOT EXISTS phrase_links (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                phrase_a    TEXT NOT NULL,
                phrase_b    TEXT NOT NULL,
                link_type   TEXT NOT NULL DEFAULT 'synonym',
                note        TEXT DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now', 'localtime')),
                UNIQUE(phrase_a, phrase_b, link_type)
            );

            /* ── 動画のトピックタグ ── */
            CREATE TABLE IF NOT EXISTS video_topics (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id    TEXT NOT NULL,
                topic       TEXT NOT NULL,
                created_at  TEXT DEFAULT (datetime('now', 'localtime')),
                UNIQUE(video_id, topic),
                FOREIGN KEY (video_id) REFERENCES videos(video_id)
            );

            /* ── 動画間の関連 ── */
            CREATE TABLE IF NOT EXISTS video_links (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id_a  TEXT NOT NULL,
                video_id_b  TEXT NOT NULL,
                link_type   TEXT NOT NULL DEFAULT 'topic',
                score       REAL DEFAULT 0,
                created_at  TEXT DEFAULT (datetime('now', 'localtime')),
                UNIQUE(video_id_a, video_id_b, link_type),
                FOREIGN KEY (video_id_a) REFERENCES videos(video_id),
                FOREIGN KEY (video_id_b) REFERENCES videos(video_id)
            );

            CREATE INDEX IF NOT EXISTS idx_phrases_video ON phrases(video_id);
            CREATE INDEX IF NOT EXISTS idx_phrases_en ON phrases(phrase_en);
            CREATE INDEX IF NOT EXISTS idx_videos_title ON videos(title);
            CREATE INDEX IF NOT EXISTS idx_phrase_links_a ON phrase_links(phrase_a);
            CREATE INDEX IF NOT EXISTS idx_phrase_links_b ON phrase_links(phrase_b);
            CREATE INDEX IF NOT EXISTS idx_video_topics ON video_topics(topic);
            CREATE INDEX IF NOT EXISTS idx_video_links_a ON video_links(video_id_a);
            CREATE INDEX IF NOT EXISTS idx_video_links_b ON video_links(video_id_b);
        """)
        self.conn.commit()

    def upsert_video(self, video_id: str, **kwargs) -> None:
        existing = self.conn.execute(
            "SELECT id FROM videos WHERE video_id = ?", (video_id,)
        ).fetchone()

        if existing:
            sets = ", ".join(f"{k} = ?" for k in kwargs)
            vals = list(kwargs.values()) + [video_id]
            self.conn.execute(
                f"UPDATE videos SET {sets}, updated_at = datetime('now','localtime') WHERE video_id = ?",
                vals
            )
        else:
            kwargs["video_id"] = video_id
            cols = ", ".join(kwargs.keys())
            placeholders = ", ".join("?" for _ in kwargs)
            self.conn.execute(
                f"INSERT INTO videos ({cols}) VALUES ({placeholders})",
                list(kwargs.values())
            )
        self.conn.commit()

    def add_phrases(self, video_id: str, phrases: list[dict]) -> None:
        self.conn.execute("DELETE FROM phrases WHERE video_id = ?", (video_id,))
        for p in phrases:
            self.conn.execute(
                "INSERT INTO phrases (video_id, phrase_en, phrase_ja, note, is_top) VALUES (?,?,?,?,?)",
                (video_id, p.get("en", ""), p.get("ja", ""), p.get("note", ""), p.get("is_top", 0))
            )
        self.conn.commit()

    def add_phrase_links(self, links: list[dict]) -> int:
        count = 0
        for lk in links:
            a = lk.get("a", "").strip().lower()
            b = lk.get("b", "").strip().lower()
            lt = lk.get("type", "synonym")
            note = lk.get("note", "")
            if not a or not b or a == b:
                continue
            if a > b:
                a, b = b, a
            try:
                self.conn.execute(
                    "INSERT OR IGNORE INTO phrase_links (phrase_a, phrase_b, link_type, note) VALUES (?,?,?,?)",
                    (a, b, lt, note)
                )
                count += 1
            except Exception:
                pass
        self.conn.commit()
        return count

    def get_phrase_links(self, phrase: str, limit: int = 20) -> list[dict]:
        p = phrase.strip().lower()
        rows = self.conn.execute("""
            SELECT phrase_a, phrase_b, link_type, note FROM phrase_links
            WHERE phrase_a = ? OR phrase_b = ?
            ORDER BY link_type LIMIT ?
        """, (p, p, limit)).fetchall()
        results = []
        for r in rows:
            other = r["phrase_b"] if r["phrase_a"] == p else r["phrase_a"]
            results.append({
                "phrase": other, "type": r["link_type"], "note": r["note"]
            })
        return results

    def get_all_links_for_video(self, video_id: str) -> list[dict]:
        phrases = self.conn.execute(
            "SELECT phrase_en FROM phrases WHERE video_id = ?", (video_id,)
        ).fetchall()
        all_links = []
        seen = set()
        for p in phrases:
            pen = p["phrase_en"].strip().lower()
            links = self.get_phrase_links(pen)
            for lk in links:
                key = (pen, lk["phrase"], lk["type"])
                if key not in seen:
                    seen.add(key)
                    all_links.append({"source": pen, **lk})
        return all_links

    def add_topics(self, video_id: str, topics: list[str]) -> None:
        self.conn.execute("DELETE FROM video_topics WHERE video_id = ?", (video_id,))
        for t in topics:
            t = t.strip().lower()
            if t:
                try:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO video_topics (video_id, topic) VALUES (?,?)",
                        (video_id, t)
                    )
                except Exception:
                    pass
        self.conn.commit()

    def get_videos_by_topic(self, topic: str, exclude_id: str = "", limit: int = 10) -> list[dict]:
        rows = self.conn.execute("""
            SELECT v.video_id, v.url, v.title, v.channel, v.created_at, vt.topic
            FROM video_topics vt JOIN videos v ON vt.video_id = v.video_id
            WHERE vt.topic = ? AND v.video_id != ?
            ORDER BY v.created_at DESC LIMIT ?
        """, (topic.strip().lower(), exclude_id, limit)).fetchall()
        return [dict(r) for r in rows]

    def add_video_links(self, video_id: str, related_ids: list[dict]) -> None:
        for r in related_ids:
            vid_b = r.get("video_id", "")
            if not vid_b or vid_b == video_id:
                continue
            a, b = (video_id, vid_b) if video_id < vid_b else (vid_b, video_id)
            try:
                self.conn.execute(
                    "INSERT OR REPLACE INTO video_links (video_id_a, video_id_b, link_type, score) VALUES (?,?,?,?)",
                    (a, b, r.get("type", "topic"), r.get("score", 0))
                )
            except Exception:
                pass
        self.conn.commit()

    def auto_link_videos_by_topic(self, video_id: str) -> int:
        topics = self.conn.execute(
            "SELECT topic FROM video_topics WHERE video_id = ?", (video_id,)
        ).fetchall()
        count = 0
        for t in topics:
            same_topic = self.get_videos_by_topic(t["topic"], exclude_id=video_id)
            for sv in same_topic:
                a, b = (video_id, sv["video_id"]) if video_id < sv["video_id"] else (sv["video_id"], video_id)
                try:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO video_links (video_id_a, video_id_b, link_type, score) VALUES (?,?,?,?)",
                        (a, b, "topic", 1.0)
                    )
                    count += 1
                except Exception:
                    pass
        self.conn.commit()
        return count

    def find_related_videos(self, subtitle_text: str, current_video_id: str = "", limit: int = 5) -> list[dict]:
        words = set(re.findall(r"[a-zA-Z]{4,}", subtitle_text.lower()))
        if not words:
            return []

        rows = self.conn.execute(
            "SELECT DISTINCT v.video_id, v.url, v.title, v.channel, v.created_at "
            "FROM videos v WHERE v.video_id != ? AND v.subtitle_en != '' "
            "ORDER BY v.created_at DESC LIMIT 50",
            (current_video_id,)
        ).fetchall()

        scored = []
        for row in rows:
            past_phrases = self.conn.execute(
                "SELECT phrase_en FROM phrases WHERE video_id = ?", (row["video_id"],)
            ).fetchall()
            phrase_words = set()
            for p in past_phrases:
                phrase_words.update(re.findall(r"[a-zA-Z]{4,}", p["phrase_en"].lower()))

            overlap = len(words & phrase_words)
            if overlap > 0:
                scored.append({
                    "video_id": row["video_id"],
                    "url": row["url"],
                    "title": row["title"],
                    "channel": row["channel"],
                    "date": row["created_at"],
                    "score": overlap,
                })

        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored[:limit]

    def find_related_by_keyword(self, keyword: str, limit: int = 10) -> list[dict]:
        results = []
        kw = f"%{keyword}%"

        rows = self.conn.execute("""
            SELECT p.phrase_en, p.phrase_ja, p.note, p.is_top,
                   v.video_id, v.url, v.title, v.created_at
            FROM phrases p JOIN videos v ON p.video_id = v.video_id
            WHERE p.phrase_en LIKE ? OR p.phrase_ja LIKE ? OR p.note LIKE ?
            ORDER BY v.created_at DESC LIMIT ?
        """, (kw, kw, kw, limit)).fetchall()

        for r in rows:
            results.append({
                "phrase_en": r["phrase_en"], "phrase_ja": r["phrase_ja"],
                "note": r["note"], "is_top": r["is_top"],
                "video_id": r["video_id"], "url": r["url"],
                "title": r["title"], "date": r["created_at"],
            })

        sub_rows = self.conn.execute("""
            SELECT video_id, url, title, created_at
            FROM videos
            WHERE subtitle_en LIKE ? OR subtitle_ja LIKE ? OR title LIKE ?
            ORDER BY created_at DESC LIMIT ?
        """, (kw, kw, kw, limit)).fetchall()

        seen_ids = {r["video_id"] for r in results}
        for r in sub_rows:
            if r["video_id"] not in seen_ids:
                results.append({
                    "phrase_en": "", "phrase_ja": "",
                    "note": "(字幕本文にマッチ)", "is_top": 0,
                    "video_id": r["video_id"], "url": r["url"],
                    "title": r["title"], "date": r["created_at"],
                })

        return results

    def get_history(self, limit: int = 30) -> list[dict]:
        rows = self.conn.execute("""
            SELECT video_id, url, title, channel, llm_model, created_at,
                   (SELECT COUNT(*) FROM phrases WHERE phrases.video_id = videos.video_id) as phrase_count
            FROM videos ORDER BY created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def get_past_top_phrases(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute("""
            SELECT p.phrase_en, p.phrase_ja, p.note, v.title, v.url, v.created_at
            FROM phrases p JOIN videos v ON p.video_id = v.video_id
            WHERE p.is_top = 1
            ORDER BY v.created_at DESC LIMIT ?
        """, (limit,)).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self.conn.close()


# ============================================================
# URL解決
# ============================================================
def resolve_urls(urls: list[str], no_playlist: bool = False) -> list[dict]:
    resolved = []
    for url in urls:
        is_playlist = "list=" in url and not no_playlist
        if is_playlist and "watch?v=" in url:
            print(f"📋 個別動画として処理: {url}")
            info = get_video_info(url)
            resolved.append(info)
        elif is_playlist:
            print(f"📋 プレイリスト展開中: {url}")
            entries = expand_playlist(url)
            resolved.extend(entries)
            print(f"  → {len(entries)}本の動画を検出")
        else:
            info = get_video_info(url)
            resolved.append(info)
    return resolved


def expand_playlist(url: str) -> list[dict]:
    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
    entries = []
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            for entry in info.get("entries", []):
                if entry:
                    vid_url = entry.get("url") or f"https://www.youtube.com/watch?v={entry.get('id','')}"
                    entries.append({
                        "url": vid_url, "title": entry.get("title", "Unknown"),
                        "video_id": entry.get("id", ""), "channel": entry.get("uploader", ""),
                        "duration": entry.get("duration", 0), "description": "",
                    })
        except Exception as e:
            print(f"  ⚠ プレイリスト展開エラー: {e}")
    return entries


def get_video_info(url: str) -> dict:
    opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
            return {
                "url": url,
                "title": info.get("title", "Unknown"),
                "video_id": info.get("id", ""),
                "channel": info.get("uploader", "") or info.get("channel", ""),
                "duration": info.get("duration", 0),
                "description": (info.get("description", "") or "")[:500],
            }
        except Exception:
            return {"url": url, "title": "Unknown", "video_id": "", "channel": "",
                    "duration": 0, "description": ""}


def get_video_title(url: str) -> str:
    return get_video_info(url).get("title", "Unknown")


# ============================================================
# 動画ダウンロード
# ============================================================
def make_safe_filename(title: str) -> str:
    safe = re.sub(r'[^a-zA-Z0-9_\-\.]', '', title)
    return safe if safe else f"video_{hash(title) % 10000:04d}"


def download_video(url: str, outdir: str, title: str = "") -> str | None:
    """
    【改造】outdir に直接動画を保存する（日付フォルダは呼び出し元で決定済み）
    """
    os.makedirs(outdir, exist_ok=True)
    if not title:
        title = get_video_title(url)
    safe_name = make_safe_filename(title)
    output_path = os.path.join(outdir, f"{safe_name}.mp4")

    print(f"  📥 動画DL中... → {output_path}")
    progress_state = {"last_update": 0}

    def progress_hook(d):
        if d["status"] == "downloading":
            now = time.time()
            if now - progress_state["last_update"] < 0.5:
                return
            progress_state["last_update"] = now
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0
            if total > 0:
                pct = downloaded / total * 100
                bar_len = 25
                filled = int(bar_len * pct / 100)
                bar = "#" * filled + "-" * (bar_len - filled)
                speed_str = f"{speed/1024/1024:.1f}MB/s" if speed else "---"
                sys.stdout.write(f"\r     [{bar}] {pct:.1f}% ({speed_str})")
                sys.stdout.flush()
        elif d["status"] == "finished":
            sys.stdout.write("\r     ファイル結合中...                         \n")
            sys.stdout.flush()

    dl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best",
        "merge_output_format": "mp4", "outtmpl": output_path,
        "noplaylist": True, "quiet": True, "no_warnings": True,
        "progress_hooks": [progress_hook],
    }
    with yt_dlp.YoutubeDL(dl_opts) as ydl:
        try:
            ydl.download([url])
        except Exception as e:
            print(f"\n  ✗ 動画DLエラー: {e}")
            return None

    for ext in [".mp4", ".mkv", ".webm"]:
        p = os.path.join(outdir, f"{safe_name}{ext}")
        if os.path.exists(p):
            mb = os.path.getsize(p) / 1024 / 1024
            print(f"  ✅ 動画保存: {mb:.1f}MB")
            return p
    print(f"  ⚠ 動画ファイル見つからず")
    return None


# ============================================================
# 字幕ダウンロード
# ============================================================
def download_subtitles(url: str, lang: str = "en,ja", outdir: str | None = None) -> dict[str, str]:
    """
    【改造】SRTファイルを outdir に保存しつつ、テキスト辞書も返す
    """
    if outdir is None:
        outdir = tempfile.mkdtemp(prefix="ytsub_")
    os.makedirs(outdir, exist_ok=True)
    lang_list = [l.strip() for l in lang.split(",")]

    print("  📋 字幕情報を取得中...")
    info_opts = {"quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True}
    with yt_dlp.YoutubeDL(info_opts) as ydl:
        try:
            info = ydl.extract_info(url, download=False)
        except Exception as e:
            print(f"  ✗ 情報取得失敗: {e}")
            return {}

    manual_subs = info.get("subtitles", {})
    auto_subs = info.get("automatic_captions", {})
    avail_m = [l for l in lang_list if l in manual_subs]
    avail_a = [l for l in lang_list if l in auto_subs]
    print(f"    手動: {avail_m or 'なし'} / 自動: {avail_a or 'なし'}")

    if not avail_m and not avail_a:
        return {}

    tmpdir = tempfile.mkdtemp(prefix="ytsub_dl_")
    dl_opts = {
        "quiet": True, "no_warnings": True, "skip_download": True, "noplaylist": True,
        "writesubtitles": bool(avail_m),
        "writeautomaticsub": bool(avail_a) and not bool(avail_m),
        "subtitleslangs": lang_list, "subtitlesformat": "srt/vtt/best",
        "outtmpl": os.path.join(tmpdir, "%(id)s.%(ext)s"),
        "postprocessors": [{"key": "FFmpegSubtitlesConvertor", "format": "srt"}],
    }
    if avail_m and avail_a:
        if [l for l in lang_list if l not in manual_subs]:
            dl_opts["writeautomaticsub"] = True

    with yt_dlp.YoutubeDL(dl_opts) as ydl:
        try:
            ydl.download([url])
        except Exception as e:
            print(f"  ⚠ 字幕DL警告: {e}")

    # 【改造】タイトルベースのファイル名でoutdirに保存
    video_title = info.get("title", "subtitle")
    safe_title = make_safe_filename(video_title)[:60]

    subtitles = {}
    for f in os.listdir(tmpdir):
        if f.endswith(".srt") or f.endswith(".vtt"):
            fp = os.path.join(tmpdir, f)
            parts = f.rsplit(".", 2)
            lc = parts[-2] if len(parts) >= 3 else "unknown"
            with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
            clean = srt_to_text(raw)
            if clean.strip():
                subtitles[lc] = clean
                # .md 保存
                md_filename = f"{safe_title}.{lc}.md"
                md_path = os.path.join(outdir, md_filename)
                with open(md_path, "w", encoding="utf-8") as mf:
                    mf.write(f"# {video_title}\n\n")
                    mf.write(f"**言語**: {lc}  \n")
                    mf.write(f"**文字数**: {len(clean)}  \n\n")
                    mf.write("---\n\n")
                    mf.write(clean + "\n")
                # .html 保存
                html_filename = f"{safe_title}.{lc}.html"
                html_path = os.path.join(outdir, html_filename)
                html_lines = "".join(f"<p>{line}</p>\n" for line in clean.splitlines() if line.strip())
                with open(html_path, "w", encoding="utf-8") as hf:
                    hf.write(f"""<!DOCTYPE html>
<html lang="{lc}">
<head>
<meta charset="utf-8">
<title>{video_title}</title>
<style>body{{font-family:sans-serif;max-width:800px;margin:2em auto;line-height:1.8}}p{{margin:.4em 0}}</style>
</head>
<body>
<h1>{video_title}</h1>
{html_lines}</body>
</html>
""")
                print(f"    ✅ {lc}: {len(clean)}文字 → {md_filename} / {html_filename}")
    shutil.rmtree(tmpdir, ignore_errors=True)

    if not subtitles:
        print("    ⚠ URL直接取得を試みます...")
        subtitles = extract_subs_from_info(info, lang_list)
    return subtitles


def extract_subs_from_info(info, lang_list):
    subtitles = {}
    for sk in ["subtitles", "automatic_captions"]:
        sd = info.get(sk, {})
        for lc in lang_list:
            if lc in subtitles or lc not in sd:
                continue
            for fmt in sd[lc]:
                ext, sub_url = fmt.get("ext",""), fmt.get("url","")
                if ext in ("srt","vtt","srv1","srv2","srv3","json3") and sub_url:
                    try:
                        r = requests.get(sub_url, timeout=30)
                        if r.status_code == 200:
                            c = json3_to_text(r.text) if ext == "json3" else srt_to_text(r.text)
                            if c.strip():
                                subtitles[lc] = c
                                print(f"    ✅ {lc} (URL/{ext}): {len(c)}文字")
                                break
                    except Exception:
                        continue
    return subtitles


def json3_to_text(t):
    try:
        d = json.loads(t)
        lines, seen = [], set()
        for ev in d.get("events", []):
            for seg in ev.get("segs", []):
                tx = seg.get("utf8","").strip()
                if tx and tx != "\n" and tx not in seen:
                    seen.add(tx); lines.append(tx)
        return " ".join(lines)
    except:
        return ""


def srt_to_text(content):
    lines, seen = [], set()
    for line in content.split("\n"):
        line = line.strip()
        if not line: continue
        if line.startswith("WEBVTT") or line.startswith("Kind:") or line.startswith("Language:"): continue
        if re.match(r"^\d+$", line): continue
        if re.match(r"\d{2}:\d{2}:\d{2}", line) or "-->" in line: continue
        line = re.sub(r"<[^>]+>", "", line)
        line = re.sub(r"align:\w+ position:\d+%", "", line).strip()
        if line and line not in seen:
            seen.add(line); lines.append(line)
    return "\n".join(lines)


# ============================================================
# OpenRouter LLM
# ============================================================
def call_openrouter(prompt: str, model: str, api_key: str) -> str:
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": (
                "You are an expert English teacher specializing in practical English phrases "
                "from native conversations. You help Japanese learners (targeting Eiken Grade 1 / "
                "interpreter level) identify key phrases, idioms, and expressions. "
                "Always provide Japanese translations. When referencing related past videos, "
                "include their URLs so the learner can review them."
            )},
            {"role": "user", "content": prompt},
        ],
        "max_tokens": 6000,
        "temperature": 0.3,
    }
    print(f"\n  🤖 LLM分析中 ({model})...")
    try:
        resp = requests.post("https://openrouter.ai/api/v1/chat/completions",
                             headers=headers, json=payload, timeout=180)
    except requests.exceptions.Timeout:
        print("  ✗ APIタイムアウト"); return ""
    if resp.status_code != 200:
        print(f"  ✗ APIエラー: {resp.status_code}")
        try: print(f"     {resp.json().get('error',{}).get('message','')[:300]}")
        except: print(f"     {resp.text[:300]}")
        return ""
    return resp.json()["choices"][0]["message"]["content"]


# ============================================================
# プロンプト構築（過去動画参照付き）
# ============================================================
def build_prompt(subtitles: dict[str, str], title: str, url: str,
                 video_info: dict, top_n: int,
                 related_videos: list[dict], past_top_phrases: list[dict]) -> str:

    sub_sections = ""
    for lc, text in subtitles.items():
        truncated = text[:6000] if len(text) > 6000 else text
        sub_sections += f"\n--- {lc} subtitles ---\n{truncated}\n"

    related_section = ""
    if related_videos:
        related_section = "\n## 参考: 過去に学習した関連動画\n"
        for rv in related_videos:
            related_section += f"- 「{rv['title']}」({rv['url']}) - {rv.get('date','')}\n"

    past_phrases_section = ""
    if past_top_phrases:
        past_phrases_section = "\n## 参考: 過去に選出されたTOPフレーズ（重複を避けるため）\n"
        for pp in past_top_phrases[:15]:
            past_phrases_section += f"- {pp['phrase_en']} | {pp['phrase_ja']} (動画: {pp['title']})\n"

    channel = video_info.get("channel", "")
    desc = video_info.get("description", "")[:300]
    duration = video_info.get("duration", 0)
    dur_str = f"{duration//60}分{duration%60}秒" if duration else "不明"

    return f"""以下はYouTube動画の字幕テキストです。

## 動画情報
- **タイトル**: {title}
- **URL**: {url}
- **チャンネル**: {channel}
- **動画長**: {dur_str}
- **概要**: {desc}

{sub_sections}
{related_section}
{past_phrases_section}

この字幕から以下の作業を行ってください：

## タスク1: 会話の流れ・文脈の要約
この動画で扱われている会話のシーン・状況・文脈を簡潔に説明してください。

## タスク2: 英文フレーズリスト
字幕に含まれる重要な英語フレーズ・表現をすべて抽出し、以下の形式でリストにしてください：
- 英語フレーズ | 和訳 | 使用場面・ニュアンスの簡単な説明

## タスク3: 特に覚えるべきTOP {top_n} フレーズ
上記リストの中から特に覚えるべきフレーズを{top_n}つ選び、それぞれについて詳しく説明してください：
{"（注意: 過去のTOPフレーズと重複しないものを優先してください）" if past_top_phrases else ""}

1. **フレーズ**: (英語)
   - **和訳**: 
   - **選んだ理由**: なぜこのフレーズが重要か
   - **使用例**: このフレーズを使った別の例文（英語＋和訳）
   - **関連表現**: 類似表現や言い換え

## タスク4: 関連動画への言及
{"上記の「過去に学習した関連動画」リストの中に、今回のフレーズと関連するものがあれば、復習ポイントとして言及してください。" if related_videos else "（過去の学習データなし）"}

## タスク5: トピックタグ
この動画の内容を表すトピックタグを5～10個、英語の小文字で出力してください。
TOPICS: tag1, tag2, tag3, tag4, tag5

## タスク6: フレーズ関連マップ
タスク2で抽出したフレーズ同士の関連を列挙してください：
LINK: フレーズA ||| フレーズB ||| 関連タイプ ||| 補足メモ

関連タイプ: synonym, antonym, root, broader, narrower, collocate, variant

出力は日本語で行ってください（TOPICS行とLINK行のみ英語）。
"""


# ============================================================
# MD保存（リッチ版）
# ============================================================
def save_rich_md(llm_result: str, title: str, url: str, video_info: dict,
                 related_videos: list[dict], phrase_links: list[dict],
                 topics: list[str], outdir: str, model: str) -> str:
    os.makedirs(outdir, exist_ok=True)
    safe_name = make_safe_filename(title)[:50]
    filepath = os.path.join(outdir, f"{safe_name}_phrases.md")

    channel = video_info.get("channel", "")
    duration = video_info.get("duration", 0)
    dur_str = f"{duration//60}分{duration%60}秒" if duration else "不明"

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"# {title}\n\n")
        f.write(f"## 動画情報\n")
        f.write(f"| 項目 | 内容 |\n|---|---|\n")
        f.write(f"| URL | {url} |\n")
        f.write(f"| チャンネル | {channel} |\n")
        f.write(f"| 動画長 | {dur_str} |\n")
        f.write(f"| 分析モデル | {model} |\n")
        f.write(f"| 分析日時 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |\n")
        if topics:
            f.write(f"| トピック | {', '.join(topics)} |\n")
        f.write("\n")

        if related_videos:
            f.write(f"## 🔗 関連する過去の学習動画\n")
            for rv in related_videos:
                f.write(f"- [{rv['title']}]({rv['url']}) ({rv.get('date','')})\n")
            f.write("\n")

        f.write("---\n\n")

        for line in llm_result.split("\n"):
            stripped = line.strip()
            if stripped.startswith("TOPICS:") or stripped.startswith("LINK:"):
                continue
            f.write(line + "\n")

        if phrase_links:
            f.write(f"\n---\n\n## 🔗 フレーズ関連マップ\n\n")
            type_labels = {
                "synonym": "類義語", "antonym": "対義語", "root": "同語根",
                "broader": "上位概念", "narrower": "下位概念",
                "collocate": "共起", "variant": "変形",
            }
            by_type = {}
            for lk in phrase_links:
                lt = lk.get("type", "synonym")
                by_type.setdefault(lt, []).append(lk)

            for lt, items in by_type.items():
                label = type_labels.get(lt, lt)
                f.write(f"### {label}\n")
                for lk in items:
                    note = f" → {lk['note']}" if lk.get("note") else ""
                    f.write(f"- **{lk['source']}** → **{lk['phrase']}**{note}\n")
                f.write("\n")

        f.write("\n")

    return filepath


def save_summary(all_results: list[dict], outdir: str) -> str:
    os.makedirs(outdir, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filepath = os.path.join(outdir, f"phrases_summary_{ts}.md")
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(f"# 英語フレーズ抽出サマリー\n")
        f.write(f"生成日時: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
        f.write(f"処理動画数: {len(all_results)}\n\n---\n\n")
        for i, item in enumerate(all_results, 1):
            f.write(f"## {i}. [{item['title']}]({item['url']})\n\n")
            if item.get("result"):
                f.write(item["result"])
            f.write("\n\n---\n\n")
    return filepath


# ============================================================
# フレーズ簡易パース（LLM出力からDBに登録用）
# ============================================================
def parse_phrases_from_llm(llm_result: str) -> list[dict]:
    phrases = []
    in_top = False
    for line in llm_result.split("\n"):
        line = line.strip()
        if re.search(r"(TOP\s*\d|特に覚える|タスク3)", line, re.IGNORECASE):
            in_top = True
        if re.search(r"(タスク4|タスク5|タスク6|トピック|関連マップ)", line, re.IGNORECASE):
            in_top = False

        m = re.match(r"^[-*]\s*(.+?)\s*\|\s*(.+?)(?:\s*\|\s*(.+))?$", line)
        if m:
            phrases.append({
                "en": m.group(1).strip(),
                "ja": m.group(2).strip(),
                "note": (m.group(3) or "").strip(),
                "is_top": 1 if in_top else 0,
            })
            continue

        m2 = re.match(r"\*?\*?フレーズ\*?\*?\s*[:：]\s*(.+)", line)
        if m2 and in_top:
            phrases.append({
                "en": m2.group(1).strip().strip("*"),
                "ja": "", "note": "", "is_top": 1,
            })

    return phrases


def parse_topics_from_llm(llm_result: str) -> list[str]:
    topics = []
    for line in llm_result.split("\n"):
        line = line.strip()
        m = re.match(r"^TOPICS?\s*[:：]\s*(.+)", line, re.IGNORECASE)
        if m:
            raw = m.group(1)
            for tag in re.split(r"[,、;；]+", raw):
                tag = tag.strip().lower()
                tag = re.sub(r"[^a-z0-9\s\-]", "", tag).strip()
                if tag and len(tag) > 1:
                    topics.append(tag)
            break
    return topics


def parse_links_from_llm(llm_result: str) -> list[dict]:
    links = []
    for line in llm_result.split("\n"):
        line = line.strip()
        m = re.match(r"^LINK\s*[:：]\s*(.+)", line, re.IGNORECASE)
        if m:
            parts = [p.strip() for p in m.group(1).split("|||")]
            if len(parts) >= 3:
                link = {
                    "a": parts[0],
                    "b": parts[1],
                    "type": parts[2].lower().strip(),
                    "note": parts[3] if len(parts) > 3 else "",
                }
                valid_types = {"synonym","antonym","root","broader","narrower","collocate","variant"}
                if link["type"] not in valid_types:
                    link["type"] = "synonym"
                links.append(link)
    return links


# ============================================================
# Web記事処理（Bridge English等のブログ記事対応）
# ============================================================
def is_web_url(url: str) -> bool:
    yt_patterns = ["youtube.com", "youtu.be", "youtube-nocookie.com"]
    return not any(p in url.lower() for p in yt_patterns)


def fetch_web_article(url: str) -> dict | None:
    try:
        r = requests.get(url, timeout=30, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        r.raise_for_status()
        html = r.text
    except Exception as e:
        print(f"  ⚠ Web記事取得エラー: {e}")
        return None

    return parse_bridge_article(html, url)


def parse_bridge_article(html: str, url: str) -> dict:
    from html.parser import HTMLParser
    import html as html_module

    title = ""
    m = re.search(r"<title[^>]*>([^<]+)</title>", html, re.IGNORECASE)
    if m:
        title = html_module.unescape(m.group(1)).strip()
        for sep in [": ", "： "]:
            if sep in title:
                title = title.split(sep, 1)[-1].strip()

    body = ""
    m_start = re.search(
        r"class=['\"]post-body\s+entry-content['\"][^>]*>",
        html, re.DOTALL | re.IGNORECASE
    )
    if m_start:
        rest = html[m_start.end():]
        m_end = re.search(r"class=['\"]post-footer", rest, re.IGNORECASE)
        if m_end:
            body = rest[:m_end.start()]
        else:
            body = rest[:5000]
    else:
        m_start2 = re.search(
            r"class=['\"]post-body['\"][^>]*>",
            html, re.DOTALL | re.IGNORECASE
        )
        if m_start2:
            rest = html[m_start2.end():]
            m_end = re.search(r"class=['\"]post-footer", rest, re.IGNORECASE)
            if m_end:
                body = rest[:m_end.start()]
            else:
                body = rest[:5000]

    links_found = []
    def replace_link(m):
        href = m.group(1)
        text = re.sub(r"<[^>]+>", "", m.group(2))
        links_found.append({"href": href, "text": text.strip()})
        return f"[LINK:{text.strip()}→{href}]"

    body_with_links = re.sub(r'<a[^>]+href=["\']([^"\']+)["\'][^>]*>(.*?)</a>', replace_link, body, flags=re.DOTALL)
    body_with_links = re.sub(r'<b>(.*?)</b>', r'**\1**', body_with_links)
    body_with_links = re.sub(r'<strong>(.*?)</strong>', r'**\1**', body_with_links)
    body_with_links = re.sub(r'<br\s*/?>', '\n', body_with_links)
    body_with_links = re.sub(r'</p>', '\n', body_with_links)
    body_with_links = re.sub(r'</div>', '\n', body_with_links)

    plain = re.sub(r'<[^>]+>', '', body_with_links)
    plain = html_module.unescape(plain)
    plain = re.sub(r'\n{3,}', '\n\n', plain).strip()

    is_quiz = bool(re.search(r'練習問題', title))

    quiz_answer_url = ""
    if is_quiz:
        for lk in links_found:
            if re.search(r'\d+\s*words?', lk["text"], re.IGNORECASE):
                quiz_answer_url = lk["href"]
                break
            if "bridge-english" in lk["href"] and lk["href"] != url:
                quiz_answer_url = lk["href"]
                break

    date = ""
    m_date = re.search(r'(\d{4})年(\d{1,2})月(\d{1,2})日', html)
    if m_date:
        date = f"{m_date.group(1)}-{m_date.group(2).zfill(2)}-{m_date.group(3).zfill(2)}"

    return {
        "url": url,
        "title": title,
        "body": plain,
        "links": links_found,
        "is_quiz": is_quiz,
        "quiz_answer_url": quiz_answer_url,
        "date": date,
        "source": "web",
    }


def build_web_prompt(article: dict, top_n: int, related_videos: list[dict],
                     past_top_phrases: list[dict], answer_article: dict | None = None) -> str:

    body = article["body"][:6000]

    related_section = ""
    if related_videos:
        related_section = "\n## 参考: 過去に学習した関連データ\n"
        for rv in related_videos:
            related_section += f"- 「{rv['title']}」({rv['url']}) - {rv.get('date','')}\n"

    past_phrases_section = ""
    if past_top_phrases:
        past_phrases_section = "\n## 参考: 過去に選出されたTOPフレーズ（重複を避けるため）\n"
        for pp in past_top_phrases[:15]:
            past_phrases_section += f"- {pp['phrase_en']} | {pp['phrase_ja']} (出典: {pp['title']})\n"

    answer_section = ""
    if answer_article:
        answer_body = answer_article["body"][:3000]
        answer_section = f"""
## 参考: 穴埋め問題の答えの記事
URL: {answer_article['url']}
タイトル: {answer_article['title']}

{answer_body}
"""

    if article["is_quiz"]:
        return f"""以下は英語学習ブログ「Bridge English」のヒアリング練習問題の記事です。

## 記事情報
- **タイトル**: {article['title']}
- **URL**: {article['url']}
- **日付**: {article.get('date', '')}

## 記事本文
{body}
{answer_section}
{related_section}
{past_phrases_section}

この記事は「(N words)」の部分が穴埋め問題です。リンク先が答えのフレーズです。

## タスク1: 問題と答えの解説
穴埋め問題を特定し、以下の形式で解説してください：
- **問題文（英語）**: 穴埋め部分を含む英文全体
- **答えのフレーズ**: (N words) の部分に入る英語
- **答えの和訳**: フレーズの日本語訳
- **英文全体の和訳**: 完成した文全体の意味

## タスク2: 答えフレーズの詳細解説
1. **フレーズ**: (英語)
   - **和訳**:
   - **意味・ニュアンス**:
   - **使用例**: 別の例文（英語＋和訳）を2つ
   - **関連表現**:

## タスク3: 記事中のその他の重要表現
- 英語フレーズ | 和訳 | 使用場面・ニュアンスの簡単な説明

## タスク4: 特に覚えるべきTOP {top_n} フレーズ
{"（注意: 過去のTOPフレーズと重複しないものを優先してください）" if past_top_phrases else ""}

1. **フレーズ**: (英語)
   - **和訳**:
   - **選んだ理由**:
   - **使用例**:
   - **関連表現**:

## タスク5: トピックタグ
TOPICS: tag1, tag2, tag3, tag4, tag5

## タスク6: フレーズ関連マップ
LINK: フレーズA ||| フレーズB ||| 関連タイプ ||| 補足メモ

関連タイプ: synonym, antonym, root, broader, narrower, collocate, variant

出力は日本語で行ってください（TOPICS行とLINK行のみ英語）。
"""
    else:
        return f"""以下は英語学習ブログ「Bridge English」の単語・フレーズ解説記事です。

## 記事情報
- **タイトル**: {article['title']}
- **URL**: {article['url']}
- **日付**: {article.get('date', '')}

## 記事本文
{body}
{related_section}
{past_phrases_section}

## タスク1: メインフレーズの詳細解説
1. **フレーズ**: (英語)
   - **和訳**:
   - **品詞・文法情報**:
   - **意味・ニュアンス**:
   - **使用例**: 記事内の例文の解説
   - **追加例文**: 記事にない別の使用例（英語＋和訳）を2つ
   - **関連表現**:
   - **注意点**:

## タスク2: 記事中のその他の重要表現
- 英語フレーズ | 和訳 | 使用場面・ニュアンスの簡単な説明

## タスク3: 特に覚えるべきTOP {top_n} フレーズ
{"（注意: 過去のTOPフレーズと重複しないものを優先してください）" if past_top_phrases else ""}

1. **フレーズ**: (英語)
   - **和訳**:
   - **選んだ理由**:
   - **使用例**:
   - **関連表現**:

## タスク4: トピックタグ
TOPICS: tag1, tag2, tag3, tag4, tag5

## タスク5: フレーズ関連マップ
LINK: フレーズA ||| フレーズB ||| 関連タイプ ||| 補足メモ

関連タイプ: synonym, antonym, root, broader, narrower, collocate, variant

出力は日本語で行ってください（TOPICS行とLINK行のみ英語）。
"""


def process_web_article(url: str, model: str, api_key: str, outdir: str,
                        top_n: int, db: PhraseDB) -> dict | None:
    """
    【改造】Web記事を処理してDBに登録 + 日付フォルダに保存
    outdir は既に日付フォルダ込みのパスを受け取る
    """
    print(f"\n{'─' * 55}")
    print(f"🌐 {url}")
    print(f"{'─' * 55}")

    article = fetch_web_article(url)
    if not article or not article["body"]:
        print(f"  ✗ 記事取得失敗")
        return None

    title = article["title"]
    print(f"  📖 {title}")
    print(f"  {'📝 練習問題' if article['is_quiz'] else '📚 フレーズ解説'}")

    article_id = re.sub(r'[^a-zA-Z0-9]', '_', url.split("//")[-1])[:80]

    result_data = {"url": url, "title": title, "video_path": None, "md_path": None, "result": ""}

    answer_article = None
    if article["is_quiz"] and article["quiz_answer_url"]:
        answer_url = article["quiz_answer_url"]
        if not answer_url.startswith("http"):
            answer_url = "https://bridge-english.blogspot.com" + answer_url
        print(f"  📋 答え記事取得中: {answer_url}")
        answer_article = fetch_web_article(answer_url)
        if answer_article:
            print(f"  ✅ 答え: {answer_article['title']}")

    db.upsert_video(article_id,
                    url=url, title=title,
                    channel="Bridge English",
                    subtitle_en=article["body"],
                    description=f"source:web quiz:{article['is_quiz']}")

    related = db.find_related_videos(article["body"], article_id)
    past_top = db.get_past_top_phrases()
    if related:
        print(f"  🔗 関連する過去データ: {len(related)}件")

    prompt = build_web_prompt(article, top_n, related, past_top, answer_article)
    llm_result = call_openrouter(prompt, model, api_key)

    if llm_result:
        result_data["result"] = llm_result
        db.upsert_video(article_id, llm_result=llm_result, llm_model=model)

        phrases = parse_phrases_from_llm(llm_result)
        if phrases:
            db.add_phrases(article_id, phrases)
            print(f"  📚 {len(phrases)}フレーズをDBに登録")

        topics = parse_topics_from_llm(llm_result)
        if topics:
            db.add_topics(article_id, topics)
            print(f"  🏷  トピック: {', '.join(topics)}")
            vlink_count = db.auto_link_videos_by_topic(article_id)
            if vlink_count:
                print(f"  🔗 関連リンク: {vlink_count}件追加")

        phrase_links = parse_links_from_llm(llm_result)
        if phrase_links:
            link_count = db.add_phrase_links(phrase_links)
            print(f"  🕸 フレーズ関連: {link_count}件登録")

        # 【改造】MDをoutdir（日付フォルダ）に保存
        video_info = {"channel": "Bridge English", "duration": 0}
        all_phrase_links = db.get_all_links_for_video(article_id)
        md_path = save_rich_md(llm_result, title, url, video_info,
                               related, all_phrase_links, topics, outdir, model)
        result_data["md_path"] = md_path
        db.upsert_video(article_id, md_path=md_path)
        print(f"  📄 MD保存: {md_path}")
    else:
        print(f"  ✗ LLM分析失敗")

    if not result_data["result"]:
        return None
    return result_data


# ============================================================
# 単一動画処理
# ============================================================
def process_single_video(url: str, video_info: dict, mode: str, model: str,
                         api_key: str, lang: str, outdir: str, top_n: int,
                         db: PhraseDB) -> dict | None:
    """
    【改造】outdir は日付フォルダ込みのパス。
    動画・字幕・MDすべてこのフォルダに保存する。
    """
    title = video_info.get("title", "Unknown")
    video_id = video_info.get("video_id", "")

    print(f"\n{'─' * 55}")
    print(f"🎬 {title}")
    print(f"   {url}")
    print(f"{'─' * 55}")

    result_data = {"url": url, "title": title, "video_path": None, "md_path": None, "result": ""}

    # 動画DL → 日付フォルダへ
    if mode in ("video", "both"):
        vp = download_video(url, outdir, title)
        result_data["video_path"] = vp

    # 字幕＋LLM
    if mode in ("sub", "both"):
        # 【改造】字幕SRTファイルも日付フォルダへ保存
        subtitles = download_subtitles(url, lang=lang, outdir=outdir)

        if subtitles:
            db.upsert_video(video_id,
                            url=url, title=title,
                            channel=video_info.get("channel", ""),
                            duration=video_info.get("duration", 0),
                            description=video_info.get("description", ""),
                            subtitle_en=subtitles.get("en", ""),
                            subtitle_ja=subtitles.get("ja", ""))

            sub_text = subtitles.get("en", "") + " " + subtitles.get("ja", "")
            related = db.find_related_videos(sub_text, video_id)
            past_top = db.get_past_top_phrases()

            if related:
                print(f"  🔗 関連する過去動画: {len(related)}件")

            prompt = build_prompt(subtitles, title, url, video_info, top_n, related, past_top)
            llm_result = call_openrouter(prompt, model, api_key)

            if llm_result:
                result_data["result"] = llm_result
                db.upsert_video(video_id, llm_result=llm_result, llm_model=model)

                phrases = parse_phrases_from_llm(llm_result)
                if phrases:
                    db.add_phrases(video_id, phrases)
                    print(f"  📚 {len(phrases)}フレーズをDBに登録")

                topics = parse_topics_from_llm(llm_result)
                if topics:
                    db.add_topics(video_id, topics)
                    print(f"  🏷  トピック: {', '.join(topics)}")
                    vlink_count = db.auto_link_videos_by_topic(video_id)
                    if vlink_count:
                        print(f"  🔗 動画間リンク: {vlink_count}件追加")

                phrase_links = parse_links_from_llm(llm_result)
                if phrase_links:
                    link_count = db.add_phrase_links(phrase_links)
                    print(f"  🕸 フレーズ関連: {link_count}件登録")

                # 【改造】MDを日付フォルダへ保存
                all_phrase_links = db.get_all_links_for_video(video_id)
                md_path = save_rich_md(llm_result, title, url, video_info,
                                       related, all_phrase_links, topics, outdir, model)
                result_data["md_path"] = md_path
                db.upsert_video(video_id, md_path=md_path,
                                video_path=result_data.get("video_path","") or "")
                print(f"  📄 MD保存: {md_path}")
            else:
                print(f"  ✗ LLM分析失敗")
        else:
            print(f"  ⚠ 字幕なし")
            db.upsert_video(video_id, url=url, title=title,
                            channel=video_info.get("channel",""),
                            duration=video_info.get("duration",0))

    if not result_data["video_path"] and not result_data["result"]:
        return None
    return result_data


# ============================================================
# メイン
# ============================================================
def main():
    parser = argparse.ArgumentParser(
        description="YouTube動画＆字幕DL → 日付別フォルダに整理 → DB蓄積 → 英語フレーズ抽出",
    )
    parser.add_argument("urls", nargs="*", help="YouTube動画URL or Web記事URL（複数可）")

    mode_group = parser.add_mutually_exclusive_group()
    mode_group.add_argument("--both", action="store_const", const="both", dest="mode", help="動画＋字幕＋LLM")
    mode_group.add_argument("--video", action="store_const", const="video", dest="mode", help="動画のみ")
    mode_group.add_argument("--sub", action="store_const", const="sub", dest="mode", help="字幕＋LLMのみ")

    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--lang", default="en")
    parser.add_argument("--outdir", default="/mnt/c/00data/dropbox/Dropbox/02_audio",
                        help="出力先ルートディレクトリ（この下にYYYYMMDDフォルダが作成される）")
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--no-playlist", action="store_true")
    parser.add_argument("--db", default=None, help="DBファイルパス")

    # DB管理コマンド
    parser.add_argument("--history", action="store_true")
    parser.add_argument("--search", type=str, default=None)
    parser.add_argument("--links", type=str, default=None)
    parser.add_argument("--topics", action="store_true")
    parser.add_argument("--list-models", action="store_true")

    parser.set_defaults(mode="both")
    args = parser.parse_args()

    db_path = args.db or os.path.join(os.path.expanduser("~"), "youtube_phrases.db")
    db = PhraseDB(db_path)

    if args.list_models:
        print("\n📋 おすすめモデル一覧:")
        print("-" * 60)
        for mid, desc in RECOMMENDED_MODELS.items():
            marker = " ← デフォルト" if mid == DEFAULT_MODEL else ""
            print(f"  {mid}\n    {desc}{marker}")
        print("-" * 60)
        db.close(); sys.exit(0)

    if args.history:
        history = db.get_history()
        if not history:
            print("📭 まだ処理履歴がありません。")
        else:
            print(f"\n📋 処理履歴 ({len(history)}件)")
            print("-" * 70)
            for h in history:
                pc = h.get("phrase_count", 0)
                print(f"  [{h['created_at']}] {h['title'][:40]}")
                print(f"    URL: {h['url']}")
                print(f"    チャンネル: {h['channel']}  フレーズ数: {pc}  モデル: {h['llm_model']}")
            print("-" * 70)
        db.close(); sys.exit(0)

    if args.search:
        results = db.find_related_by_keyword(args.search)
        if not results:
            print(f"🔍 「{args.search}」に該当するデータなし。")
        else:
            print(f"\n🔍 「{args.search}」の検索結果 ({len(results)}件)")
            print("-" * 70)
            for r in results:
                top_mark = "⭐" if r["is_top"] else "  "
                if r["phrase_en"]:
                    print(f"  {top_mark} {r['phrase_en']} | {r['phrase_ja']}")
                    print(f"      動画: {r['title'][:35]}  ({r['url']})")
                else:
                    print(f"  🎬 {r['title'][:40]}  ({r['url']})")
                    print(f"      {r['note']}")
            print("-" * 70)
        db.close(); sys.exit(0)

    if args.links:
        phrase = args.links
        links = db.get_phrase_links(phrase)
        if not links:
            print(f"🕸 「{phrase}」の関連語がDBにありません。")
        else:
            type_labels = {
                "synonym": "類義語", "antonym": "対義語", "root": "同語根",
                "broader": "上位概念", "narrower": "下位概念",
                "collocate": "共起", "variant": "変形",
            }
            print(f"\n🕸 「{phrase}」の関連語ネットワーク ({len(links)}件)")
            print("-" * 60)
            for lk in links:
                label = type_labels.get(lk["type"], lk["type"])
                note = f"  ({lk['note']})" if lk["note"] else ""
                print(f"  [{label}] {lk['phrase']}{note}")
            print("-" * 60)

            all_phrases = [phrase] + [lk["phrase"] for lk in links]
            print(f"\n🎬 関連フレーズが登場する動画:")
            shown = set()
            for p in all_phrases:
                rows = db.conn.execute("""
                    SELECT DISTINCT v.title, v.url, p.phrase_en
                    FROM phrases p JOIN videos v ON p.video_id = v.video_id
                    WHERE LOWER(p.phrase_en) = ?
                """, (p.lower(),)).fetchall()
                for r in rows:
                    key = r["url"]
                    if key not in shown:
                        shown.add(key)
                        print(f"  - [{r['title'][:40]}]({r['url']})")
                        print(f"    フレーズ: {r['phrase_en']}")
        db.close(); sys.exit(0)

    if args.topics:
        rows = db.conn.execute("""
            SELECT topic, COUNT(*) as cnt,
                   GROUP_CONCAT(v.title, ' / ') as titles
            FROM video_topics vt JOIN videos v ON vt.video_id = v.video_id
            GROUP BY topic ORDER BY cnt DESC
        """).fetchall()
        if not rows:
            print("🏷  トピックがまだ登録されていません。")
        else:
            print(f"\n🏷  登録トピック一覧 ({len(rows)}種)")
            print("-" * 60)
            for r in rows:
                titles = r["titles"][:60] + "..." if len(r["titles"]) > 60 else r["titles"]
                print(f"  [{r['cnt']}本] {r['topic']}")
                print(f"         {titles}")
            print("-" * 60)
        db.close(); sys.exit(0)

    # URL必須チェック
    if not args.urls:
        parser.print_help()
        db.close(); sys.exit(1)

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    has_web = any(is_web_url(u) for u in args.urls)
    if (args.mode in ("sub", "both") or has_web) and not api_key:
        print("✗ OPENROUTER_API_KEY が未設定。")
        print("   export OPENROUTER_API_KEY='sk-or-v1-xxxxxxxx'")
        db.close(); sys.exit(1)

    # 【改造】日付フォルダを作成（今回の処理は全てここへ）
    dated_outdir = get_dated_outdir(args.outdir)

    yt_urls = [u for u in args.urls if not is_web_url(u)]
    web_urls = [u for u in args.urls if is_web_url(u)]

    mode_label = {"both": "動画＋字幕＋LLM", "video": "動画のみ", "sub": "字幕＋LLM"}
    print("=" * 60)
    print("🎓 YouTube＆Web英語フレーズ抽出ツール")
    print(f"   モード: {mode_label[args.mode]} / モデル: {args.model}")
    if yt_urls:
        print(f"   YouTube: {len(yt_urls)}本")
    if web_urls:
        print(f"   Web記事: {len(web_urls)}本")
    print(f"   出力先: {dated_outdir}")  # 【改造】日付フォルダを表示
    print(f"   DB: {db_path}")
    print("=" * 60)

    videos = resolve_urls(yt_urls, no_playlist=args.no_playlist) if yt_urls else []
    total = len(videos) + len(web_urls)
    print(f"\n📋 処理対象: {total}件")

    all_results = []
    idx = 0

    for i, v in enumerate(videos, 1):
        idx += 1
        print(f"\n[{idx}/{total}]", end="")
        result = process_single_video(
            url=v["url"], video_info=v, mode=args.mode, model=args.model,
            api_key=api_key, lang=args.lang,
            outdir=dated_outdir,  # 【改造】日付フォルダを渡す
            top_n=args.top, db=db,
        )
        if result:
            all_results.append(result)

    for w_url in web_urls:
        idx += 1
        print(f"\n[{idx}/{total}]", end="")
        result = process_web_article(
            url=w_url, model=args.model, api_key=api_key,
            outdir=dated_outdir,  # 【改造】日付フォルダを渡す
            top_n=args.top, db=db,
        )
        if result:
            all_results.append(result)

    print("\n" + "=" * 60)
    print(f"✅ {len(all_results)}/{total}件処理完了")
    print("=" * 60)

    if not all_results:
        print("✗ 処理できたコンテンツなし。")
        db.close(); sys.exit(1)

    if len(all_results) == 1 and all_results[0].get("result"):
        print(f"\n{all_results[0]['result']}")

    # 【改造】サマリーMDも日付フォルダへ保存
    sub_results = [r for r in all_results if r.get("result")]
    if len(sub_results) > 1:
        sp = save_summary(sub_results, dated_outdir)
        print(f"\n📋 サマリー: {sp}")

    print("\n📁 保存先フォルダ:")
    print(f"   {dated_outdir}")
    print("\n📂 保存ファイル:")
    for r in all_results:
        if r.get("video_path"): print(f"   🎬 {os.path.basename(r['video_path'])}")
        if r.get("md_path"):    print(f"   📄 {os.path.basename(r['md_path'])}")
    print(f"\n🗄  DB: {db_path}")
    print("=" * 60)
    db.close()


if __name__ == "__main__":
    main()

