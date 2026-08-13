#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
まとめサイトから店舗名・応募期間・応募方法を検出する。

対象は robots.txt で記事ページの取得が許可されているサイトのみ。
2026-08-13 時点で確認:
  premium.gamepedia.jp       … /wp-admin/ のみ拒否
  pokemon-infomation.com     … /wp-admin/ のみ拒否
  osomatsusan.hatenablog.com … /api/ /draft/ /preview のみ拒否
  gamenv.net は User-agent:* に拒否指定があるため対象外。

取得できる精度には限界がある。
  ・年が書かれていない期間が多いので、今の月から前後で推定する
  ・書式が統一されていないため、読めない店舗は期間なしになる
  ・まとめサイト側が古い情報を放置していれば、それをそのまま拾う
"""

import json
import os
import re
import urllib.request
from datetime import datetime, timedelta, timezone

JST = timezone(timedelta(hours=9))
UA = {"User-Agent": "pokeca-calendar/1.0 (personal, 2 req/day)"}

SOURCES = [
    "https://premium.gamepedia.jp/pokeca/archives/23226",
    "https://pokemon-infomation.com/latest-information-pokemoncard-schedule2023/",
    "https://osomatsusan.hatenablog.com/entry/pokemoncard30thCELEBRATION",
]

STORE_ALIASES = {
    "ポケモンセンターオンライン": ["ポケモンセンターオンライン", "ポケセンオンライン", "ポケセン"],
    "Amazon": ["Amazon", "アマゾン"],
    "楽天ブックス": ["楽天ブックス"],
    "楽天市場": ["楽天市場"],
    "Yahoo!ショッピング": ["Yahoo!ショッピング", "ヤフーショッピング"],
    "セブンネットショッピング": ["セブンネット"],
    "ヤマダ電機": ["ヤマダ電機", "ヤマダデンキ", "ヤマダウェブコム"],
    "ヨドバシカメラ": ["ヨドバシ"],
    "エディオン": ["エディオン"],
    "ビックカメラ": ["ビックカメラ"],
    "コジマ": ["コジマ"],
    "ジョーシン": ["ジョーシン", "上新電機"],
    "ノジマオンライン": ["ノジマ"],
    "イトーヨーカドー": ["イトーヨーカドー"],
    "ファミマオンライン": ["ファミマオンライン", "ファミリーマート"],
    "HMV": ["HMV"],
    "ローソン": ["ローソン"],
    "ドン・キホーテ": ["ドン・キホーテ", "ドンキホーテ", "ドンキ"],
    "イオン": ["イオン北海道", "iAEON", "イオンスタイル", "イオン"],
    "平和堂": ["平和堂"],
    "トイザらス": ["トイザらス", "トイザラス"],
    "あみあみ": ["あみあみ"],
    "駿河屋": ["駿河屋"],
    "キデイランド": ["キデイランド", "キディランド"],
    "ゲオ": ["ゲオ", "GEO"],
    "TSUTAYA": ["TSUTAYA"],
    "ブックオフ": ["ブックオフ"],
    "古本市場": ["古本市場", "ふるいち"],
    "三洋堂書店": ["三洋堂"],
    "ホビーステーション": ["ホビーステーション", "ホビステ"],
    "イエローサブマリン": ["イエローサブマリン"],
    "ホビーゾーン": ["ホビーゾーン"],
    "カードラボ": ["カードラボ"],
    "トレカキャピタル": ["トレカキャピタル"],
    "お宝創庫": ["お宝創庫"],
    "シーガル": ["シーガル"],
    "アニメイト": ["アニメイト"],
    "トレカプラザ55": ["トレカプラザ"],
    "おもちゃのペリカン": ["ペリカン"],
    "ゲームアーク": ["ゲームアーク"],
}

CONTEXT = re.compile(r"抽選|応募|受付|予約")
BEFORE, AFTER = 40, 220      # 店舗名の前後をどこまで読むか

# 6/30(火)〜7/15(水)23:59 / 8月10日（月）12時00分～8月14日（金）16時59分 / 7月2日 ～ 2026年7月7日 13時59分
_D = r"(?:(\d{4})年)?\s*(\d{1,2})\s*[月/]\s*(\d{1,2})\s*日?\s*(?:[（(][^）)]{0,4}[）)])?"
_T = r"(?:\s*(\d{1,2})\s*[時:：]\s*(\d{1,2})\s*分?)?"
RANGE_RE = re.compile(_D + _T + r"\s*(?:[〜～~]|から|-|ー)\s*" + _D + _T)
ONLY_END_RE = re.compile(_D + _T + r"\s*(?:まで|締切|〆)")
ONLY_START_RE = re.compile(_D + _T + r"\s*(?:から|〜|～|~|開始|より)")

# 応募方法として使いたい断片
HOW_RE = re.compile(
    r"(アプリ[^。、\n]{0,40}|店頭[^。、\n]{0,30}|QR[^。、\n]{0,30}|"
    r"LivePocket[^。、\n]{0,20}|モバイル会員[^。、\n]{0,20}|"
    r"会員[^。、\n]{0,20}|購入履歴[^。、\n]{0,30}|本人認証[^。、\n]{0,20})")


def log(m):
    print(f"[{datetime.now(JST):%Y-%m-%d %H:%M}] {m}", flush=True)


def get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read().decode("utf-8", "replace")


def plain_text(html):
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<[^>]*>", " ", t)
    t = t.replace("\u3000", " ")
    return re.sub(r"[ \t]+", " ", t)


def guess_year(month, today):
    y = today.year
    if month < today.month - 6:
        y += 1
    elif month > today.month + 6:
        y -= 1
    return y


def _mk(y, mo, dy, h, mi, today, default_h, default_mi):
    try:
        return datetime(int(y) if y else guess_year(int(mo), today), int(mo), int(dy),
                        int(h) if h else default_h, int(mi) if mi else default_mi, tzinfo=JST)
    except (ValueError, TypeError):
        return None


# 日付の近くにこの語があれば応募期間ではないので候補から外す
EXCLUDE_NEAR = re.compile(r"条件|履歴|購入|注文|受取|発表|当選|発売|入荷|支払|お届け|配送|次弾|予約開始")
# 除外語を見る範囲。前は広め（「当選発表 8月21日」を捕まえる）、
# 後ろは狭め（「6/30〜7/15まで アプリ…（条件：」の条件まで拾ってしまうため）
EXCLUDE_BEFORE = 14
EXCLUDE_AFTER = 8
# 片側だけの日付は誤検出しやすいので、後ろを広めに見る
EXCLUDE_AFTER_SINGLE = 30


def _mk(y, mo, dy, h, mi, today, default_h, default_mi):
    try:
        return datetime(int(y) if y else guess_year(int(mo), today), int(mo), int(dy),
                        int(h) if h else default_h, int(mi) if mi else default_mi, tzinfo=JST)
    except (ValueError, TypeError):
        return None


def _excluded(window, m, after=EXCLUDE_AFTER):
    """日付の前後に条件・発表・発売などの語があれば応募期間ではない"""
    around = window[max(0, m.start() - EXCLUDE_BEFORE): m.end() + after]
    return bool(EXCLUDE_NEAR.search(around))


def extract_period(window, today):
    """(開始, 締切) を返す。片方だけ読めた場合は他方を None にする。
    条件期間や発表日を拾わないよう、近くの語で候補を除外する。
    まとめサイトは1行に複数の日付を詰め込むので、完全には分離できない。"""
    for m in RANGE_RE.finditer(window):
        if _excluded(window, m):
            continue
        y1, m1, d1, h1, mi1, y2, m2, d2, h2, mi2 = m.groups()
        start = _mk(y1, m1, d1, h1, mi1, today, 0, 0)
        end = _mk(y2, m2, d2, h2, mi2, today, 23, 59)
        if not (start and end) or end < start or (end - start).days > 90:
            continue
        return (start, end)

    for m in ONLY_END_RE.finditer(window):
        if _excluded(window, m, EXCLUDE_AFTER_SINGLE):
            continue
        e = _mk(*m.groups(), today, 23, 59)
        if e:
            return (None, e)

    for m in ONLY_START_RE.finditer(window):
        if _excluded(window, m, EXCLUDE_AFTER_SINGLE):
            continue
        st = _mk(*m.groups(), today, 0, 0)
        if st:
            return (st, None)
    return None


def extract_how(window):
    """応募方法らしい断片を拾って1行にする"""
    parts = []
    for m in HOW_RE.finditer(window):
        p = re.sub(r"\s+", " ", m.group(1)).strip(" 　・:：")
        if p and p not in parts:
            parts.append(p)
        if len(parts) >= 3:
            break
    return "／".join(parts)[:90]


# 全店舗名をまとめた検索用。窓の終わりを「次の店舗名が出るまで」にするために使う
ANY_STORE = re.compile("|".join(re.escape(a) for aliases in STORE_ALIASES.values()
                                for a in sorted(aliases, key=len, reverse=True)))


def window_of(text, start, end):
    """店舗名から、次の店舗名か改行までを1件分の記述として切り出す。
    まとめサイトは1行1店舗の箇条書きなので、ここを区切らないと
    隣の店舗の期間を自分のものとして拾ってしまう。"""
    limit = min(len(text), end + AFTER)

    nl = text.find("\n", end)
    if nl != -1:
        limit = min(limit, nl)

    nxt = ANY_STORE.search(text, end)
    if nxt and nxt.start() < limit:
        limit = nxt.start()

    return text[start:limit]


def detect(text, today):
    """{正式店舗名: {period, how, raw}} を返す"""
    found = {}
    for official, aliases in STORE_ALIASES.items():
        for alias in aliases:
            for m in re.finditer(re.escape(alias), text):
                window = window_of(text, m.start(), m.end())
                # 文脈語は行全体で見る（「あみあみトップ」のように
                # 店舗名の直後に抽選という語が来ない書き方があるため）
                ls = text.rfind("\n", 0, m.start()) + 1
                le = text.find("\n", m.end())
                line = text[ls: le if le != -1 else len(text)]
                if not CONTEXT.search(line):
                    continue
                period = extract_period(window, today)
                how = extract_how(window)
                cur = found.get(official)
                if cur is None or (period and not cur["period"]):
                    found[official] = {"period": period, "how": how,
                                       "raw": re.sub(r"\s+", " ", window)[:180]}
                elif cur and not cur["how"] and how:
                    cur["how"] = how
            if official in found and found[official]["period"]:
                break
    return found


def scan():
    today = datetime.now(JST)
    merged = {}
    for url in SOURCES:
        try:
            text = plain_text(get(url))
        except Exception as e:
            log(f"取得失敗 {url}: {e}")
            continue
        hits = detect(text, today)
        for name, info in hits.items():
            info["source"] = url
            cur = merged.get(name)
            if cur is None or (info["period"] and not cur["period"]):
                merged[name] = info
        log(f"{len(hits)}店舗を検出: {url.split('/')[2]}")

    withp = sum(1 for v in merged.values() if v["period"])
    log(f"合計 {len(merged)}店舗（期間が読めたもの {withp}件）")
    return merged


if __name__ == "__main__":
    res = scan()
    print()
    for name, info in sorted(res.items()):
        if info["period"]:
            s, e = info["period"]
            p = (f'{s:%m/%d %H:%M}' if s else "  開始不明  ") + " → " + (f'{e:%m/%d %H:%M}' if e else "  締切不明  ")
        else:
            p = "        期間なし        "
        print(f"  {name:22} | {p} | {info['how'][:40]}")
