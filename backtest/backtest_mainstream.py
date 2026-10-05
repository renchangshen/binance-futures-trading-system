#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
样本外测试：主流币种回测
==================================================================
为了验证策略的普适性，使用完全不同的一组主流大市值币种进行回测。
这些币种流动性好、市值大，与原来的小市值币种特征完全不同。

如果策略在主流币上也能盈利，说明策略具有普适性，不是过拟合小币种的结果。
"""

import os
import json
import numpy as np
import pandas as pd

# ==================== 配置 ====================
DATA_DIR   = "./data"
RESULT_DIR = "./backtest_results"

# 样本外测试：选择主流大市值币种
SYMBOLS = [
    "BTCUSDT",      # 比特币 - 市值第1
    "ETHUSDT",      # 以太坊 - 市值第2
    "SOLUSDT",      # Solana - 主流L1
    "BNBUSDT",      # 币安币 - 交易所平台币
    "XRPUSDT",      # 瑞波币 - 老牌主流币
]

EMA_FAST1, EMA_FAST2 = 144, 169
EMA_SLOW1, EMA_SLOW2 = 576, 676
ATR_PERIOD = 38
ATR_MULT   = 32.0
SL_PCT     = 0.03

FEE_RATE     = 0.0004
INIT_CAPITAL = 44444.0
LEVERAGE     = 20
FIXED_MARGIN = 400.0

WARMUP_BARS = 5 * EMA_SLOW2
MIN_BARS    = WARMUP_BARS + 500
# =============================================


def add_indicators(df):
    c = df["close"]
    df["ema_f1"] = c.ewm(span=EMA_FAST1, adjust=False).mean()
    df["ema_f2"] = c.ewm(span=EMA_FAST2, adjust=False).mean()
    df["ema_s1"] = c.ewm(span=EMA_SLOW1, adjust=False).mean()
    df["ema_s2"] = c.ewm(span=EMA_SLOW2, adjust=False).mean()
    prev = c.shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev).abs(),
                    (df["low"] - prev).abs()], axis=1).max(axis=1)
    df["atr"] = tr.ewm(alpha=1.0 / ATR_PERIOD, adjust=False).mean()
    return df


def compute_signals(df, invert=False):
    f1, f2 = df["ema_f1"], df["ema_f2"]
    s1, s2 = df["ema_s1"], df["ema_s2"]
    f1a = (f1 > s1) & (f1 > s2); f1b = (f1 < s1) & (f1 < s2)
    f2a = (f2 > s1) & (f2 > s2); f2b = (f2 < s1) & (f2 < s2)
    f1cu = f1a & (~f1a.shift(1, fill_value=False))
    f2cu = f2a & (~f2a.shift(1, fill_value=False))
    f1cd = f1b & (~f1b.shift(1, fill_value=False))
    f2cd = f2b & (~f2b.shift(1, fill_value=False))
    long_sig  = (f1cu & f2a) | (f2cu & f1a)
    short_sig = (f1cd & f2b) | (f2cd & f1b)
    if invert:
        long_sig, short_sig = short_sig, long_sig
    return long_sig.to_numpy(), short_sig.to_numpy()


def _trade(symbol, side, t_in, t_out, entry, exitp, bars, raised, cost, reason=None):
    gross = (exitp / entry - 1.0) if side == 1 else (entry - exitp) / entry
    net = gross - 2.0 * cost
    if reason is None:
        reason = "atr_trail" if raised else "fixed_sl"
    return {"symbol": symbol, "side": side, "entry_ms": int(t_in), "exit_ms": int(t_out),
            "entry": entry, "exit": exitp, "net_ret": net, "bars": int(bars), "reason": reason}


def backtest_symbol(symbol, df, invert=False):
    df = add_indicators(df)
    long_sig, short_sig = compute_signals(df, invert)

    op = df["open"].to_numpy().tolist();  hi = df["high"].to_numpy().tolist()
    lo = df["low"].to_numpy().tolist();   cl = df["close"].to_numpy().tolist()
    atr = df["atr"].to_numpy().tolist();  ts = df["open_time"].to_numpy().tolist()
    ls = long_sig.tolist();               ss = short_sig.tolist()
    n = len(op); cost = FEE_RATE
    trades = []
    pos = 0; entry = 0.0; entry_i = 0; trail = 0.0; ext = 0.0; raised = False

    def open_pos(side, i):
        nonlocal pos, entry, entry_i, trail, ext, raised
        pos = side; entry = op[i]; entry_i = i; raised = False
        if side == 1: trail = entry * (1 - SL_PCT); ext = hi[i]
        else:         trail = entry * (1 + SL_PCT); ext = lo[i]

    i = max(WARMUP_BARS, 1)
    while i < n:
        sl_, ss_ = ls[i - 1], ss[i - 1]
        if pos == 0:
            if sl_: open_pos(1, i)
            elif ss_: open_pos(-1, i)
        elif pos == 1 and ss_:
            trades.append(_trade(symbol, 1, ts[entry_i], ts[i], entry, op[i], i - entry_i, raised, cost, "reverse_signal"))
            open_pos(-1, i)
        elif pos == -1 and sl_:
            trades.append(_trade(symbol, -1, ts[entry_i], ts[i], entry, op[i], i - entry_i, raised, cost, "reverse_signal"))
            open_pos(1, i)

        if pos == 1:
            if lo[i] <= trail:
                exitp = op[i] if op[i] <= trail else trail
                trades.append(_trade(symbol, 1, ts[entry_i], ts[i], entry, exitp, i - entry_i, raised, cost))
                pos = 0
            else:
                if hi[i] > ext: ext = hi[i]
                cand = ext - ATR_MULT * atr[i]
                if cand > trail: trail = cand; raised = True
        elif pos == -1:
            if hi[i] >= trail:
                exitp = op[i] if op[i] >= trail else trail
                trades.append(_trade(symbol, -1, ts[entry_i], ts[i], entry, exitp, i - entry_i, raised, cost))
                pos = 0
            else:
                if lo[i] < ext: ext = lo[i]
                cand = ext + ATR_MULT * atr[i]
                if cand < trail: trail = cand; raised = True
        i += 1

    if pos != 0:
        trades.append(_trade(symbol, pos, ts[entry_i], ts[n - 1], entry, cl[n - 1], (n - 1) - entry_i, raised, cost, "end_of_data"))
    return trades


def _max_drawdown(equity):
    eq = np.asarray(equity, dtype=float)
    peak = np.maximum.accumulate(eq)
    return float((eq / peak - 1.0).min())


def simulate_standalone(trades):
    ts = sorted(trades, key=lambda t: t["exit_ms"])
    balance = INIT_CAPITAL; eq = [INIT_CAPITAL]; wins = n = 0; blown = False
    for t in ts:
        if balance <= 0: blown = True; break
        margin = min(FIXED_MARGIN, balance)
        pnl = max(margin * LEVERAGE * t["net_ret"], -margin)
        balance += pnl; n += 1
        if pnl > 0: wins += 1
        if balance < 0: balance = 0
        eq.append(balance)
    return {"final": balance, "max_dd": _max_drawdown(eq), "trades": n, "wins": wins, "blown": blown}


def simulate_portfolio(all_trades):
    if not all_trades:
        return None
    events = []
    for k, t in enumerate(all_trades):
        events.append((t["entry_ms"], 0, k, "entry"))
        events.append((t["exit_ms"], 1, k, "exit"))
    events.sort(key=lambda e: (e[0], e[1]))

    balance = INIT_CAPITAL
    notion = {}
    times = [events[0][0]]; eq = [INIT_CAPITAL]
    opened = 0; blown = False; ledger = []
    for tm, _, k, typ in events:
        if balance <= 0:
            blown = True; break
        if typ == "entry":
            margin = min(FIXED_MARGIN, balance)
            notion[k] = margin * LEVERAGE
            opened += 1
        else:
            if k in notion:
                margin = notion[k] / LEVERAGE
                pnl = max(notion[k] * all_trades[k]["net_ret"], -margin)
                balance += pnl
                if balance < 0: balance = 0
                times.append(tm); eq.append(balance)
                t = all_trades[k]
                ledger.append({"symbol": t["symbol"], "side": t["side"],
                               "entry_ms": t["entry_ms"], "exit_ms": t["exit_ms"],
                               "entry": t["entry"], "exit": t["exit"], "net_ret": t["net_ret"],
                               "notional": notion[k], "pnl_usd": pnl, "balance_after": balance,
                               "bars": t["bars"], "reason": t["reason"]})
                if balance <= 0:
                    blown = True; break
    return {"final": balance, "max_dd": _max_drawdown(eq), "blown": blown,
            "trades": opened, "times": times, "equity": eq, "ledger": ledger}


def main():
    os.makedirs(RESULT_DIR, exist_ok=True)
    print(f"【样本外测试 - 主流币种】")
    print(f"回测配置：本金 {INIT_CAPITAL:.0f}U | {LEVERAGE}x | 每单保证金=固定 {FIXED_MARGIN:.0f}U | 止损 {SL_PCT:.0%} | ATR止盈 {ATR_MULT:.0f}x")

    dfs = {}; missing = []
    for sym in SYMBOLS:
        path = os.path.join(DATA_DIR, f"{sym}.parquet")
        if not os.path.exists(path):
            missing.append(sym); continue
        df = pd.read_parquet(path, columns=["open_time", "open", "high", "low", "close"])
        df = df.sort_values("open_time").reset_index(drop=True)
        if len(df) < MIN_BARS:
            print(f"  {sym} 数据太短({len(df)})，跳过"); continue
        dfs[sym] = df
    if missing:
        print(f"⚠ 以下交易对在 {DATA_DIR} 没有数据：{missing}")
    if not dfs:
        print("没有可回测的数据，退出。"); return

    # 打印数据时间范围
    print(f"\n数据时间范围：")
    for sym, df in dfs.items():
        start = pd.to_datetime(df['open_time'].iloc[0], unit='ms')
        end = pd.to_datetime(df['open_time'].iloc[-1], unit='ms')
        print(f"  {sym}: {start.strftime('%Y-%m-%d')} 至 {end.strftime('%Y-%m-%d')} ({len(df)}根K线, {(end-start).days}天)")

    print(f"\n将回测：{list(dfs.keys())}\n")

    for tag, invert, name in [("forward", False, "正向")]:
        all_trades = []; per_symbol = []
        for sym, df in dfs.items():
            trs = backtest_symbol(sym, df, invert)
            all_trades += trs
            ss = simulate_standalone(trs)
            wr = (ss["wins"] / ss["trades"]) if ss["trades"] else 0.0
            per_symbol.append({
                "交易对": sym, "交易次数": ss["trades"], "胜率": round(wr, 4),
                "单币最终本金U": round(ss["final"], 2),
                "单币收益率": round((ss["final"] - INIT_CAPITAL) / INIT_CAPITAL, 4),
                "单币最大回撤": round(ss["max_dd"], 4),
                "是否爆仓": "是" if ss["blown"] else "否",
            })
        port = simulate_portfolio(all_trades)

        ps = pd.DataFrame(per_symbol).sort_values("单币收益率", ascending=False)
        ps.to_csv(os.path.join(RESULT_DIR, f"mainstream_summary_{tag}.csv"),
                  index=False, encoding="utf-8-sig")

        if port and port["ledger"]:
            led = pd.DataFrame(port["ledger"])
            led["入场时间"] = pd.to_datetime(led["entry_ms"], unit="ms", utc=True)
            led["出场时间"] = pd.to_datetime(led["exit_ms"], unit="ms", utc=True)
            led["方向"] = led["side"].map({1: "多", -1: "空"})
            out = led[["symbol", "方向", "入场时间", "出场时间", "entry", "exit",
                       "net_ret", "notional", "pnl_usd", "balance_after", "bars", "reason"]].copy()
            out.columns = ["交易对", "方向", "入场时间", "出场时间", "入场价", "出场价",
                           "收益率", "名义价值U", "盈亏U", "平仓后本金U", "持仓K线数", "离场原因"]
            out["收益率"] = out["收益率"].round(5)
            for c in ["名义价值U", "盈亏U", "平仓后本金U"]:
                out[c] = out[c].round(4)
            out.to_csv(os.path.join(RESULT_DIR, f"mainstream_trades_{tag}.csv"),
                       index=False, encoding="utf-8-sig")

        lines = [f"==== 【样本外测试 - 主流币种】{name} 回测  (本金 {INIT_CAPITAL:.0f}U | {LEVERAGE}x | 每单保证金=固定 {FIXED_MARGIN:.0f}U) ===="]
        if port:
            lines += [
                "【组合（所有交易对共用一个账户）】",
                f"  开仓次数      : {port['trades']}",
                f"  最终本金      : {port['final']:.2f}U",
                f"  总收益率      : {(port['final'] - INIT_CAPITAL) / INIT_CAPITAL:.2%}",
                f"  组合最大回撤  : {port['max_dd']:.2%}",
                f"  是否爆仓归零  : {'是 ⚠（已触发停止）' if port['blown'] else '否'}",
            ]
        lines.append(f"\n【各交易对最大回撤（各自独立 {INIT_CAPITAL:.0f}U 账户）】")
        for r in sorted(per_symbol, key=lambda x: x["单币最大回撤"]):
            lines.append(f"  {r['交易对']:<10} 最大回撤 {r['单币最大回撤']:>8.2%} | 交易 {r['交易次数']:>4} | "
                         f"胜率 {r['胜率']:>6.1%} | 收益率 {r['单币收益率']:>9.2%}"
                         f"{' | 爆仓' if r['是否爆仓']=='是' else ''}")
        text = "\n".join(lines)
        print("\n" + text)
        with open(os.path.join(RESULT_DIR, f"mainstream_overall_{tag}.txt"), "w", encoding="utf-8") as f:
            f.write(text + "\n")

        try:
            import matplotlib; matplotlib.use("Agg")
            import matplotlib.pyplot as plt
            t = pd.to_datetime(port["times"], unit="ms", utc=True)
            plt.figure(figsize=(11, 5))
            plt.plot(t, port["equity"])
            plt.axhline(INIT_CAPITAL, color="gray", ls="--", lw=1)
            plt.title(f"Mainstream coins - Forward portfolio equity ({INIT_CAPITAL:.0f}U, {LEVERAGE}x, {FIXED_MARGIN:.0f}U/trade)")
            plt.xlabel("time"); plt.ylabel("balance (USDT)"); plt.grid(True, alpha=0.3)
            plt.tight_layout()
            plt.savefig(os.path.join(RESULT_DIR, f"mainstream_equity_curve_{tag}.png"), dpi=120)
        except Exception:
            pass

    print(f"\n样本外测试结果已写入：{os.path.abspath(RESULT_DIR)}")
    print("  mainstream_overall_forward.txt")
    print("  mainstream_summary_forward.csv")
    print("  mainstream_trades_forward.csv")


if __name__ == "__main__":
    main()
