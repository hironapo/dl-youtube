#!/usr/bin/env python3
"""
YouTube学習管理UI - Flask バックエンド
出力ディレクトリ: このスクリプトと同じフォルダ（YYYYMMDD/ サブフォルダ）
DB: ~/youtube_phrases.db
"""

import os
import re
import json
import queue
import sqlite3
import subprocess
import threading
import uuid
import requests as req_lib
from contextlib import contextmanager
from pathlib import Path
from flask import Flask, render_template, jsonify, request, send_file, abort, Response, stream_with_context
from dotenv import load_dotenv

# .env ファイルがあれば読み込む（OPENROUTER_API_KEY など）
load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), '.env'))

# ─── HTTP セッション（接続プール・リトライ付き）────────────────────
_http_session = req_lib.Session()
_http_adapter = req_lib.adapters.HTTPAdapter(pool_connections=4, pool_maxsize=8, max_retries=2)
_http_session.mount('https://', _http_adapter)
_http_session.mount('http://', _http_adapter)

app = Flask(__name__)

# このスクリプトと同じフォルダを出力ルートとする
OUTDIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.expanduser("~/youtube_phrases.db")
SCRIPT  = os.path.join(OUTDIR, 'dl_youtube_sub_llm.py')

# ─── ダウンロードジョブ管理 ───────────────────────────────────
# jobs[job_id] = {'status': 'running'|'done'|'error', 'log': [...], 'q': Queue}
_jobs: dict[str, dict] = {}
_jobs_lock = threading.Lock()

# ─── コメント取得ジョブ管理 ──────────────────────────────────
_comment_jobs: dict[str, dict] = {}
_comment_jobs_lock = threading.Lock()


# ─────────────────────────────────────────────
# DB ユーティリティ
# ─────────────────────────────────────────────

def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")   # 読み書き並行を許可
    conn.execute("PRAGMA synchronous=NORMAL") # WAL時の推奨設定
    conn.execute("PRAGMA busy_timeout=10000") # 10秒待ってからエラー
    return conn


@contextmanager
def db_conn():
    """SQLite接続をコンテキストマネージャで管理（必ずcloseされる）"""
    conn = get_db()
    try:
        yield conn
    finally:
        conn.close()


def db_exists():
    return os.path.exists(DB_PATH)


def migrate_db():
    """既存DBに新カラムを追加（冪等）"""
    if not db_exists():
        return
    try:
        with db_conn() as conn:
            # explanation カラムがなければ追加
            cols = [r[1] for r in conn.execute("PRAGMA table_info(phrases)").fetchall()]
            if 'explanation' not in cols:
                conn.execute("ALTER TABLE phrases ADD COLUMN explanation TEXT DEFAULT ''")
                conn.commit()
                print("DB migrated: phrases.explanation added")
            # tags カラムがなければ追加
            if 'tags' not in cols:
                conn.execute("ALTER TABLE phrases ADD COLUMN tags TEXT DEFAULT ''")
                conn.commit()
                print("DB migrated: phrases.tags added")
            # video_comments テーブル作成
            conn.execute("""
                CREATE TABLE IF NOT EXISTS video_comments (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    video_id TEXT NOT NULL,
                    author TEXT DEFAULT '',
                    text TEXT NOT NULL,
                    likes INTEGER DEFAULT 0,
                    explanation TEXT DEFAULT '',
                    created_at TEXT DEFAULT (datetime('now', 'localtime'))
                )
            """)
            conn.commit()
    except Exception as e:
        print(f"DB migration warning: {e}")


migrate_db()


# ─────────────────────────────────────────────
# SRT パーサ
# ─────────────────────────────────────────────

def parse_srt(content: str) -> list[dict]:
    entries = []
    blocks = re.split(r'\n\n+', content.strip())
    for block in blocks:
        lines = block.strip().split('\n')
        if len(lines) < 3:
            continue
        try:
            int(lines[0].strip())  # index (discard)
            m = re.match(
                r'(\d{2}):(\d{2}):(\d{2})[,.](\d{3})\s*-->\s*(\d{2}):(\d{2}):(\d{2})[,.](\d{3})',
                lines[1].strip()
            )
            if not m:
                continue
            g = m.groups()
            start = int(g[0]) * 3600 + int(g[1]) * 60 + int(g[2]) + int(g[3]) / 1000
            end   = int(g[4]) * 3600 + int(g[5]) * 60 + int(g[6]) + int(g[7]) / 1000
            text = ' '.join(lines[2:]).strip()
            text = re.sub(r'<[^>]+>', '', text)
            if text:
                entries.append({'start': start, 'end': end, 'text': text})
        except (ValueError, IndexError):
            continue
    return entries


# ─────────────────────────────────────────────
# ルート
# ─────────────────────────────────────────────

@app.route('/')
def index():
    return render_template('index.html')


@app.route('/api/dates')
def api_dates():
    """YYYYMMDD フォルダ一覧（フォルダ＋DB両方を参照）"""
    date_counts: dict[str, int] = {}

    # フォルダから mp4 をカウント
    for item in Path(OUTDIR).iterdir():
        if item.is_dir() and re.match(r'^\d{8}$', item.name):
            cnt = len(list(item.glob('*.mp4')))
            date_counts[item.name] = date_counts.get(item.name, 0) + cnt

    # DB から created_at 別に動画数を補完（フォルダにmp4がない場合も表示）
    if db_exists():
        try:
            with db_conn() as conn:
                rows = conn.execute(
                    "SELECT REPLACE(SUBSTR(created_at,1,10),'-','') AS d, COUNT(*) AS c "
                    "FROM videos GROUP BY d"
                ).fetchall()
            for row in rows:
                d = row['d']
                if re.match(r'^\d{8}$', d):
                    date_counts[d] = max(date_counts.get(d, 0), row['c'])
        except Exception:
            pass

    dates = sorted(date_counts.keys(), reverse=True)
    return jsonify([{
        'date': d,
        'display': f"{d[:4]}/{d[4:6]}/{d[6:8]}",
        'count': date_counts[d],
    } for d in dates if date_counts[d] > 0])


@app.route('/api/dates/<date>/videos')
def api_videos_by_date(date):
    """指定日付の動画一覧（フォルダ＋DB両方を参照）"""
    date_dir = Path(OUTDIR) / date
    seen_stems: set[str] = set()
    videos = []

    # フォルダから mp4 を列挙
    if date_dir.is_dir():
        for mp4 in sorted(date_dir.glob('*.mp4')):
            stem = mp4.stem
            seen_stems.add(stem)
            en_srt  = date_dir / f"{stem}.en.srt"
            ja_srt  = date_dir / f"{stem}.ja.srt"
            md_file = date_dir / f"{stem}_phrases.md"
            title = stem; channel = ''; video_id = None; duration = 0; url = ''
            if db_exists():
                try:
                    with db_conn() as conn:
                        row = conn.execute(
                            "SELECT video_id, title, channel, duration, url FROM videos "
                            "WHERE video_path LIKE ? ORDER BY created_at DESC LIMIT 1",
                            (f'%{stem}%',)
                        ).fetchone()
                        # video_path が一致しない場合はtitle(stem)で照合
                        if not row:
                            row = conn.execute(
                                "SELECT video_id, title, channel, duration, url FROM videos "
                                "WHERE REPLACE(REPLACE(title,' ',''),'_','') LIKE ? ORDER BY created_at DESC LIMIT 1",
                                (f'%{stem[:20]}%',)
                            ).fetchone()
                        if row:
                            video_id = row['video_id']; title = row['title']
                            channel = row['channel']; duration = row['duration']
                            url = row['url'] or ''
                            # video_idもseen_stemsに追加して重複防止
                            if video_id:
                                seen_stems.add(video_id)
                except Exception:
                    pass
            def _srt_path(lang_ext, local_path, vid_id):
                if local_path.exists():
                    return f"{date}/{stem}.{lang_ext}.srt"
                if vid_id:
                    return f"srt/{vid_id}/{lang_ext}"
                return None
            videos.append({
                'stem': stem, 'title': title, 'channel': channel,
                'duration': duration, 'video_id': video_id,
                'url': url,
                'mp4': f"{date}/{stem}.mp4",
                'en_srt': _srt_path('en', en_srt, video_id),
                'ja_srt': _srt_path('ja', ja_srt, video_id),
                'md': f"{date}/{stem}_phrases.md" if md_file.exists() else None,
            })

    # DBから同日付の動画を補完（フォルダにmp4がなくても表示）
    if db_exists():
        try:
            date_str = f"{date[:4]}-{date[4:6]}-{date[6:8]}"
            with db_conn() as conn:
                rows = conn.execute(
                    "SELECT video_id, title, channel, duration, url, video_path "
                    "FROM videos WHERE DATE(created_at) = ? ORDER BY created_at DESC",
                    (date_str,)
                ).fetchall()
            for row in rows:
                vp = row['video_path'] or ''
                stem = Path(vp).stem if vp else row['video_id'] or ''
                if stem in seen_stems:
                    continue
                seen_stems.add(stem)
                # ローカルファイル存在確認
                en_srt = date_dir / f"{stem}.en.srt" if date_dir.is_dir() else None
                ja_srt = date_dir / f"{stem}.ja.srt" if date_dir.is_dir() else None
                mp4_path = date_dir / f"{stem}.mp4"
                vid_id = row['video_id']
                def _srt2(lang, local):
                    if local and local.exists():
                        return f"{date}/{stem}.{lang}.srt"
                    if vid_id:
                        return f"srt/{vid_id}/{lang}"
                    return None
                videos.append({
                    'stem': stem,
                    'title': row['title'] or stem,
                    'channel': row['channel'] or '',
                    'duration': row['duration'] or 0,
                    'video_id': vid_id,
                    'url': row['url'] or '',
                    'mp4': f"{date}/{stem}.mp4" if mp4_path.exists() else None,
                    'en_srt': _srt2('en', en_srt),
                    'ja_srt': _srt2('ja', ja_srt),
                    'md': None,
                    'db_only': not mp4_path.exists(),
                })
        except Exception:
            pass

    return jsonify(videos)


@app.route('/api/video/<video_id>/phrases')
def api_video_phrases(video_id):
    """動画別フレーズ"""
    if not db_exists():
        return jsonify([])
    try:
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT * FROM phrases WHERE video_id = ? ORDER BY is_top DESC, id ASC",
                (video_id,)
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/phrases')
def api_all_phrases():
    """全フレーズ（ページング・検索・top_only・タグフィルター）"""
    page     = request.args.get('page', 1, type=int)
    per_page = request.args.get('per_page', 50, type=int)
    q        = request.args.get('q', '').strip()
    top_only = request.args.get('top_only', '0') in ('1', 'true', 'yes')
    tag      = request.args.get('tag', '').strip()
    level    = request.args.get('level', '').strip()

    if not db_exists():
        return jsonify({'phrases': [], 'total': 0})

    try:
        with db_conn() as conn:
            conditions = []
            params_count = []
            params_rows  = []

            if top_only:
                conditions.append('p.is_top = 1')

            if q:
                like = f'%{q}%'
                conditions.append(
                    '(p.phrase_en LIKE ? OR p.phrase_ja LIKE ? OR p.note LIKE ? OR p.explanation LIKE ? OR p.tags LIKE ?)'
                )
                params_count.extend([like, like, like, like, like])
                params_rows.extend([like, like, like, like, like])

            if tag:
                tag_like = f'%{tag}%'
                conditions.append('p.tags LIKE ?')
                params_count.append(tag_like)
                params_rows.append(tag_like)

            if level:
                conditions.append('p.level = ?')
                params_count.append(level)
                params_rows.append(level)

            where = ('WHERE ' + ' AND '.join(conditions)) if conditions else ''

            rows = conn.execute(
                f"""SELECT p.*, v.title AS video_title FROM phrases p
                   LEFT JOIN videos v ON p.video_id = v.video_id
                   {where}
                   ORDER BY p.is_top DESC, p.created_at DESC
                   LIMIT ? OFFSET ?""",
                params_rows + [per_page, (page - 1) * per_page]
            ).fetchall()
            total = conn.execute(
                f"SELECT COUNT(*) FROM phrases p {where}", params_count
            ).fetchone()[0]
        return jsonify({'phrases': [dict(r) for r in rows], 'total': total})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/phrases/tags')
def api_phrases_tags():
    """phrases.tags からユニークタグ一覧を返す"""
    if not db_exists():
        return jsonify([])
    try:
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT tags FROM phrases WHERE tags IS NOT NULL AND tags != ''"
            ).fetchall()
        tag_set = set()
        for row in rows:
            for t in (row['tags'] or '').split(','):
                t = t.strip()
                if t:
                    tag_set.add(t)
        return jsonify(sorted(tag_set))
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/phrase/<int:pid>/video')
def api_phrase_video(pid):
    """フレーズに紐づく動画情報を返す（ファイル存在確認付き）"""
    if not db_exists():
        return jsonify({'error': 'DB not found'}), 404
    try:
        with db_conn() as conn:
            row = conn.execute(
                """SELECT p.video_id, p.phrase_en,
                          v.title, v.url, v.video_path, v.channel,
                          v.created_at
                   FROM phrases p
                   LEFT JOIN videos v ON p.video_id = v.video_id
                   WHERE p.id = ?""",
                (pid,)
            ).fetchone()
        if not row:
            return jsonify({'error': 'not found'}), 404

        d = dict(row)
        # 動画ファイルの存在確認・相対パス解決
        vp = d.get('video_path') or ''
        d['file_exists'] = False
        d['media_path'] = None

        if vp:
            # 絶対パスまたは OUTDIR からの相対パスを試みる
            for candidate in [vp, os.path.join(OUTDIR, vp), os.path.join(OUTDIR, os.path.basename(vp))]:
                if os.path.exists(candidate):
                    d['file_exists'] = True
                    d['media_path'] = os.path.relpath(candidate, OUTDIR)
                    break

        # YYYYMMDD フォルダからも探す（video_path が古い絶対パスの場合）
        if not d['file_exists'] and d.get('video_id'):
            for date_dir in sorted(Path(OUTDIR).glob('[0-9]' * 8), reverse=True):
                for mp4 in date_dir.glob('*.mp4'):
                    if d.get('title') and d['title'][:15].lower() in mp4.stem.lower():
                        d['file_exists'] = True
                        d['media_path'] = str(mp4.relative_to(OUTDIR))
                        break
                if d['file_exists']:
                    break

        # 日付フォルダからSRTも探す
        if d['file_exists'] and d['media_path']:
            stem = Path(d['media_path']).stem
            date = str(Path(d['media_path']).parent)
            en_srt = Path(OUTDIR) / date / f"{stem}.en.srt"
            ja_srt = Path(OUTDIR) / date / f"{stem}.ja.srt"
            d['en_srt'] = str(en_srt.relative_to(OUTDIR)) if en_srt.exists() else None
            d['ja_srt'] = str(ja_srt.relative_to(OUTDIR)) if ja_srt.exists() else None
            d['date'] = date

        return jsonify(d)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/phrases', methods=['POST'])
def api_create_phrase():
    """フレーズを手動登録"""
    data = request.json or {}
    phrase_en = (data.get('phrase_en') or '').strip()
    if not phrase_en:
        return jsonify({'error': 'phrase_en は必須です'}), 400
    phrase_ja = (data.get('phrase_ja') or '').strip()
    note      = (data.get('note') or '').strip()
    level     = (data.get('level') or '').strip()
    video_id  = (data.get('video_id') or '').strip() or '_manual_'  # 手動登録の場合
    is_top    = 1 if data.get('is_top') else 0
    if not db_exists():
        return jsonify({'error': 'DB not found'}), 500
    try:
        with db_conn() as conn:
            cur = conn.execute(
                "INSERT INTO phrases (video_id, phrase_en, phrase_ja, note, is_top, level) VALUES (?,?,?,?,?,?)",
                (video_id, phrase_en, phrase_ja, note, is_top, level)
            )
            conn.commit()
            pid = cur.lastrowid
            row = dict(conn.execute("SELECT * FROM phrases WHERE id=?", (pid,)).fetchone())
        return jsonify(row)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/phrases/<int:pid>/toggle_top', methods=['POST'])
def api_toggle_top(pid):
    try:
        with db_conn() as conn:
            row = conn.execute("SELECT is_top FROM phrases WHERE id = ?", (pid,)).fetchone()
            if row:
                conn.execute("UPDATE phrases SET is_top = ? WHERE id = ?",
                             (0 if row['is_top'] else 1, pid))
                conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/phrases/<int:pid>', methods=['DELETE'])
def api_delete_phrase(pid):
    try:
        with db_conn() as conn:
            conn.execute("DELETE FROM phrases WHERE id = ?", (pid,))
            conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/phrases/all', methods=['DELETE'])
def api_delete_all_phrases():
    """全フレーズ削除"""
    try:
        with db_conn() as conn:
            conn.execute("DELETE FROM phrase_links")
            conn.execute("DELETE FROM phrases")
            conn.commit()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─────────────────────────────────────────────
# フレーズ候補（LLM抽出・未登録）API
# ─────────────────────────────────────────────

def _parse_phrases_from_llm(llm_result: str) -> list[dict]:
    """llm_result テキストからフレーズ候補を抽出（DB登録なし）"""
    import sys as _sys
    _sys.path.insert(0, OUTDIR)
    try:
        from dl_youtube_sub_llm import parse_phrases_from_llm
        phrases = parse_phrases_from_llm(llm_result)
    except Exception:
        return []
    # levelが空の場合はヒューリスティックで判定
    for p in phrases:
        if not p.get('level'):
            p['level'] = _heuristic_level(p.get('en', ''), p.get('is_top', 0))
    return phrases


# 英検1級ヒューリスティック判定用キーワード
_EIKEN1_PATTERNS = re.compile(
    r'\b(in disguise|at the helm|by dint of|come to grips|get wind of'
    r'|par excellence|sine qua non|coup de|carte blanche|vis-à-vis'
    r'|conspicuous|ostentatious|propitious|ameliorate|venerate|exacerbate'
    r'|obfuscate|perfunctory|inveterate|egregious|fastidious|sycophant'
    r'|ephemeral|tenacious|precocious|recalcitrant|magnanimous|perspicacious'
    r'|serendipitous?|nonchalant|sanguine|voluminous|supercilious)\b',
    re.IGNORECASE
)
_EIKEN_P1_PATTERNS = re.compile(
    r'\b(on the verge|come to terms|take for granted|catch on|pull off'
    r'|blessing in disguise|go out of one\'?s way|make ends meet'
    r'|turn a blind eye|once in a blue moon|bite the bullet)\b',
    re.IGNORECASE
)

def _heuristic_level(phrase_en: str, is_top: int) -> str:
    """英語フレーズの難易度をヒューリスティックで推定"""
    clean = re.sub(r'[*_`]', '', phrase_en).strip().lower()
    words = clean.split()
    word_count = len(words)

    # 英検1級パターン一致
    if _EIKEN1_PATTERNS.search(clean):
        return '英検1級'
    # 英検準1級パターン
    if _EIKEN_P1_PATTERNS.search(clean):
        return '英検準1級'
    # is_top=1 かつ複数語は英検1級候補
    if is_top and word_count >= 2:
        return '英検1級'
    # 単語数・長さによる推定
    avg_len = sum(len(w) for w in words) / max(word_count, 1)
    if word_count == 1:
        return '英検1級' if avg_len >= 9 else ('中級' if avg_len >= 6 else '初級')
    if word_count <= 3:
        return '英検準1級' if avg_len >= 6 else '中級'
    return '英検準1級' if avg_len >= 5 else '中級'


@app.route('/api/video/<video_id>/suggested_phrases')
def api_suggested_phrases(video_id):
    """動画のllm_resultからフレーズ候補を返す（DB未登録のもののみ）"""
    if not db_exists():
        return jsonify([])
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT llm_result FROM videos WHERE video_id=?", (video_id,)
            ).fetchone()
            if not row or not row['llm_result']:
                return jsonify([])
            # 既登録フレーズ（このvideoのもの）
            existing = {r['phrase_en'] for r in conn.execute(
                "SELECT phrase_en FROM phrases WHERE video_id=?", (video_id,)
            ).fetchall()}

        phrases = _parse_phrases_from_llm(row['llm_result'])
        # 未登録のみ返す
        result = [p for p in phrases if p['en'] not in existing]
        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/video/<video_id>/register_phrases', methods=['POST'])
def api_register_phrases(video_id):
    """選択したフレーズをDBに一括登録"""
    if not db_exists():
        return jsonify({'error': 'DB not found'}), 500
    data = request.json or {}
    phrases = data.get('phrases', [])  # [{en, ja, note, is_top}, ...]
    if not phrases:
        return jsonify({'error': 'phrases is empty'}), 400
    try:
        with db_conn() as conn:
            inserted = 0
            for p in phrases:
                en = (p.get('en') or '').strip()
                if not en:
                    continue
                conn.execute(
                    "INSERT OR IGNORE INTO phrases (video_id, phrase_en, phrase_ja, note, is_top, level) VALUES (?,?,?,?,?,?)",
                    (video_id, en, p.get('ja', ''), p.get('note', ''), int(p.get('is_top', 0)), p.get('level', ''))
                )
                inserted += 1
            conn.commit()
        return jsonify({'success': True, 'inserted': inserted})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─────────────────────────────────────────────
# プリフェッチ API
# ─────────────────────────────────────────────

# prefetch ジョブ管理
_prefetch_jobs: dict[str, dict] = {}
_prefetch_lock = threading.Lock()


def _run_prefetch(job_id: str):
    """explanationが空のフレーズにバックグラウンドで解説を取得してDBに保存"""
    import json as _json
    import sys as _sys
    # dl_youtube_sub_llm の関数を使う
    try:
        _sys.path.insert(0, OUTDIR)
        from dl_youtube_sub_llm import _explain_phrase_openrouter, auto_tag_phrase
    except Exception as e:
        with _prefetch_lock:
            _prefetch_jobs[job_id]['status'] = 'error'
            _prefetch_jobs[job_id]['error'] = str(e)
        return

    api_key = os.environ.get('OPENROUTER_API_KEY', '')
    model = _prefetch_jobs[job_id].get('model', 'deepseek/deepseek-chat')

    try:
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT id, phrase_en, note FROM phrases WHERE explanation IS NULL OR explanation = '' ORDER BY id"
            ).fetchall()
    except Exception as e:
        with _prefetch_lock:
            _prefetch_jobs[job_id]['status'] = 'error'
        return

    total = len(rows)
    done = 0
    with _prefetch_lock:
        _prefetch_jobs[job_id]['total'] = total

    for row in rows:
        pid = row['id']
        phrase_en = row['phrase_en'] or ''
        note = row['note'] or ''
        clean_en = phrase_en.replace('**', '').replace('*', '').replace('`', '').strip()
        if not clean_en:
            done += 1
            continue
        try:
            expl = _explain_phrase_openrouter(clean_en, note, model, api_key)
            if expl:
                tags = auto_tag_phrase(clean_en, note, expl)
                with db_conn() as conn2:
                    conn2.execute(
                        "UPDATE phrases SET explanation=?, tags=? WHERE id=?",
                        (_json.dumps(expl, ensure_ascii=False), tags, pid)
                    )
                    conn2.commit()
        except Exception:
            pass
        done += 1
        with _prefetch_lock:
            _prefetch_jobs[job_id]['done'] = done

    with _prefetch_lock:
        _prefetch_jobs[job_id]['status'] = 'done'
        _prefetch_jobs[job_id]['done'] = done


@app.route('/api/phrases/prefetch', methods=['POST'])
def api_phrases_prefetch():
    """explanationが空のフレーズを一括取得するジョブを開始"""
    if not db_exists():
        return jsonify({'error': 'DB not found'}), 500

    data = request.json or {}
    model = data.get('model', 'deepseek/deepseek-chat')

    job_id = str(uuid.uuid4())[:8]
    with _prefetch_lock:
        _prefetch_jobs[job_id] = {
            'status': 'running', 'done': 0, 'total': 0, 'model': model
        }

    t = threading.Thread(target=_run_prefetch, args=(job_id,), daemon=True)
    t.start()

    return jsonify({'job_id': job_id, 'total': 0})


@app.route('/api/phrases/prefetch/<job_id>')
def api_phrases_prefetch_status(job_id):
    """プリフェッチジョブの進捗確認"""
    with _prefetch_lock:
        job = _prefetch_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'not found'}), 404
    return jsonify({
        'status': job['status'],
        'done': job.get('done', 0),
        'total': job.get('total', 0),
    })


# ─────────────────────────────────────────────
# 動画削除 API
# ─────────────────────────────────────────────

@app.route('/api/video/<video_id>', methods=['DELETE'])
def api_delete_video(video_id):
    """動画をDBとローカルファイルから削除"""
    if not db_exists():
        return jsonify({'error': 'DB not found'}), 500
    try:
        with db_conn() as conn:
            row = conn.execute("SELECT video_path, title FROM videos WHERE video_id=?", (video_id,)).fetchone()
            deleted_files = []
            if row and row['video_path']:
                vp = Path(row['video_path'])
                stem = vp.stem
                parent = vp.parent
                for f in parent.glob(f"{stem}*"):
                    try:
                        f.unlink()
                        deleted_files.append(str(f))
                    except Exception:
                        pass
            # DBから削除（関連フレーズ・コメントも）
            conn.execute("DELETE FROM phrase_links WHERE phrase_a IN (SELECT id FROM phrases WHERE video_id=?) OR phrase_b IN (SELECT id FROM phrases WHERE video_id=?)", (video_id, video_id))
            conn.execute("DELETE FROM phrases WHERE video_id=?", (video_id,))
            conn.execute("DELETE FROM video_comments WHERE video_id=?", (video_id,))
            conn.execute("DELETE FROM videos WHERE video_id=?", (video_id,))
            conn.commit()
        return jsonify({'ok': True, 'deleted_files': deleted_files})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# コメント API
# ─────────────────────────────────────────────

@app.route('/api/video/<video_id>/comments/refresh', methods=['POST'])
def api_refresh_video_comments(video_id):
    """特定動画のコメントを再取得"""
    if not db_exists():
        return jsonify({'error': 'DB not found'}), 500
    data = request.json or {}
    model = data.get('model', 'anthropic/claude-3-haiku')
    try:
        with db_conn() as conn:
            row = conn.execute("SELECT url FROM videos WHERE video_id=?", (video_id,)).fetchone()
        if not row or not row['url']:
            return jsonify({'error': '動画URLが見つかりません'}), 404
        url = row['url']
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    job_id = str(uuid.uuid4())[:8]
    with _comment_jobs_lock:
        _comment_jobs[job_id] = {'status': 'running', 'done': 0, 'total': 1, 'model': model, 'log': []}

    def _do_refresh():
        import sys as _sys
        try:
            _sys.path.insert(0, OUTDIR)
            from dl_youtube_sub_llm import fetch_video_comments, PhraseDB
        except Exception as e:
            with _comment_jobs_lock:
                _comment_jobs[job_id]['status'] = 'error'
            return
        api_key = os.environ.get('OPENROUTER_API_KEY', '')
        db = PhraseDB(DB_PATH)
        try:
            n = fetch_video_comments(url, video_id, model, api_key, db)
            with _comment_jobs_lock:
                _comment_jobs[job_id]['log'].append(f'✅ {n}件取得')
                _comment_jobs[job_id]['done'] = 1
        except Exception as ex:
            with _comment_jobs_lock:
                _comment_jobs[job_id]['log'].append(f'❌ {ex}')
        db.conn.close()
        with _comment_jobs_lock:
            _comment_jobs[job_id]['status'] = 'done'

    t = threading.Thread(target=_do_refresh, daemon=True)
    t.start()
    return jsonify({'job_id': job_id})


@app.route('/api/video/<video_id>/comments')
def api_video_comments(video_id):
    """動画コメント一覧"""
    if not db_exists():
        return jsonify([])
    try:
        with db_conn() as conn:
            rows = conn.execute(
                "SELECT id, author, text, likes, explanation FROM video_comments WHERE video_id=? ORDER BY likes DESC",
                (video_id,)
            ).fetchall()
        return jsonify([dict(r) for r in rows])
    except Exception as e:
        # yt-dlp失敗時もLLM翻訳にフォールバック
        return _translate_sub_with_llm(video_id)


def _translate_sub_to_ja(video_id: str) -> dict:
    """英語字幕をOpenRouterで日本語翻訳してDBに保存（Flaskコンテキスト不要）"""
    api_key = os.environ.get('OPENROUTER_API_KEY', '')
    model = 'anthropic/claude-3-haiku'
    if not api_key:
        return {'error': 'OPENROUTER_API_KEY が未設定です'}

    try:
        with db_conn() as conn:
            row = conn.execute("SELECT subtitle_en FROM videos WHERE video_id=?", (video_id,)).fetchone()
        if not row or not row['subtitle_en']:
            return {'error': '英語字幕がDBにありません'}
        en_text = row['subtitle_en']
    except Exception as e:
        return {'error': str(e)}

    if '-->' in en_text:
        subs = parse_srt(en_text)
    else:
        import re as _re
        sentences = _re.split(r'(?<=[.!?])\s+', en_text.strip())
        t = 0.0
        subs = []
        for s in sentences:
            s = s.strip()
            if s:
                d = max(2.0, len(s) * 0.08)
                subs.append({'start': t, 'end': t+d, 'text': s})
                t += d + 0.5

    if not subs:
        return {'error': '字幕データなし'}

    numbered = '\n'.join(f"{i+1}. {s['text']}" for i, s in enumerate(subs))
    prompt = f"""以下の英語字幕を番号順に日本語訳してください。
番号はそのままにして、英語と同じ番号・数だけ日本語訳を出力してください。
自然な日本語に翻訳し、口語表現は口語のまま訳してください。

{numbered}

出力形式:
1. 日本語訳
2. 日本語訳
..."""

    try:
        resp = _http_session.post(
            'https://openrouter.ai/api/v1/chat/completions',
            headers={'Authorization': f'Bearer {api_key}', 'Content-Type': 'application/json'},
            json={'model': model, 'messages': [{'role': 'user', 'content': prompt}], 'temperature': 0.2},
            timeout=60,
        )
        resp.raise_for_status()
        content = resp.json()['choices'][0]['message']['content']
    except Exception as e:
        return {'error': f'LLM翻訳失敗: {e}'}

    import re as _re2
    ja_lines = {}
    for line in content.strip().splitlines():
        m = _re2.match(r'^(\d+)\.\s*(.+)', line.strip())
        if m:
            ja_lines[int(m.group(1))] = m.group(2).strip()

    def _sec_to_srt(sec):
        h = int(sec // 3600); m2 = int((sec % 3600) // 60)
        s2 = int(sec % 60); ms = int((sec - int(sec)) * 1000)
        return f"{h:02d}:{m2:02d}:{s2:02d},{ms:03d}"

    srt_parts = []
    result_subs = []
    for i, s in enumerate(subs):
        ja = ja_lines.get(i+1, '')
        srt_parts.append(f"{i+1}\n{_sec_to_srt(s['start'])} --> {_sec_to_srt(s['end'])}\n{ja}\n")
        result_subs.append({'start': s['start'], 'end': s['end'], 'text': ja})

    srt_text = '\n'.join(srt_parts)
    try:
        with db_conn() as conn:
            conn.execute("UPDATE videos SET subtitle_ja=? WHERE video_id=?", (srt_text, video_id))
            conn.commit()
    except Exception:
        pass

    return {'ok': True, 'count': len(result_subs)}


def _translate_sub_with_llm(video_id: str):
    """英語字幕をOpenRouterで日本語翻訳してDBに保存（Flask API用）"""
    result = _translate_sub_to_ja(video_id)
    if 'error' in result:
        code = 400 if 'API_KEY' in result['error'] else 404 if 'なし' in result['error'] else 500
        return jsonify(result), code
    # result_subs を再構築してレスポンスに含める
    try:
        with db_conn() as conn:
            row = conn.execute("SELECT subtitle_ja FROM videos WHERE video_id=?", (video_id,)).fetchone()
        subs = parse_srt(row['subtitle_ja']) if row and row['subtitle_ja'] else []
    except Exception:
        subs = []
    return jsonify({'ok': True, 'count': result.get('count', 0), 'subtitles': subs, 'source': 'llm'})


# ─────────────────────────────────────────────
# ボキャブラリーネットワーク API
# ─────────────────────────────────────────────

@app.route('/api/fetch_all_ja_subs', methods=['POST'])
def api_fetch_all_ja_subs():
    """全動画の日本語字幕をバックグラウンドで取得"""
    if not db_exists():
        return jsonify({'error': 'DB not found'}), 500

    job_id = str(uuid.uuid4())[:8]
    with _comment_jobs_lock:
        _comment_jobs[job_id] = {'status': 'running', 'done': 0, 'total': 0, 'log': []}

    def _do():
        try:
            with db_conn() as conn:
                rows = conn.execute(
                    "SELECT video_id, url FROM videos WHERE url IS NOT NULL AND (subtitle_ja IS NULL OR subtitle_ja='')"
                ).fetchall()
        except Exception:
            with _comment_jobs_lock:
                _comment_jobs[job_id]['status'] = 'error'
            return

        total = len(rows)
        with _comment_jobs_lock:
            _comment_jobs[job_id]['total'] = total

        for i, row in enumerate(rows):
            vid = row['video_id']
            try:
                resp = _http_session.post(
                    f'http://localhost:5000/api/video/{vid}/fetch_ja_sub',
                    json={}, timeout=60
                )
                data = resp.json()
                msg = f'✅ {vid}: {data.get("count",0)}文' if data.get('ok') else f'❌ {vid}: {data.get("error","")}'
            except Exception as e:
                msg = f'❌ {vid}: {e}'
            with _comment_jobs_lock:
                _comment_jobs[job_id]['log'].append(msg)
                _comment_jobs[job_id]['done'] = i + 1

        with _comment_jobs_lock:
            _comment_jobs[job_id]['status'] = 'done'

    threading.Thread(target=_do, daemon=True).start()
    return jsonify({'job_id': job_id})


@app.route('/api/network')
def api_network():
    """フレーズ関連ネットワークデータ（nodes + links）を返す"""
    if not db_exists():
        return jsonify({'nodes': [], 'links': []})
    try:
        with db_conn() as conn:
            phrases = conn.execute(
                "SELECT id, phrase_en, tags, explanation, is_top FROM phrases ORDER BY is_top DESC, id ASC"
            ).fetchall()
            links_rows = conn.execute(
                "SELECT phrase_a, phrase_b, link_type FROM phrase_links"
            ).fetchall()

        nodes = []
        for p in phrases:
            clean = re.sub(r'\*+', '', p['phrase_en'] or '').strip()
            tags = [t.strip() for t in (p['tags'] or '').split(',') if t.strip()]
            nodes.append({
                'id': p['id'],
                'label': clean[:30] if clean else f'phrase_{p["id"]}',
                'tags': tags,
                'is_top': bool(p['is_top']),
            })

        links = [{'source': int(r['phrase_a']), 'target': int(r['phrase_b']), 'type': r['link_type']} for r in links_rows]
        return jsonify({'nodes': nodes, 'links': links})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/network/build', methods=['POST'])
def api_network_build():
    """タグの一致からフレーズ関連リンクを自動生成"""
    if not db_exists():
        return jsonify({'error': 'DB not found'}), 500
    try:
        with db_conn() as conn:
            phrases = conn.execute(
                "SELECT id, phrase_en, tags, explanation FROM phrases WHERE tags IS NOT NULL AND tags != ''"
            ).fetchall()

            # タグが一致するフレーズ同士を紐付け（既存を全削除して再生成）
            conn.execute("DELETE FROM phrase_links")
            inserted = 0
            tag_map: dict[str, list[int]] = {}
            for p in phrases:
                for tag in (p['tags'] or '').split(','):
                    tag = tag.strip()
                    if tag:
                        tag_map.setdefault(tag, []).append(p['id'])

            seen_pairs: set[tuple] = set()
            for tag, ids in tag_map.items():
                for i in range(len(ids)):
                    for j in range(i + 1, len(ids)):
                        a, b = min(ids[i], ids[j]), max(ids[i], ids[j])
                        if (a, b) not in seen_pairs:
                            seen_pairs.add((a, b))
                            conn.execute(
                                "INSERT INTO phrase_links (phrase_a, phrase_b, link_type) VALUES (?,?,?)",
                                (a, b, f'tag:{tag}')
                            )
                            inserted += 1

            # related フィールドからもリンク生成
            for p in phrases:
                if not p['explanation']:
                    continue
                try:
                    expl = json.loads(p['explanation'])
                    related = expl.get('related', [])
                    for rel in related:
                        rel_clean = re.sub(r'\*+', '', rel).strip().lower()
                        # 類似フレーズをDB内で検索
                        match = conn.execute(
                            "SELECT id FROM phrases WHERE LOWER(REPLACE(REPLACE(phrase_en,'**',''),'*','')) LIKE ? LIMIT 1",
                            (f'%{rel_clean[:15]}%',)
                        ).fetchone()
                        if match and match['id'] != p['id']:
                            a, b = min(p['id'], match['id']), max(p['id'], match['id'])
                            if (a, b) not in seen_pairs:
                                seen_pairs.add((a, b))
                                conn.execute(
                                    "INSERT INTO phrase_links (phrase_a, phrase_b, link_type) VALUES (?,?,?)",
                                    (a, b, 'related')
                                )
                                inserted += 1
                except Exception:
                    pass

            conn.commit()
        return jsonify({'ok': True, 'links_created': inserted})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─────────────────────────────────────────────
# コメント一括取得 API
# ─────────────────────────────────────────────

def _run_fetch_comments(job_id: str):
    """全動画のコメントをバックグラウンドで取得"""
    import sys as _sys
    try:
        _sys.path.insert(0, OUTDIR)
        from dl_youtube_sub_llm import fetch_video_comments, PhraseDB
    except Exception as e:
        with _comment_jobs_lock:
            _comment_jobs[job_id]['status'] = 'error'
            _comment_jobs[job_id]['error'] = str(e)
        return

    api_key = os.environ.get('OPENROUTER_API_KEY', '')
    model = _comment_jobs[job_id].get('model', 'deepseek/deepseek-chat')

    try:
        with db_conn() as conn:
            rows = conn.execute("SELECT video_id, url FROM videos WHERE url IS NOT NULL").fetchall()
    except Exception as e:
        with _comment_jobs_lock:
            _comment_jobs[job_id]['status'] = 'error'
        return

    total = len(rows)
    done = 0
    with _comment_jobs_lock:
        _comment_jobs[job_id]['total'] = total

    db = PhraseDB(DB_PATH)
    for row in rows:
        video_id = row['video_id']
        url = row['url']
        try:
            n = fetch_video_comments(url, video_id, model, api_key, db)
            with _comment_jobs_lock:
                _comment_jobs[job_id]['log'].append(f'✅ {video_id}: {n}件取得')
        except Exception as ex:
            with _comment_jobs_lock:
                _comment_jobs[job_id]['log'].append(f'❌ {video_id}: {ex}')
        done += 1
        with _comment_jobs_lock:
            _comment_jobs[job_id]['done'] = done
    db.conn.close()

    with _comment_jobs_lock:
        _comment_jobs[job_id]['status'] = 'done'
        _comment_jobs[job_id]['done'] = done


@app.route('/api/comments/fetch_all', methods=['POST'])
def api_fetch_all_comments():
    """全動画のコメントを一括取得するジョブを開始"""
    if not db_exists():
        return jsonify({'error': 'DB not found'}), 500
    data = request.json or {}
    model = data.get('model', 'deepseek/deepseek-chat')
    job_id = str(uuid.uuid4())[:8]
    with _comment_jobs_lock:
        _comment_jobs[job_id] = {'status': 'running', 'done': 0, 'total': 0, 'model': model, 'log': []}
    t = threading.Thread(target=_run_fetch_comments, args=(job_id,), daemon=True)
    t.start()
    return jsonify({'job_id': job_id})


@app.route('/api/comments/fetch_all/<job_id>')
def api_fetch_all_comments_status(job_id):
    with _comment_jobs_lock:
        job = _comment_jobs.get(job_id)
    if not job:
        return jsonify({'error': 'job not found'}), 404
    return jsonify({
        'status': job['status'],
        'done': job['done'],
        'total': job['total'],
        'log': job.get('log', []),
    })


# ─────────────────────────────────────────────
# ダウンロード API
# ─────────────────────────────────────────────

def _run_download(job_id: str, cmd: list[str]):
    """サブプロセスでダウンローダーを実行し、出力をジョブキューに流す"""
    q = _jobs[job_id]['q']
    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=OUTDIR,
            text=True,
            encoding='utf-8',
            errors='replace',
            start_new_session=True,  # プロセスグループ作成（killpg で子プロセスも停止）
        )
        _jobs[job_id]['pid'] = proc.pid
        for line in proc.stdout:
            line = line.rstrip('\n')
            _jobs[job_id]['log'].append(line)
            q.put(line)
        proc.wait()
        status = 'done' if proc.returncode == 0 else 'error'
        # DL完了後、JA字幕が空の動画を自動翻訳
        if status == 'done' and db_exists():
            try:
                with db_conn() as conn:
                    no_ja = conn.execute(
                        "SELECT video_id FROM videos WHERE subtitle_en != '' AND (subtitle_ja IS NULL OR subtitle_ja='')"
                    ).fetchall()
                for row in no_ja:
                    vid = row['video_id']
                    q.put(f'🌐 日本語訳を自動取得中: {vid}')
                    _jobs[job_id]['log'].append(f'🌐 日本語訳を自動取得中: {vid}')
                    res = _translate_sub_to_ja(vid)
                    msg = f'  ✅ 日本語訳完了: {res.get("count", 0)}行' if 'ok' in res else f'  ⚠️ 翻訳スキップ: {res.get("error","")}'
                    q.put(msg)
                    _jobs[job_id]['log'].append(msg)
            except Exception as e:
                q.put(f'[JA自動取得エラー] {e}')
    except Exception as e:
        _jobs[job_id]['log'].append(f'[ERROR] {e}')
        q.put(f'[ERROR] {e}')
        status = 'error'
    finally:
        _jobs[job_id]['status'] = status
        q.put(None)  # sentinel
        # 古い完了ジョブを削除（直近10件のみ保持）
        with _jobs_lock:
            done_ids = [jid for jid, j in _jobs.items() if j['status'] != 'running']
            for old_id in done_ids[:-10]:
                del _jobs[old_id]


@app.route('/api/download', methods=['POST'])
def api_download():
    """ダウンロードジョブを開始"""
    data = request.json or {}
    urls = [u.strip() for u in data.get('urls', '').splitlines() if u.strip()]
    if not urls:
        return jsonify({'error': 'URLが空です'}), 400

    mode        = data.get('mode', 'both')     # both / video / sub
    model       = data.get('model', 'deepseek/deepseek-chat')
    top_n       = str(data.get('top_n', 3))
    lang        = data.get('lang', 'en')
    auto_phrases = data.get('auto_phrases', True)   # デフォルトON

    cmd = ['python3', SCRIPT] + urls + [
        f'--{mode}',
        '--model', model,
        '--top', top_n,
        '--lang', lang,
        '--outdir', OUTDIR,
    ]
    if not auto_phrases:
        cmd.append('--no-auto-phrases')

    job_id = str(uuid.uuid4())[:8]
    with _jobs_lock:
        _jobs[job_id] = {'status': 'running', 'log': [], 'q': queue.Queue(), 'pid': None}

    t = threading.Thread(target=_run_download, args=(job_id, cmd), daemon=True)
    t.start()
    return jsonify({'job_id': job_id})


@app.route('/api/download/stream/<job_id>')
def api_download_stream(job_id):
    """SSE でリアルタイムログをストリーム"""
    if job_id not in _jobs:
        return jsonify({'error': 'ジョブが見つかりません'}), 404

    def generate():
        job = _jobs[job_id]
        # 既存ログを先に流す
        for line in list(job['log']):
            yield f"data: {json.dumps({'line': line, 'status': job['status']})}\n\n"
        # 追加ログをキューから読む
        if job['status'] == 'running':
            q = job['q']
            while True:
                try:
                    line = q.get(timeout=30)
                except queue.Empty:
                    yield "data: {\"line\": \"\", \"status\": \"running\"}\n\n"
                    continue
                if line is None:
                    break
                yield f"data: {json.dumps({'line': line, 'status': 'running'})}\n\n"
        final_status = job['status']  # 'done' | 'error' | 'cancelled'
        yield f"data: {json.dumps({'line': '', 'status': final_status, 'done': True})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@app.route('/api/download/status/<job_id>')
def api_download_status(job_id):
    if job_id not in _jobs:
        return jsonify({'error': 'not found'}), 404
    job = _jobs[job_id]
    return jsonify({'status': job['status'], 'log': job['log'][-50:]})


@app.route('/api/download/cancel/<job_id>', methods=['POST'])
def api_download_cancel(job_id):
    """実行中のDLジョブを停止"""
    if job_id not in _jobs:
        return jsonify({'error': 'not found'}), 404
    job = _jobs[job_id]
    if job['status'] != 'running':
        return jsonify({'success': False, 'reason': 'not running'})
    pid = job.get('pid')
    if pid:
        import signal
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, signal.SIGTERM)
            except Exception:
                pass
    job['status'] = 'cancelled'
    job['q'].put(None)  # SSE ストリームを終了
    return jsonify({'success': True})


@app.route('/api/explain', methods=['POST'])
def api_explain():
    """OpenRouter で文章・フレーズを解説（DBキャッシュ優先）"""
    api_key = os.environ.get('OPENROUTER_API_KEY', '')

    data = request.json or {}
    text = data.get('text', '').strip()
    context = data.get('context', '').strip()
    model = data.get('model', 'anthropic/claude-3-haiku')

    if not text:
        return jsonify({'error': 'text が空です'}), 400

    # DBキャッシュを先に確認（markdown記法除去して照合）
    clean_text = re.sub(r'\*+', '', text).strip()
    if db_exists():
        try:
            with db_conn() as conn:
                row = conn.execute(
                    "SELECT explanation FROM phrases WHERE phrase_en = ? OR REPLACE(REPLACE(phrase_en,'**',''),'*','') = ? LIMIT 1",
                    (text, clean_text)
                ).fetchone()
            if row and row['explanation']:
                cached = json.loads(row['explanation'])
                if cached and isinstance(cached, dict) and cached.get('meaning'):
                    return jsonify(cached)
        except Exception:
            pass

    if not api_key:
        return jsonify({'error': 'OPENROUTER_API_KEY が設定されていません'}), 400

    level = data.get('level', 'eiken1')

    prompt = f"""英検1級・上級英語学習者向けに、以下の英語フレーズ・センテンスを解説してください。
言語学的知見（語源学、認知言語学、音象徴など）を活用して、深く・記憶に残る解説を作成してください。

フレーズ/文: {text}
{f'前後の文脈: {context}' if context else ''}

以下のJSON形式のみで返答してください（他のテキスト不要）:
{{
  "meaning": "意味・説明（日本語・詳しく）",
  "usage": "使い方・ニュアンス・語法（日本語）",
  "example": "英検1級レベルの例文（英語）",
  "example_ja": "例文の日本語訳",
  "etymology": "語源・成り立ち（ラテン語/ギリシャ語/古英語の語根・接頭辞・接尾辞の分析）",
  "linguistics_note": "言語学的考察（音象徴・認知言語学的メタファー・形態素分析・イメージスキーマなど）",
  "story": "覚え方ストーリー（日本語・情景が浮かぶ具体的なエピソード）",
  "mnemonic": "ゴロ合わせや記憶術（日本語・ユニークで覚えやすいもの）",
  "related": ["英検1級・TOEFL・GRE レベルの類語・対義語・派生語 最大4つ（基本語は避け、高度な語彙のみ）"],
  "eiken_note": "英検1級での出題傾向・注意点（あれば）",
  "level": "初級/中級/上級"
}}"""

    try:
        resp = _http_session.post(
            'https://openrouter.ai/api/v1/chat/completions',
            headers={
                'Authorization': f'Bearer {api_key}',
                'Content-Type': 'application/json',
                'HTTP-Referer': 'http://localhost:5000',
            },
            json={
                'model': model,
                'messages': [{'role': 'user', 'content': prompt}],
                'response_format': {'type': 'json_object'},
                'temperature': 0.3,
            },
            timeout=30,
        )
        resp.raise_for_status()
        content = resp.json()['choices'][0]['message']['content']
        # markdownコードブロックを除去
        content = re.sub(r'^```(?:json)?\s*', '', content.strip(), flags=re.IGNORECASE)
        content = re.sub(r'\s*```$', '', content.strip())
        try:
            result = json.loads(content)
        except json.JSONDecodeError:
            result = {'meaning': content}

        # 取得した解説をDBに保存（phrase_en が一致するレコードを更新）
        if db_exists() and isinstance(result, dict) and result.get('meaning'):
            try:
                expl_json = json.dumps(result, ensure_ascii=False)
                with db_conn() as conn:
                    conn.execute(
                        "UPDATE phrases SET explanation = ? WHERE phrase_en = ? OR REPLACE(REPLACE(phrase_en,'**',''),'*','') = ?",
                        (expl_json, text, clean_text)
                    )
            except Exception:
                pass

        return jsonify(result)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/subtitle/<path:filepath>')
def api_subtitle(filepath):
    """SRT → JSON (OUTDIR 相対パス)"""
    full = os.path.join(OUTDIR, filepath)
    if not os.path.exists(full):
        return jsonify([])
    with open(full, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    return jsonify(parse_srt(content))


@app.route('/api/srt/<video_id>/<lang>')
def api_srt_by_video(video_id, lang):
    """DB の video_path から SRT を探して返す"""
    if not db_exists():
        return jsonify([])
    try:
        with db_conn() as conn:
            row = conn.execute(
                "SELECT video_path, subtitle_en, subtitle_ja FROM videos WHERE video_id = ?",
                (video_id,)
            ).fetchone()
        if not row:
            return jsonify([])

        # 方法1: video_path に隣接する .srt ファイル
        vp = row['video_path'] or ''
        if vp:
            vp_dir  = os.path.dirname(vp)
            vp_stem = Path(vp).stem
            srt_path = os.path.join(vp_dir, f"{vp_stem}.{lang}.srt")
            if os.path.exists(srt_path):
                with open(srt_path, 'r', encoding='utf-8', errors='ignore') as f:
                    return jsonify(parse_srt(f.read()))

        # 方法2: OUTDIR の全日付フォルダを探す
        stem_hint = Path(vp).stem if vp else ''
        if stem_hint:
            for d in sorted(Path(OUTDIR).glob('[0-9]' * 8), reverse=True):
                candidate = d / f"{stem_hint}.{lang}.srt"
                if candidate.exists():
                    with open(candidate, 'r', encoding='utf-8', errors='ignore') as f:
                        result = parse_srt(f.read())
                    if result:
                        return jsonify(result)

        # 方法3: DB の subtitle_en/subtitle_ja テキストから疑似SRT
        text = (row['subtitle_en'] if lang == 'en' else row['subtitle_ja']) or ''
        if '-->' in text:
            return jsonify(parse_srt(text))
        if text.strip():
            import re as _re
            sentences = _re.split(r'(?<=[.!?])\s+', text.strip())
            pseudo = []
            t = 0.0
            for sent in sentences:
                sent = sent.strip()
                if not sent:
                    continue
                duration = max(2.0, len(sent) * 0.08)
                pseudo.append({'start': t, 'end': t + duration, 'text': sent})
                t += duration + 0.5
            if pseudo:
                return jsonify(pseudo)

        return jsonify([])
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/video/<video_id>/fetch_ja_sub', methods=['POST'])
def api_fetch_ja_sub(video_id):
    """yt-dlp で日本語字幕を取得してDBに保存"""
    if not db_exists():
        return jsonify({'error': 'DB not found'}), 500
    try:
        with db_conn() as conn:
            row = conn.execute("SELECT url FROM videos WHERE video_id=?", (video_id,)).fetchone()
        if not row or not row['url']:
            return jsonify({'error': 'URL not found'}), 404
        url = row['url']
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    import tempfile, sys as _sys
    _sys.path.insert(0, OUTDIR)
    try:
        import yt_dlp
    except ImportError:
        return jsonify({'error': 'yt-dlp not installed'}), 500

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            ydl_opts = {
                'skip_download': True,
                'writesubtitles': True,
                'writeautomaticsub': True,
                'subtitleslangs': ['ja'],
                'subtitlesformat': 'vtt',
                'outtmpl': os.path.join(tmpdir, '%(id)s.%(ext)s'),
                'quiet': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            # VTT→SRT変換してDBに保存
            vtt_path = os.path.join(tmpdir, f"{video_id}.ja.vtt")
            if not os.path.exists(vtt_path):
                # 自動字幕ファイル名を探す
                for f in os.listdir(tmpdir):
                    if f.endswith('.vtt'):
                        vtt_path = os.path.join(tmpdir, f)
                        break

            if not os.path.exists(vtt_path):
                # yt-dlpで取得できない場合はOpenRouterで翻訳
                return _translate_sub_with_llm(video_id)


            with open(vtt_path, encoding='utf-8', errors='ignore') as f:
                vtt_text = f.read()

            # VTT → parse_srt 用のSRT変換
            import re as _re
            # VTTのタイムスタンプ行を除去してテキスト抽出
            lines = vtt_text.splitlines()
            srt_lines = []
            idx = 1
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if '-->' in line:
                    # タイムスタンプ行（VTT形式 → SRT形式に変換）
                    ts = line.replace('.', ',').split(' --> ')
                    if len(ts) == 2:
                        t1 = ts[0].strip().split()[0]  # 位置タグ除去
                        t2 = ts[1].strip().split()[0]
                        # ミリ秒部分の桁数を3桁に
                        def fix_ts(t):
                            parts = t.split(',')
                            if len(parts) == 2:
                                return parts[0] + ',' + parts[1][:3].ljust(3,'0')
                            return t
                        srt_lines.append(str(idx))
                        srt_lines.append(f"{fix_ts(t1)} --> {fix_ts(t2)}")
                        idx += 1
                        i += 1
                        while i < len(lines) and lines[i].strip():
                            txt = _re.sub(r'<[^>]+>', '', lines[i].strip())
                            if txt:
                                srt_lines.append(txt)
                            i += 1
                        srt_lines.append('')
                        continue
                i += 1

            srt_text = '\n'.join(srt_lines)
            parsed = parse_srt(srt_text)
            if not parsed:
                return jsonify({'error': 'SRT変換失敗'}), 500

            # DBに保存
            with db_conn() as conn:
                conn.execute("UPDATE videos SET subtitle_ja=? WHERE video_id=?", (srt_text, video_id))
                conn.commit()

            return jsonify({'ok': True, 'count': len(parsed), 'subtitles': parsed})
    except Exception as e:
        # yt-dlp失敗時はLLM翻訳にフォールバック
        return _translate_sub_with_llm(video_id)


@app.route('/api/video/<video_id>/fetch_en_sub', methods=['POST'])
def api_fetch_en_sub(video_id):
    """yt-dlp で英語字幕を取得してDBに保存"""
    if not db_exists():
        return jsonify({'error': 'DB not found'}), 500
    try:
        with db_conn() as conn:
            row = conn.execute("SELECT url FROM videos WHERE video_id=?", (video_id,)).fetchone()
        if not row or not row['url']:
            return jsonify({'error': 'URL not found'}), 404
        url = row['url']
    except Exception as e:
        return jsonify({'error': str(e)}), 500

    import tempfile
    try:
        import yt_dlp
    except ImportError:
        return jsonify({'error': 'yt-dlp not installed'}), 500

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            ydl_opts = {
                'skip_download': True,
                'writesubtitles': True,
                'writeautomaticsub': True,
                'subtitleslangs': ['en'],
                'subtitlesformat': 'vtt',
                'outtmpl': os.path.join(tmpdir, '%(id)s.%(ext)s'),
                'quiet': True,
            }
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([url])

            # VTTファイルを探す
            vtt_path = None
            for f in os.listdir(tmpdir):
                if f.endswith('.vtt'):
                    vtt_path = os.path.join(tmpdir, f)
                    break

            if not vtt_path:
                return jsonify({'error': '英語字幕が見つかりません'}), 404

            with open(vtt_path, encoding='utf-8', errors='ignore') as f:
                vtt_text = f.read()

            # VTT → SRT変換
            import re as _re
            lines = vtt_text.splitlines()
            srt_lines = []
            idx = 1
            i = 0
            while i < len(lines):
                line = lines[i].strip()
                if '-->' in line:
                    ts = line.replace('.', ',').split(' --> ')
                    if len(ts) == 2:
                        t1 = ts[0].strip().split()[0]
                        t2 = ts[1].strip().split()[0]
                        def fix_ts(t):
                            parts = t.split(',')
                            if len(parts) == 2:
                                return parts[0] + ',' + parts[1][:3].ljust(3, '0')
                            return t
                        srt_lines.append(str(idx))
                        srt_lines.append(f"{fix_ts(t1)} --> {fix_ts(t2)}")
                        idx += 1
                        i += 1
                        while i < len(lines) and lines[i].strip():
                            txt = _re.sub(r'<[^>]+>', '', lines[i].strip())
                            if txt:
                                srt_lines.append(txt)
                            i += 1
                        srt_lines.append('')
                        continue
                i += 1

            srt_text = '\n'.join(srt_lines)
            parsed = parse_srt(srt_text)
            if not parsed:
                return jsonify({'error': 'SRT変換失敗'}), 500

            with db_conn() as conn:
                conn.execute("UPDATE videos SET subtitle_en=? WHERE video_id=?", (srt_text, video_id))
                conn.commit()

            return jsonify({'ok': True, 'count': len(parsed), 'subtitles': parsed})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/media/<path:filepath>')
def serve_media(filepath):
    """動画・字幕ファイル配信（OUTDIR と DB video_path の両方を探す）"""
    # OUTDIR 内を先に確認
    full = os.path.join(OUTDIR, filepath)
    if os.path.exists(full):
        return _stream_video(full) if filepath.endswith('.mp4') else send_file(full)

    # OUTDIR になければ DB の video_path から探す
    stem = Path(filepath).stem
    if db_exists():
        try:
            with db_conn() as conn:
                row = conn.execute(
                    "SELECT video_path FROM videos WHERE video_path LIKE ? LIMIT 1",
                    (f'%{stem}%',)
                ).fetchone()
            if row and row['video_path']:
                vp = row['video_path']
                if os.path.exists(vp):
                    return _stream_video(vp) if vp.endswith('.mp4') else send_file(vp)
                # video_path に隣接するファイルを探す
                vp_dir = os.path.dirname(vp)
                vp_stem = Path(vp).stem
                candidate = os.path.join(vp_dir, Path(filepath).name)
                if os.path.exists(candidate):
                    return _stream_video(candidate) if candidate.endswith('.mp4') else send_file(candidate)
        except Exception:
            pass

    abort(404)


def _stream_video(path: str):
    file_size = os.path.getsize(path)
    range_header = request.headers.get('Range')

    if range_header:
        m = re.match(r'bytes=(\d+)-(\d*)', range_header)
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else file_size - 1
        end = min(end, file_size - 1)
        length = end - start + 1

        def generate():
            with open(path, 'rb') as f:
                f.seek(start)
                remaining = length
                while remaining:
                    chunk = f.read(min(65536, remaining))
                    if not chunk:
                        break
                    remaining -= len(chunk)
                    yield chunk

        rv = Response(generate(), 206, mimetype='video/mp4', direct_passthrough=True)
        rv.headers['Content-Range'] = f'bytes {start}-{end}/{file_size}'
        rv.headers['Accept-Ranges'] = 'bytes'
        rv.headers['Content-Length'] = str(length)
        return rv

    def generate():
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(65536)
                if not chunk:
                    break
                yield chunk

    rv = Response(generate(), 200, mimetype='video/mp4', direct_passthrough=True)
    rv.headers['Content-Length'] = str(file_size)
    rv.headers['Accept-Ranges'] = 'bytes'
    return rv


if __name__ == '__main__':
    print(f"📁 Output directory : {OUTDIR}")
    print(f"🗄️  Database         : {DB_PATH}")
    print(f"🌐 UI               : http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)
