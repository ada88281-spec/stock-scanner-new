"""
notifier.py — 统一推送模块
Token 只从环境变量读取，绝不写死在代码里（避免公开仓库泄露）
"""


import os
import requests
from typing import Optional


# ============================================================
# 配置：全部走环境变量
#   PUSHPLUS_TOKEN   pushplus 用户 token（必填）
#   WECOM_WEBHOOK    企业微信机器人 webhook（可选）
# ============================================================
PUSHPLUS_TOKEN = os.getenv("PUSHPLUS_TOKEN", "")
WECOM_WEBHOOK = os.getenv("WECOM_WEBHOOK", "")

# pushplus 接口地址（官方域名）
PUSHPLUS_URL = "http://www.pushplus.plus/send"
# 备用域名，主域名不通时切换
PUSHPLUS_BACKUP = "http://pushplus.hxtrip.com/send"


def _post(url: str, payload: dict, timeout: int = 15) -> dict:
    """统一 POST，返回 dict；失败抛异常由上层处理"""
    r = requests.post(url, json=payload, timeout=timeout)
    r.raise_for_status()
    return r.json()


def notify(title: str, body: str, channel: str = "pushplus") -> bool:
    """
    统一入口
    channel: pushplus | wecom | both
    """
    results = []

    if channel in ("pushplus", "both"):
        results.append(_notify_pushplus(title, body))
    if channel in ("wecom", "both"):
        results.append(_notify_wecom(title, body))

    if not results:
        print("❌ 没有可用的推送通道")
        return False
    return all(results)


def _notify_pushplus(title: str, content: str) -> bool:
    if not PUSHPLUS_TOKEN:
        print("⚠️ 环境变量 PUSHPLUS_TOKEN 未设置，跳过推送")
        return False

    payload = {
        "token": PUSHPLUS_TOKEN,
        "title": title,
        "content": content,
        "template": "html",
    }

    # 主域名优先，失败则切备用
    for url in (PUSHPLUS_URL, PUSHPLUS_BACKUP):
        try:
            resp = _post(url, payload)
            code = resp.get("code")
            if code == 200:
                print(f"✅ PushPlus 推送成功（{title}）")
                return True
            # 905 = 未实名，直接报错不用重试
            if code == 905:
                print(f"❌ PushPlus 未实名，请到官网完成实名认证（{resp}）")
                return False
            print(f"⚠️ PushPlus 返回异常 code={code}，尝试备用域名")
        except Exception as e:
            print(f"⚠️ PushPlus 请求失败（{url}）: {e}")

    print("❌ PushPlus 主备域名均失败")
    return False


def _notify_wecom(title: str, content: str) -> bool:
    if not WECOM_WEBHOOK:
        print("⚠️ 环境变量 WECOM_WEBHOOK 未设置，跳过企业微信")
        return False

    # 企业微信 markdown 不支持 <br>，用 \n 和引用语法
    md = f"## {title}\n{content}"
    try:
        resp = _post(WECOM_WEBHOOK, {
            "msgtype": "markdown",
            "markdown": {"content": md}
        })
        if resp.get("errcode") == 0:
            print(f"✅ 企业微信推送成功（{title}）")
            return True
        print(f"❌ 企业微信推送失败: {resp}")
        return False
    except Exception as e:
        print(f"❌ 企业微信请求失败: {e}")
        return False


# ============================================================
# 格式化工具
# ============================================================
def format_watchlist_html(watchlist: list, max_count: int = 10) -> str:
    """候选池 → HTML（pushplus 用）"""
    items = watchlist[:max_count]
    if not items:
        return "<p>今日无符合条件的标的</p>"

    lines = []
    for i, r in enumerate(items, 1):
        if r.get("score", 0) >= 70:
            grade = "🟢A"
        elif r.get("score", 0) >= 55:
            grade = "🟡B"
        else:
            grade = "⚪C"
        lines.append(
            f"{i}. {grade} <b>{r['code']}</b> "
            f"得分:<b>{r['score']}</b> "
            f"现价:{r.get('close', '')} "
            f"ATR%:{r.get('atr_pct', 0) * 100:.1f}% "
            f"止损:{r.get('stop', 0):.2f} "
            f"仓位:{r.get('shares', 0)}股"
        )
    return "<br>".join(lines) + f"<br><br>共 {len(watchlist)} 只入选"


def format_watchlist_md(watchlist: list, max_count: int = 10) -> str:
    """候选池 → Markdown（企业微信用）"""
    items = watchlist[:max_count]
    if not items:
        return "今日无符合条件的标的"

    lines = []
    for r in items:
        grade = "A" if r.get("score", 0) >= 70 else ("B" if r.get("score", 0) >= 55 else "C")
        lines.append(
            f"> **{r['code']}** 得分:{r['score']}({grade}) "
            f"止损:{r.get('stop', 0):.2f} 仓位:{r.get('shares', 0)}股"
        )
    lines.append(f"\n> 共 {len(watchlist)} 只入选")
    return "\n".join(lines)


if __name__ == "__main__":
    # 本地自测：python notifier.py
    test_list = [
        {"code": "sh.600519", "score": 78, "close": 1688.0, "atr_pct": 0.025, "stop": 1620.5, "shares": 100},
        {"code": "sz.000001", "score": 72, "close": 12.35, "atr_pct": 0.031, "stop": 11.82, "shares": 800},
    ]
    print("--- 测试 PushPlus ---")
    notify("测试：盘后候选池", format_watchlist_html(test_list), channel="pushplus")
    print("--- 测试企业微信 ---")
    notify("测试：盘后候选池", format_watchlist_md(test_list), channel="wecom")
