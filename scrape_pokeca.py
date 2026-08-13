#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ポケカ抽選カレンダー — 公式サイトだけから自動生成する版
スプレッドシートを使わない。取れるのは公式が出している情報だけ。

取得元
  1. ポケカ30周年特別サイト 商品情報   … 商品名・定価・発売日・公式URL
  2. ポケモンセンターオンライン お知らせ … 抽選販売の告知と応募期間

店舗の応募方法・事前準備は stores.json（店舗マスタ）から読む。
弾が変わっても中身は変わらないので、一度書けば以後は編集不要。

取得できないもの（仕様として諦める）
  ・店舗抽選の実施有無と応募期間（アプリ内・店頭告知でWebに情報がない）
  ・メルカリ等の実売相場（規約で自動取得ができない）

cron 例（1日2回。相手のサーバーに負荷をかけないため高頻度にはしない）
  10 9,21 * * * cd /opt/pokeca && /usr/bin/python3 scrape_pokeca.py >> /var/log/pokeca.log 2>&1

環境変数
  WEB_DIR     Nginxの公開ディレクトリ（既定 /var/www/pokeca）
  LINE_TOKEN  Messaging APIのチャネルアクセストークン
  LINE_TO     送信先のユーザーIDまたはグループID
  SITE_URL    公開サイトのURL

注意
  このスクリプトは実際のHTMLを見て書いたが、公式サイトの構造が変われば動かなくなる。
  そのときは0件になり、前回のdata.jsonを維持したうえでLINEに失敗を通知する。
  通知が来たら PATTERN を直すこと。
"""

import hashlib
import json
import os
import re
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

WEB_DIR = os.environ.get("WEB_DIR", "/var/www/pokeca")
LINE_TOKEN = os.environ.get("LINE_TOKEN", "")
LINE_TO = os.environ.get("LINE_TO", "")
SITE_URL = os.environ.get("SITE_URL", "")

DATA = os.path.join(WEB_DIR, "data.json")
STATE = os.path.join(WEB_DIR, ".state.json")
JST = timezone(timedelta(hours=9))
UA = {"User-Agent": "pokeca-calendar/1.0 (personal, 1-2 req/day)"}

PRODUCT_URL = "https://www.30th.pokemon-card.com/product"
POL_NEWS_BASE = "https://www.pokemoncenter-online.com/news/?id="
# お知らせのIDは日付そのもの。一覧ページはJavaScript描画でリンクが取れないため、
# 直近の日付を新しい順に試して「抽選応募受付期間」を含むページを探す。
POL_LOOKBACK_DAYS = int(os.environ.get("LOOKBACK_DAYS", "45"))

SERIES = "30th CELEBRATION"

# ポケセン抽選の共通情報。公式が方式を変えたらここを直す
POL_PREP = "ポケモン公式プレイヤーズクラブへの登録と、マイナンバーカードでの本人認証"
POL_HOW = "マイナンバーカードで認証した人向けと、していない人向けの2つに分かれています"
POL_HOME = "https://www.pokemoncenter-online.com/"


def log(m):
    print(f"[{datetime.now(JST):%Y-%m-%d %H:%M}] {m}", flush=True)


def get(url, timeout=25):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as res:
        return res.read().decode("utf-8", "replace")


def line(text):
    if not (LINE_TOKEN and LINE_TO):
        log("LINE未設定。通知はスキップ")
        return
    payload = json.dumps({"to": LINE_TO,
                          "messages": [{"type": "text", "text": text[:4900]}]}).encode()
    req = urllib.request.Request(
        "https://api.line.me/v2/bot/message/push", data=payload,
        headers={**UA, "Content-Type": "application/json",
                 "Authorization": f"Bearer {LINE_TOKEN}"})
    try:
        with urllib.request.urlopen(req, timeout=20) as res:
            log(f"LINE送信 {res.status}")
    except Exception as e:
        log(f"LINE送信失敗: {e}")


def strip_tags(html):
    html = re.sub(r"<script.*?</script>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<style.*?</style>", " ", html, flags=re.S | re.I)
    html = re.sub(r"<[^>]+>", " ", html)
    return re.sub(r"\s+", " ", html)


# --------------------------------------------------------- 発売情報（30周年サイト）
# 実際のHTML:
#   <a href="/product/m6a"> ... 商品名 ...
#   <dt>希望小売価格</dt><dd class="...">360<span>円（税込）</span></dd>
#   <dt>発売日</dt><dd class="...">2026<span>年</span>9<span>月</span>16<span>日（水）</span></dd>
# クラス名にはビルドごとに変わるハッシュが付くので、表示文字だけを目印にする。

KIND_RULES = [
    ("拡張パック", "拡張パック"),
    ("カードセット", "カードセット"),
    ("デッキセット", "構築デッキ"),
    ("構築デッキ", "構築デッキ"),
    ("スターターセット", "スターターセット"),
    ("BOX", "特別セット"),
]


def guess_kind(name):
    for word, kind in KIND_RULES:
        if word in name:
            return kind
    return "その他"


URL_MARK = re.compile(r'<a\s[^>]*href="([^"]*?/product/[^"]+)"[^>]*>', re.I)

# タグを消した後のテキストに対して当てる。商品名は「希望小売価格」の直前にある
PRODUCT_RE = re.compile(
    r"(?P<name>[^@]{4,160}?)"
    r"希望小売価格\s*(?P<price>[0-9,]+)\s*円（税込）"
    r"[^@]{0,80}?発売日\s*(?P<y>\d{4})\s*年\s*(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日"
)


def clean_name(raw):
    """『拡張パック「A」予約受付中 拡張パック「A」』のように同じ名前が2回出るので畳む"""
    n = re.sub(r"^\s*）", " ", raw)                     # 前の商品の「日（水）」の残り
    n = re.sub(r"^\s*（[月火水木金土日]?）", " ", n)
    n = re.sub(r"ポケモンセンターオンライン(予約受付中|)", " ", n)
    n = re.sub(r"(詳細を見る|COMING SOON|CHECK)", " ", n)
    n = re.sub(r"\s+", " ", n).strip(" -・|（）")
    half = len(n) // 2
    if half > 4 and n[:half].strip() == n[half:].strip():
        n = n[:half].strip()
    # 前の商品の末尾が混ざることがあるので、区切り記号以降だけを使う
    n = re.split(r"(?:円（税込）|日（[月火水木金土日]）)", n)[-1].strip()
    return n[:120]


def parse_releases(html):
    # リンクを目印に変換してからタグを落とす。位置関係で商品とURLを結びつける
    marked = URL_MARK.sub(lambda m: f' @@{m.group(1)}@@ ', html)
    text = strip_tags(marked)

    out, seen = [], set()
    for m in PRODUCT_RE.finditer(text):
        name = clean_name(m.group("name"))
        if not name:
            continue

        # 直前に出てきたURLをこの商品のリンクとする
        marks = re.findall(r"@@(\S+?)@@", text[:m.start()])
        href = marks[-1] if marks else ""
        if href.startswith("/"):
            href = "https://www.30th.pokemon-card.com" + href

        date = f'{int(m.group("y")):04d}-{int(m.group("m")):02d}-{int(m.group("d")):02d}'
        key = (name, date)
        if key in seen:
            continue
        seen.add(key)

        out.append({
            "series": SERIES,
            "date": date,
            "name": name,
            "kind": guess_kind(name),
            "price": int(m.group("price").replace(",", "")),
            "note": "",
            "resale": None,
            "official": href,
        })
    out.sort(key=lambda r: (r["date"], r["name"]))
    return out


# ------------------------------------------------ 抽選情報（ポケセンお知らせ本文）
# 実物の構造（1ページに複数の抽選が入る）
#   【抽選販売を実施する商品】
#   ・ポケモンカードゲーム MEGA 拡張パック「30th CELEBRATION」BOX　7,200円（税込）
#   ...
#   ■<商品名>
#   ・抽選応募受付期間
#   8月10日（月）12時00分～8月14日（金）16時59分
#   ・抽選結果発表日
#   拡張パック「30th CELEBRATION」BOX：8月21日（金）13時00分以降
#   ・注文および、支払い期間
#   ...
# 「・見出し」ごとに区切り、抽選応募受付期間の節の中だけから日時を取る。
# こうしないと結果発表日や支払い期間の日付を締切と誤認する。

ITEM_RE = re.compile(r"・\s*(ポケモンカードゲーム.{5,90}?)\s*([0-9,]+)\s*円")
PERIOD_RE = re.compile(
    r"(\d{1,2})月(\d{1,2})日[^\d]{0,8}(\d{1,2})時(\d{1,2})分"
    r"\s*[～~〜-]\s*"
    r"(\d{1,2})月(\d{1,2})日[^\d]{0,8}(\d{1,2})時(\d{1,2})分")
RESULT_RE = re.compile(r"(\d{1,2})月(\d{1,2})日[^\d]{0,8}(\d{1,2})時(\d{1,2})分以降")


def lines_of(html):
    """タグを改行に置き換えて、意味のある行だけ取り出す"""
    t = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
    t = re.sub(r"<[^>]*>", "\n", t)
    out = []
    for ln in t.split("\n"):
        ln = ln.replace("\u3000", " ").strip()
        if ln:
            out.append(ln)
    return out


def guess_year(month, today):
    """年が書かれていないので今の月から前後で推定する"""
    y = today.year
    if month < today.month - 6:
        y += 1
    elif month > today.month + 6:
        y -= 1
    return y


def to_dt(month, day, hour, minute, today):
    try:
        return datetime(guess_year(month, today), month, day, hour, minute, tzinfo=JST)
    except ValueError:
        return None


def split_sections(lines):
    """「・見出し」で区切る。返り値は [(見出し, [行, ...]), ...]"""
    secs, cur, buf = [], None, []
    for ln in lines:
        if ln.startswith("・") and len(ln) <= 24 and "。" not in ln:
            if cur:
                secs.append((cur, buf))
            cur, buf = ln.lstrip("・").strip(), []
        elif cur:
            buf.append(ln)
    if cur:
        secs.append((cur, buf))
    return secs


def parse_lottery(html, url):
    lines = lines_of(html)
    today = datetime.now(JST)

    # ページ全体から商品と価格の一覧をとる（表記ゆれの吸収用）
    prices = {}
    for ln in lines:
        m = ITEM_RE.search(ln)
        if m:
            prices[re.sub(r"\s+", " ", m.group(1)).strip()] = m.group(2)

    # 「■商品名」の見出しで抽選の塊に分ける。
    # 実物では対象商品ごとに■が連続して並び、そのあと期間が1回だけ書かれる。
    #   ■BOX / ■デッキセット / ■FUTURISTIC BOX
    #   ・抽選応募受付期間 → 8月10日〜8月14日（3商品に共通）
    # そのため連続する■は1つの塊にまとめ、複数商品を持たせる。
    blocks, cur = [], None
    for ln in lines:
        if ln.startswith("■"):
            title = ln.lstrip("■").strip()
            if "応募する場合" in title:      # 応募手順の見出しは商品ではない
                cur = None
                continue
            if cur is not None and not cur["lines"]:
                cur["titles"].append(title)   # 直前も■だったので同じ塊に足す
            else:
                cur = {"titles": [title], "lines": []}
                blocks.append(cur)
        elif cur is not None:
            cur["lines"].append(ln)

    if not blocks:      # ■が無い書式なら全体を1つの塊として扱う
        blocks = [{"titles": [], "lines": lines}]

    rows = []
    for blk in blocks:
        secs = dict(split_sections(blk["lines"]))
        apply_sec = next((v for k, v in secs.items() if "抽選応募受付期間" in k), [])
        result_sec = next((v for k, v in secs.items() if "抽選結果発表" in k), [])

        period = None
        for ln in apply_sec:
            m = PERIOD_RE.search(ln)
            if m:
                g = [int(x) for x in m.groups()]
                start_dt = to_dt(g[0], g[1], g[2], g[3], today)
                end_dt = to_dt(g[4], g[5], g[6], g[7], today)
                if start_dt and end_dt:
                    period = (start_dt, end_dt)
                break
        if not period:
            continue

        # この塊が対象にしている商品を、価格一覧から名前で照合する
        heads = key_of(" ".join(blk["titles"]))
        targets = [n for n in prices if key_of(n) and key_of(n) in heads] if heads else list(prices)
        if not targets:
            targets = blk["titles"] or ["対象商品"]

        for name in targets:
            result = ""
            for ln in result_sec:
                if len(result_sec) == 1 or (key_of(name) and key_of(name) in key_of(ln)):
                    m = RESULT_RE.search(ln)
                    if m:
                        g = [int(x) for x in m.groups()]
                        d = to_dt(g[0], g[1], g[2], g[3], today)
                        if d:
                            result = d.strftime("%-m月%-d日 %H:%M以降")
                        break

            note = []
            if name in prices:
                note.append(prices[name] + "円（税込）")
            if result:
                note.append("結果発表 " + result)

            rows.append({
                "series": SERIES,
                "start": period[0].strftime("%Y-%m-%dT%H:%M+09:00"),
                "end": period[1].strftime("%Y-%m-%dT%H:%M+09:00"),
                "item": name,
                "where": "ポケモンセンターオンライン",
                "cat": "A 公式・メーカー直販",
                "how": POL_HOW,
                "prep": POL_PREP,
                "lead": "数日必要",
                "url": "",
                "home": POL_HOME,
                "info": url,
                "note": "／".join(note),
                "state": "",
            })

    # 同じ商品が複数の塊で拾われたら、締切が遅い方を残す
    best = {}
    for r in rows:
        k = r["item"]
        if k not in best or r["end"] > best[k]["end"]:
            best[k] = r
    return sorted(best.values(), key=lambda r: r["end"])


def key_of(text):
    """比較用に記号・空白・共通の接頭辞を落とす。
    見出しでは『ポケモンカードゲーム MEGA 拡張パック「A」』、
    商品一覧では『ポケモンカードゲーム MEGA「A」』と語順が違うため、
    共通部分を消してから部分一致で照合する"""
    t = re.sub(r"ポケモンカードゲーム|MEGA|拡張パック", "", text or "")
    return re.sub(r"[\s　「」『』（）()【】・：:/、,]", "", t)


# ------------------------------------------- 抽選が載っているお知らせを自分で探す
def find_lottery_news(st):
    """直近の日付IDを新しい順に試す。前回当たったIDは最初に試す。
    見つかったら残りは打ち切るので、通常のアクセスは数回で済む。"""
    ids = []
    last = st.get("news_id")
    if last:
        ids.append(last)

    today = datetime.now(JST)
    for i in range(POL_LOOKBACK_DAYS):
        d = (today - timedelta(days=i)).strftime("%Y%m%d")
        if d != last:
            ids.append(d)

    tried = 0
    for nid in ids:
        url = POL_NEWS_BASE + nid
        try:
            html = get(url, timeout=15)
        except Exception:
            continue
        tried += 1
        if "抽選応募受付期間" not in html:
            continue
        rows = parse_lottery(html, url)
        if rows:
            st["news_id"] = nid
            log(f"抽選のお知らせを発見: {nid}（{tried}回のアクセスで判明）")
            return rows, nid
    log(f"抽選のお知らせが見つからなかった（{tried}件を確認）")
    return [], None


# ------------------------------------------------------- 店舗マスタとのマージ
STORES = os.environ.get("STORES", os.path.join(os.path.dirname(os.path.abspath(__file__)), "stores.json"))


def merge_stores(lottery):
    """公式から期間が取れた店舗はそのまま。
    取れていない店舗は、店舗マスタの応募方法と事前準備だけを出す。
    実施しているとは書かない（実際にやらない回もあるため）。"""
    try:
        with open(STORES, encoding="utf-8") as f:
            master = json.load(f).get("stores", [])
    except Exception as e:
        log(f"stores.json を読めなかったので店舗情報なしで続行: {e}")
        return lottery

    have = {r["where"] for r in lottery}
    added = 0
    for st in master:
        if st["where"] in have:
            continue
        lottery.append({
            "series": SERIES,
            "start": "", "end": "",
            "item": "この商品の抽選をやる場合",
            "where": st["where"],
            "cat": st.get("cat", ""),
            "how": st.get("how", "未確認"),
            "prep": st.get("prep", "未確認"),
            "lead": st.get("lead", "未確認"),
            "url": "", "home": st.get("home", ""), "info": "",
            "note": ((st.get("note", "") + "／") if st.get("note") else "")
                    + "今回実施するかどうかと応募期間は未確認です。各店舗の告知を確認してください",
            "state": "要確認",
        })
        added += 1
    log(f"店舗マスタから{added}件を追加")
    return lottery


# ------------------------------------------------------------------------ 保存
def load_json(path, default):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
    os.replace(tmp, path)


# ------------------------------- まとめサイトから店舗・期間・応募方法を取り込む
def apply_aggregators(lottery):
    """robots.txtで許可されているまとめサイトから拾った情報を反映する。
    ・stores.json にある店舗 … 期間が未設定なら埋める
    ・stores.json に無い店舗 … 行を追加する
    公式から期間が取れている行は上書きしない（公式が優先）。"""
    try:
        import detect_stores
        found = detect_stores.scan()
    except Exception as e:
        log(f"まとめサイトの取得をスキップ: {e}")
        return lottery

    if not found:
        return lottery

    def norm(v):
        return re.sub(r"[\s　（）()／/、,・]", "", v or "")

    by_where = {}
    for r in lottery:
        by_where.setdefault(norm(r["where"]), []).append(r)

    filled = added = 0
    for name, info in found.items():
        key = norm(name)
        rows = by_where.get(key) or [r for k, v in by_where.items()
                                     for r in v if key and (key in k or k in key)]
        period = info.get("period")
        st_s = period[0].strftime("%Y-%m-%dT%H:%M+09:00") if period and period[0] else ""
        en_s = period[1].strftime("%Y-%m-%dT%H:%M+09:00") if period and period[1] else ""

        if rows:
            for r in rows:
                if r["end"]:          # 公式で締切が取れている行は触らない
                    continue
                if st_s or en_s:
                    r["start"], r["end"] = st_s, en_s
                    r["state"] = "" if en_s else "要確認"
                    filled += 1
                if info.get("how") and r.get("how") in ("", "未確認"):
                    r["how"] = info["how"]
        else:
            lottery.append({
                "series": SERIES,
                "start": st_s, "end": en_s,
                "item": "この商品の抽選をやる場合",
                "where": name,
                "cat": "",
                "how": info.get("how") or "未確認",
                "prep": "未確認",
                "lead": "未確認",
                "url": "", "home": "", "info": info.get("source", ""),
                "note": "応募方法は各店舗の告知を確認してください",
                "state": "" if en_s else "要確認",
            })
            added += 1

    log(f"まとめサイトから 期間を補完{filled}件 / 店舗を追加{added}件")
    return lottery


def main():
    os.makedirs(WEB_DIR, exist_ok=True)
    st = load_json(STATE, {})
    prev = load_json(DATA, {})
    problems = []

    try:
        releases = parse_releases(get(PRODUCT_URL))
        log(f"発売情報 {len(releases)}件")
    except Exception as e:
        releases, _ = [], problems.append(f"商品情報の取得に失敗: {e}")

    lottery = []
    try:
        lottery, found = find_lottery_news(st)
        log(f"抽選情報 {len(lottery)}件（取得元 {found or 'なし'}）")
    except Exception as e:
        problems.append(f"抽選情報の取得に失敗: {e}")

    if not releases:
        problems.append("商品情報が0件。公式サイトの構造が変わった可能性")
    if not lottery:
        problems.append("抽選情報が0件。お知らせの書式が変わった可能性")

    # 片方でも0件なら前回のデータで補い、サイトが空にならないようにする
    if not releases:
        releases = prev.get("releases", [])
    if not lottery:
        lottery = prev.get("lottery", [])

    lottery = merge_stores(lottery)

    if os.environ.get("DETECT_STORES", "1") != "0":
        lottery = apply_aggregators(lottery)

    if not releases and not lottery:
        line("【ポケカサイト】更新に失敗しました\n" + "\n".join(problems))
        sys.exit("両方0件のため中止")

    data = {
        "updated": datetime.now(JST).strftime("%Y-%m-%d"),
        "notice": "掲載情報は公式サイトと各まとめサイトから自動取得したものです。誤りが含まれる場合があります。応募前に必ず各店舗の公式発表をご確認ください。",
        "lineUrl": os.environ.get("LINE_ADD_URL", ""),
        "lottery": lottery,
        "releases": releases,
    }
    save_json(DATA, data)
    log(f"data.json 更新 抽選{len(lottery)}件 商品{len(releases)}件")

    # 新しく増えたものだけ通知する
    keys = sorted([f"L:{r['where']}|{r['item']}" for r in lottery]
                  + [f"R:{r['date']}|{r['name']}" for r in releases])
    known = set(st.get("keys", []))
    added = [k for k in keys if k not in known]
    if known and added:
        body = "\n".join("・" + k[2:].replace("|", " / ") for k in added[:10])
        line(f"【ポケカ 新着 {len(added)}件】\n\n{body}"
             + (f"\n\n{SITE_URL}" if SITE_URL else ""))
    st["keys"] = keys

    if problems:
        line("【ポケカサイト 要確認】\n" + "\n".join(problems))

    save_json(STATE, st)
    if not known:
        log("初回実行。次回から新着を通知します")


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        log(f"想定外のエラー: {e}")
        line(f"【ポケカサイト】想定外のエラー\n{e}")
        sys.exit(1)
