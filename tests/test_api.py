#!/usr/bin/env python3
"""Flask API 総合テスト"""
import json
import requests
import sys

BASE = "http://localhost:5000"
PASS = "\033[92m✓\033[0m"
FAIL = "\033[91m✗\033[0m"
errors = []

def test(name, fn):
    try:
        result = fn()
        if result:
            print(f"{PASS} {name}")
        else:
            print(f"{FAIL} {name}")
            errors.append(name)
    except Exception as e:
        print(f"{FAIL} {name}: {e}")
        errors.append(name)

def get(path): return requests.get(f"{BASE}{path}", timeout=10)
def post(path, body={}): return requests.post(f"{BASE}{path}", json=body, timeout=10)

# ─── 基本 ───────────────────────────────────────────────────────
print("\n【基本API】")
test("UI ページ読み込み",       lambda: get("/").status_code == 200)
test("日付一覧取得",             lambda: len(get("/api/dates").json()) > 0)
test("動画一覧(日付指定)",       lambda: isinstance(get("/api/dates/20260310/videos").json(), list))
test("フレーズ一覧取得",         lambda: get("/api/phrases").json().get("total", 0) > 0)
test("タグ一覧取得",             lambda: isinstance(get("/api/phrases/tags").json(), list))

# ─── フレーズ検索 ──────────────────────────────────────────────
print("\n【検索】")
test("フレーズ英語検索",         lambda: get("/api/phrases?q=blessing").json().get("total", 0) > 0)
test("フレーズ日本語検索",       lambda: isinstance(get("/api/phrases?q=幸運").json().get("phrases"), list))
test("explanation内検索",       lambda: isinstance(get("/api/phrases?q=ラテン語").json().get("phrases"), list))
test("タグフィルター",           lambda: isinstance(get("/api/phrases?tag=イディオム").json().get("phrases"), list))
test("top_onlyフィルター",      lambda: isinstance(get("/api/phrases?top_only=1").json().get("phrases"), list))

# ─── 字幕 ──────────────────────────────────────────────────────
print("\n【字幕】")
def _get_video_id():
    vs = get("/api/dates/20260310/videos").json()
    return next((v["video_id"] for v in vs if v.get("video_id")), None)

vid = _get_video_id()
test("video_id 取得",           lambda: vid is not None)
if vid:
    test("EN字幕取得(DB経由)",   lambda: isinstance(get(f"/api/srt/{vid}/en").json(), list))
    test("JA字幕取得(DB経由)",   lambda: isinstance(get(f"/api/srt/{vid}/ja").json(), list))
    test("動画別フレーズ取得",    lambda: isinstance(get(f"/api/video/{vid}/phrases").json(), list))
    test("コメント一覧取得",      lambda: isinstance(get(f"/api/video/{vid}/comments").json(), list))

# ─── ネットワーク ───────────────────────────────────────────────
print("\n【ネットワーク】")
net = get("/api/network").json()
test("ネットワーク nodes あり",  lambda: len(net.get("nodes", [])) > 0)
test("ネットワーク links あり",  lambda: isinstance(net.get("links", []), list))

# ─── フレーズ登録・削除 ─────────────────────────────────────────
print("\n【フレーズCRUD】")
r = post("/api/phrases", {"phrase_en": "_test_word_", "phrase_ja": "テスト", "note": ""})
test("フレーズ登録",             lambda: r.json().get("id") is not None)
new_id = r.json().get("id")
if new_id:
    test("お気に入りトグル",     lambda: post(f"/api/phrases/{new_id}/toggle_top").json().get("success") is True)
    test("フレーズ削除",         lambda: requests.delete(f"{BASE}/api/phrases/{new_id}", timeout=10).json().get("success") is True)

# ─── 解説キャッシュ ─────────────────────────────────────────────
print("\n【解説】")
r = post("/api/explain", {"text": "blessing in disguise", "context": "", "model": "anthropic/claude-3-haiku"})
test("解説APIレスポンス形式",    lambda: any(k in r.json() for k in ("definition", "etymology", "eiken_note", "error")))

# ─── 単語帳・フレーズ帳 判定ロジック（JS側と同等をPythonで検証）──
print("\n【単語帳/フレーズ帳 分類】")
import re
def is_word(phrase_en):
    clean = re.sub(r'[*_`]', '', phrase_en).strip()
    return len(clean.split()) <= 2

phrases_data = get("/api/phrases?per_page=200").json().get("phrases", [])
words   = [p for p in phrases_data if is_word(p["phrase_en"])]
phrases = [p for p in phrases_data if not is_word(p["phrase_en"])]
test("単語帳に1語の単語が含まれる",   lambda: len(words) > 0)
test("フレーズ帳に複数語が含まれる",  lambda: len(phrases) > 0)
test("単語の例: blessing",           lambda: any("blessing" in p["phrase_en"].lower() for p in words + phrases))

# ─── スライディングウィンドウ重複除去（Python版）──────────────────
print("\n【字幕重複除去ロジック】")
sample = [
    {"start": 1.0, "end": 1.5, "text": "誰?"},
    {"start": 1.5, "end": 2.5, "text": "誰? あ、いらっしゃい。"},
    {"start": 2.5, "end": 3.5, "text": "あ、いらっしゃい。"},
]
def dedupe(subs):
    result = []
    for i, cur in enumerate(subs):
        if not cur["text"].strip(): continue
        nxt = subs[i+1] if i+1 < len(subs) else None
        if nxt and (nxt["start"] - cur["start"]) < 2.5 and nxt["text"].find(cur["text"]) >= 0:
            continue
        if result:
            prev = result[-1]
            dt = cur["start"] - prev["start"]
            if cur["text"] == prev["text"]: continue
            if dt < 2.5:
                if cur["text"].find(prev["text"]) >= 0: result[-1] = cur; continue
                if prev["text"].find(cur["text"]) >= 0: continue
        result.append(cur)
    return result

deduped = dedupe(sample)
test("重複除去: 3→1エントリに",       lambda: len(deduped) == 1)
test("重複除去: 最長エントリが残る",   lambda: deduped[0]["text"] == "誰? あ、いらっしゃい。")

# ─── 結果 ──────────────────────────────────────────────────────
print(f"\n{'─'*40}")
total = len([t for t in dir() if t.startswith('test')])
if errors:
    print(f"\033[91m❌ {len(errors)}件 失敗:\033[0m")
    for e in errors: print(f"  - {e}")
    sys.exit(1)
else:
    print(f"\033[92m✅ 全テスト通過\033[0m")
