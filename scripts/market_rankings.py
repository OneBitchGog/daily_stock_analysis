#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场榜单脚本：涨幅榜 / 换手率榜 / 主力资金榜 / 板块榜 / 龙虎榜机构 / 涨停池 + 明日板块预测

数据源说明（已实测可用，绕开东财被封的 push2 接口）：
  - 涨幅/换手率/主力资金：腾讯全A行情 stock_zh_a_spot_tx（字段 zdf/hsl/zljlr）
  - 行业板块：同花顺 stock_board_industry_summary_ths（含净流入）
  - 龙虎榜·机构买卖：stock_lhb_jgmmtj_em（机构净买入额）
  - 涨停池：stock_zt_pool_em（连板数/行业/封板资金）
  - 明日板块预测：DeepSeek（读环境变量 DEEPSEEK_API_KEY）

用法：python scripts/market_rankings.py
可选环境变量：
  DEEPSEEK_API_KEY    必填（做明日板块预测）
  DEEPSEEK_API_MODEL  可选，默认 deepseek-chat
  FEISHU_WEBHOOK_URL  必填（推送飞书）
  RANK_TOPN           可选，默认 10（每个榜单条数）
"""
import os
import sys
import traceback
from datetime import date, timedelta

import pandas as pd
import requests


def log(msg):
    print(f"[market_rankings] {msg}", flush=True)


def fetch_tencent_spot():
    """腾讯全A行情：code/name/zdf(涨跌幅)/hsl(换手率)/lb(量比)/zljlr(主力净流入,万元)/turnover(成交额,万)"""
    import akshare as ak
    df = ak.stock_zh_a_spot_tx()
    if df is None or df.empty:
        raise RuntimeError("腾讯全A行情为空")
    # 过滤北交所（bj 开头，涨跌幅30%会干扰沪深榜单）
    if "code" in df.columns:
        df = df[~df["code"].str.startswith("bj", na=False)]
    # 腾讯返回的数值列是 str（含 "-" 占位），统一转数值
    for col in ["zdf", "hsl", "lb", "zljlr", "turnover", "zxj"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def fetch_industry():
    """同花顺行业板块（含净流入，单位元）"""
    import akshare as ak
    df = ak.stock_board_industry_summary_ths()
    if df is None or df.empty:
        raise RuntimeError("同花顺行业板块为空")
    # 净流入等数值列转数值（可能含逗号/千分位）
    for col in ["涨跌幅", "净流入"]:
        if col in df.columns:
            df[col] = (
                df[col].astype(str).str.replace(",", "").str.replace("元", "")
            )
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def fetch_lhb():
    """龙虎榜·机构买卖（近 8 天区间，取最新上榜日）"""
    import akshare as ak
    end = date.today()
    start = end - timedelta(days=8)
    df = ak.stock_lhb_jgmmtj_em(
        start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")
    )
    if df is None or df.empty:
        raise RuntimeError("龙虎榜数据为空")
    # 只保留最新上榜日
    if "上榜日期" in df.columns:
        latest = df["上榜日期"].astype(str).max()
        df = df[df["上榜日期"].astype(str) == latest]
    return df


def fetch_zt_pool():
    """涨停池：自动找最近 7 天内有数据的交易日"""
    import akshare as ak
    for i in range(7):
        d = (date.today() - timedelta(days=i)).strftime("%Y%m%d")
        try:
            df = ak.stock_zt_pool_em(date=d)
            if df is not None and not df.empty:
                log(f"涨停池数据日期: {d}, {len(df)} 条")
                return df
        except Exception:
            continue
    raise RuntimeError("涨停池数据为空")


def fmt_pct(x):
    try:
        return f"{float(x):.2f}%"
    except (TypeError, ValueError):
        return "-"


def fmt_money_wan(x):
    """万元 → 亿"""
    try:
        return f"{float(x) / 1e4:.2f}亿"
    except (TypeError, ValueError):
        return "-"


def fmt_money_yuan(x):
    """元 → 亿"""
    try:
        return f"{float(x) / 1e8:.2f}亿"
    except (TypeError, ValueError):
        return "-"


def fmt_billion(x):
    """同花顺净流入等，单位本身就是亿元，直接显示"""
    try:
        return f"{float(x):.2f}亿"
    except (TypeError, ValueError):
        return "-"


def build_markdown(topn, tencent=None, ind=None, lhb=None, zt=None, prediction=None):
    lines = []
    lines.append("📊 **每日市场榜单**")
    lines.append(f"🕐 {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    # 1. 涨幅榜（腾讯 zdf）
    if tencent is not None and "zdf" in tencent.columns:
        try:
            df = tencent.nlargest(topn, "zdf")
            lines.append(f"🚀 **今日涨幅榜 TOP{topn}**")
            lines.append("代码 | 名称 | 涨幅 | 换手率")
            lines.append("-" * 40)
            for _, r in df.iterrows():
                lines.append(
                    f"{r['code']} | {r['name']} | {fmt_pct(r['zdf'])} | {fmt_pct(r.get('hsl', 0))}"
                )
            lines.append("")
        except Exception as e:
            log(f"涨幅榜生成失败: {e}")

    # 2. 换手率榜（腾讯 hsl）
    if tencent is not None and "hsl" in tencent.columns:
        try:
            df = tencent.nlargest(topn, "hsl")
            lines.append(f"🔥 **今日换手率榜 TOP{topn}**")
            lines.append("代码 | 名称 | 换手率 | 涨幅")
            lines.append("-" * 40)
            for _, r in df.iterrows():
                lines.append(
                    f"{r['code']} | {r['name']} | {fmt_pct(r['hsl'])} | {fmt_pct(r.get('zdf', 0))}"
                )
            lines.append("")
        except Exception as e:
            log(f"换手率榜生成失败: {e}")

    # 3. 主力资金榜（腾讯 zljlr，万元）
    if tencent is not None and "zljlr" in tencent.columns:
        try:
            df = tencent.nlargest(topn, "zljlr")
            lines.append(f"💰 **今日主力净流入榜 TOP{topn}**")
            lines.append("代码 | 名称 | 主力净流入 | 涨幅")
            lines.append("-" * 40)
            for _, r in df.iterrows():
                lines.append(
                    f"{r['code']} | {r['name']} | {fmt_money_wan(r['zljlr'])} | {fmt_pct(r.get('zdf', 0))}"
                )
            lines.append("")
        except Exception as e:
            log(f"主力资金榜生成失败: {e}")

    # 4. 板块榜（同花顺，含净流入）
    if ind is not None and "板块" in ind.columns:
        try:
            df = ind.nlargest(topn, "涨跌幅")
            lines.append(f"🧭 **今日行业板块涨幅 TOP{topn}**")
            lines.append("板块 | 涨跌幅 | 净流入 | 领涨股")
            lines.append("-" * 40)
            for _, r in df.iterrows():
                leader = r.get("领涨股", "-")
                lines.append(
                    f"{r['板块']} | {fmt_pct(r['涨跌幅'])} | {fmt_billion(r.get('净流入', 0))} | {leader}"
                )
            lines.append("")
        except Exception as e:
            log(f"板块榜生成失败: {e}")

    # 5. 龙虎榜·机构净买入（机构交易量排行）
    if lhb is not None and "机构买入净额" in lhb.columns:
        try:
            # 同一股票可能因多个上榜原因重复，按代码去重保留净买入最大
            df = (
                lhb.sort_values("机构买入净额", ascending=False)
                .drop_duplicates(subset=["代码"])
            )
            df = df.nlargest(topn, "机构买入净额")
            lines.append(f"🏦 **龙虎榜·机构净买入 TOP{topn}**")
            lines.append("代码 | 名称 | 机构净买入 | 涨幅 | 上榜原因")
            lines.append("-" * 40)
            for _, r in df.iterrows():
                reason = str(r.get("上榜原因", "-"))[:20]
                lines.append(
                    f"{r['代码']} | {r['名称']} | {fmt_money_yuan(r['机构买入净额'])} | {fmt_pct(r.get('涨跌幅', 0))} | {reason}"
                )
            lines.append("")
        except Exception as e:
            log(f"龙虎榜生成失败: {e}")

    # 6. 涨停池
    if zt is not None and "名称" in zt.columns:
        try:
            df = zt.head(topn)
            if "连板数" in df.columns:
                df = df.sort_values("连板数", ascending=False)
            lines.append(f"🏆 **今日涨停池 TOP{topn}**（连板优先）")
            lines.append("代码 | 名称 | 连板 | 涨跌幅 | 行业")
            lines.append("-" * 40)
            for _, r in df.head(topn).iterrows():
                lb = r.get("连板数", "-")
                ind_name = r.get("所属行业", "-")
                lines.append(
                    f"{r['代码']} | {r['名称']} | {lb} | {fmt_pct(r.get('涨跌幅', 0))} | {ind_name}"
                )
            lines.append("")
        except Exception as e:
            log(f"涨停池生成失败: {e}")

    # 7. 明日板块预测
    if prediction:
        lines.append("🔮 **明日关注板块预测（AI）**")
        lines.append(prediction.strip())
        lines.append("")
        lines.append("⚠️ 预测仅供参考，不构成投资建议")
    return "\n".join(lines)


def predict_sectors(ind_df, api_key, model="deepseek-chat", topn=15):
    """用 DeepSeek 基于板块表现预测明日关注板块"""
    if not api_key:
        log("DEEPSEEK_API_KEY 未配置，跳过明日板块预测")
        return None
    if ind_df is None or ind_df.empty or "板块" not in ind_df.columns:
        return None
    try:
        df = ind_df.head(topn)
        lines = []
        for _, r in df.iterrows():
            lines.append(
                f"- {r['板块']}: 涨跌幅{r['涨跌幅']}% 净流入{fmt_billion(r.get('净流入', 0))} 领涨:{r.get('领涨股','-')}"
            )
        board_text = "\n".join(lines)
        prompt = (
            "你是一名资深A股板块分析师。以下是今日A股行业板块涨跌表现（按涨幅排序）：\n"
            f"{board_text}\n\n"
            "请结合板块涨幅、资金净流入、领涨股和板块强度，预测明日最可能继续走强的 3 个板块。\n"
            "要求：只输出 3 行，每行格式『板块名 | 一句话理由』，不要其他内容，不要输出序号。"
        )
        resp = requests.post(
            "https://api.deepseek.com/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.5,
                "max_tokens": 400,
            },
            timeout=90,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as e:
        log(f"明日板块预测失败: {e}")
        return None


def notify_feishu(text):
    webhook = os.environ.get("FEISHU_WEBHOOK_URL", "").strip()
    if not webhook:
        log("FEISHU_WEBHOOK_URL 未配置，无法推送")
        return False
    try:
        # 飞书 text 消息正文约 20KB 限制，超长则按段落分片发送
        MAX_BYTES = 18000
        if len(text.encode("utf-8")) > MAX_BYTES:
            parts = []
            current = []
            size = 0
            for para in text.split("\n"):
                if size + len(para.encode("utf-8")) > MAX_BYTES and current:
                    parts.append("\n".join(current))
                    current, size = [], 0
                current.append(para)
                size += len(para.encode("utf-8")) + 1
            if current:
                parts.append("\n".join(current))
            log(f"内容超长，分片为 {len(parts)} 条推送")
            for p in parts:
                requests.post(webhook, json={"msg_type": "text", "content": {"text": p}}, timeout=30)
            return True
        resp = requests.post(
            webhook,
            json={"msg_type": "text", "content": {"text": text}},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        if data.get("code") == 0 or data.get("StatusCode") == 0:
            log("飞书推送成功")
            return True
        log(f"飞书返回异常: {data}")
        return False
    except Exception as e:
        log(f"飞书推送失败: {e}")
        return False


def main():
    topn = int(os.environ.get("RANK_TOPN", "10"))
    api_key = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    model = os.environ.get("DEEPSEEK_API_MODEL", "deepseek-chat").strip()

    tencent = ind = lhb = zt = None
    prediction = None

    # 各数据源独立容错，单个失败不影响其他
    for name, fn in [
        ("腾讯行情", fetch_tencent_spot),
        ("行业板块", fetch_industry),
        ("龙虎榜", fetch_lhb),
        ("涨停池", fetch_zt_pool),
    ]:
        try:
            df = fn()
            if name == "腾讯行情":
                tencent = df
            elif name == "行业板块":
                ind = df
            elif name == "龙虎榜":
                lhb = df
            else:
                zt = df
            log(f"{name}数据获取成功: {len(df)} 条")
        except Exception:
            log(f"{name}数据获取失败:\n{traceback.format_exc(limit=2)}")

    prediction = predict_sectors(ind, api_key, model, topn=15)

    text = build_markdown(topn, tencent, ind, lhb, zt, prediction)
    print(text)
    print("=" * 60)

    if not (tencent is None and ind is None and lhb is None and zt is None):
        notify_feishu(text)
    else:
        log("无任何数据，不推送")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("脚本异常结束:\n" + traceback.format_exc())
        sys.exit(1)
