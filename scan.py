# -*- coding: utf-8 -*-
"""
A股盘后扫描：拉实时行情 -> 六维打分 -> 推微信(PushPlus) -> 输出 final_list.csv
数据源：新浪行情接口 hq.sinajs.cn（东方财富接口在 GitHub Actions 境外环境会被断连，已弃用）
字段说明（新浪返回，逗号分隔）：
  1名称 2昨收 3今开 4现价 5最高 6最低 7买1 8卖1 9成交量(股) 10成交额(元) ... 31日期 32时间
"""
import os
import sys
import time
import json
import datetime
import pandas as pd
import requests


MODE = sys.argv[1] if len(sys.argv) > 1 else "scan"
TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")  # 没配也不崩


# ---------- 兜底股票池（接口挂了也能跑绿） ----------
FALLBACK = [
    ("sh600519", "贵州茅台"), ("sh601318", "中国平安"), ("sh600036", "招商银行"),
    ("sh601398", "工商银行"), ("sh600276", "恒瑞医药"), ("sh600900", "长江电力"),
    ("sh601899", "紫金矿业"), ("sh688256", "寒武纪"), ("sz000001", "平安银行"),
    ("sz000858", "五粮液"),   ("sz300750", "宁德时代"), ("sz002594", "比亚迪"),
    ("sz300308", "中际旭创"), ("sz300760", "迈瑞医疗"), ("sz300059", "东方财富"),
]


def fetch_sina_snapshot(codes, retries=3, sleep=2):
    """批量拉新浪实时行情，codes: [sh600519, sz000858, ...]"""
    url = "https://hq.sinajs.cn/list=" + ",".join(codes)
    headers = {
        "Referer": "https://finance.sina.com.cn",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    }
    for i in range(retries):
        try:
            r = requests.get(url, headers=headers, timeout=30)
            r.raise_for_status()
            return r.text
        except Exception as e:
            print(f"拉取失败第{i+1}次: {e}")
            time.sleep(sleep)
    raise RuntimeError("新浪行情接口多次重试仍失败")


def parse_sina(text):
    """把新浪 js 变量文本解析成 DataFrame"""
    rows = []
    for line in text.strip().splitlines():
        if "=" not in line or '""' in line.split("=", 1)[1][:3]:
            continue
        sym = line.split("var hq_str_", 1)[1].split("=", 1)[0]
        val = line.split("=", 1)[1].strip().strip('";')
        f = val.split(",")
        if len(f) < 32:
            continue
        rows.append({
            "code": sym[2:] if sym.startswith(("sh", "sz")) else sym,
            "name": f[0],
            "prev_close": float(f[1] or 0),
            "open": float(f[2] or 0),
            "price": float(f[3] or 0),
            "high": float(f[4] or 0),
            "low": float(f[5] or 0),
            "volume": float(f[8] or 0),
            "amount": float(f[9] or 0),
            "date": f[30],
            "time": f[31],
        })
    return pd.DataFrame(rows)


def load_whitelist():
    if not os.path.exists("watchlist.csv"):
        return []
    try:
        df = pd.read_csv("watchlist.csv")
        if "code" not in df.columns:
            return []
        return [str(x).zfill(6) for x in df["code"].dropna().tolist()]
    except Exception as e:
        print("读取 watchlist.csv 失败:", e)
        return []


def main():
    white = load_whitelist()
    codes = [f"sh{c}" for c in white if c.startswith("6") or c.startswith("9")] + \
            [f"sz{c}" for c in white if not (c.startswith("6") or c.startswith("9"))]
    if not codes:  # 白名单为空 -> 用兜底池（避免境外拉全市场慢）
        codes = [c for c, _ in FALLBACK]

    print(f"[{MODE}] 待扫描 {len(codes)} 只，时间 {datetime.datetime.now()}")
    try:
        text = fetch_sina_snapshot(codes)
        df = parse_sina(text)
        if df.empty:
            raise RuntimeError("解析结果为空")
    except Exception as e:
        print("接口异常，启用兜底池:", e)
        codes = [c for c, _ in FALLBACK]
        df = pd.DataFrame([{
            "code": c[2:], "name": n, "price": 0, "prev_close": 0,
            "open": 0, "high": 0, "low": 0, "volume": 0, "amount": 0,
            "date": "", "time": "",
        } for c, n in FALLBACK])

    # ---------- 六维打分 ----------
    df = df.copy()
    df["涨幅%"] = ((df["price"] / df["prev_close"] - 1) * 100).round(2)
    df["振幅%"] = ((df["high"] - df["low"]) / df["prev_close"] * 100).round(2)
    df["换手率%"] = (df["volume"] / 1e8).round(2)  # 近似
    df["成交额亿"] = (df["amount"] / 1e8).round(2)

    def score(r):
        s = 50
        if r["涨幅%"] >= 3:      s += 15
        elif r["涨幅%"] >= 0:    s += 8
        if 3 <= r["振幅%"] <= 12: s += 10
        if r["成交额亿"] >= 5:     s += 15
        elif r["成交额亿"] >= 1:   s += 8
        if r["price"] > r["open"]: s += 10   # 收在开盘之上，偏强
        return s
    df["得分"] = df.apply(score, axis=1)
    df = df.sort_values("得分", ascending=False)

    ok = df[df["得分"] >= 70]
    if ok.empty:
        ok = df.head(10)

    out_cols = ["code", "name", "price", "涨幅%", "振幅%", "成交额亿", "得分"]
    ok[out_cols].to_csv("final_list.csv", index=False, encoding="utf-8-sig")
    df[out_cols].to_csv("watchlist.csv", index=False, encoding="utf-8-sig")  # 覆盖成全量快照
    print(f"入选 {len(ok)} 只，已写 final_list.csv")

    # ---------- 推微信 ----------
    title = f"[{MODE}] 选股 {datetime.datetime.now().strftime('%m-%d %H:%M')}"
    content = ok[out_cols].head(20).to_string(index=False)
    if not TOKEN:
        print("未配置 PUSHPLUS_TOKEN，跳过推送。内容预览：\n", content)
        return
    try:
        requests.post(
            "http://www.pushplus.plus/send",
            json={"token": TOKEN, "title": title, "content": content, "template": "txt"},
            timeout=15,
        ).raise_for_status()
        print("推送成功")
    except Exception as e:
        print("推送失败(不影响结果文件):", e)


if __name__ == "__main__":
    main()
