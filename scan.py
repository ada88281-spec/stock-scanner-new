"""
scan.py — 选股主程序（v2：双时段推送）
====================================================================
每天两个时段自动跑，结果都推送到微信（pushplus）：

  时段一  17:30 收盘后  → 候选池（基于昨日日线，次日观察名单）
  时段二  09:35 开盘后  → 最终可执行名单（用量能二次确认，过滤掉没放量的票）

数据说明（重要，务必理解）：
  - 盘后候选池用的是"已收盘的日线"，信号可靠，是真正的选股依据
  - 盘前最终名单用的是"当日盘中快照"，量比、成交额是实时的，用来剔掉
    那些"昨天看着好、今天没资金跟进"的票
  - 两者缺一不可：只看盘后名单容易买到"放量一日游"的票；
    只看盘前快照则没有趋势依据

盘中快照数据源：akshare.stock_zh_a_spot_em（东方财富实时行情）
日线数据源：baostock（免费、无需注册、字段稳定）
====================================================================
"""

import os
import sys
import json
import time
import datetime

import baostock as bs
import pandas as pd
import numpy as np

from notifier import notify, format_watchlist_html, format_watchlist_md

# ============================================================
# 参数区（均可通过环境变量覆盖，方便云端部署改配置不用改代码）
# ============================================================
CAPITAL = float(os.getenv("CAPITAL", "200000"))          # 账户资金
RISK_PCT = float(os.getenv("RISK_PCT", "0.005"))        # 单笔最大亏损 0.5%
ATR_MULT = float(os.getenv("ATR_MULT", "2"))            # ATR 倍数
MAX_TOTAL_POS = float(os.getenv("MAX_TOTAL_POS", "0.70"))  # 总仓位上限 70%
MAX_SINGLE_POS = float(os.getenv("MAX_SINGLE_POS", "0.40"))  # 单票上限 40%
MIN_SCORE = int(os.getenv("MIN_SCORE", "55"))           # 候选池入选线（宽松）
FINAL_MIN_SCORE = int(os.getenv("FINAL_MIN_SCORE", "65"))  # 最终名单线（严格）
VOL_RATIO_MIN = float(os.getenv("VOL_RATIO_MIN", "1.5"))   # 二次确认量比门槛
TURN_MIN = float(os.getenv("TURN_MIN", "0.03"))         # 换手率下限 3%
TURN_MAX = float(os.getenv("TURN_MAX", "0.15"))         # 换手率上限 15%
AMOUNT_MIN = float(os.getenv("AMOUNT_MIN", "1"))        # 成交额下限 1 亿
ATR_PCT_MIN = float(os.getenv("ATR_PCT_MIN", "0.02"))   # ATR% 下限
ATR_PCT_MAX = float(os.getenv("ATR_PCT_MAX", "0.06"))   # ATR% 上限
TOP_N = int(os.getenv("TOP_N", "10"))                   # 推送展示前 N 只

# 数据目录（云端用 /tmp，本地用当前目录）
DATA_DIR = os.getenv("DATA_DIR", ".")
CACHE_FILE = os.path.join(DATA_DIR, "watchlist_cache.json")


def log(msg):
    """带时间戳的日志，云端日志里好排查"""
    print(f"[{datetime.datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


# ============================================================
# 交易日判断
# ============================================================
def is_trading_day(dt: datetime.datetime = None) -> bool:
    """
    A 股交易日 = 周一到周五 且 不在法定节假日里
    简化版：只判断工作日，节假日名单需要定期维护
    TODO: 接入 akshare.tool.trade_date_hist_sina 拿真实交易日历后替换此处
    """
    if dt is None:
        dt = datetime.datetime.now()
    if dt.weekday() >= 5:  # 周六日
        return False
    # 以下为大致的法定节假日，每年需更新一次
    holidays = {
        "2026-01-01", "2026-01-02", "2026-01-29", "2026-01-30",
        "2026-02-02", "2026-02-03", "2026-02-04", "2026-02-05",
        "2026-02-06", "2026-04-06", "2026-05-01", "2026-05-04",
        "2026-05-05", "2026-06-19", "2026-09-25", "2026-10-01",
        "2026-10-02", "2026-10-05", "2026-10-06", "2026-10-07",
        "2026-10-08",
    }
    return dt.strftime("%Y-%m-%d") not in holidays


# ============================================================
# 数据获取
# ============================================================
def get_all_codes() -> list:
    """
    拿全市场 A 股代码
    baostock 的 query_stock_basic 返回全量证券，我们只要：
      sh.6 上证主板   sz.0 深证主板   sz.3 创业板   sh.5 科创板(可选)
    排除指数、基金、B股
    """
    rs = bs.query_stock_basic()
    df = rs.get_data()
    if df.empty:
        log("⚠️ 获取股票列表为空，检查 baostock 登录状态")
        return []
    prefixes = ("sh.6", "sz.0", "sz.3", "sh.5")
    codes = df[df["code"].str.startswith(prefixes)]["code"].tolist()
    log(f"代码池: {len(codes)} 只")
    return codes


def fetch_day_data(code: str, start_date: str, end_date: str) -> pd.DataFrame | None:
    """拉单只股票日线，字段不足 30 根直接丢弃（新股不参与）"""
    try:
        rs = bs.query_history_k_data_plus(
            code,
            "date,open,high,low,close,volume,amount,turn",
            start_date=start_date, end_date=end_date,
            frequency="d", adjustflag="2"
        )
        df = rs.get_data()
    except Exception as e:
        log(f"拉取 {code} 失败: {e}")
        return None

    if df is None or df.empty:
        return None
    try:
        df = df.apply(pd.to_numeric, errors="coerce")
    except Exception:
        return None
    df = df.dropna(subset=["close", "high", "low", "volume"])
    if len(df) < 30:
        return None
    df["code"] = code
    return df


def fetch_spot() -> pd.DataFrame:
    """
    获取当日盘中快照（9:30 后才有数据）
    用东方财富接口，字段包含量比、换手率、成交额——这是二次确认的关键
    """
    import akshare as ak
    for attempt in range(3):
        try:
            df = ak.stock_zh_a_spot_em()
            log(f"盘中快照: {len(df)} 只")
            return df
        except Exception as e:
            log(f"快照第 {attempt + 1} 次失败: {e}")
            if attempt < 2:
                time.sleep(8)
    return pd.DataFrame()


# ============================================================
# 因子计算
# ============================================================
def add_factors(df: pd.DataFrame) -> pd.DataFrame:
    df = df.sort_values("date").copy()
    df["tr"] = np.maximum(
        df["high"] - df["low"],
        np.maximum(
            (df["high"] - df["close"].shift(1)).abs(),
            (df["low"] - df["close"].shift(1)).abs()
        )
    )
    df["atr"] = df["tr"].ewm(alpha=1 / 14, adjust=False).mean()  # Wilder 平滑
    df["atr_pct"] = df["atr"] / df["close"]
    df["ma5"] = df["close"].ewm(span=5, adjust=False).mean()
    df["ma10"] = df["close"].ewm(span=10, adjust=False).mean()
    df["ma20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["vol_ma5"] = df["volume"].rolling(5).mean()
    df["vol_ratio"] = df["volume"] / df["vol_ma5"].replace(0, np.nan)
    # MACD
    ema12 = df["close"].ewm(span=12, adjust=False).mean()
    ema26 = df["close"].ewm(span=26, adjust=False).mean()
    df["macd"] = ema12 - ema26
    df["macd_signal"] = df["macd"].ewm(span=9, adjust=False).mean()
    df["macd_hist"] = df["macd"] - df["macd_signal"]
    return df


# ============================================================
# 六维打分（100 分）
# ============================================================
def score_row(row: pd.Series) -> int:
    s = 0
    # 1. 信号强度 20
    if row["close"] > row["ma5"]: s += 7
    if row["close"] > row["ma10"]: s += 7
    if row["macd_hist"] > 0: s += 6

    # 2. 量能验证 18
    vr = row.get("vol_ratio", 0)
    if vr > 2.0: s += 18
    elif vr > 1.5: s += 14
    elif vr > 1.0: s += 8

    # 3. 均线位置 17
    if row["ma5"] > row["ma10"]: s += 9
    if row["ma10"] > row["ma20"]: s += 8

    # 4. 资金确认 17（简化为量价配合 + 收盘位置）
    if row["close"] > row["open"] and vr > 1.2: s += 10
    if row["close"] >= row["high"] * 0.98: s += 7  # 收在最高附近

    # 5. 波动适配 14（你的止损体系最看重这项）
    ap = row["atr_pct"]
    if ATR_PCT_MIN <= ap <= ATR_PCT_MAX: s += 14
    elif ap < 0.015: s += 4
    elif ap > 0.07: s += 2  # 太猛，风控要减仓

    # 6. 风险过滤 14
    turn = row.get("turn", 0)
    if 0.03 <= turn <= 0.15: s += 8
    if row["close"] > 0: s += 6  # 正常交易

    return min(s, 100)


# ============================================================
# 仓位与止损
# ============================================================
def calc_position(row: pd.Series) -> tuple:
    """返回 (可买股数, 止损价, 实际单笔风险占账户%)"""
    price = row["close"]
    atr = row["atr"]
    if atr <= 0 or price <= 0:
        return 0, 0, 0
    stop = price - ATR_MULT * atr
    per_risk = price - stop
    if per_risk <= 0:
        return 0, stop, 0
    max_loss = CAPITAL * RISK_PCT
    theory = max_loss / per_risk
    single_max = (CAPITAL * MAX_SINGLE_POS) / price
    final = min(theory, single_max)
    final = (final // 100) * 100
    risk_pct = per_risk * final / CAPITAL * 100 if final > 0 else 0
    return int(final), round(stop, 2), round(risk_pct, 3)


def to_record(row: pd.Series) -> dict:
    shares, stop, risk_pct = calc_position(row)
    return {
        "code": row["code"],
        "score": int(row["_score"]),
        "close": round(float(row["close"]), 2),
        "atr_pct": round(float(row["atr_pct"]), 4),
        "vol_ratio": round(float(row.get("vol_ratio", 0) or 0), 2),
        "turn": round(float(row.get("turn", 0) or 0), 4),
        "stop": stop,
        "shares": shares,
        "risk_pct": risk_pct,
    }


# ============================================================
# 主扫描（盘后，产出候选池）
# ============================================================
def run_scan() -> list:
    today = datetime.datetime.now()
    end_date = today.strftime("%Y-%m-%d")
    start_date = (today - datetime.timedelta(days=150)).strftime("%Y-%m-%d")

    bs.login()
    try:
        codes = get_all_codes()
        if not codes:
            return []

        total = len(codes)
        watchlist = []
        for i, code in enumerate(codes):
            df = fetch_day_data(code, start_date, end_date)
            if df is None:
                continue
            df = add_factors(df)
            latest = df.iloc[-1]
            score = score_row(latest)
            if score >= MIN_SCORE:
                latest = latest.copy()
                latest["_score"] = score
                rec = to_record(latest)
                if rec["shares"] >= 100:
                    watchlist.append(rec)
            if (i + 1) % 800 == 0:
                log(f"扫描进度 {i + 1}/{total}")

        watchlist.sort(key=lambda x: x["score"], reverse=True)
        return watchlist
    finally:
        bs.logout()


# ============================================================
# 二次确认（盘前，用量能过滤候选池）
# ============================================================
def run_confirm(cache: list) -> tuple:
    """
    输入：盘后候选池
    逻辑：拿当日盘中快照，检查量比、换手、成交额
    返回：(confirmed 最终名单, rejected 被过滤的票及原因)
    """
    if not cache:
        return [], []

    spot = fetch_spot()
    if spot.empty:
        log("⚠️ 快照为空，可能是非交易时段，直接返回候选池")
        return cache, []

    # 统一代码格式：baostock 用 sh.600519，akshare 用 600519
    cache_map = {r["code"].split(".")[-1]: r for r in cache}
    confirmed, rejected = [], []

    for _, row in spot.iterrows():
        code6 = str(row.get("代码", "")).zfill(6)
        if code6 not in cache_map:
            continue
        rec = dict(cache_map[code6])
        reason = []

        try:
            vol_ratio = float(row.get("量比", 0) or 0)
            turn = float(row.get("换手率", 0) or 0) / 100
            amount = float(row.get("成交额", 0) or 0) / 1e8  # 转亿
        except (ValueError, TypeError):
            reason.append("快照字段异常")
            rejected.append({**rec, "reason": "；".join(reason)})
            continue

        rec["vol_ratio"] = round(vol_ratio, 2)
        rec["turn"] = round(turn, 4)

        if vol_ratio < VOL_RATIO_MIN:
            reason.append(f"量比{vol_ratio:.2f}<{VOL_RATIO_MIN}")
        if not (TURN_MIN <= turn <= TURN_MAX):
            reason.append(f"换手{turn*100:.1f}%越界")
        if amount < AMOUNT_MIN:
            reason.append(f"成交额{amount:.2f}亿<{AMOUNT_MIN}亿")
        if rec["score"] < FINAL_MIN_SCORE:
            reason.append(f"分数{rec['score']}<{FINAL_MIN_SCORE}")

        if reason:
            rejected.append({**rec, "reason": "；".join(reason)})
        else:
            confirmed.append(rec)

    confirmed.sort(key=lambda x: x["score"], reverse=True)
    rejected.sort(key=lambda x: x["score"], reverse=True)
    return confirmed, rejected


# ============================================================
# 缓存（候选池存盘，供次日盘前读取）
# ============================================================
def save_cache(data: list):
    with open(CACHE_FILE, "w", encoding="utf-8") as f:
        json.dump({"updated": datetime.datetime.now().isoformat(), "data": data}, f, ensure_ascii=False)
    log(f"缓存已保存: {CACHE_FILE} ({len(data)} 只)")


def load_cache() -> list:
    if not os.path.exists(CACHE_FILE):
        return []
    with open(CACHE_FILE, "r", encoding="utf-8") as f:
        j = json.load(f)
    log(f"读取缓存: {len(j.get('data', []))} 只 (更新于 {j.get('updated', '?')})")
    return j.get("data", [])


# ============================================================
# 推送格式化
# ============================================================
def build_candidate_msg(watchlist: list) -> str:
    """盘后候选池消息（HTML）"""
    items = watchlist[:TOP_N]
    if not items:
        return "<p>今日无符合条件的标的</p>"
    lines = []
    for i, r in enumerate(items, 1):
        grade = "🟢A" if r["score"] >= 70 else ("🟡B" if r["score"] >= 55 else "⚪C")
        lines.append(
            f"{i}. {grade} <b>{r['code']}</b> "
            f"得分:<b>{r['score']}</b> "
            f"现价:{r['close']} "
            f"ATR%:{r['atr_pct']*100:.1f}% "
            f"止损:{r['stop']:.2f} "
            f"仓位:{r['shares']}股"
        )
    tail = (
        f"<br><br>共 {len(watchlist)} 只入选，展示前 {len(items)} 只"
        f"<br><br>⚠️ 此为盘后候选池，次日开盘须等量能确认后再执行"
    )
    return "<br>".join(lines) + tail


def build_final_msg(confirmed: list, rejected: list) -> str:
    """盘前最终名单（HTML）"""
    if not confirmed:
        return "<p>今日无票通过量能确认，建议空仓观望</p>"

    lines = ["<b>✅ 通过确认的票：</b>"]
    for i, r in enumerate(confirmed[:TOP_N], 1):
        lines.append(
            f"{i}. <b>{r['code']}</b> "
            f"得分:{r['score']} "
            f"量比:{r.get('vol_ratio',0):.2f} "
            f"止损:{r['stop']:.2f} "
            f"仓位:{r['shares']}股"
        )

    if rejected:
        lines.append(f"<br><b>❌ 被过滤 {len(rejected)} 只（量能不达标）：</b>")
        for r in rejected[:5]:
            lines.append(f"• {r['code']} {r.get('reason','')}")

    tail = (
        f"<br><br>可执行 {len(confirmed)} 只，展示前 {min(len(confirmed), TOP_N)} 只"
        f"<br>高开>5% 一律放弃 · 持有满5日强制清仓 · 连亏3次停手一周"
    )
    return "<br>".join(lines) + tail


# ============================================================
# 入口
# ============================================================
def main_scan():
    """盘后扫描，产出候选池并缓存"""
    if not is_trading_day():
        log("非交易日，跳过扫描")
        return
    log("🚀 开始盘后全市场扫描...")
    watchlist = run_scan()
    log(f"✅ 扫描完成，入选 {len(watchlist)} 只")
    save_cache(watchlist)

    msg = build_candidate_msg(watchlist)
    notify("盘后候选池", msg, channel="pushplus")

    pd.DataFrame(watchlist).to_csv(
        os.path.join(DATA_DIR, "watchlist.csv"), index=False, encoding="utf-8-sig"
    )
    log("📁 watchlist.csv 已保存")


def main_confirm():
    """盘前二次确认，产出最终可执行名单"""
    cache = load_cache()
    if not cache:
        log("无候选池缓存，跳过确认（可能盘后任务还没跑过）")
        notify("盘前确认", "<p>暂无候选池数据，请等待盘后扫描完成</p>", channel="pushplus")
        return

    log(f"🔍 开始量能二次确认，候选 {len(cache)} 只...")
    confirmed, rejected = run_confirm(cache)
    log(f"✅ 确认完成: 通过 {len(confirmed)} 只 / 过滤 {len(rejected)} 只")

    msg = build_final_msg(confirmed, rejected)
    notify("盘前最终名单", msg, channel="pushplus")

    pd.DataFrame(confirmed).to_csv(
        os.path.join(DATA_DIR, "final_list.csv"), index=False, encoding="utf-8-sig"
    )
    log("📁 final_list.csv 已保存")


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "scan"
    if mode == "scan":
        main_scan()
    elif mode == "confirm":
        main_confirm()
    else:
        print("用法: python scan.py [scan|confirm]")
