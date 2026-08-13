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


def to_symbol(code):
    """6位数字代码 → 带市场前缀的 symbol（腾讯/新浪用）"""
    code = str(code).zfill(6)
    if code.startswith(("6", "5")):
        return "sh" + code
    if code.startswith(("0", "3")):
        return "sz" + code
    return "bj" + code


def fetch_history_yf(symbol):
    """Yahoo Finance 兜底（国内源全失败时，GitHub Actions 在国外可直连）"""
    import yfinance as yf
    code = str(symbol).zfill(6)
    if code.startswith(("6", "5")):
        ysym = code + ".SS"
    elif code.startswith(("0", "3")):
        ysym = code + ".SZ"
    else:
        raise RuntimeError("yfinance 不支持北交所")
    start = (date.today() - timedelta(days=550)).strftime("%Y-%m-%d")
    df = yf.Ticker(ysym).history(start=start)
    if df is None or df.empty:
        raise RuntimeError("yfinance 历史为空")
    df = df.reset_index()
    df = df.rename(
        columns={
            "Date": "date", "Open": "open", "Close": "close",
            "High": "high", "Low": "low", "Volume": "volume",
        }
    )
    return df


def fetch_history(symbol):
    """拉单只股票最近约 1 年日K（前复权）：腾讯 → 新浪 → Yahoo Finance"""
    import akshare as ak
    sym = to_symbol(symbol)
    start = (date.today() - timedelta(days=500)).strftime("%Y%m%d")
    end = date.today().strftime("%Y%m%d")
    df = None
    try:
        df = ak.stock_zh_a_hist_tx(symbol=sym, start_date=start, end_date=end, adjust="qfq")
    except Exception:
        try:
            df = ak.stock_zh_a_daily(symbol=sym, start_date=start, end_date=end, adjust="qfq")
        except Exception:
            log(f"{symbol} 腾讯/新浪历史K线均失败，尝试 Yahoo Finance")
            df = fetch_history_yf(symbol)
    if df is None or df.empty:
        raise RuntimeError("历史K线为空")
    # 统一列名：小写英文 → 中文
    df = df.rename(
        columns={
            "date": "日期", "open": "开盘", "close": "收盘",
            "high": "最高", "low": "最低", "volume": "成交量",
        }
    )
    for col in ["开盘", "收盘", "最高", "最低", "成交量"]:
        if col in df.columns:
            df[col] = pd.to_numeric(df[col], errors="coerce")
    return df


def norm_code(c):
    """'sz300404'/'sh600519'/'bj...' → 6位纯数字代码"""
    s = str(c)
    return "".join(ch for ch in s if ch.isdigit())[-6:] if s else s


def get_stock_name(code, zt_df, tencent_df):
    for df, ck, nk in [(zt_df, "代码", "名称"), (tencent_df, "code", "name")]:
        if df is not None and ck in df.columns:
            hit = df[df[ck].astype(str).str.contains(code, na=False)]
            if not hit.empty:
                return str(hit.iloc[0][nk])
    return code


def signal_ma250(hist):
    """回踩年线（简化）：站上年线 + 近5日回踩不破(≥0.97年线) + 量能不放大"""
    close = hist["收盘"]
    vol = hist["成交量"]
    if len(close) < 260 or pd.isna(close.iloc[-1]):
        return False
    ma250 = close.rolling(250).mean()
    today_ma = ma250.iloc[-1]
    if pd.isna(today_ma):
        return False
    if close.iloc[-1] <= today_ma:
        return False
    if close.iloc[-5:].min() < today_ma * 0.97:
        return False
    avg_vol = vol.iloc[-11:-1].mean()
    if avg_vol > 0 and vol.iloc[-1] > avg_vol * 1.3:
        return False  # 放量上涨不算回踩
    return True


def signal_reversal(hist):
    """反转形态：今日锤头线 / 看涨吞没（近2根K线）"""
    if len(hist) < 3:
        return False
    o, c, h, l = hist["开盘"], hist["收盘"], hist["最高"], hist["最低"]
    cur = {"开盘": o.iloc[-1], "收盘": c.iloc[-1], "最高": h.iloc[-1], "最低": l.iloc[-1]}
    prev = {"开盘": o.iloc[-2], "收盘": c.iloc[-2], "最高": h.iloc[-2], "最低": l.iloc[-2]}
    body = abs(cur["收盘"] - cur["开盘"])
    upper = cur["最高"] - max(cur["收盘"], cur["开盘"])
    lower = min(cur["收盘"], cur["开盘"]) - cur["最低"]
    is_hammer = body > 0 and lower > 2 * body and upper < body
    is_engulf = (
        prev["收盘"] < prev["开盘"]
        and cur["收盘"] > cur["开盘"]
        and cur["开盘"] <= prev["收盘"]
        and cur["收盘"] >= prev["开盘"]
    )
    return is_hammer or is_engulf


def signal_breakout(hist):
    """放量突破：今日放量(>1.5×5日均量) 且突破前10日高点"""
    if len(hist) < 20:
        return False
    high = hist["最高"]
    vol = hist["成交量"]
    close = hist["收盘"]
    recent_high = high.iloc[-11:-1].max()
    vol_avg = vol.iloc[-6:-1].mean()
    return (
        close.iloc[-1] > recent_high
        and vol_avg > 0
        and vol.iloc[-1] > vol_avg * 1.5
        and close.iloc[-1] > close.iloc[-2]
    )


def scan_signals(zt_df, tencent_df, topn=20, limit=40):
    """扫描热门股池（涨停池 + 涨幅榜前topn），套用选股信号"""
    codes = set()
    if zt_df is not None and "代码" in zt_df.columns:
        codes |= {norm_code(c) for c in zt_df["代码"]}
    if tencent_df is not None and "code" in tencent_df.columns:
        top = tencent_df.nlargest(topn, "zdf")
        codes |= {norm_code(c) for c in top["code"]}
    results = []
    for code in sorted(codes)[:limit]:
        try:
            hist = fetch_history(code)
            if hist is None or len(hist) < 260:
                continue
            sigs = []
            if signal_ma250(hist):
                sigs.append("回踩年线")
            if signal_reversal(hist):
                sigs.append("反转形态")
            if signal_breakout(hist):
                sigs.append("放量突破")
            if sigs:
                results.append((code, get_stock_name(code, zt_df, tencent_df), sigs))
        except Exception as e:
            log(f"{code} 策略扫描失败: {e}")
            continue
    return results


def fetch_wencai_concepts():
    """问财查询今日涨幅居前股票的所属概念（实验性，约30-60秒，失败重试1次）"""
    import time as _time
    import pywencai
    for attempt in range(2):
        try:
            df = pywencai.get(query="今日涨幅居前的股票，所属概念")
            if df is not None and hasattr(df, "columns"):
                if "所属概念" in df.columns and "股票简称" in df.columns:
                    return df
            log(f"问财第{attempt + 1}次尝试返回异常，重试")
        except Exception as e:
            log(f"问财第{attempt + 1}次尝试失败: {e}")
        if attempt == 0:
            _time.sleep(3)
    raise RuntimeError("问财多次尝试均失败")


# 通用属性标签（非题材概念），聚合时过滤，避免占榜
_IGNORE_CONCEPTS = {
    "融资融券", "转融券标的", "融资标的", "深股通", "沪股通", "港股通",
    "MSCI中国", "标普道琼斯A股", "富时罗素", "MSCI概念", "证金持股",
    "QFII重仓", "机构重仓", "央企国资改革", "地方国资改革",
    "预盈预增", "破净股", "昨日涨停", "昨日连板", "新股与次新股",
    "注册制次新股", "举牌", "参股银行", "参股保险", "参股券商", "低价股",
}


def aggregate_concepts(wc_df, topn=10):
    """聚合热门概念：统计强势股涉及的每个题材概念的出现次数与代表股（过滤通用属性标签）"""
    from collections import Counter, defaultdict
    counts = Counter()
    reps = defaultdict(list)
    for _, r in wc_df.iterrows():
        name = str(r.get("股票简称", ""))
        concepts = str(r.get("所属概念", ""))
        for c in (x.strip() for x in concepts.split(";") if x.strip()):
            if c in _IGNORE_CONCEPTS:
                continue
            counts[c] += 1
            if len(reps[c]) < 3:
                reps[c].append(name)
    return [(c, cnt, reps[c]) for c, cnt in counts.most_common(topn)]


# 大分类 → 小分类（概念关键词）映射，用于给股票标注所属行业分类
SECTOR_MAP = {
    "科技": ["芯片", "半导体", "存储", "算力", "人工智能", "AI", "软件", "云计算",
             "光模块", "通信", "5G", "数据中心", "服务器", "信创", "操作系统",
             "智能驾驶", "智能汽车", "车联网", "数据要素", "数字经济", "华为"],
    "医药": ["创新药", "CRO", "细胞免疫", "医疗器械", "中药", "疫苗", "生物医药",
             "基因", "减肥药", "流感", "肝炎", "医疗", "脑机接口", "仿制药"],
    "新能源": ["光伏", "锂电", "储能", "风电", "氢能", "电池", "充电桩", "新能源汽车",
               "钠离子", "固态电池"],
    "消费": ["白酒", "食品", "家电", "零售", "旅游", "免税", "饮料", "乳品",
             "服装", "美妆", "宠物", "预制菜", "电商"],
    "金融": ["银行", "券商", "保险", "互联网金融", "数字货币", "期货", "参股金融"],
    "周期": ["钢铁", "煤炭", "有色", "化工", "石油", "水泥", "稀土", "小金属",
             "磷化工", "氟化工", "贵金属"],
    "军工": ["军工", "国防", "航天", "卫星", "北斗", "航母", "无人机"],
    "汽车": ["汽车", "新能源车", "零部件", "汽车电子", "轮胎"],
    "机器人": ["机器人", "减速器", "伺服", "执行器", "人形机器人", "工业母机"],
    "传媒": ["传媒", "游戏", "影视", "文化", "短剧", "AIGC", "元宇宙", "IP"],
    "地产建筑": ["房地产", "建材", "建筑", "基建", "装配式建筑"],
    "农业": ["农业", "养殖", "种业", "农产品", "化肥"],
    "电力": ["电力", "核电", "特高压", "电网", "虚拟电厂"],
    "环保": ["环保", "污水处理", "固废", "碳中和"],
}


def classify_stock(concepts_str):
    """从股票所属概念串中提取 大分类（含小分类），如 ['科技(芯片、算力、AI应用)']"""
    concepts = [c.strip() for c in str(concepts_str).split(";") if c.strip()]
    out = []
    for big, smalls in SECTOR_MAP.items():
        matched = [c for c in concepts if any(s in c for s in smalls)]
        if matched:
            out.append(f"{big}({'、'.join(matched[:3])})")
    return out


def fetch_individual_flow():
    """同花顺全市场个股资金流（含流入/流出/净额，单位亿元）"""
    import akshare as ak
    df = ak.stock_fund_flow_individual(symbol="即时")
    if df is None or df.empty or "净额" not in df.columns:
        raise RuntimeError("个股资金流为空")
    return df


def fetch_industry_flow():
    """同花顺行业资金流（含流入/流出/净额，单位亿元）"""
    import akshare as ak
    df = ak.stock_fund_flow_industry(symbol="即时")
    if df is None or df.empty or "净额" not in df.columns:
        raise RuntimeError("行业资金流为空")
    return df


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


def parse_money_str(s):
    """解析同花顺资金流字符串（'1.39亿'/'2909.03万'/纯数字元）→ 亿元数值"""
    s = str(s).strip().replace("+", "")
    if not s or s in ("-", "nan", "None", "NaN"):
        return 0.0
    if s.endswith("亿"):
        return float(s[:-1])
    if s.endswith("万"):
        return float(s[:-1]) / 1e4
    try:
        return float(s) / 1e8  # 纯数字按元
    except ValueError:
        return 0.0


def build_markdown(topn, tencent=None, ind=None, lhb=None, zt=None, prediction=None,
                   signals=None, concepts=None, ind_flow=None, flow_map=None, concept_map=None):
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

    # 4.4 行业资金流（净流入 / 净流出）
    if ind_flow is not None and {"行业", "净额"}.issubset(ind_flow.columns):
        try:
            df = ind_flow.copy()
            df["净额"] = pd.to_numeric(df["净额"], errors="coerce").fillna(0)
            inflow = df.nlargest(topn, "净额")
            lines.append(f"💰 **行业资金净流入 TOP{topn}**")
            lines.append("行业 | 净流入 | 领涨股")
            lines.append("-" * 40)
            for _, r in inflow.iterrows():
                lines.append(
                    f"{r['行业']} | {fmt_billion(r['净额'])} | {r.get('领涨股', '-')}"
                )
            lines.append("")
            outflow = df.nsmallest(topn, "净额")
            lines.append(f"💸 **行业资金净流出 TOP{topn}**")
            lines.append("行业 | 净流出 | 领涨股")
            lines.append("-" * 40)
            for _, r in outflow.iterrows():
                lines.append(
                    f"{r['行业']} | {fmt_billion(r['净额'])} | {r.get('领涨股', '-')}"
                )
            lines.append("")
        except Exception as e:
            log(f"行业资金流生成失败: {e}")

    # 4.5 热门概念（问财实验性）
    if concepts:
        lines.append(f"🧪 **今日热门概念 TOP{min(topn, len(concepts))}**（强势股涉及）")
        lines.append("概念 | 涉及股数 | 代表股")
        lines.append("-" * 40)
        for concept, cnt, reps in concepts:
            lines.append(f"{concept} | {cnt} | {','.join(reps)}")
        lines.append("")
        lines.append("（数据源：问财，实验性）")
        lines.append("")

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

    # 6.5 策略信号（热门股池：涨停池 + 涨幅榜前20），附分类与主力资金
    if signals:
        lines.append(f"🧠 **今日热门股策略信号**")
        lines.append("代码 | 名称 | 信号 | 分类 | 主力净额")
        lines.append("-" * 60)
        for code, name, sigs in signals:
            net = "-"
            if flow_map and code in flow_map:
                net = f"{parse_money_str(flow_map[code].get('净额')):.2f}亿"
            cls = "、".join(classify_stock(concept_map.get(code, ""))) if concept_map else ""
            lines.append(
                f"{code} | {name} | {'、'.join(sigs)} | {cls[:22] or '—'} | {net}"
            )
        lines.append("")
        lines.append("（扫描范围：今日涨停池 + 涨幅榜前20；分类/资金仅供参考）")
        lines.append("")

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

    # 问财热门概念（实验性，失败不影响主流程）；同时构建 股票代码→所属概念 映射
    concepts = []
    concept_map = {}
    wc = None
    try:
        wc = fetch_wencai_concepts()
        concepts = aggregate_concepts(wc, topn)
        for _, r in wc.iterrows():
            code = norm_code(r.get("code", r.get("股票代码", ""))).zfill(6)
            concept_map[code] = str(r.get("所属概念", ""))
        log(f"问财热门概念聚合完成: {len(concepts)} 个，概念映射 {len(concept_map)} 只")
    except Exception as e:
        log(f"问财概念获取失败(实验性，跳过): {e}")

    # 同花顺资金流：行业资金流 + 个股资金流（单位亿元）
    ind_flow = None
    flow_map = {}
    try:
        ind_flow = fetch_industry_flow()
        individual_flow = fetch_individual_flow()
        for _, r in individual_flow.iterrows():
            code = norm_code(r.get("股票代码", "")).zfill(6)
            flow_map[code] = {
                "流入": r.get("流入资金"), "流出": r.get("流出资金"), "净额": r.get("净额"),
            }
        log(f"资金流获取成功: 行业 {len(ind_flow)} 条, 个股 {len(flow_map)} 条")
    except Exception as e:
        log(f"资金流获取失败: {e}")

    # 策略信号扫描（热门股池），耗时约 1-2 分钟
    signals = []
    try:
        signals = scan_signals(zt, tencent, topn=20)
        log(f"策略信号扫描完成: {len(signals)} 只命中")
    except Exception as e:
        log(f"策略信号扫描失败: {e}")

    text = build_markdown(
        topn, tencent, ind, lhb, zt, prediction,
        signals, concepts, ind_flow, flow_map, concept_map,
    )
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
