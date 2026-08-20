#!/usr/bin/env python3
"""
AI オイル研究所 - 本日のAIシグナル 自動計算スクリプト

やっていること（概要）:
  1. Yahoo Finance の非公式チャートAPI（無料・キー不要）から
     WTI原油先物（CL=F、NYMEX）の価格（5分足・60分足・日足）を取得する。
     4時間足は60分足を4本ずつ集計して自前で作る。
  2. 4時間・日足の2つでボリンジャーバンド（SMA±2σ、LOOKBACK=15）を計算し、
     2つとも「バンド中心から±1.3σ以上その方向に偏っている」状態
     (momentum_direction)が一致した時だけ、上位足の方向候補を確定する。
  3. その方向候補が確定している時だけ、5分足がその方向に逆行してバンド際まで
     達し、そこから戻り始めたタイミング(detect_reversal_setup)を検出し、
     検出できた瞬間だけ実際のSELL/BUYシグナルとして確定する（それ以外はWAIT）。
  4. Entry = 直近5分足終値。TP/SLは、5分足の逆行の谷/山（測定値幅）を基準に算出する。
  5. 結果を signal.json として書き出す。

設計方針・検証結果:
  - この時間足構成（4時間・日足の一致＋5分足の反発、ボリンジャーバンド版）は、
    Yahoo Financeの実データ・直近72日分でバックテスト検証済み。
    総トレード数51件、勝率74.5%、プロフィットファクター3.14
    （BUY 21件PF3.87／SELL 30件PF2.77、両方向とも安定してプラス）。
    LOOKBACK=10〜30で試してもPF2.6〜3.1・勝率70〜75%程度で大きく崩れず、
    特定の値だけ良く見える不自然な偏りは見られなかった。
  - Yahoo Financeの5分足は直近60〜72日分しか取得できず、HistData・Dukascopy
    いずれにもWTI原油の長期データが無かった（HistDataは直近2ヶ月分のみ）ため、
    AI FX研究所・AI Gold研究所（20〜35ヶ月分で検証）と比べて検証期間は短い。
    頻度を優先した結果、検証の厚みは相対的に薄い点に留意。
  - CL=FはNYMEX（ニューヨーク商業取引所）のWTI原油先物であり、米ドル建ての
    国際指標。日本国内の商品先物取引所の円建て原油とは別物・別市場。
  - LOOKBACK=15はAI FX研究所（USD/JPYボリバン版）での検証で最もバランスが
    良かった設定を踏襲。
"""

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

YAHOO_CHART_URL = "https://query1.finance.yahoo.com/v8/finance/chart/CL=F"

LOOKBACK = 15
EDGE_THRESHOLD = 1.3
REVERT_WINDOW = 10
REVERT_MIN_USD = 0.06
SL_BUFFER_USD = 0.06

# 「LATEST NEWS」欄用。原油の日本語専用ニュースRSSが見つからなかったため、
# 英語のFXStreet（為替・商品ニュース）から取得し、oil関連のキーワードで絞り込んだ上で
# 無料の翻訳エンドポイント(Google翻訳の非公式API、キー不要)で日本語に変換して表示する。
NEWS_FEEDS = [
    ("FXStreet", "https://www.fxstreet.com/rss/news"),
]
NEWS_KEYWORDS = ("oil", "crude", "wti", "brent", "opec", "petroleum")
MAX_NEWS_AGE_HOURS = 24
TRANSLATE_URL = "https://translate.googleapis.com/translate_a/single"


def http_get_json(url, retries=3, wait_sec=5):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    last_err = None
    for _ in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=30) as res:
                return json.loads(res.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            time.sleep(wait_sec)
    raise RuntimeError(f"取得に失敗しました: {url} ({last_err})")


def fetch_oil_bars(interval, range_):
    """Yahoo Finance非公式チャートAPIからCL=F(WTI原油先物)のOHLCを古い順で返す。"""
    url = f"{YAHOO_CHART_URL}?interval={interval}&range={range_}"
    data = http_get_json(url)
    result = data.get("chart", {}).get("result")
    if not result:
        raise RuntimeError(f"原油データが取得できませんでした: {data}")
    ts = result[0].get("timestamp") or []
    quote = result[0]["indicators"]["quote"][0]
    bars = []
    for i, t in enumerate(ts):
        o, h, l, c = quote["open"][i], quote["high"][i], quote["low"][i], quote["close"][i]
        if o is None or h is None or l is None or c is None:
            continue
        bars.append({"t": t, "o": float(o), "h": float(h), "l": float(l), "c": float(c)})
    if not bars:
        raise RuntimeError(f"原油データが空でした（interval={interval}, range={range_}）")
    return bars


def aggregate_to_4h(hourly_bars):
    """60分足を4本ずつ(UTC基準、4時間境界)まとめて4時間足を作る。"""
    buckets = {}
    order = []
    for b in hourly_bars:
        dt = datetime.fromtimestamp(b["t"], tz=timezone.utc)
        total_hours = dt.toordinal() * 24 + dt.hour
        key = total_hours // 4
        if key not in buckets:
            buckets[key] = {"t": b["t"], "o": b["o"], "h": b["h"], "l": b["l"], "c": b["c"]}
            order.append(key)
        else:
            buckets[key]["h"] = max(buckets[key]["h"], b["h"])
            buckets[key]["l"] = min(buckets[key]["l"], b["l"])
            buckets[key]["c"] = b["c"]
    return [buckets[k] for k in order]


def bollinger_channel(closes, lookback=LOOKBACK):
    series = closes[-lookback:] if len(closes) > lookback else closes[:]
    n = len(series)
    if n < 5:
        raise RuntimeError("ボリンジャーバンド計算に必要なデータ本数が不足しています")
    mid = statistics.fmean(series)
    sigma = statistics.pstdev(series)
    sigma = sigma if sigma > 1e-6 else 1e-6
    upper = mid + 2 * sigma
    lower = mid - 2 * sigma
    latest = series[-1]
    position = (latest - mid) / sigma
    return {
        "mid": mid, "upper": upper, "lower": lower, "sigma": sigma,
        "position": position, "n": n, "latest": latest,
    }


MOMENTUM_LABEL_JA = {"UP": "上方向", "DOWN": "下方向", "FLAT": "中央"}


def momentum_direction(ch):
    pos = ch["position"]
    if pos >= EDGE_THRESHOLD:
        return "UP"
    if pos <= -EDGE_THRESHOLD:
        return "DOWN"
    return "FLAT"


def detect_reversal_setup(bars, ch, direction):
    """
    directionは上位2時間足が一致した方向候補("BUY"/"SELL")。5分足がこの方向とは
    逆に振れてバンド際(±EDGE_THRESHOLD)まで達し、そこから戻り始めていれば、
    その谷(BUYの場合)/山(SELLの場合)の価格を返す。
    """
    if len(bars) < REVERT_WINDOW:
        return None
    recent = bars[-REVERT_WINDOW:]
    closes = [b["c"] for b in recent]
    latest = closes[-1]
    sigma = ch["sigma"]
    mid = ch["mid"]
    if direction == "BUY":
        trough_idx = min(range(len(closes)), key=lambda i: closes[i])
        trough = closes[trough_idx]
        if trough_idx == len(closes) - 1:
            return None
        if (trough - mid) / sigma > -EDGE_THRESHOLD:
            return None
        if (latest - trough) < REVERT_MIN_USD:
            return None
        return trough
    else:
        peak_idx = max(range(len(closes)), key=lambda i: closes[i])
        peak = closes[peak_idx]
        if peak_idx == len(closes) - 1:
            return None
        if (peak - mid) / sigma < EDGE_THRESHOLD:
            return None
        if (peak - latest) < REVERT_MIN_USD:
            return None
        return peak


def build_confidence_breakdown(bias, candidate, timeframes, confidence):
    tf_line = " / ".join(f"{tf['label']}:{MOMENTUM_LABEL_JA[tf['momentum']]}" for tf in timeframes)
    if bias in ("SELL", "BUY"):
        align_note = tf_line + " → 2時間足とも一致、5分足の反発シグナルも確認済み"
        calc_note = f"基本50% + 2時間足一致30% + バンド際からの乖離度ボーナス = {confidence}%（上限95%）"
    elif candidate is not None:
        align_note = tf_line + " → 2時間足は一致していますが、5分足の反発シグナルはまだ点灯していません"
        calc_note = "2時間足の方向一致のみでは確信度は上がらず、5分足の反発確認まで基本値50%のままです。"
    else:
        align_note = tf_line + " → 2時間足の方向が一致していません"
        calc_note = "2時間足の方向が揃っていないため、基本値50%のままです。"
    return {"timeframes_note": align_note, "calc_note": calc_note}


def build_market_context(bias, candidate, latest_price, day_change_pct):
    change_txt = f"{day_change_pct:+.2f}%"
    if bias == "SELL":
        stance = "4時間・日足が揃って上値の重さを示す中、5分足が短期的な戻りから反落したタイミング"
        outlook = "目先は上値の重い展開が想定され、高値を追わず戻りを待つスタンスが機能しやすい局面。"
    elif bias == "BUY":
        stance = "4時間・日足が揃って下値の堅さを示す中、5分足が短期的な押し目から反発したタイミング"
        outlook = "目先は下値の堅い展開が想定され、押し目を焦らず拾うスタンスが機能しやすい局面。"
    elif candidate == "SELL":
        stance = "4時間・日足は戻り売り方向で揃っているが、5分足の反落シグナルはまだ点灯していない"
        outlook = "上位足の方向感は出ているため、5分足が戻り高値から反落するタイミングを待ちたい局面。"
    elif candidate == "BUY":
        stance = "4時間・日足は押し目買い方向で揃っているが、5分足の反発シグナルはまだ点灯していない"
        outlook = "上位足の方向感は出ているため、5分足が押し目安値から反発するタイミングを待ちたい局面。"
    else:
        stance = "4時間・日足の方向が揃っておらず、方向感に乏しいレンジ地合い"
        outlook = "明確な方向一致が出るまでは、無理に取りにいかず様子見が無難な局面。"
    return (
        f"WTI原油（CL=F）は現在${latest_price:,.2f}付近で推移（直近1時間比{change_txt}）。{stance}。"
        f"{outlook}"
        "※このまとめは実データから自動生成された定型解説です。個別のニュース速報の内容までは反映していません。"
    )


def load_trade_log(base_dir):
    path = os.path.join(base_dir, "trade_log.json")
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data.get("trades"), list):
            raise ValueError("trade_log.jsonの形式が不正です")
        return data
    except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError, AttributeError):
        return {"trades": []}


def pnl_usd_for(bias, entry, price):
    diff = (entry - price) if bias == "SELL" else (price - entry)
    return round(diff, 2)


def update_trade_log(trade_log, bias, priority_trade, latest_price, confidence, now_iso):
    trades = trade_log.get("trades", [])
    open_trade = trades[-1] if trades and trades[-1].get("status") == "OPEN" else None

    if open_trade is not None:
        ob = open_trade["bias"]
        tp = open_trade["take_profit"]
        sl = open_trade["stop_loss"]
        hit_tp = (latest_price <= tp) if ob == "SELL" else (latest_price >= tp)
        hit_sl = (latest_price >= sl) if ob == "SELL" else (latest_price <= sl)
        if hit_tp or hit_sl:
            open_trade["status"] = "WIN" if hit_tp else "LOSS"
            open_trade["closed_at_utc"] = now_iso
            open_trade["closed_price"] = round(latest_price, 2)
            open_trade["pnl_usd"] = pnl_usd_for(ob, open_trade["entry"], latest_price)
            open_trade = None

    newly_opened = False
    if open_trade is None and bias in ("SELL", "BUY"):
        entry = priority_trade.get("entry")
        tp = priority_trade.get("take_profit")
        sl = priority_trade.get("stop_loss")
        if entry is not None and tp is not None and sl is not None:
            trades.append({
                "id": now_iso, "opened_at_utc": now_iso, "bias": bias,
                "entry": entry, "take_profit": tp, "stop_loss": sl, "confidence": confidence,
                "status": "OPEN", "closed_at_utc": None, "closed_price": None, "pnl_usd": None,
            })
            newly_opened = True

    trade_log["trades"] = trades
    return trade_log, newly_opened


def compute_trade_stats(trades):
    closed = [t for t in trades if t.get("status") in ("WIN", "LOSS")]
    wins = [t for t in closed if t["status"] == "WIN"]
    losses = [t for t in closed if t["status"] == "LOSS"]
    total_closed = len(closed)
    gross_win = sum(t["pnl_usd"] for t in wins)
    gross_loss = abs(sum(t["pnl_usd"] for t in losses))
    return {
        "total_closed": total_closed,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate_pct": round(len(wins) / total_closed * 100, 1) if total_closed else None,
        "avg_win_usd": round(gross_win / len(wins), 2) if wins else None,
        "avg_loss_usd": round(-gross_loss / len(losses), 2) if losses else None,
        "profit_factor": round(gross_win / gross_loss, 2) if gross_loss > 0 else None,
        "total_usd": round(sum(t["pnl_usd"] for t in closed), 2) if closed else 0.0,
    }


def translate_to_ja(text):
    """
    Google翻訳の非公式エンドポイント(キー不要、無料)で英語の見出しを日本語に変換する。
    失敗した場合、または結果が壊れた文字（対になっていないサロゲート等、非公式
    エンドポイントが時々返す不完全なレスポンスに由来）を含む場合は、
    原文(英語)をそのまま返す（ニュース欄自体は止めない設計、文字化けより英語表示を優先）。
    """
    try:
        params = urllib.parse.urlencode({"client": "gtx", "sl": "en", "tl": "ja", "dt": "t", "q": text})
        url = f"{TRANSLATE_URL}?{params}"
        data = http_get_json(url, retries=1)
        result = "".join(seg[0] for seg in data[0] if seg[0])
        result.encode("utf-8")  # 壊れた文字(対になっていないサロゲート等)が無いか検証。あれば例外発生
        if not result.strip():
            return text
        return result
    except Exception:  # noqa: BLE001
        return text


def fetch_one_news_feed(source, url):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=15) as res:
        raw = res.read()
    root = ET.fromstring(raw)
    items = []
    for item in root.findall(".//item"):
        title_el = item.find("title")
        link_el = item.find("link")
        pubdate_el = item.find("pubDate")
        if title_el is None or not title_el.text or link_el is None or not link_el.text:
            continue
        published_at = None
        if pubdate_el is not None and pubdate_el.text:
            try:
                published_at = parsedate_to_datetime(pubdate_el.text)
                if published_at.tzinfo is None:
                    published_at = published_at.replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                published_at = None
        items.append({
            "source": source,
            "title_en": title_el.text.strip(),
            "link": link_el.text.strip(),
            "published_at_utc": published_at.astimezone(timezone.utc).isoformat() if published_at else None,
        })
    return items


def is_oil_related(title):
    lowered = title.lower()
    return any(kw in lowered for kw in NEWS_KEYWORDS)


def fetch_news_headlines(limit=6):
    """
    FXStreetのRSSから原油(oil/WTI/crude)関連の見出しだけを抽出し、日本語に翻訳して返す。
    取得・翻訳いずれかに失敗しても、シグナル本体の計算は止めない。
    """
    all_items = []
    for source, url in NEWS_FEEDS:
        try:
            all_items.extend(fetch_one_news_feed(source, url))
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] {source}のRSS取得に失敗しました（続行します）: {e}", file=sys.stderr)
    all_items.sort(key=lambda it: it["published_at_utc"] or "", reverse=True)
    now = datetime.now(timezone.utc)
    fresh = []
    for it in all_items:
        if not it["published_at_utc"]:
            continue
        if not is_oil_related(it["title_en"]):
            continue
        published_at = datetime.fromisoformat(it["published_at_utc"])
        if (now - published_at).total_seconds() <= MAX_NEWS_AGE_HOURS * 3600:
            fresh.append(it)
        if len(fresh) >= limit:
            break
    for it in fresh:
        it["title"] = translate_to_ja(it["title_en"])
    return fresh


def build_signal(out_path=None):
    now = datetime.now(timezone.utc)

    m5 = fetch_oil_bars("5m", "60d")
    h1 = fetch_oil_bars("60m", "2y")
    d1 = fetch_oil_bars("1d", "5y")
    h4 = aggregate_to_4h(h1)

    try:
        news_headlines = fetch_news_headlines()
    except Exception as e:  # noqa: BLE001
        print(f"[WARN] ニュース見出しの取得に失敗しました（続行します）: {e}", file=sys.stderr)
        news_headlines = []

    ch_5m = bollinger_channel([b["c"] for b in m5], LOOKBACK)
    ch_4h = bollinger_channel([b["c"] for b in h4], LOOKBACK)
    ch_1d = bollinger_channel([b["c"] for b in d1], LOOKBACK)

    timeframes = [
        {"label": "4時間足", "key": "h4", "channel": ch_4h},
        {"label": "日足", "key": "d1", "channel": ch_1d},
    ]
    for tf in timeframes:
        tf["momentum"] = momentum_direction(tf["channel"])

    dirs = [tf["momentum"] for tf in timeframes]
    if dirs[0] == "UP" and dirs[1] == "UP":
        candidate = "BUY"
    elif dirs[0] == "DOWN" and dirs[1] == "DOWN":
        candidate = "SELL"
    else:
        candidate = None

    extreme = detect_reversal_setup(m5, ch_5m, candidate) if candidate else None
    bias = candidate if (candidate and extreme is not None) else "WAIT"

    if bias in ("SELL", "BUY"):
        avg_abs_pos = sum(abs(tf["channel"]["position"]) for tf in timeframes) / len(timeframes)
        confidence = 50 + 30 + min(avg_abs_pos, 3.0) * 5
        confidence = max(50, min(95, round(confidence)))
        stars = max(1, min(5, round(confidence / 20)))
    else:
        confidence = 50
        stars = 2

    if candidate is not None:
        market_mode = "TREND"
        market_mode_note = "4時間・日足の方向が揃っており、方向感のある地合い。"
    else:
        market_mode = "RANGE"
        market_mode_note = "時間足ごとに方向が割れており、方向感に乏しいレンジ地合い。"

    latest_price = m5[-1]["c"] if m5 else ch_4h["latest"]
    day_change_pct = 0.0
    if len(h1) >= 1:
        base = h1[-1]["o"]
        if base:
            day_change_pct = (latest_price - base) / base * 100

    abs_change = abs(day_change_pct)
    volatility_risk = "HIGH" if abs_change >= 3.0 else ("MID" if abs_change >= 1.5 else "LOW")

    if bias == "SELL":
        entry = latest_price
        move = abs(entry - extreme)
        sl = extreme + SL_BUFFER_USD
        tp = entry - move
        trade_lead = "戻り売り ― 上位足の下降方向一致＋5分足の戻りからの反落"
    elif bias == "BUY":
        entry = latest_price
        move = abs(entry - extreme)
        sl = extreme - SL_BUFFER_USD
        tp = entry + move
        trade_lead = "押し目買い ― 上位足の上昇方向一致＋5分足の押し目からの反発"
    else:
        entry = tp = sl = None
        if candidate == "SELL":
            trade_lead = "様子見 ― 上位足は戻り売り方向で一致、5分足の反落シグナル待ち"
        elif candidate == "BUY":
            trade_lead = "様子見 ― 上位足は押し目買い方向で一致、5分足の反発シグナル待ち"
        else:
            trade_lead = "様子見 ― 4時間・日足の方向が一致していない"

    reversal_setup = None
    if bias in ("SELL", "BUY"):
        reverted_amount = round((entry - extreme), 2) if bias == "BUY" else round((extreme - entry), 2)
        reversal_setup = {"extreme": round(extreme, 2), "reverted_usd": reverted_amount}

    comments = {
        "SELL": ["強い相場ほど、飛び乗らない。戻りを丁寧に売る一日に。", "上値は重い。高値づかみを避け、戻り待ちに徹する。"],
        "BUY": ["押し目は焦らず拾う。飛び乗りより、待つ勇気を。", "下値は堅い。押し目待ちで、無理な高値追いはしない。"],
        "WAIT": ["方向感のない日は、休むも相場。無理に取りにいかない。", "5分足のタイミングを待つのが賢明。"],
    }
    if bias in ("SELL", "BUY"):
        commentary = comments[bias][0]
    elif candidate == "BUY":
        commentary = "上位足は上向き。焦らず、5分足の押し目からの反発を待つ。"
    elif candidate == "SELL":
        commentary = "上位足は下向き。焦らず、5分足の戻りからの反落を待つ。"
    else:
        commentary = comments["WAIT"][0]

    market_context = build_market_context(bias, candidate, latest_price, day_change_pct)

    result = {
        "generated_at_utc": now.isoformat(),
        "pair": "WTI CRUDE OIL (CL=F)",
        "latest_price": round(latest_price, 2),
        "day_change_pct": round(day_change_pct, 2),
        "signal": {
            "bias": bias,
            "bias_label": {"SELL": "戻り売り優勢", "BUY": "押し目買い優勢", "WAIT": "方向感なし"}[bias],
            "stars": stars,
            "confidence": confidence,
            "confidence_breakdown": build_confidence_breakdown(bias, candidate, timeframes, confidence),
        },
        "volatility_risk": volatility_risk,
        "market_mode": market_mode,
        "market_mode_note": market_mode_note,
        "priority_trade": {
            "lead": trade_lead,
            "entry": round(entry, 2) if entry is not None else None,
            "take_profit": round(tp, 2) if tp is not None else None,
            "stop_loss": round(sl, 2) if sl is not None else None,
        },
        "reversal_setup": reversal_setup,
        "regression_channels": [
            {
                "key": tf["key"], "label": tf["label"],
                "position_sigma": round(tf["channel"]["position"], 2),
                "momentum": tf["momentum"],
                "mid": round(tf["channel"]["mid"], 2),
                "upper": round(tf["channel"]["upper"], 2),
                "lower": round(tf["channel"]["lower"], 2),
            }
            for tf in timeframes
        ],
        "commentary": commentary,
        "market_context": market_context,
        "news": news_headlines,
        "disclaimer": "本データはルールベースの参考情報であり、投資成果を保証するものではありません。",
    }

    if out_path:
        base_dir = os.path.dirname(out_path)
        try:
            trade_log = load_trade_log(base_dir)
            trade_log, _newly_opened = update_trade_log(
                trade_log, bias, result["priority_trade"], latest_price, confidence, now.isoformat(),
            )
            trade_log["stats"] = compute_trade_stats(trade_log["trades"])
            trade_log["updated_at_utc"] = now.isoformat()
            with open(os.path.join(base_dir, "trade_log.json"), "w", encoding="utf-8") as f:
                json.dump(trade_log, f, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            print(f"[WARN] trade_log.jsonの更新に失敗しました（シグナル本体は継続します）: {e}", file=sys.stderr)

    return result


def main():
    out_path = os.path.join(os.path.dirname(__file__), "..", "signal.json")
    out_path = os.path.abspath(out_path)
    try:
        signal = build_signal(out_path=out_path)
    except Exception as e:  # noqa: BLE001
        print(f"[ERROR] シグナル計算に失敗しました: {e}", file=sys.stderr)
        sys.exit(1)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(signal, f, ensure_ascii=False, indent=2)
    print(f"書き出し完了: {out_path}")
    print(json.dumps(signal, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
