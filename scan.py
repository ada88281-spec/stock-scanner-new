import os, sys, pandas as pd, akshare as ak, requests, datetime

MODE = sys.argv[1] if len(sys.argv) > 1 else "scan"
TOKEN = os.environ["PUSHPLUS_TOKEN"]

# 读白名单（空=全市场）
if os.path.exists("watchlist.csv"):
    wl = pd.read_csv("watchlist.csv")
    white = wl["code"].astype(str).str.zfill(6).tolist() if "code" in wl.columns else []
else:
    white = []

# 拉全市场快照
print("拉取全市场A股...")
spot = ak.stock_zh_a_spot_em()  # 返回 代码/名称/最新价/换手率/量比 等 [4](@ref)
spot = spot[["代码","名称","最新价","换手率","量比","成交额"]].copy()
spot["代码"] = spot["代码"].astype(str).str.zfill(6)
if white:
    spot = spot[spot["代码"].isin(white)]

# 六维极简过滤（你原来的打分逻辑留着，这里先跑通）
spot["得分"] = 60
spot.loc[spot["量比"].astype(float) >= 1.5, "得分"] += 10
spot.loc[spot["换手率"].astype(float).between(3,15), "得分"] += 10
spot.loc[spot["成交额"].astype(float) >= 1e8, "得分"] += 10
ok = spot[spot["得分"] >= 70].sort_values("得分", ascending=False)

# 输出
ok.to_csv("final_list.csv", index=False, encoding="utf-8-sig")

# 推微信
title = f"[{MODE}] 选股结果 {datetime.datetime.now().strftime('%m-%d %H:%M')}"
content = ok.head(20).to_markdown(index=False) if not ok.empty else "今日无符合条件股票"
requests.post("http://www.pushplus.plus/send", json={
    "token": TOKEN,
    "title": title,
    "content": content,
    "template": "markdown"
}).raise_for_status()
print("推送成功", len(ok))
