#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ワンピースカードゲーム — 公式サイトとまとめサイトから情報を取得する。

取得元
  1. ONE PIECEカードゲーム公式 PRODUCTS  … 発売日・希望小売価格・商品URL
     https://www.onepiece-cardgame.com/products/
     一覧ページに商品名が入っていないため、個別ページの <title> から取る。
  2. まとめサイト（robots.txt で記事ページの取得が許可されているもの）
     nyuka-now.com          … /wp-admin/ /campaign/ のみ拒否
     premium.gamepedia.jp   … /wp-admin/ のみ拒否
     ※ cardchusen.com は robots.txt と利用規約で機械的取得を明確に禁止しているため使わない。

公式サイトに抽選情報は無い（「抽選」の語が0件）。
そのため抽選の期間・店舗はすべてまとめサイト由来になる。ポケカ版より確度は低い。

出力
  data_onepiece.json （data.json と同じ形式）
"""

import json
import os
import re
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

WEB_DIR = os.environ.get("WEB_DIR", ".")
OUT = os.path.join(WEB_DIR, "data_onepiece.json")
STATE = os.path.join(WEB_DIR, ".state_onepiece.json")
STORES = os.environ.get("STORES_OP",
                        os.path.join(os.path.dirname(os.path.abspath(__file__)), "stores_onepiece.json"))

JST = timezone(timedelta(hours=9))
UA = {"User-Agent": "onepiece-calendar/1.0 (personal, 2 req/day)"}

PRODUCT_LIST = "https://www.onepiece-cardgame.com/products/"
AGG_SOURCES = [
    "https://premium.gamepedia.jp/toreca/archives/24301",
    "https://nyuka-now.com/archives/97393",
]

# 商品URL・発売日・価格が1件ずつ順番に並ぶので、その順序を使って対応させる
ITEM_RE = re.compile(
    r'href="(?P<url>https://www\.onepiece-cardgame\.com/products/[^"]+)"'
    r'.*?datetime="(?P<date>\d{4}-\d{2}-\d{2})"'
    r'.*?希望小売価格</span><span class="data">(?P<price>[0-9,]+)円',
    re.S)
TITLE_RE = re.compile(r"<title>([^<]+)</title>", re.I)

KIND_RULES = [
    ("ブースターパック", "ブースターパック"),
    ("エクストラブースター", "エクストラブースター"),
    ("スタートデッキ", "スタートデッキ"),
    ("プレミアムブースター", "プレミアムブースター"),
    ("カードケース", "サプライ"),
    ("スリーブ", "サプライ"),
    ("プレイマット", "サプライ"),
    ("カードコレクション", "特別セット"),
]


def log(m):
    print(f"[{datetime.now(JST):%Y-%m-%d %H:%M}] {m}", flush=True)


def get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read().decode("utf-8", "replace")


def guess_kind(name):
    for word, kind in KIND_RULES:
        if word in name:
            return kind
    return "その他"


def clean_title(t):
    """『ブースターパック 世界最強の戦士 − PRODUCTS｜ONE PIECEカードゲーム公式サイト｜ワンピース』
    から商品名だけを取り出す"""
    t = re.split(r"[−–—|｜]", t)[0]
    return re.sub(r"\s+", " ", t).strip()


def parse_releases(html, fetch_titles=True):
    out, seen = [], set()
    for m in ITEM_RE.finditer(html):
        url, date = m.group("url"), m.group("date")
        if url in seen:
            continue
        seen.add(url)

        name = ""
        if fetch_titles:
            try:
                tm = TITLE_RE.search(get(url, timeout=15))
                if tm:
                    name = clean_title(tm.group(1))
            except Exception as e:
                log(f"商品ページを開けず: {url} {e}")
        if not name:
            # 個別ページが取れなければURLの末尾を仮の名前にする
            name = url.rstrip("/").split("/")[-1].replace(".html", "").upper()

        out.append({
            "series": "ワンピースカード",
            "date": date,
            "name": name,
            "kind": guess_kind(name),
            "price": int(m.group("price").replace(",", "")),
            "note": "",
            "resale": None,
            "official": url,
        })
    out.sort(key=lambda r: (r["date"], r["name"]))
    return out


# ---------------------------------------- まとめサイトの表形式から抽選情報を読む
# 実物の構造（premium.gamepedia.jp）
#   一覧部分            詳細部分
#     麦わらストア         抽選開始日時
#     抽選                 8/14(金) 0:00
#     受付中               抽選終了日時
#     8/16(日) 23:59       8/16(日) 23:59
#                          抽選結果発表
#                          8/17(月)~8/19(水)中予定
#                          購入期間
# 項目ごとに改行されているので、ラベルの次の行を値として読む。
# 「抽選終了日時」の次の行だけを締切として扱えば、結果発表や購入期間と混ざらない。

DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})(?:\([^)]*\))?\s*(?:(\d{1,2}):(\d{2}))?")
STATE_WORDS = ("受付中", "受付終了", "受付前", "予定", "終了")


def lines_of(html):
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<[^>]*>", "\n", t)
    out = []
    for ln in t.split("\n"):
        ln = ln.replace("\u3000", " ").strip()
        if ln:
            out.append(ln)
    return out


def to_dt(m, today, default_h, default_mi):
    try:
        mo, d = int(m.group(1)), int(m.group(2))
        h = int(m.group(3)) if m.group(3) else default_h
        mi = int(m.group(4)) if m.group(4) else default_mi
        y = today.year
        if mo < today.month - 6:
            y += 1
        elif mo > today.month + 6:
            y -= 1
        return datetime(y, mo, d, h, mi, tzinfo=JST)
    except (ValueError, TypeError):
        return None


def parse_table(lines, names, today):
    """ラベルの次の行を値として読む。{店舗名: {start, end, state}} を返す"""
    found = {}
    cur = None
    for i, ln in enumerate(lines):
        hit = next((n for n in names if n and n in ln and len(ln) <= len(n) + 12), None)
        if hit:
            cur = found.setdefault(hit, {})
            continue
        if cur is None:      # 空の辞書は偽になるので None かどうかで判定する
            continue
        nxt = lines[i + 1] if i + 1 < len(lines) else ""
        if "抽選開始日時" in ln or "応募開始" in ln:
            m = DATE_RE.search(nxt)
            if m:
                cur["start"] = to_dt(m, today, 0, 0)
        elif "抽選終了日時" in ln or "応募終了" in ln or "受付終了日時" in ln:
            m = DATE_RE.search(nxt)
            if m:
                cur["end"] = to_dt(m, today, 23, 59)
        elif ln in STATE_WORDS:
            cur["state"] = ln
        # 「抽選結果発表」「購入期間」の値は読まない（締切と混同しないため）
    return {k: v for k, v in found.items() if v.get("start") or v.get("end")}


def scan_tables(names):
    today = datetime.now(JST)
    merged = {}
    for url in AGG_SOURCES:
        try:
            lines = lines_of(get(url))
        except Exception as e:
            log(f"取得失敗 {url}: {e}")
            continue
        got = parse_table(lines, names, today)
        for k, v in got.items():
            v["source"] = url
            if k not in merged or (v.get("end") and not merged[k].get("end")):
                merged[k] = v
        log(f"{len(got)}店舗の期間を検出: {url.split('/')[2]}")
    return merged


def main():
    os.makedirs(WEB_DIR, exist_ok=True)
    prev = {}
    if os.path.exists(OUT):
        try:
            prev = json.load(open(OUT, encoding="utf-8"))
        except Exception:
            pass

    problems = []
    try:
        releases = parse_releases(get(PRODUCT_LIST))
        log(f"発売情報 {len(releases)}件")
    except Exception as e:
        releases = []
        problems.append(f"商品情報の取得に失敗: {e}")

    if not releases:
        releases = prev.get("releases", [])
        problems.append("商品情報が0件。公式サイトの構造が変わった可能性")

    # 抽選はまとめサイトのみ。店舗マスタの名前で表を読む
    lottery = []
    try:
        master = json.load(open(STORES, encoding="utf-8")).get("stores", [])
    except Exception as e:
        master = []
        problems.append(f"店舗マスタを読めず: {e}")

    names = [s["where"] for s in master] + ["ふるいち(古本市場)", "プレミアムバンダイ(2回目)",
                                            "イオン九州", "イオン北海道", "麦わらストア"]
    try:
        found = scan_tables(names)
        log(f"期間が読めた店舗 {len(found)}件")
    except Exception as e:
        found = {}
        problems.append(f"抽選情報の取得に失敗: {e}")

    by = {s["where"]: s for s in master}
    for name, info in found.items():
        base = by.get(name) or by.get(name.split("(")[0]) or {}
        st, en = info.get("start"), info.get("end")
        lottery.append({
            "series": "ワンピースカード",
            "start": st.strftime("%Y-%m-%dT%H:%M+09:00") if st else "",
            "end": en.strftime("%Y-%m-%dT%H:%M+09:00") if en else "",
            "item": "ワンピースカード 関連商品",
            "where": name,
            "cat": base.get("cat", ""),
            "how": base.get("how", ""),
            "prep": base.get("prep", ""),
            "lead": base.get("lead", "未確認"),
            "url": "", "home": base.get("home", ""), "info": info.get("source", ""),
            "note": base.get("note", ""),
            "state": "" if en else "要確認",
        })

    if not lottery:
        lottery = prev.get("lottery", [])

    # 期間が読めなかった店舗も、応募方法だけ出せるよう行を足す
    have = {r["where"] for r in lottery}
    for s_ in master:
        if s_["where"] in have or any(s_["where"] in h for h in have):
            continue
        lottery.append({
            "series": "ワンピースカード", "start": "", "end": "",
            "item": "ワンピースカード 関連商品", "where": s_["where"],
            "cat": s_.get("cat", ""), "how": s_.get("how", ""),
            "prep": s_.get("prep", ""), "lead": s_.get("lead", "未確認"),
            "url": "", "home": s_.get("home", ""), "info": "",
            "note": s_.get("note", ""), "state": "要確認",
        })
    log(f"店舗マスタを反映（全{len(lottery)}件）")

    if not releases and not lottery:
        sys.exit("両方0件のため中止")

    data = {
        "updated": datetime.now(JST).strftime("%Y-%m-%d"),
        "notice": "掲載情報は公式サイトと各まとめサイトから自動取得したものです。"
                  "誤りが含まれる場合があります。応募前に必ず各店舗の公式発表をご確認ください。",
        "lottery": lottery,
        "releases": releases,
    }

    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, OUT)
    log(f"{OUT} 更新 抽選{len(lottery)}件 商品{len(releases)}件")

    for p in problems:
        log("要確認: " + p)


if __name__ == "__main__":
    main()
