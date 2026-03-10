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
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
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
                explanation TEXT DEFAULT '',
                tags        TEXT DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now', 'localtime')),
                FOREIGN KEY (video_id) REFERENCES videos(video_id)
            );

            CREATE TABLE IF NOT EXISTS video_comments (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                video_id    TEXT NOT NULL,
                author      TEXT DEFAULT '',
                text        TEXT NOT NULL,
                likes       INTEGER DEFAULT 0,
                explanation TEXT DEFAULT '',
                created_at  TEXT DEFAULT (datetime('now', 'localtime'))
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
        # 冪等マイグレーション: phrases.tags カラムが存在しない場合に追加
        try:
            cols = [r[1] for r in self.conn.execute("PRAGMA table_info(phrases)").fetchall()]
            if 'tags' not in cols:
                self.conn.execute("ALTER TABLE phrases ADD COLUMN tags TEXT DEFAULT ''")
                self.conn.commit()
        except Exception:
            pass

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
                "INSERT INTO phrases (video_id, phrase_en, phrase_ja, note, is_top, explanation, level) VALUES (?,?,?,?,?,?,?)",
                (video_id, p.get("en", ""), p.get("ja", ""), p.get("note", ""), p.get("is_top", 0), p.get("explanation", ""), p.get("level", ""))
            )
        self.conn.commit()

    def update_phrase_explanation(self, phrase_id: int, explanation_json: str) -> None:
        self.conn.execute("UPDATE phrases SET explanation = ? WHERE id = ?", (explanation_json, phrase_id))
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

    def add_comment_explanation(self, comment_id: int, explanation_json: str) -> None:
        self.conn.execute("UPDATE video_comments SET explanation = ? WHERE id = ?",
                          (explanation_json, comment_id))
        self.conn.commit()

    def close(self):
        self.conn.close()


# ============================================================
# 自動タグ付け
# ============================================================
def auto_tag_phrase(phrase_en: str, note: str, explanation_dict: dict) -> str:
    """フレーズに自動タグを付与してカンマ区切りで返す"""
    tags = []

    # レベルタグ
    level = (explanation_dict.get('level') or '').strip()
    if level in ('初級', '中級', '上級'):
        tags.append(level)

    # 英検1級タグ
    eiken_note = (explanation_dict.get('eiken_note') or '').strip()
    if eiken_note:
        tags.append('英検1級')

    # 句動詞判定
    phrasal_particles = r'\b(up|out|in|off|on|through|over|down|away|back|around|with|into|for|about|after|across)\b'
    words = phrase_en.strip().split()
    if len(words) >= 2 and re.search(phrasal_particles, words[-1].lower()):
        tags.append('句動詞')
    elif len(words) >= 3 and re.search(r'\b(the|a|an)\b', phrase_en.lower()):
        tags.append('イディオム')
    elif len(words) == 1:
        tags.append('単語')
    else:
        if '句動詞' not in tags and 'イディオム' not in tags:
            tags.append('表現')

    # 分野キーワード判定
    combined = (phrase_en + ' ' + note).lower()
    domain_map = [
        ('食・料理', r'\b(eat|food|cook|recipe|meal|dish|restaurant|taste|flavor|cuisine|ingredient)\b'),
        ('ビジネス', r'\b(business|work|office|meeting|deal|profit|client|market|strategy|management)\b'),
        ('旅行', r'\b(travel|trip|journey|hotel|airport|flight|destination|tour|visit|vacation)\b'),
        ('感情', r'\b(feel|emotion|happy|sad|angry|excited|afraid|love|hate|worry|stress)\b'),
        ('日常会話', r'\b(say|talk|speak|chat|tell|ask|reply|conversation|daily|everyday)\b'),
    ]
    for domain, pattern in domain_map:
        if re.search(pattern, combined):
            tags.append(domain)
            break

    return ','.join(dict.fromkeys(tags))  # 重複除去して順序保持


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
def download_subtitles(url: str, lang: str = "en", outdir: str | None = None,
                       model: str = DEFAULT_MODEL, api_key: str = "") -> dict[str, str]:
    """
    字幕をダウンロードし、OpenRouter LLMで整形して .md / .html として保存する
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

    subtitles = {}       # raw SRT → DB保存用（タイムスタンプ付き）
    formatted_subs = {}  # 整形テキスト → LLMプロンプト・MD/HTML保存用
    for f in os.listdir(tmpdir):
        if f.endswith(".srt") or f.endswith(".vtt"):
            fp = os.path.join(tmpdir, f)
            parts = f.rsplit(".", 2)
            lc = parts[-2] if len(parts) >= 3 else "unknown"
            with open(fp, "r", encoding="utf-8", errors="replace") as fh:
                raw = fh.read()
            # .srt を outdir に保存（タイムスタンプ保持）
            srt_filename = f"{safe_title}.{lc}.srt"
            srt_path = os.path.join(outdir, srt_filename)
            with open(srt_path, "w", encoding="utf-8") as sf:
                sf.write(raw)
            clean = srt_to_text(raw)
            if clean.strip():
                # DB には raw SRT（タイムスタンプ付き）を保存
                subtitles[lc] = raw
                # LLMで整形（APIキーがある場合）
                formatted = format_subtitle_with_llm(clean, lc, video_title, model, api_key) if api_key else clean
                formatted_subs[lc] = formatted
                # .md 保存
                md_filename = f"{safe_title}.{lc}.md"
                md_path = os.path.join(outdir, md_filename)
                lang_label = {"en": "🇬🇧 English", "ja": "🇯🇵 日本語"}.get(lc, lc.upper())
                with open(md_path, "w", encoding="utf-8") as mf:
                    mf.write(f"# 📄 {video_title}\n\n")
                    mf.write(f"> {lang_label}　｜　文字数: {len(formatted):,}　｜　{datetime.now().strftime('%Y-%m-%d')}\n\n")
                    mf.write("---\n\n")
                    # 段落ごとに空行を入れて読みやすく
                    for para in formatted.split("\n\n"):
                        para = para.strip()
                        if para:
                            mf.write(para + "\n\n")
                # .html 保存
                html_filename = f"{safe_title}.{lc}.html"
                html_path = os.path.join(outdir, html_filename)
                html_paras = "".join(
                    f"<p>{line}</p>\n" for line in formatted.splitlines() if line.strip()
                )
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
{html_paras}</body>
</html>
""")
                print(f"    ✅ {lc}: {len(formatted)}文字 → {md_filename} / {html_filename}")
    shutil.rmtree(tmpdir, ignore_errors=True)

    if not subtitles:
        print("    ⚠ URL直接取得を試みます...")
        fallback = extract_subs_from_info(info, lang_list)
        subtitles = fallback
        formatted_subs = fallback
    return subtitles, formatted_subs


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


def format_subtitle_with_llm(raw_text: str, lang: str, title: str, model: str, api_key: str) -> str:
    """OpenRouter LLMで字幕テキストを読みやすく整形する"""
    lang_label = "English" if lang.startswith("en") else "Japanese" if lang.startswith("ja") else lang
    prompt = f"""以下はYouTube動画「{title}」の{lang_label}字幕テキストです。
自動生成字幕のため、文の途中で改行されていたり重複があります。

以下のルールで整形してください：
- 文章として自然な段落に整理する
- 重複している行を除去する
- 意味の区切りで段落を分ける（空行を入れる）
- タイムスタンプや番号は除去済みなのでそのまま使う
- 内容は一切変えず、整形のみ行う
- 出力は整形後のテキストのみ（説明文不要）

--- 字幕テキスト ---
{raw_text[:8000]}
"""
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 4000,
        "temperature": 0.1,
    }
    print(f"  ✏️  字幕整形中 (LLM)...")
    try:
        resp = requests.post("https://openrouter.ai/api/v1/chat/completions",
                             headers=headers, json=payload, timeout=120)
        if resp.status_code == 200:
            return resp.json()["choices"][0]["message"]["content"].strip()
        print(f"  ⚠ LLM整形エラー: {resp.status_code} → 元テキストを使用")
    except Exception as e:
        print(f"  ⚠ LLM整形失敗: {e} → 元テキストを使用")
    return raw_text


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
- 英語フレーズ | 和訳 | 難易度(初級/中級/英検準1級/英検1級) | 使用場面・ニュアンスの簡単な説明

難易度の目安：
- 初級: 中学〜高校基礎レベルの日常表現
- 中級: 高校〜大学受験レベル
- 英検準1級: CEFR B2レベル、やや高度なイディオム・表現
- 英検1級: CEFR C1以上、ネイティブ的表現・高度なイディオム・専門的語彙

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
# MD整形ヘルパー
# ============================================================

def _flush_phrase_table(rows: list[tuple]) -> list[str]:
    """フレーズ行リストを Markdown テーブルに変換"""
    lines = [
        "| フレーズ（英語） | 日本語訳 | 説明・ニュアンス |",
        "|:---|:---|:---|",
    ]
    for en, ja, note in rows:
        en   = en.replace("|", "｜").strip()
        ja   = ja.replace("|", "｜").strip()
        note = note.replace("|", "｜").strip()
        lines.append(f"| **{en}** | {ja} | {note} |")
    lines.append("")
    return lines


def _llm_to_nice_md(llm_result: str) -> str:
    """LLM生テキストを見やすい Markdown に整形"""
    TASK_HEADERS = {
        r"タスク1": "## 📝 文脈・シーン概要",
        r"タスク2": "## 📚 フレーズ一覧",
        r"タスク3": "## ⭐ 特に覚えるべきフレーズ",
        r"タスク4": "## 🔄 過去学習との関連",
    }

    result: list[str] = []
    table_rows: list[tuple] = []
    in_table = False

    def flush_table():
        nonlocal in_table, table_rows
        if table_rows:
            result.extend(_flush_phrase_table(table_rows))
        table_rows = []
        in_table = False

    for raw_line in llm_result.split("\n"):
        stripped = raw_line.strip()

        # TOPICS / LINK 行は除外
        if re.match(r"^(TOPICS?|LINK)\s*[:：]", stripped, re.IGNORECASE):
            continue

        # タスクヘッダー変換
        replaced = False
        for pattern, replacement in TASK_HEADERS.items():
            if re.search(pattern, stripped):
                flush_table()
                result.append(replacement)
                replaced = True
                break
        if replaced:
            continue

        # フレーズ行（- EN | JA または - EN | JA | note）
        m = re.match(r"^[-*]\s*(.+?)\s*\|\s*(.+?)(?:\s*\|\s*(.+))?$", stripped)
        if m and "|" in stripped:
            table_rows.append((m.group(1), m.group(2), m.group(3) or ""))
            in_table = True
            continue

        # テーブルモード中に非テーブル行 → フラッシュ
        if in_table:
            flush_table()

        result.append(raw_line)

    flush_table()

    # 連続空行を 1 行に圧縮
    final: list[str] = []
    prev_blank = False
    for line in result:
        is_blank = not line.strip()
        if is_blank and prev_blank:
            continue
        final.append(line)
        prev_blank = is_blank

    return "\n".join(final)


# ============================================================
# MD保存（リッチ版）
# ============================================================
def save_rich_md(llm_result: str, title: str, url: str, video_info: dict,
                 related_videos: list[dict], phrase_links: list[dict],
                 topics: list[str], outdir: str, model: str) -> str:
    os.makedirs(outdir, exist_ok=True)
    safe_name = make_safe_filename(title)[:50]
    filepath = os.path.join(outdir, f"{safe_name}_phrases.md")

    channel  = video_info.get("channel", "")
    duration = video_info.get("duration", 0)
    dur_str  = f"{duration // 60}分{duration % 60}秒" if duration else "不明"
    now_str  = datetime.now().strftime("%Y-%m-%d %H:%M")

    type_labels = {
        "synonym": "類義語", "antonym": "対義語", "root": "同語根",
        "broader": "上位概念", "narrower": "下位概念",
        "collocate": "共起", "variant": "変形",
    }

    with open(filepath, "w", encoding="utf-8") as f:
        # ── ヘッダー ──────────────────────────────────────
        f.write(f"# 🎬 {title}\n\n")

        meta_parts = []
        if channel:  meta_parts.append(f"📺 {channel}")
        meta_parts.append(f"⏱ {dur_str}")
        meta_parts.append(f"📅 {now_str}")
        f.write("> " + "　｜　".join(meta_parts) + "\n\n")
        f.write(f"🔗 [動画を開く]({url})　　🤖 `{model}`\n\n")
        f.write("---\n\n")

        # ── トピック ─────────────────────────────────────
        if topics:
            tags = "　".join(f"`{t}`" for t in topics)
            f.write(f"**🏷️ トピック**: {tags}\n\n")
            f.write("---\n\n")

        # ── 関連する過去動画 ──────────────────────────────
        if related_videos:
            f.write("## 🔗 関連する過去の学習動画\n\n")
            for rv in related_videos:
                date_str = f" _(学習日: {rv.get('date','')})" if rv.get("date") else ""
                f.write(f"- [{rv['title']}]({rv['url']}){date_str}\n")
            f.write("\n---\n\n")

        # ── LLM本文（整形済み）────────────────────────────
        f.write(_llm_to_nice_md(llm_result))
        f.write("\n\n")

        # ── フレーズ関連マップ ────────────────────────────
        if phrase_links:
            f.write("---\n\n## 🕸️ フレーズ関連マップ\n\n")
            by_type: dict[str, list] = {}
            for lk in phrase_links:
                by_type.setdefault(lk.get("type", "synonym"), []).append(lk)
            for lt, items in by_type.items():
                label = type_labels.get(lt, lt)
                f.write(f"### {label}\n\n")
                for lk in items:
                    note = f"　_({lk['note']})_" if lk.get("note") else ""
                    f.write(f"- **{lk['source']}** → **{lk['phrase']}**{note}\n")
                f.write("\n")

        # ── フッター ──────────────────────────────────────
        f.write(f"---\n\n_🤖 自動生成: {model}　｜　{now_str}_\n")

    return filepath


# ============================================================
# 英検1級レベル解析（文ごとの英和解説 + 語彙抽出）
# ============================================================
def build_eiken_prompt(text: str, title: str) -> str:
    """英検1級レベルで文ごとの解説 + 語彙抽出プロンプトを生成"""
    # 長すぎる場合は先頭3000文字に絞る
    truncated = text[:3000] if len(text) > 3000 else text

    return f"""以下はYouTube動画または英語学習記事「{title}」のテキストです。
英検1級レベルの英語学習者向けに、以下の2つのタスクを実行してください。

---テキスト---
{truncated}
---

## タスク1: 文ごとの英和解説（最大20文）
テキストから英文を1文ずつ取り出し、以下の形式で出力してください。
重複文・短すぎる断片は除外してください。

[SENTENCE]
EN: （英文をそのまま1文）
JA: （自然な日本語訳）
EXP: （英検1級レベルの解説：文法構造・語法・表現のニュアンス・イディオムの説明など）
[/SENTENCE]

## タスク2: 英検1級レベル語彙リスト
テキストに登場する英検1級レベルの語彙・イディオム・コロケーションを10〜20個抽出し、以下の形式で出力してください。

[VOCAB]
WORD: （単語またはフレーズ）
POS: （品詞：名詞/動詞/形容詞/副詞/イディオム/コロケーションのいずれか）
MEANING: （日本語の意味・訳語）
EXAMPLE: （テキスト内の用例またはわかりやすい別例文）
NOTE: （語源・ニュアンス・使用上の注意・類義語など）
[/VOCAB]

出力は必ず上記の[SENTENCE]〜[/SENTENCE]と[VOCAB]〜[/VOCAB]形式を守ってください。
日本語で出力してください（ENとEXAMPLE行の英文部分を除く）。
"""


def parse_eiken_result(eiken_text: str) -> dict:
    """LLM出力から文解説リストと語彙リストをパース"""
    sentences = []
    vocab_list = []

    # [SENTENCE] ブロックを抽出
    sent_blocks = re.findall(r"\[SENTENCE\](.*?)\[/SENTENCE\]", eiken_text, re.DOTALL)
    for block in sent_blocks:
        entry = {}
        m_en = re.search(r"^EN:\s*(.+)", block, re.MULTILINE)
        m_ja = re.search(r"^JA:\s*(.+)", block, re.MULTILINE)
        m_exp = re.search(r"^EXP:\s*(.+(?:\n(?!(?:EN:|JA:|EXP:|\[)).+)*)", block, re.MULTILINE)
        if m_en:
            entry["en"] = m_en.group(1).strip()
        if m_ja:
            entry["ja"] = m_ja.group(1).strip()
        if m_exp:
            entry["exp"] = m_exp.group(1).strip().replace("\n", " ")
        if entry.get("en"):
            sentences.append(entry)

    # [VOCAB] ブロックを抽出
    vocab_blocks = re.findall(r"\[VOCAB\](.*?)\[/VOCAB\]", eiken_text, re.DOTALL)
    for block in vocab_blocks:
        entry = {}
        m_word = re.search(r"^WORD:\s*(.+)", block, re.MULTILINE)
        m_pos = re.search(r"^POS:\s*(.+)", block, re.MULTILINE)
        m_meaning = re.search(r"^MEANING:\s*(.+)", block, re.MULTILINE)
        m_example = re.search(r"^EXAMPLE:\s*(.+)", block, re.MULTILINE)
        m_note = re.search(r"^NOTE:\s*(.+(?:\n(?!(?:WORD:|POS:|MEANING:|EXAMPLE:|NOTE:|\[)).+)*)", block, re.MULTILINE)
        if m_word:
            entry["word"] = m_word.group(1).strip()
        if m_pos:
            entry["pos"] = m_pos.group(1).strip()
        if m_meaning:
            entry["meaning"] = m_meaning.group(1).strip()
        if m_example:
            entry["example"] = m_example.group(1).strip()
        if m_note:
            entry["note"] = m_note.group(1).strip().replace("\n", " ")
        if entry.get("word"):
            vocab_list.append(entry)

    return {"sentences": sentences, "vocab": vocab_list}


def save_eiken_html(eiken_result: dict, title: str, url: str, outdir: str, model: str) -> str:
    """英検1級解析結果を美しいHTMLで保存"""
    os.makedirs(outdir, exist_ok=True)
    safe_name = make_safe_filename(title)[:50]
    filepath = os.path.join(outdir, f"{safe_name}_eiken1.html")

    sentences = eiken_result.get("sentences", [])
    vocab_list = eiken_result.get("vocab", [])

    # 文解説カードのHTML生成
    sentence_cards_html = ""
    for i, s in enumerate(sentences, 1):
        en = s.get("en", "").replace("<", "&lt;").replace(">", "&gt;")
        ja = s.get("ja", "").replace("<", "&lt;").replace(">", "&gt;")
        exp = s.get("exp", "").replace("<", "&lt;").replace(">", "&gt;")
        sentence_cards_html += f"""
        <div class="sentence-card">
          <div class="sentence-num">#{i}</div>
          <div class="sentence-en">{en}</div>
          {"<div class='sentence-ja'><span class='label-ja'>和訳</span>" + ja + "</div>" if ja else ""}
          {"<div class='sentence-exp'><span class='label-exp'>解説</span>" + exp + "</div>" if exp else ""}
        </div>"""

    # 語彙カードのHTML生成
    pos_colors = {
        "名詞": ("#1565c0", "#e3f2fd"),
        "動詞": ("#2e7d32", "#e8f5e9"),
        "形容詞": ("#6a1b9a", "#f3e5f5"),
        "副詞": ("#00695c", "#e0f2f1"),
        "イディオム": ("#bf360c", "#fbe9e7"),
        "コロケーション": ("#e65100", "#fff3e0"),
    }
    default_pos_colors = ("#37474f", "#eceff1")

    vocab_cards_html = ""
    for v in vocab_list:
        word = v.get("word", "").replace("<", "&lt;").replace(">", "&gt;")
        pos = v.get("pos", "")
        meaning = v.get("meaning", "").replace("<", "&lt;").replace(">", "&gt;")
        example = v.get("example", "").replace("<", "&lt;").replace(">", "&gt;")
        note = v.get("note", "").replace("<", "&lt;").replace(">", "&gt;")
        fc, bc = pos_colors.get(pos, default_pos_colors)
        vocab_cards_html += f"""
        <div class="vocab-card" style="border-top-color:{fc};">
          <div class="vocab-word">{word}</div>
          {"<span class='vocab-pos' style='color:" + fc + ";background:" + bc + ";'>" + pos + "</span>" if pos else ""}
          {"<div class='vocab-meaning'>" + meaning + "</div>" if meaning else ""}
          {"<div class='vocab-example'>例: " + example + "</div>" if example else ""}
          {"<div class='vocab-note'>&#9654; " + note + "</div>" if note else ""}
        </div>"""

    now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
    url_display = url.replace("<", "&lt;").replace(">", "&gt;")
    title_display = title.replace("<", "&lt;").replace(">", "&gt;")

    html = f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>英検1級解析: {title_display}</title>
<style>
  :root {{
    --blue: #1a3a6b;
    --light-blue: #e8f0fe;
    --green: #1b5e20;
    --light-green: #e8f5e9;
    --orange: #e65100;
    --light-orange: #fff3e0;
    --gray: #546e7a;
    --light-gray: #f5f7fa;
    --accent: #c62828;
    --card-shadow: 0 2px 8px rgba(0,0,0,0.09);
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: 'Segoe UI', 'Noto Sans JP', 'Hiragino Kaku Gothic ProN', sans-serif;
    max-width: 960px;
    margin: 0 auto;
    padding: 24px 20px 60px;
    background: #f0f4f8;
    color: #263238;
    line-height: 1.7;
  }}
  .page-header {{
    background: linear-gradient(135deg, var(--blue) 0%, #2962ff 100%);
    color: white;
    padding: 28px 32px;
    border-radius: 12px;
    margin-bottom: 24px;
    box-shadow: 0 4px 16px rgba(26,58,107,0.25);
  }}
  .page-header h1 {{
    font-size: 1.45em;
    font-weight: 700;
    margin-bottom: 10px;
    line-height: 1.4;
  }}
  .meta-info {{
    font-size: 0.82em;
    opacity: 0.88;
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
  }}
  .meta-info a {{ color: #90caf9; }}
  .section-header {{
    display: flex;
    align-items: center;
    gap: 10px;
    margin: 32px 0 16px;
  }}
  .section-header h2 {{
    font-size: 1.2em;
    color: var(--blue);
    font-weight: 700;
  }}
  .section-header .count-badge {{
    background: var(--light-blue);
    color: var(--blue);
    font-size: 0.78em;
    font-weight: 600;
    padding: 3px 10px;
    border-radius: 20px;
  }}
  .section-divider {{
    height: 3px;
    background: linear-gradient(90deg, var(--blue), transparent);
    border-radius: 2px;
    margin-bottom: 20px;
  }}
  /* 文解説カード */
  .sentence-card {{
    background: white;
    border-radius: 10px;
    box-shadow: var(--card-shadow);
    margin-bottom: 14px;
    padding: 16px 20px;
    border-left: 5px solid var(--blue);
    transition: box-shadow .2s;
  }}
  .sentence-card:hover {{ box-shadow: 0 4px 16px rgba(0,0,0,0.13); }}
  .sentence-num {{
    font-size: 0.72em;
    font-weight: 700;
    color: var(--blue);
    opacity: 0.5;
    margin-bottom: 4px;
    letter-spacing: .05em;
  }}
  .sentence-en {{
    font-size: 1.08em;
    color: #1a237e;
    font-weight: 600;
    line-height: 1.65;
    margin-bottom: 9px;
  }}
  .sentence-ja {{
    font-size: 0.93em;
    color: var(--gray);
    background: var(--light-gray);
    padding: 7px 12px;
    border-radius: 5px;
    margin-bottom: 9px;
  }}
  .sentence-exp {{
    font-size: 0.86em;
    color: var(--green);
    background: var(--light-green);
    padding: 9px 13px;
    border-radius: 5px;
    border-left: 3px solid #4caf50;
    line-height: 1.65;
  }}
  .label-ja, .label-exp {{
    display: inline-block;
    font-size: 0.72em;
    font-weight: 700;
    padding: 1px 6px;
    border-radius: 3px;
    margin-right: 6px;
    vertical-align: middle;
  }}
  .label-ja {{ background: #b0bec5; color: white; }}
  .label-exp {{ background: #66bb6a; color: white; }}
  /* 語彙カード */
  .vocab-section {{ margin-top: 40px; }}
  .vocab-grid {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(270px, 1fr));
    gap: 13px;
  }}
  .vocab-card {{
    background: white;
    border-radius: 10px;
    box-shadow: var(--card-shadow);
    padding: 15px 17px;
    border-top: 4px solid var(--orange);
    transition: transform .15s, box-shadow .2s;
  }}
  .vocab-card:hover {{ transform: translateY(-2px); box-shadow: 0 6px 18px rgba(0,0,0,0.12); }}
  .vocab-word {{
    font-size: 1.18em;
    font-weight: 700;
    color: var(--orange);
    margin-bottom: 5px;
    letter-spacing: .02em;
  }}
  .vocab-pos {{
    display: inline-block;
    font-size: 0.73em;
    font-weight: 600;
    padding: 2px 9px;
    border-radius: 12px;
    margin-bottom: 8px;
  }}
  .vocab-meaning {{
    font-size: 0.92em;
    color: #263238;
    font-weight: 500;
    margin: 5px 0 7px;
  }}
  .vocab-example {{
    font-size: 0.82em;
    color: var(--gray);
    font-style: italic;
    background: var(--light-gray);
    padding: 6px 9px;
    border-radius: 4px;
    margin-top: 5px;
    line-height: 1.5;
  }}
  .vocab-note {{
    font-size: 0.79em;
    color: var(--accent);
    margin-top: 7px;
    line-height: 1.5;
  }}
  .empty-msg {{
    text-align: center;
    color: var(--gray);
    font-size: 0.9em;
    padding: 30px;
    background: white;
    border-radius: 8px;
  }}
</style>
</head>
<body>
<div class="page-header">
  <h1>&#127891; 英検1級解析: {title_display}</h1>
  <div class="meta-info">
    <span>&#128279; <a href="{url_display}" target="_blank">{url_display[:70]}{"..." if len(url_display) > 70 else ""}</a></span>
    <span>&#128197; {now_str}</span>
    <span>&#129302; {model}</span>
  </div>
</div>

<div class="section-header">
  <h2>&#128214; 文ごとの英和解説</h2>
  <span class="count-badge">{len(sentences)}文</span>
</div>
<div class="section-divider"></div>
{"".join([sentence_cards_html]) if sentences else '<div class="empty-msg">解析データがありません</div>'}

<div class="vocab-section">
  <div class="section-header">
    <h2>&#128218; 英検1級レベル語彙</h2>
    <span class="count-badge">{len(vocab_list)}語</span>
  </div>
  <div class="section-divider"></div>
  {"<div class='vocab-grid'>" + vocab_cards_html + "</div>" if vocab_list else '<div class="empty-msg">語彙データがありません</div>'}
</div>

</body>
</html>
"""

    with open(filepath, "w", encoding="utf-8") as f:
        f.write(html)
    return filepath


def save_summary(all_results: list[dict], outdir: str) -> str:
    os.makedirs(outdir, exist_ok=True)
    ts       = datetime.now().strftime("%Y%m%d_%H%M%S")
    now_str  = datetime.now().strftime("%Y-%m-%d %H:%M")
    filepath = os.path.join(outdir, f"phrases_summary_{ts}.md")

    with open(filepath, "w", encoding="utf-8") as f:
        # ── ヘッダー ──────────────────────────────────────
        f.write("# 📊 英語学習サマリー\n\n")
        f.write(f"> 📅 生成日時: **{now_str}**　｜　🎬 処理動画数: **{len(all_results)} 本**\n\n")
        f.write("---\n\n")

        # ── 動画インデックス ──────────────────────────────
        if len(all_results) > 1:
            f.write("## 📋 動画一覧\n\n")
            for i, item in enumerate(all_results, 1):
                f.write(f"{i}. [{item['title']}]({item['url']})\n")
            f.write("\n---\n\n")

        # ── 各動画のフレーズ ──────────────────────────────
        for i, item in enumerate(all_results, 1):
            f.write(f"## {i}. {item['title']}\n\n")
            f.write(f"🔗 [{item['url']}]({item['url']})\n\n")

            if item.get("result"):
                f.write(_llm_to_nice_md(item["result"]))

            f.write("\n\n---\n\n")

        f.write(f"_🤖 自動生成: {now_str}_\n")

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

        # 箇条書き（- / * / 1. / 2. など）+「|」区切り形式にマッチ
        m = re.match(r"^(?:[-*]|\d+\.)\s*(.+?)\s*\|\s*(.+?)(?:\s*\|\s*(.+?))?(?:\s*\|\s*(.+))?$", line)
        if m:
            en_raw = m.group(1).strip()
            # 「使用例:」「例文:」などのラベル行はスキップ
            if re.search(r"使用例|例文|例:|example", en_raw, re.IGNORECASE):
                continue
            # **ラベル**: や *ラベル*: の形式のプレフィックスを除去
            en_clean = re.sub(r'^\*{1,2}[^*]+\*{1,2}\s*[:：]\s*', '', en_raw).strip()
            # **phrase** の前後 ** を除去（太字フレーズそのもの）
            en_clean = re.sub(r'^\*{1,2}(.+?)\*{1,2}$', r'\1', en_clean).strip()
            if not en_clean:
                continue
            # 難易度フィールドを検出（3番目または4番目の|区切り）
            g3 = (m.group(3) or "").strip()
            g4 = (m.group(4) or "").strip()
            level_map = {"初級": "初級", "中級": "中級", "英検準1級": "英検準1級", "英検1級": "英検1級"}
            level = ""
            note = ""
            if g3 in level_map:
                level = g3
                note = g4
            elif g4 in level_map:
                level = g4
                note = g3
            else:
                note = g3  # 旧フォーマット互換
            phrases.append({
                "en": en_clean,
                "ja": m.group(2).strip(),
                "note": note,
                "level": level,
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

        # 英検1級HTML生成
        if article["body"] and api_key:
            print(f"  📝 英検1級解析中 (LLM)...")
            eiken_prompt = build_eiken_prompt(article["body"], title)
            eiken_raw = call_openrouter(eiken_prompt, model, api_key)
            if eiken_raw:
                eiken_result = parse_eiken_result(eiken_raw)
                eiken_path = save_eiken_html(eiken_result, title, url, outdir, model)
                print(f"  🎓 英検1級HTML: {eiken_path}")
    else:
        print(f"  ✗ LLM分析失敗")

    if not result_data["result"]:
        return None
    return result_data


# ============================================================
# フレーズ解説プリフェッチ
# ============================================================
def _explain_phrase_openrouter(phrase_en: str, note: str, model: str, api_key: str) -> dict:
    """OpenRouter で1フレーズの解説を取得して dict で返す"""
    import requests as _req
    prompt = f"""英検1級・上級英語学習者向けに、以下の英語フレーズを解説してください。
言語学的知見（語源学、認知言語学、音象徴など）を活用して、深く・記憶に残る解説を作成してください。

フレーズ: {phrase_en}
{f'文脈メモ: {note}' if note else ''}

以下のJSON形式のみで返答してください（他のテキスト不要）:
{{
  "meaning": "意味・説明（日本語・詳しく）",
  "usage": "使い方・ニュアンス・語法（日本語）",
  "example": "英検1級レベルの例文（英語）",
  "example_ja": "例文の日本語訳",
  "etymology": "語源・成り立ち（ラテン語/ギリシャ語/古英語の語根・接頭辞・接尾辞の分析。語根が他の単語にも現れる場合は例示）",
  "linguistics_note": "言語学的考察（音象徴・認知言語学的メタファー・形態素分析・イメージスキーマなど、記憶の助けになる観点）",
  "story": "覚え方ストーリー（日本語・情景が浮かぶ具体的なエピソード。語源や音のイメージと結びつけると効果的）",
  "mnemonic": "ゴロ合わせや記憶術（日本語・音・形・意味を結びつける）",
  "related": ["英検1級・TOEFL・GRE レベルの類語・対義語・派生語 最大4つ（基本語は避け、高度な語彙のみ）"],
  "eiken_note": "英検1級での出題傾向・注意点（あれば）",
  "level": "初級/中級/上級"
}}"""
    try:
        resp = _req.post(
            'https://openrouter.ai/api/v1/chat/completions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={'model': model, 'messages': [{'role': 'user', 'content': prompt}],
                  'response_format': {'type': 'json_object'}, 'temperature': 0.3},
            timeout=30,
        )
        resp.raise_for_status()
        content = resp.json()['choices'][0]['message']['content']
        return json.loads(content)
    except Exception:
        return {}


def prefetch_phrase_explanations(db: PhraseDB, video_id: str, phrases: list[dict],
                                  model: str, api_key: str) -> None:
    """フレーズ一覧の解説をOpenRouterで一括取得してDBに保存"""
    rows = db.conn.execute(
        "SELECT id, phrase_en, note FROM phrases WHERE video_id = ? ORDER BY id",
        (video_id,)
    ).fetchall()
    total = len(rows)
    print(f"  🤖 フレーズ解説プリフェッチ: {total}件")
    for i, row in enumerate(rows, 1):
        pid, pen, note = row["id"], row["phrase_en"], row["note"] or ""
        # markdown記法除去
        clean_en = pen.replace("**", "").replace("*", "").replace("`", "").strip()
        if not clean_en:
            continue
        print(f"    [{i}/{total}] {clean_en[:40]}", end="", flush=True)
        expl = _explain_phrase_openrouter(clean_en, note, model, api_key)
        if expl:
            tags = auto_tag_phrase(clean_en, note, expl)
            db.conn.execute(
                "UPDATE phrases SET explanation=?, tags=? WHERE id=?",
                (json.dumps(expl, ensure_ascii=False), tags, pid)
            )
            db.conn.commit()
            print(f" ✓")
        else:
            print(f" ✗")


# ============================================================
# YouTubeコメント取得・LLMフィルタリング
# ============================================================
def fetch_video_comments(url: str, video_id: str, model: str, api_key: str, db: 'PhraseDB') -> int:
    """yt-dlpでコメント取得→LLMで英検1級向けフィルタリング→DB保存"""
    print("  💬 コメント取得中...")
    try:
        import yt_dlp as ydl_mod
        opts = {
            'quiet': True, 'no_warnings': True,
            'getcomments': True,
            'extractor_args': {'youtube': {'comment_sort': ['top'], 'max_comments': ['30,5']}},
            'skip_download': True,
        }
        with ydl_mod.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
        comments = info.get('comments') or []
    except Exception as e:
        print(f"  ⚠ コメント取得失敗: {e}")
        return 0

    if not comments:
        print("  ⚠ コメントなし")
        return 0

    # いいね数でソート上位30件
    comments_sorted = sorted(comments, key=lambda c: c.get('like_count') or 0, reverse=True)[:30]
    comment_texts = '\n'.join(
        f"[{i+1}] ({c.get('like_count',0)}likes) {(c.get('text','')[:200])}"
        for i, c in enumerate(comments_sorted)
    )

    # LLMで英検1級向けフィルタリング
    selected_indices = set(range(len(comments_sorted)))  # フォールバック: 全部
    if api_key:
        prompt = f"""以下のYouTubeコメントから英検1級レベルの英語学習に役立つものを選んでください。
選定基準: 高度な英語表現・イディオム・語彙・文法が含まれ、学習者に有益なもの。日本語のみ・単純すぎる内容は除外。

{comment_texts}

JSON形式のみで返答: {{"selected": [1,3,5,...]}} (選んだ番号のリスト)"""
        try:
            import requests as _req
            resp = _req.post(
                'https://openrouter.ai/api/v1/chat/completions',
                headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
                json={
                    'model': model,
                    'messages': [{'role': 'user', 'content': prompt}],
                    'response_format': {'type': 'json_object'},
                    'temperature': 0.3,
                },
                timeout=30,
            )
            if resp.status_code == 200:
                content = resp.json()['choices'][0]['message']['content']
                data = json.loads(content)
                nums = data.get('selected', [])
                if nums:
                    selected_indices = {n-1 for n in nums if 1 <= n <= len(comments_sorted)}
        except Exception:
            pass

    # DB保存
    db.conn.execute("DELETE FROM video_comments WHERE video_id = ?", (video_id,))
    count = 0
    for i in sorted(selected_indices):
        c = comments_sorted[i]
        text = (c.get('text') or '').strip()
        if not text:
            continue
        db.conn.execute(
            "INSERT INTO video_comments (video_id, author, text, likes) VALUES (?,?,?,?)",
            (video_id, c.get('author', ''), text[:500], c.get('like_count', 0) or 0)
        )
        count += 1
    db.conn.commit()
    print(f"  💬 コメント {count}件保存")
    return count


# ============================================================
# 単一動画処理
# ============================================================
def process_single_video(url: str, video_info: dict, mode: str, model: str,
                         api_key: str, lang: str, outdir: str, top_n: int,
                         db: PhraseDB, auto_phrases: bool = False) -> dict | None:
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
        # subtitles = raw SRT（DB保存用）, formatted_subs = 整形テキスト（LLM用）
        subtitles, formatted_subs = download_subtitles(url, lang=lang, outdir=outdir, model=model, api_key=api_key)

        if subtitles:
            db.upsert_video(video_id,
                            url=url, title=title,
                            channel=video_info.get("channel", ""),
                            duration=video_info.get("duration", 0),
                            description=video_info.get("description", ""),
                            subtitle_en=subtitles.get("en", ""),
                            subtitle_ja=subtitles.get("ja", ""))

            sub_text = formatted_subs.get("en", "") + " " + formatted_subs.get("ja", "")
            related = db.find_related_videos(sub_text, video_id)
            past_top = db.get_past_top_phrases()

            if related:
                print(f"  🔗 関連する過去動画: {len(related)}件")

            prompt = build_prompt(formatted_subs, title, url, video_info, top_n, related, past_top)
            llm_result = call_openrouter(prompt, model, api_key)

            if llm_result:
                result_data["result"] = llm_result
                db.upsert_video(video_id, llm_result=llm_result, llm_model=model)

                phrases = parse_phrases_from_llm(llm_result)
                if phrases:
                    if auto_phrases:
                        db.add_phrases(video_id, phrases)
                        print(f"  📚 {len(phrases)}フレーズをDBに登録")
                        if api_key:
                            prefetch_phrase_explanations(db, video_id, phrases, model, api_key)
                    else:
                        print(f"  📚 {len(phrases)}フレーズを抽出（自動登録OFF）")

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

                # 英検1級HTML生成
                en_text = subtitles.get("en", "")
                if en_text and api_key:
                    print(f"  📝 英検1級解析中 (LLM)...")
                    eiken_prompt = build_eiken_prompt(en_text, title)
                    eiken_raw = call_openrouter(eiken_prompt, model, api_key)
                    if eiken_raw:
                        eiken_result = parse_eiken_result(eiken_raw)
                        eiken_path = save_eiken_html(eiken_result, title, url, outdir, model)
                        print(f"  🎓 英検1級HTML: {eiken_path}")

                # コメント取得
                if video_id and api_key:
                    fetch_video_comments(url, video_id, model, api_key, db)
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
    parser.add_argument("--outdir", default=os.path.dirname(os.path.abspath(__file__)),
                        help="出力先ルートディレクトリ（この下にYYYYMMDDフォルダが作成される）")
    parser.add_argument("--top", type=int, default=3)
    parser.add_argument("--no-playlist", action="store_true")
    parser.add_argument("--no-auto-phrases", action="store_true", help="フレーズ自動登録を無効化")
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
    _print_lock = threading.Lock()

    def _process_video(idx_v):
        idx, v = idx_v
        with _print_lock:
            print(f"\n[{idx}/{total}] 🎬 {v.get('title','')[:50]}")
        # スレッドごとに独立したDB接続を使用
        _db = PhraseDB(db_path)
        try:
            result = process_single_video(
                url=v["url"], video_info=v, mode=args.mode, model=args.model,
                api_key=api_key, lang=args.lang,
                outdir=dated_outdir,
                top_n=args.top, db=_db,
                auto_phrases=not args.no_auto_phrases,
            )
        finally:
            _db.close()
        return result

    def _process_web(idx_u):
        idx, w_url = idx_u
        with _print_lock:
            print(f"\n[{idx}/{total}] 🌐 {w_url[:60]}")
        _db = PhraseDB(db_path)
        try:
            result = process_web_article(
                url=w_url, model=args.model, api_key=api_key,
                outdir=dated_outdir,
                top_n=args.top, db=_db,
            )
        finally:
            _db.close()
        return result

    # 並列実行（YouTube動画とWeb記事を同時処理）
    max_workers = min(4, total) if total > 1 else 1
    print(f"\n⚡ 並列処理: {max_workers}スレッド")

    futures_map = {}
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for i, v in enumerate(videos, 1):
            f = executor.submit(_process_video, (i, v))
            futures_map[f] = f"video:{v.get('url','')}"
        for j, w_url in enumerate(web_urls, len(videos) + 1):
            f = executor.submit(_process_web, (j, w_url))
            futures_map[f] = f"web:{w_url}"

        for future in as_completed(futures_map):
            try:
                result = future.result()
                if result:
                    all_results.append(result)
            except Exception as e:
                with _print_lock:
                    print(f"\n  ✗ エラー: {e}")

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
    # 英検1級HTMLファイルも一覧表示
    import glob as _glob
    for eiken_f in _glob.glob(os.path.join(dated_outdir, "*_eiken1.html")):
        print(f"   🎓 {os.path.basename(eiken_f)}")
    print(f"\n🗄  DB: {db_path}")
    print("=" * 60)
    db.close()


if __name__ == "__main__":
    main()

