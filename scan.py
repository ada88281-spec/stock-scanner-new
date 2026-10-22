import os
import sys
import time
import random
import requests
import datetime
import pandas as pd

# ============ 配置 ============
MODE = sys.argv[1] if len(sys.argv) > 1 else "scan"
TOKEN = os.environ.get("PUSHPLUS_TOKEN", "")

# ============ 读 watchlist ============
white = []
if os.path.exists("watchlist.csv"):
    try:
        wl = pd.read_csv("watchlist.csv")
        if "code" in wl.columns and not wl.empty:
            white = wl["code"].astype(str).str.zfill(6).tolist()
    except Exception as e:
        print(f"读取 watchlist.csv 失败: {e}")

print(f"白名单共 {len(white)} 只")

# ============ 行情拉取（腾讯接口，境外相对最稳） ============
def get_tencent(codes):
    pref = lambda c: ("sh" if c[0] in "609" else "sz") + c
    chunks = [codes[i:i+30] for i in range(0, len(codes), 30)]
    rows = []
    for chunk in chunks:
        url = "https://qt.gtimg.cn/q=" + ",".join(pref(c) for c in chunk)
        try:
            r = requests.get(url, headers={"Referer": "https://gu.qq.com/"}, timeout=10)
            r.encoding = "gbk"
            for line in r.text.strip().split(";"):
                if "=" not in line:
                    continue
                body = line.split("=")[1].strip('"')
                if not body or len(body) < 5:
                    continue
                parts = body.split("~")
                if len(parts) < 33:
                    continue
                code_raw = parts[0].replace("sh", "").replace("sz", "")
                try:
                    price = float(parts[3]) if parts[3] else 0
                    yclose = float(parts[4]) if parts[4] else 0
                    change_pct = round((price - yclose) / yclose * 100, 2) if yclose else 0
                    amp = float(parts[8]) if parts[8] else 0
                    amount = float(parts[7]) / 1e8 if parts[7] else 0
                    rows.append([code_raw, parts[1], price, change_pct, amp, amount])
                except Exception:
                    continue
            time.sleep(random.uniform(0.3, 0.6))
        except Exception as e:
            print(f"腾讯接口异常: {e}")
    return pd.DataFrame(rows, columns=["代码", "名称", "最新价", "涨幅%", "振幅%", "成交额亿"])


# ============ 主流程 ============
print(f"开始拉取 {len(white)} 只行情...")
df = get_tencent(white)

if df.empty or df["最新价"].sum() == 0:
    print("⚠️ 腾讯接口未返回有效数据，使用 watchlist 名称兜底（价格填0）")
    df = pd.DataFrame({
        "代码": white,
        "名称": white,  # 没名字就用代码占位
        "最新价": [0] * len(white),
        "涨幅%": [0] * len(white),
        "振幅%": [0] * len(white),
        "成交额亿": [0] * len(white),
    })
    # 尝试从 watchlist 补名称
    if os.path.exists("watchlist.csv"):
        wl = pd.read_csv("watchlist.csv")
        if "name" in wl.columns:
            name_map = dict(zip(wl["code"].astype(str).str.zfill(6), wl["name"]))
            df["名称"] = df["代码"].map(name_map).fillna(df["代码"])

df["代码"] = df["代码"].astype(str).str.zfill(6)

# ============ 打分 ============
df["得分"] = 60
df.loc[df["成交额亿"] >= 3, "得分"] += 8
df.loc[df["成交额亿"] >= 10, "得分"] += 8
df.loc[df["成交额亿"] >= 30, "得分"] += 9
df.loc[abs(df["涨幅%"]) >= 1, "得分"] += 5
df.loc[abs(df["涨幅%"]) >= 3, "得分"] += 5
df.loc[df["振幅%"] >= 2, "得分"] += 5
df.loc[df["振幅%"] >= 5, "得分"] += 5
df.loc[df["最新价"] >= 5, "得分"] += 5  # 过滤仙股

ok = df[df["得分"] >= 75].sort_values("得分", ascending=False)

# ============ 输出 ============
ok.to_csv("final_list.csv", index=False, encoding="utf-8-sig")
print(f"✅ 入选 {len(ok)} 只，已输出 final_list.csv")

# ============ 推送 ============
title = f"[{MODE}] 选股结果 {datetime.datetime.now().strftime('%m-%d %H:%M')}"
content = ok.head(25).to_markdown(index=False) if not ok.empty else "今日无符合条件股票"

if TOKEN:
    try:
        r = requests.post(
            "http://www.pushplus.plus/send",
            json={
                "token": TOKEN,
                "title": title,
                "content": content,
                "template": "markdown",
            },
            timeout=10,
        )
        resp = r.json()
        if resp.get("code") == 200:
            print("✅ PushPlus 推送成功")
        else:
            print(f"⚠️ PushPlus 返回: {resp}")
    except Exception as e:
        print(f"❌ 推送失败: {e}")
else:
    print("ℹ️ 未配置 PUSHPLUS_TOKEN，跳过推送。预览:\n")
    print(content)
