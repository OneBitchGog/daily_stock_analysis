#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
市场榜单脚本：换手率榜 / 主力资金榜 / 涨跌幅榜 / 板块榜 + 明日板块预测
- 数据源：akshare（东财免费接口，项目已随 requirements 安装）
- 明日板块预测：调用 DeepSeek（读环境变量 DEEPSEEK_API_KEY）
- 推送：飞书 Webhook（读环境变量 FEISHU_WEBHOOK_URL）

用法：python scripts/market_rankings.py
可选环境变量：
  DEEPSEEK_API_KEY  必填（做明日板块预测）
  DEEPSEEK_API_MODEL 可选，默认 deepseek-chat
  FEISHU_WEBHOOK_URL 必填（推送飞书）
  RANK_TOPN 可选，默认 10（每个榜单条数）
"""
import os
import sys
import traceback

import pandas as pd
import requests


def log(msg):
    print(f"[market_rankings] {msg}", flush=True)


def fetch_spot():
    """全A实时行情快照（含涨跌幅、换手率、量比、成交额）"""
    import akshare as ak
    df = ak.stock_zh_a_spot_em()
    if df is None or df.empty:
        raise RuntimeError("全A行情快照为空")
    return df


def fetch_fund_flow():
    """全A当日主力资金流排行（主力净流入净额）"""
    import akshare as ak
    df = ak.stock_individual_fund_flow_rank(indicator="今日")
    if df is None or df.empty:
        raise RuntimeError("资金流排行数据为空")
    return df


def fetch_industry():
    """行业板块涨跌榜（含领涨股票）"""
    import akshare as ak
    df = ak.stock_board_industry_name_em()
    if df is None or df.empty:
        raise RuntimeError("行业板块数据为空")
    return df


def fmt_num(x, suffix=""):
    """数字格式化为亿/万"""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return "-"
    if abs(v) >= 1e8:
        return f"{v / 1e8:.2f}亿{suffix}"
    if abs(v) >= 1e4:
        return f"{v / 1e4:.1f}万{suffix}"
    return f"{v:.2f}{suffix}"


def to_list(df, cols):
    """按列安全取值，返回 list[tuple]；列缺失返回空"""
    if df is None or df.empty:
        return []
    missing = [c for c in cols if c not in df.columns]
    if missing:
        log(f"警告: 缺少列 {missing}，跳过该榜单")
        return []
    return [tuple(row) for _, row in df.head(30).iterrows()]


def build_markdown(topn, spot_df=None, flow_df=None, ind_df=None, prediction=None):
    lines = []
    lines.append("📊 **每日市场榜单**")
    lines.append(f"🕐 {pd.Timestamp.now().strftime('%Y-%m-%d %H:%M')}")
    lines.append("")

    # 1. 涨幅榜
    if spot_df is not None and {"代码", "名称", "涨跌幅"}.issubset(spot_df.columns):
        try:
            df = spot_df.nlargest(topn, "涨跌幅")
            lines.append(f"🚀 **今日涨幅榜 TOP{topn}**")
            lines.append("代码 | 名称 | 涨幅 | 换手率 | 成交额")
            lines.append("-" * 40)
            for _, r in df.iterrows():
                turnover = r.get("换手率", "-")
                amount = fmt_num(r.get("成交额", 0))
                lines.append(f"{r['代码']} | {r['名称']} | {r['涨跌幅']}% | {turnover}% | {amount}")
            lines.append("")
        except Exception as e:
            log(f"涨幅榜生成失败: {e}")

    # 2. 换手率榜
    if spot_df is not None and {"代码", "名称", "换手率"}.issubset(spot_df.columns):
        try:
            df = spot_df.nlargest(topn, "换手率")
            lines.append(f"🔥 **今日换手率榜 TOP{topn}**")
            lines.append("代码 | 名称 | 换手率 | 涨幅 | 量比")
            lines.append("-" * 40)
            for _, r in df.iterrows():
                change = r.get("涨跌幅", "-")
                vr = r.get("量比", "-")
                lines.append(f"{r['代码']} | {r['名称']} | {r['换手率']}% | {change}% | {vr}")
            lines.append("")
        except Exception as e:
            log(f"换手率榜生成失败: {e}")

    # 3. 主力资金榜（近似机构交易量排行）
    if flow_df is not None and "今日主力净流入-净额" in flow_df.columns:
        try:
            col = "今日主力净流入-净额"
            df = flow_df.nlargest(topn, col)
            lines.append(f"💰 **今日主力净流入榜 TOP{topn}**（大单+超大单）")
            lines.append("代码 | 名称 | 主力净流入 | 今日涨幅")
            lines.append("-" * 40)
            for _, r in df.iterrows():
                name = r.get("名称", "-")
                code = r.get("代码", "-")
                net = fmt_num(r.get(col, 0))
                chg = r.get("今日涨跌幅", "-")
                lines.append(f"{code} | {name} | {net} | {chg}%")
            lines.append("")
        except Exception as e:
            log(f"主力资金榜生成失败: {e}")

    # 4. 板块榜
    if ind_df is not None and "板块名称" in ind_df.columns:
        try:
            df = ind_df.nlargest(topn, "涨跌幅")
            lines.append(f"🧭 **今日行业板块涨幅 TOP{topn}**")
            lines.append("板块 | 涨跌幅 | 领涨股")
            lines.append("-" * 40)
            for _, r in df.iterrows():
                leader = r.get("领涨股票", "-")
                leader_chg = r.get("领涨股票-涨跌幅", "")
                lines.append(f"{r['板块名称']} | {r['涨跌幅']}% | {leader}({leader_chg}%)")
            lines.append("")
        except Exception as e:
            log(f"板块榜生成失败: {e}")

    # 5. 明日板块预测
    if prediction:
        lines.append("🔮 **明日关注板块预测（AI）**")
        lines.append(prediction.strip())
        lines.append("")
        lines.append("⚠️ 预测仅供参考，不构成投资建议")
    return "\n".join(lines)


def predict_sectors(ind_df, api_key, model="deepseek-chat", topn=15):
    """用 DeepSeek 基于今日板块表现预测明日关注板块"""
    if not api_key:
        log("DEEPSEEK_API_KEY 未配置，跳过明日板块预测")
        return None
    if ind_df is None or ind_df.empty or "板块名称" not in ind_df.columns:
        return None
    try:
        df = ind_df.head(topn)
        lines = []
        for _, r in df.iterrows():
            leader = r.get("领涨股票", "-")
            lines.append(
                f"- {r['板块名称']}: 涨跌幅{r['涨跌幅']}% 换手率{r.get('换手率','-')}% 领涨股:{leader}"
            )
        board_text = "\n".join(lines)
        prompt = (
            "你是一名资深A股板块分析师。以下是今日A股行业板块涨跌表现（按涨幅排序）：\n"
            f"{board_text}\n\n"
            "请基于板块强度、持续性、资金偏好和市场情绪，预测明日最可能继续走强的 3 个板块。\n"
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
        # 飞书 text 消息，正文用 \n 分隔
        payload = {"msg_type": "text", "content": {"text": text[:20000]}}
        resp = requests.post(webhook, json=payload, timeout=30)
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

    spot_df = flow_df = ind_df = None
    prediction = None

    # 各数据源独立容错，单个失败不影响其他
    for name, fn in [
        ("全A行情", fetch_spot),
        ("资金流", fetch_fund_flow),
        ("行业板块", fetch_industry),
    ]:
        try:
            df = fn()
            if name == "全A行情":
                spot_df = df
            elif name == "资金流":
                flow_df = df
            else:
                ind_df = df
            log(f"{name}数据获取成功: {len(df)} 条")
        except Exception:
            log(f"{name}数据获取失败:\n{traceback.format_exc(limit=2)}")

    # 明日板块预测（依赖行业板块 + DeepSeek）
    prediction = predict_sectors(ind_df, api_key, model, topn=15)

    text = build_markdown(topn, spot_df, flow_df, ind_df, prediction)
    print(text)
    print("=" * 60)

    if not (spot_df is None and flow_df is None and ind_df is None):
        notify_feishu(text)
    else:
        log("无任何数据，不推送")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log("脚本异常结束:\n" + traceback.format_exc())
        sys.exit(1)
