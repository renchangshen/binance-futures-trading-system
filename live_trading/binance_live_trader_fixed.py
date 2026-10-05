#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
币安 U 本位合约 · 实盘自动交易（Live Trading）
================================================
本文件由你的「模拟盘 paper_trader」改写而来。
策略 / 指标 / 入场出场判定 与模拟盘【完全一致】，只是把"假装下单"换成"真实下单"。

与模拟盘相比的关键改动：
  1. 真实下单：用 python-binance 向币安合约下市价单开/平仓。
  2. 杠杆 & 保证金：启动时自动给每个交易对设置 10x、逐仓(ISOLATED)。
  3. 服务器端保护性止损：每次开仓后，立刻在【币安服务器】挂一个 STOP_MARKET 止损单。
     —— 这样即使你的机器人崩溃/断网/关机，仓位也有一道硬止损兜底，不会裸奔。
     ATR 移动止盈通过"撤旧挂新"不断上移这个服务器止损单来实现。
  4. 真实盈亏：平仓后从币安账单(income)读取真实已实现盈亏+手续费，统计更准确。
  5. 安全闸门：USE_TESTNET=False（真实盘）时，启动需手动输入 "LIVE" 确认。
  6. 启动自检：账户必须是【单向持仓】模式；相关交易对不能有残留持仓/挂单。

资金：杠杆 10x | 每单保证金 = 开仓时「当前总钱包余额 × 10%」（动态，随余额复利缩放）→ 名义价值 = 保证金 × 10x

依赖：
    pip install python-binance requests pandas numpy
运行：
    1) 先在 config.py 填好 API Key，并把 USE_TESTNET 先留 True 跑通流程
    2) python binance_live_trader.py        （建议 screen/tmux 挂后台）

================================================================
⚠⚠⚠  风险声明（请务必读完）  ⚠⚠⚠
  * 这是真实下单的合约交易程序，10x 杠杆，亏损会很快且不可逆。
  * 我不是投资顾问，本代码不构成任何投资建议；是否使用、用多少钱，由你自行判断与承担。
  * 务必：先测试网跑流程 → 再用【极小金额/单个币种】实盘观察几笔 → 确认行为符合预期后再放大。
  * 实盘与回测/模拟必然有差异（滑点、成交延迟、资金费率、交易所价格与你本地价格不同步等）。
================================================================
"""

import os
import time
import warnings
import signal as _signal
from decimal import Decimal, ROUND_DOWN, ROUND_HALF_UP
from datetime import datetime, timezone

import requests
import numpy as np
import pandas as pd

# ---- 第三方：币安官方 REST 封装 ----
try:
    from binance.client import Client
    from binance.exceptions import BinanceAPIException, BinanceRequestException
except ImportError:
    raise SystemExit("缺少依赖 python-binance，请先运行：pip install python-binance")

# ---- 读取密钥（优先 config.py，其次环境变量）----
try:
    import config
    API_KEY     = getattr(config, "API_KEY", "")
    API_SECRET  = getattr(config, "API_SECRET", "")
    USE_TESTNET = bool(getattr(config, "USE_TESTNET", True))
    PROXY       = getattr(config, "PROXY", "") or ""
except ImportError:
    API_KEY     = os.environ.get("BINANCE_API_KEY", "")
    API_SECRET  = os.environ.get("BINANCE_API_SECRET", "")
    USE_TESTNET = os.environ.get("BINANCE_USE_TESTNET", "1") != "0"
    PROXY       = os.environ.get("BINANCE_PROXY", "")

# 代理字典（同时给行情请求和下单接口用）；为空则直连
PROXIES = {"http": PROXY, "https": PROXY} if PROXY else None

warnings.simplefilter("ignore", FutureWarning)


# ==================== 配置（策略部分务必与模拟盘保持一致）====================
SYMBOLS = [                      # ← 只跑这几个；启动会自动校验拼写/是否可交易
    "AEROUSDT", "ORDIUSDT",
    "CLOUSDT", "EVAAUSDT", "SKYAIUSDT",
]

# 行情来源固定用【真实盘】公开接口：指标(EMA)需要真实历史价格，
# 测试网没有足够历史、价格也不真实，所以行情永远从真实盘拉，只有"下单"才走测试网。
FAPI_BASE = "https://fapi.binance.com"
INTERVAL  = "1m"

EMA_FAST1, EMA_FAST2 = 144, 169
EMA_SLOW1, EMA_SLOW2 = 576, 676
ATR_PERIOD = 38
ATR_MULT   = 32.0      # ATR 移动止盈倍数
SL_PCT     = 0.03      # 固定止损 3%

LEVERAGE     = 10
MARGIN_RATIO = 0.10     # ★每单保证金 = 开仓时「当前总钱包余额」× 10%（动态，随余额复利缩放）
MIN_MARGIN   = 0.0      # 保证金下限(U)：算出来低于此值就用此值，避免名义价值过小被交易所拒单；设 0 表示纯按比例、不设下限
FEE_RATE     = 0.0004   # 仅用于显示估算；真实手续费以账单为准

MARGIN_TYPE       = "ISOLATED"        # 逐仓；如想全仓改 "CROSSED"，不想改动则设 None
STOP_WORKING_TYPE = "CONTRACT_PRICE"  # 止损触发价类型：CONTRACT_PRICE=最新价(贴近回测)；MARK_PRICE=标记价(更抗插针)

WARMUP_BARS = 10000     # 启动每个交易对拉取的历史K线数（让 EMA676 彻底收敛，与回测一致）
POLL_SECONDS = 5        # 实时轮询间隔（秒）
MAX_DF_BARS  = 6000     # 内存里每个交易对保留的最大K线数

# —— 移动止损"撤旧挂新"的节流：这是本次修复的关键，防止止损委托越积越多、最终触发 -4045 ——
TRAIL_MIN_INTERVAL = 20.0    # 同一交易对两次移动止损至少间隔多少秒（原来每 5s 轮询就可能撤挂一次，太频繁）
TRAIL_MIN_IMPROVE  = 0.0010  # 新止损相对旧止损至少要改善这么多（按价格比例，0.0010=0.1%）才动手，过滤无意义的 1-tick 抖动
STOP_VERIFY_RETRY  = 3       # 撤单后核验"确实撤干净"的重试次数；核验不通过则本轮不挂新单
ORPHAN_SWEEP_INTERVAL = 30.0 # 周期性【全账户】清扫"无持仓却残留的止损单"的最小间隔(秒)。
                             # 关键修复：-4045『Reach max stop order limit』是【全账户】条件单上限(本例=200)，
                             # 不是单个交易对的上限，所以只撤"当前交易对"永远清不掉别的币种堆积的孤儿单。

RESULT_DIR = "./live_results"
TRADES_CSV = os.path.join(RESULT_DIR, "live_trades.csv")
STATS_CSV  = os.path.join(RESULT_DIR, "live_stats.csv")
LOCK_FILE  = os.path.join(RESULT_DIR, "live_trader.lock")   # 单实例锁：避免多份程序同时对同一账户下单/互撤
# ============================================================================

KLINE_COLS = ["open_time", "open", "high", "low", "close"]


# ===================================================================
#  以下【行情请求 / 指标 / 信号】与模拟盘逐行一致，保证策略行为不变
# ===================================================================
_session = requests.Session()
_session.headers.update({"User-Agent": "live-trader/1.0"})
if PROXIES:
    _session.proxies.update(PROXIES)


def safe_get(path, params, max_retries=6):
    url = FAPI_BASE + path
    attempt = 0
    while True:
        attempt += 1
        try:
            r = _session.get(url, params=params, timeout=20)
        except requests.RequestException:
            if attempt > max_retries:
                raise
            time.sleep(min(30, 2 ** attempt))
            continue
        if r.status_code in (429, 418):
            wait = int(r.headers.get("Retry-After", "30")) + 5
            print(f"[限速{r.status_code}] 休眠 {wait}s")
            time.sleep(wait)
            continue
        if 500 <= r.status_code < 600:
            if attempt > max_retries:
                r.raise_for_status()
            time.sleep(min(30, 2 ** attempt))
            continue
        r.raise_for_status()
        return r.json()


def fetch_klines(symbol, limit=None, start_ms=None, end_ms=None):
    params = {"symbol": symbol, "interval": INTERVAL}
    if limit:    params["limit"] = limit
    if start_ms: params["startTime"] = start_ms
    if end_ms:   params["endTime"] = end_ms
    data = safe_get("/fapi/v1/klines", params)
    rows = [[int(k[0]), float(k[1]), float(k[2]), float(k[3]), float(k[4])] for k in data]
    return rows


def warmup_df(symbol, need=WARMUP_BARS):
    """从当前时刻往回回拉，保证数据连续且截止到最新已收盘K线。"""
    rows, seen = [], set()
    end_ms = int(time.time() * 1000)
    while len(rows) < need:
        batch = fetch_klines(symbol, limit=1500, end_ms=end_ms)
        if not batch:
            break
        new = [k for k in batch if k[0] not in seen]
        if not new:
            break
        for k in new:
            seen.add(k[0])
        rows = new + rows
        end_ms = min(k[0] for k in batch) - 1
        if len(batch) < 1500:
            break
        time.sleep(0.25)
    df = pd.DataFrame(rows, columns=KLINE_COLS).drop_duplicates("open_time")
    df = df.sort_values("open_time").reset_index(drop=True)
    now_ms = int(time.time() * 1000)
    df = df[df["open_time"] + 60_000 <= now_ms].reset_index(drop=True)
    return df


def compute_indicators(df):
    c = df["close"]
    ema_f1 = c.ewm(span=EMA_FAST1, adjust=False).mean()
    ema_f2 = c.ewm(span=EMA_FAST2, adjust=False).mean()
    ema_s1 = c.ewm(span=EMA_SLOW1, adjust=False).mean()
    ema_s2 = c.ewm(span=EMA_SLOW2, adjust=False).mean()
    prev_close = c.shift(1)
    tr = pd.concat([df["high"] - df["low"],
                    (df["high"] - prev_close).abs(),
                    (df["low"] - prev_close).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0 / ATR_PERIOD, adjust=False).mean()
    return ema_f1, ema_f2, ema_s1, ema_s2, atr


def last_signal_and_atr(df):
    """返回 (signal, atr_last, emas)；signal ∈ {'long','short',None}，基于最后一根已收盘K线。"""
    f1, f2, s1, s2, atr = compute_indicators(df)
    f1_above = (f1 > s1) & (f1 > s2)
    f1_below = (f1 < s1) & (f1 < s2)
    f2_above = (f2 > s1) & (f2 > s2)
    f2_below = (f2 < s1) & (f2 < s2)
    f1_cu = f1_above & (~f1_above.shift(1, fill_value=False))
    f2_cu = f2_above & (~f2_above.shift(1, fill_value=False))
    f1_cd = f1_below & (~f1_below.shift(1, fill_value=False))
    f2_cd = f2_below & (~f2_below.shift(1, fill_value=False))
    long_sig  = (f1_cu & f2_above) | (f2_cu & f1_above)
    short_sig = (f1_cd & f2_below) | (f2_cd & f1_below)
    i = len(df) - 1
    sig = "long" if bool(long_sig.iloc[i]) else ("short" if bool(short_sig.iloc[i]) else None)
    emas = (float(f1.iloc[i]), float(f2.iloc[i]), float(s1.iloc[i]), float(s2.iloc[i]))
    return sig, float(atr.iloc[i]), emas


# ===================================================================
#  交易所交互（python-binance）— 实盘新增部分
# ===================================================================
_client = None  # 全局币安客户端


def make_client():
    if not API_KEY or not API_SECRET or "粘贴" in API_KEY:
        raise SystemExit("尚未配置 API Key/Secret，请编辑 config.py 后再运行。")
    req_params = {"timeout": 20}
    if PROXIES:
        req_params["proxies"] = PROXIES
    c = Client(API_KEY, API_SECRET, testnet=USE_TESTNET, requests_params=req_params)
    return c


# ---- 精度处理：数量按 stepSize 向下取整；价格按 tickSize 四舍五入 ----
def _dec(x):
    return Decimal(str(x))


def _round_step(value, step, rounding):
    if step <= 0:
        return _dec(value)
    q = (_dec(value) / _dec(step)).quantize(Decimal("1"), rounding=rounding)
    return q * _dec(step)


def fmt_qty(qty, f):
    d = _round_step(qty, f["stepSize"], ROUND_DOWN)
    return f"{d:.{f['qtyPrec']}f}"


def qty_value(qty, f):
    return float(_round_step(qty, f["stepSize"], ROUND_DOWN))


def fmt_price(price, f):
    d = _round_step(price, f["tickSize"], ROUND_HALF_UP)
    return f"{d:.{f['pricePrec']}f}"


def price_value(price, f):
    return float(_round_step(price, f["tickSize"], ROUND_HALF_UP))


def build_symbol_filters(client, symbols):
    """从【交易环境】(testnet 与否对应不同环境) 的 exchangeInfo 读取每个交易对的下单精度限制。"""
    info = client.futures_exchange_info()
    bysym = {s["symbol"]: s for s in info["symbols"]}
    out, skipped = {}, []
    for sym in symbols:
        s = bysym.get(sym)
        if (not s or s.get("status") != "TRADING"
                or s.get("contractType") != "PERPETUAL"
                or s.get("quoteAsset") != "USDT"):
            skipped.append(sym)
            continue
        filt = {x["filterType"]: x for x in s["filters"]}
        lot   = filt.get("LOT_SIZE", {})
        pricf = filt.get("PRICE_FILTER", {})
        notf  = filt.get("MIN_NOTIONAL", {})
        out[sym] = {
            "stepSize":    float(lot.get("stepSize", "0.001")),
            "minQty":      float(lot.get("minQty", "0")),
            "tickSize":    float(pricf.get("tickSize", "0.0001")),
            "minNotional": float(notf.get("notional", "5")),
            "qtyPrec":     int(s.get("quantityPrecision", 3)),
            "pricePrec":   int(s.get("pricePrecision", 4)),
        }
    if skipped:
        env = "测试网" if USE_TESTNET else "真实盘"
        print(f"⚠ 以下交易对在【{env}】不可交易（已跳过）：{skipped}")
        if USE_TESTNET:
            print("  （测试网币种很少，这是正常现象；流程验证可临时改用 BTCUSDT/ETHUSDT。）")
    return out


def ensure_one_way(client):
    """脚本按【单向持仓】编写；若账户是双向(对冲)持仓则拒绝启动。"""
    try:
        m = client.futures_get_position_mode()
    except BinanceAPIException as e:
        print(f"读取持仓模式失败：{e}")
        return False
    if m.get("dualSidePosition"):
        print("检测到账户为【双向持仓/对冲模式】，本脚本只支持【单向持仓】。")
        print("请在 币安App/网页：合约 → 设置 → 持仓模式 → 改为「单向持仓」后重试。")
        return False
    return True


def setup_symbol_exchange(client, symbol):
    """给单个交易对设置杠杆与保证金模式。"""
    try:
        client.futures_change_leverage(symbol=symbol, leverage=LEVERAGE)
    except BinanceAPIException as e:
        print(f"  ⚠ 设置杠杆失败 {symbol}: {e}")
    if MARGIN_TYPE:
        try:
            client.futures_change_margin_type(symbol=symbol, marginType=MARGIN_TYPE)
        except BinanceAPIException as e:
            if e.code != -4046:   # -4046 = 已经是该保证金模式，无需更改
                print(f"  ⚠ 设置保证金模式失败 {symbol}: {e}")


def get_position(client, symbol):
    """返回 (positionAmt, entryPrice)；无持仓时返回 (0.0, 0.0)。
       注意：新版接口对没有持仓的交易对会返回空列表，这里按"无持仓"处理。"""
    info = client.futures_position_information(symbol=symbol)
    if not info:
        return 0.0, 0.0
    for p in info:
        if p.get("positionSide", "BOTH") == "BOTH":
            return float(p["positionAmt"]), float(p["entryPrice"])
    p = info[0]
    return float(p["positionAmt"]), float(p["entryPrice"])


def get_balances(client):
    """返回 (总钱包余额, 可用余额) 单位USDT。"""
    acc = client.futures_account()
    return float(acc["totalWalletBalance"]), float(acc["availableBalance"])


def realized_pnl_since(client, symbol, start_ms):
    """从账单读取某交易对自 start_ms 起的真实净盈亏（已实现盈亏 + 手续费 + 资金费）。"""
    total = 0.0
    for it in ("REALIZED_PNL", "COMMISSION", "FUNDING_FEE"):
        try:
            rows = client.futures_income_history(symbol=symbol, incomeType=it,
                                                 startTime=start_ms, limit=1000)
            total += sum(float(r["income"]) for r in rows)
        except BinanceAPIException as e:
            print(f"  ⚠ 读取账单({it})失败 {symbol}: {e}")
    return total


def cancel_order(client, symbol, order_id):
    """撤掉保护性止损单。止损是【算法委托】(algoId)。
       order_id 可能是 'algo:<algoId>'（本程序新格式）或裸 algoId/orderId；统一兼容处理。
       本程序每个交易对同一时刻只应有一个保护性止损，兜底直接撤该交易对全部止损算法单。"""
    if not order_id or order_id == "IMMEDIATE":
        return
    sid = str(order_id)
    if sid.startswith("algo:"):
        sid = sid.split(":", 1)[1]
    if sid.isdigit():
        if _cancel_one_algo(client, int(sid), symbol):
            return
    # 兜底：撤该交易对全部止损算法单
    ids = _algo_ids_for_symbol(client, symbol)
    for aid in (ids or []):
        _cancel_one_algo(client, aid, symbol)


# ============================================================
#  止损单 = Binance【算法委托 / 条件单】(algo order)，用 algoId 标识。
#  关键事实（已用诊断脚本在本账户实测确认）：
#    · futures_create_order(type="STOP_MARKET", reduceOnly=...) 建的止损单会进【算法委托簿】；
#    · 它【不在】futures_get_open_orders 里 —— 这就是之前所有撤单都是"空撤"、止损单越堆越多的根因；
#    · 必须用 futures_get_open_algo_orders() 查询、用 futures_cancel_algo_order(algoId=...) 撤销。
#  下面这组 helper 就是这套"算法委托"的查询/撤销基础设施。
# ============================================================
def _algo_open_orders(client, max_retries=3):
    """拉取【全账户】当前所有算法委托(开着的止损/条件单)。
       返回 list；查询失败返回 None（用以区分『确实没有』的空 list）。
       注意：该接口只支持全账户查询，按交易对需在本地过滤。"""
    for _ in range(max_retries):
        try:
            r = client.futures_get_open_algo_orders()
            if isinstance(r, dict):
                r = r.get("orders") or r.get("data") or []
            return r if isinstance(r, list) else []
        except BinanceAPIException as e:
            print(f"  ⚠ 拉取算法委托失败: {e}")
            time.sleep(0.5)
        except (BinanceRequestException, requests.exceptions.RequestException):
            time.sleep(0.8)
    return None


def _algo_ids_for_symbol(client, symbol, side=None, cache=None):
    """该交易对(可选限定平仓方向 side)所有止损算法单的 algoId 列表；查询失败返回 None。
       传入 cache(一次拉好的全账户算法单 list)可避免重复请求。"""
    oo = cache if cache is not None else _algo_open_orders(client)
    if oo is None:
        return None
    ids = []
    for o in oo:
        if o.get("symbol") != symbol:
            continue
        if side and o.get("side") != side:
            continue
        aid = o.get("algoId")
        if aid is not None:
            ids.append(aid)
    return ids


def _cancel_one_algo(client, algo_id, symbol=None):
    """撤掉一个算法单。撤成功/本就不存在(-2011)都算成功，返回 True；否则 False。"""
    try:
        client.futures_cancel_algo_order(algoId=algo_id)
        return True
    except BinanceAPIException as e:
        if e.code == -2011:               # 已不存在 = 也算撤干净
            return True
        if symbol is not None:            # 兜底：万一这个 id 其实是普通 orderId
            try:
                client.futures_cancel_order(symbol=symbol, orderId=algo_id)
                return True
            except Exception:
                pass
        print(f"  ⚠ 撤算法单失败 {symbol} algoId={algo_id}: {e}")
        return False
    except (BinanceRequestException, requests.exceptions.RequestException):
        return False


def _list_open_stops(client, symbol):
    """该交易对当前所有【止损算法单】的 algoId 列表；查询失败返回 None。"""
    return _algo_ids_for_symbol(client, symbol)


def cancel_all_orders(client, symbol):
    """撤掉该交易对全部【止损算法单】，并【核验确实撤干净】后才返回 True。
       止损单是算法委托：用 futures_get_open_algo_orders 查、futures_cancel_algo_order 撤。
       流程：查 algoId → 逐个撤 → 复查；只有确认『没有任何止损算法单残留』才返回 True，
       否则返回 False（这一轮就不挂新单，避免裸仓与重复堆积）。"""
    for _ in range(STOP_VERIFY_RETRY):
        ids = _algo_ids_for_symbol(client, symbol)
        if ids is None:                # 查询失败 → 无法确认是否撤净 → 保守返回 False
            return False
        if not ids:                    # ✓ 没有任何止损算法单残留
            return True
        for aid in ids:                # 逐个按 algoId 撤
            _cancel_one_algo(client, aid, symbol)
        time.sleep(0.3)

    left = _algo_ids_for_symbol(client, symbol)
    if left:
        print(f"  ⚠ {symbol} 仍有 {len(left)} 个止损算法单撤不掉，本轮不挂新单。")
    return not left


def _extract_order_id(o):
    """从下单返回里取订单标识：普通单用 orderId；条件单(部分账户的止损单)用 algoId，标记成 'algo:xxx'。"""
    if not isinstance(o, dict):
        return None
    if o.get("orderId") is not None:
        return o["orderId"]
    if o.get("algoId") is not None:
        return f"algo:{o['algoId']}"
    return None


def _find_existing_stop_id(client, symbol, close_side):
    """回查该交易对(平仓方向)是否已存在本程序这类止损算法单，返回 'algo:<algoId>' 或 None。
       用途：下单请求因网络抖动丢了响应时，单子其实可能已经挂到交易所了——
       回查一下既能避免重复下单，也能避免把"已挂上的单"当成失败而留下孤儿单。"""
    ids = _algo_ids_for_symbol(client, symbol, side=close_side)
    if not ids:
        return None
    return f"algo:{ids[0]}"


def _all_positions_map(client, max_retries=3):
    """一次性拉取【全账户】持仓，返回 {symbol: positionAmt}。失败返回 None。
       一次请求拿到所有币种持仓，避免对每个交易对各查一次（省权重、更一致）。"""
    for _ in range(max_retries):
        try:
            info = client.futures_position_information()
            m = {}
            for p in info:
                if p.get("positionSide", "BOTH") != "BOTH":   # 单向持仓只看 BOTH
                    continue
                amt = float(p.get("positionAmt", 0) or 0)
                m[p["symbol"]] = m.get(p["symbol"], 0.0) + amt
            return m
        except BinanceAPIException as e:
            print(f"  ⚠ 拉取全账户持仓失败: {e}")
            time.sleep(0.5)
        except (BinanceRequestException, requests.exceptions.RequestException):
            time.sleep(0.8)
    return None


def sweep_orphan_stops(client, symbols, reason=""):
    """【全账户·孤儿止损单清扫】——主动安全网。

    背景：止损单是【算法委托】(algoId)。移动止盈靠"撤旧挂新"；一旦某次漏撤、或仓位被交易所
    止损平掉后/平仓后残留的止损算法单没撤，它们就成了"无持仓也存在"的孤儿单，越积越多。

    做法：只针对【本程序管理的 symbols】里【当前已无持仓】的交易对，撤掉其全部止损算法单。
      · 无持仓 ⇒ 任何 reduceOnly 止损都不可能再有效触发，整体撤销绝对安全；
      · 用 futures_get_open_algo_orders 查、futures_cancel_algo_order 逐 algoId 撤（在本账户实测有效）；
      · 绝不碰有持仓的交易对（其止损可能正有效），也绝不碰 symbols 之外、你手动交易的其它币种。

    返回撤掉的数量；查询失败返回 -1。"""
    sym_set = set(symbols)

    # 先查持仓快照，确定哪些本程序交易对"当前无持仓"
    posmap = _all_positions_map(client)
    if posmap is None:
        return -1
    flat_set = {s for s in sym_set if abs(posmap.get(s, 0.0)) < 1e-12}
    if not flat_set:
        return 0

    # 一次拉取全账户算法委托，挑出"无持仓交易对"上的，逐个撤
    oo = _algo_open_orders(client)
    if oo is None:
        return -1
    cancelled = 0
    for o in oo:
        sym = o.get("symbol")
        if sym not in flat_set:
            continue
        aid = o.get("algoId")
        if aid is None:
            continue
        if _cancel_one_algo(client, aid, sym):
            cancelled += 1

    if cancelled:
        tag = f"（{reason}）" if reason else ""
        print(f"  ✓ 全账户清扫{tag}：撤掉 {cancelled} 张无持仓交易对的残留止损算法单")
    return cancelled


def _cancel_symbol_orders(client, symbol):
    """挂新止损前，撤掉该交易对【全部止损算法单】（逐 algoId 撤）。
       止损单是算法委托，必须用 futures_get_open_algo_orders 查、futures_cancel_algo_order 撤——
       这正是之前"撤旧挂新"撤不掉、止损单越堆越多的根源（旧代码撤的是看不到这些单的普通委托接口）。"""
    ids = _algo_ids_for_symbol(client, symbol)
    if not ids:                        # None(查询失败) 或 [](没有) 都无需继续
        return
    for aid in ids:
        _cancel_one_algo(client, aid, symbol)


def place_stop(client, symbol, pos_side, stop_price, qty, f, attempts=3):
    """挂服务器端 STOP_MARKET 保护性止损（reduceOnly + 指定数量，触发即市价平掉该仓位）。
       注意：本账户上这种止损单会进入【算法委托簿】(algoId)，futures_get_open_orders 看不到它，
       因此本程序的查询/撤单/清扫全部走算法委托接口（见 _algo_open_orders / _cancel_one_algo）。
       挂新止损前会先撤掉该交易对全部止损算法单（_cancel_symbol_orders），实现可靠的"撤旧挂新"。
       返回 orderId/'algo:xxx'(成功) / 'IMMEDIATE'(止损价已被现价穿过) / None(失败)。"""
    close_side = "SELL" if pos_side == 1 else "BUY"
    sp = fmt_price(stop_price, f)
    q = fmt_qty(qty, f)
    for i in range(attempts):
        # ★挂新止损前，直接撤掉该交易对【全部】旧挂单（不回查、整体撤）。这是"撤旧挂新"真正可靠的位置：
        #   紧挨着下面的下单，中间不留空隙；整体撤覆盖普通单/条件单两条通道，避免旧单残留→重复堆积。
        _cancel_symbol_orders(client, symbol)
        try:
            o = client.futures_create_order(
                symbol=symbol, side=close_side, type="STOP_MARKET",
                stopPrice=sp, quantity=q, reduceOnly="true",
                workingType=STOP_WORKING_TYPE,
            )
        except BinanceAPIException as e:
            if e.code == -2021:                # 止损价已触及现价
                return "IMMEDIATE"
            if e.code == -2022:                # 旧 reduceOnly 单刚撤、数量还没释放完 → 等一下，下一轮会再撤再挂
                print(f"  ⚠ {symbol} 旧止损单未完全释放(-2022)，重撤后重试")
                time.sleep(0.5)
                continue
            if e.code == -4045:                # 触及【全账户】止损单上限 → 跨交易对清孤儿单（本币种循环开头已撤过）
                print(f"  ⚠ {symbol} 触发止损单数量上限(-4045)，清理全账户残留止损单后重试")
                sweep_orphan_stops(client, SYMBOLS, reason=f"{symbol} 触发-4045")
                time.sleep(0.4)
                continue
            print(f"  ⚠ 挂止损单失败 {symbol}(第{i+1}次): {e}")
            time.sleep(0.5)
            continue
        except (BinanceRequestException, requests.exceptions.RequestException) as e:
            # 网络异常：下单请求可能已经送达交易所并挂上了，只是响应丢了。
            # 先回查一下是否其实已经挂上——避免重复下单 / 误判失败而漏掉这张单(变孤儿)。
            print(f"  ⚠ 挂止损单网络异常 {symbol}(第{i+1}次): {short_err(e)}")
            time.sleep(0.8)
            existing = _find_existing_stop_id(client, symbol, close_side)
            if existing is not None:
                return existing
            continue
        oid = _extract_order_id(o)
        if oid is not None:                    # 成功，返回体里直接给了 orderId / algoId
            return oid
        code = o.get("code") if isinstance(o, dict) else None
        if code == -2021:
            return "IMMEDIATE"
        # 返回体里没取到 id、但下单没报错 → 单子多半已挂进【算法委托簿】：
        # 回查算法委托确认并取回 algoId（避免把"已挂上的单"当失败 → 既不重复下单也不留孤儿）。
        time.sleep(0.4)
        found = _find_existing_stop_id(client, symbol, close_side)
        if found is not None:
            return found
        print(f"  ⚠ 挂止损单返回异常 {symbol}，原始返回: {o}")
        return None
    # 重试用尽：最后再回查一次算法委托端是否其实已经有这张止损单（应对响应反复丢失）
    return _find_existing_stop_id(client, symbol, close_side)


# ===================================================================
#  实盘账户状态
# ===================================================================
class LiveState:
    def __init__(self, start_balance):
        self.start_balance = start_balance
        self.symbols = {}     # symbol -> dict(df, pos, last_closed_ot, atr, filters, stats)

    def init_symbol(self, symbol, df, filters):
        _, atr_last, _ = last_signal_and_atr(df)
        self.symbols[symbol] = {
            "df": df,
            "pos": None,                       # None 或 dict
            "last_closed_ot": int(df["open_time"].iloc[-1]),
            "atr": atr_last,
            "filters": filters,
            "stats": {"trades": 0, "wins": 0, "pnl": 0.0},
        }


def now_str():
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


# 网络/接口异常的统一类型，便于在主循环里兜底捕获（避免偶发网络错误把程序搞崩）
NET_ERRORS = (BinanceAPIException, BinanceRequestException, requests.exceptions.RequestException)


def short_err(e):
    """把冗长的异常压成一句话，避免断网时刷屏。"""
    if isinstance(e, requests.exceptions.SSLError):
        return "SSL/证书异常（多半是DNS被污染或被中间人拦截，需走代理直连币安）"
    if isinstance(e, requests.exceptions.ConnectionError):
        return "无法连接币安（网络不可达或被拦截，检查网络/代理）"
    if isinstance(e, requests.exceptions.Timeout):
        return "连接币安超时"
    return str(e)[:160]


# ---- 开仓：市价单 + 立刻挂服务器端 3% 止损 ----
def live_open(client, st, symbol, side, ref_price, emas=None):
    s = st.symbols[symbol]
    f = s["filters"]

    # ★动态保证金：开仓时按「当前总钱包余额」× MARGIN_RATIO 计算本单保证金与名义价值
    total, avail = get_balances(client)
    margin = total * MARGIN_RATIO
    if MIN_MARGIN > 0:
        margin = max(margin, MIN_MARGIN)
    notional = margin * LEVERAGE

    # 资金检查：可用余额要够这单保证金
    if avail < margin:
        print(f"  ⚠ 可用余额不足({avail:.2f}U < 本单保证金{margin:.2f}U)，跳过开仓 {symbol}")
        return False

    qv = qty_value(notional / ref_price, f)
    if qv < f["minQty"] or qv * ref_price < f["minNotional"]:
        print(f"  ⚠ {symbol} 计算数量过小(qty={qv}, 名义{qv*ref_price:.2f}U)，不满足交易所最小限制，跳过")
        return False

    side_str = "BUY" if side == 1 else "SELL"
    try:
        client.futures_create_order(symbol=symbol, side=side_str, type="MARKET",
                                    quantity=fmt_qty(qv, f))
    except BinanceAPIException as e:
        print(f"  ⚠ 开仓失败 {symbol}: {e}")
        return False

    # 市价单成交后，重试几次读取真实持仓（接口可能有短暂延迟/空返回；
    # 若不重试，万一这里读到空就会误判"开仓失败"，从而留下一个没有止损的裸仓）
    qty_filled, entry_price = 0.0, 0.0
    for _ in range(6):
        time.sleep(0.5)
        pos_amt, ep = get_position(client, symbol)
        if abs(pos_amt) > 1e-12:
            qty_filled = abs(pos_amt)
            entry_price = ep if ep > 0 else ref_price
            break
    if qty_filled <= 0:
        print(f"  ⚠ {symbol} 下单后多次未检测到持仓，请到币安手动核对该交易对是否有仓位！")
        return False
    entry_ms = int(time.time() * 1000)

    # 立刻挂服务器端保护性止损（3%）
    stop0 = entry_price * (1 - SL_PCT) if side == 1 else entry_price * (1 + SL_PCT)
    stop0 = price_value(stop0, f)
    stop_id = place_stop(client, symbol, side, stop0, qty_filled, f)

    s["pos"] = {
        "side": side, "entry": entry_price, "qty": qty_filled,
        "entry_ms": entry_ms, "ext": entry_price,
        "stop": stop0, "stop_id": stop_id, "raised": False,
        "last_move_ts": 0.0,               # 上次移动止损的时间戳（配合 TRAIL_MIN_INTERVAL 节流）
    }

    arrow = "多" if side == 1 else "空"
    extra = ""
    if emas:
        f1, f2, s1, s2 = emas
        extra = f" | EMA144={f1:.6g} 169={f2:.6g} 576={s1:.6g} 676={s2:.6g}"
    print(f"  [{now_str()}] ▶ 开{arrow} {symbol} @ {entry_price:.6g} 数量{qty_filled:g} "
          f"保证金{margin:.2f}U(余额{total:.2f}U×{MARGIN_RATIO:.0%}) 名义{notional:.0f}U 止损{stop0:.6g}{extra}")

    # 没能挂上保护性止损 → 绝不持有"裸仓"：立即市价平掉并跳过本次交易
    if stop_id is None:
        print(f"  ⚠⚠ {symbol} 未能挂上保护性止损单，为避免裸仓已立即平仓并跳过本次开仓！")
        print(f"      若反复出现，请把上面打印的『原始返回』发我，多半是下单参数/权限/精度问题。")
        live_close_market(client, st, symbol, "no_stop_protect")
        return False

    # 极端情况：刚开仓价格就已穿过止损价 → 立刻市价平
    if stop_id == "IMMEDIATE":
        print(f"  ⚠ {symbol} 开仓瞬间已触及止损价，立即平仓")
        live_close_market(client, st, symbol, "stop_loss")
    return True


# ---- 平仓（市价，reduceOnly）：用于反手信号，或止损价已被穿过时 ----
def live_close_market(client, st, symbol, reason):
    s = st.symbols[symbol]
    pos = s["pos"]
    if not pos:
        return
    cancel_all_orders(client, symbol)   # 全撤：清掉该仓位的止损单及任何残留孤儿单

    amt, _ = get_position(client, symbol)
    if abs(amt) > 0:
        close_side = "SELL" if amt > 0 else "BUY"
        try:
            client.futures_create_order(symbol=symbol, side=close_side, type="MARKET",
                                        quantity=fmt_qty(abs(amt), s["filters"]),
                                        reduceOnly="true")
        except BinanceAPIException as e:
            print(f"  ⚠ 平仓下单提示 {symbol}: {e}")
    _finalize_close(client, st, symbol, reason)


# ---- 平仓善后：读真实盈亏、记账、打印、清空本地仓位 ----
def _finalize_close(client, st, symbol, reason):
    s = st.symbols[symbol]
    pos = s["pos"]
    if not pos:
        return
    time.sleep(1.2)  # 等账单(income)落账
    pnl = realized_pnl_since(client, symbol, pos["entry_ms"])

    stt = s["stats"]
    stt["trades"] += 1
    stt["pnl"] += pnl
    if pnl > 0:
        stt["wins"] += 1

    side = pos["side"]
    arrow = "多" if side == 1 else "空"
    total, _ = get_balances(client)
    print(f"  [{now_str()}] ■ 平{arrow} {symbol} | {reason} | 真实盈亏 {pnl:+.4f}U | 钱包余额 {total:.2f}U")
    _log_trade(symbol, side, pos["entry"], pos["qty"], pnl, reason, total)
    s["pos"] = None
    print_stats(st, total)


def _log_trade(symbol, side, entry, qty, pnl, reason, balance):
    os.makedirs(RESULT_DIR, exist_ok=True)
    head = not os.path.exists(TRADES_CSV)
    with open(TRADES_CSV, "a", encoding="utf-8-sig") as fp:
        if head:
            fp.write("时间,交易对,方向,入场价,数量,真实盈亏U,离场原因,钱包余额U\n")
        fp.write(f"{now_str()},{symbol},{'多' if side==1 else '空'},"
                 f"{entry:.8g},{qty:g},{pnl:.4f},{reason},{balance:.2f}\n")


def print_stats(st, balance):
    rows = []
    tot_tr = tot_win = 0
    tot_pnl = 0.0
    for sym in st.symbols:
        stt = st.symbols[sym]["stats"]
        tr, w, pnl = stt["trades"], stt["wins"], stt["pnl"]
        wr = (w / tr) if tr else 0.0
        rows.append((sym, tr, wr, pnl))
        tot_tr += tr; tot_win += w; tot_pnl += pnl
    rows.sort(key=lambda x: x[3], reverse=True)
    print("  ┌─ 各交易对统计 ───────────────────────────────")
    print(f"  │ {'交易对':<10}{'笔数':>5}{'胜率':>9}{'累计盈亏U':>12}")
    for sym, tr, wr, pnl in rows:
        print(f"  │ {sym:<10}{tr:>5}{wr:>8.1%}{pnl:>12.3f}")
    owr = (tot_win / tot_tr) if tot_tr else 0.0
    print(f"  │ {'合计':<10}{tot_tr:>5}{owr:>8.1%}{tot_pnl:>12.3f}")
    base = st.start_balance if st.start_balance else 1.0
    print(f"  └─ 钱包余额 {balance:.2f}U（起始 {st.start_balance:.2f}U，"
          f"变动 {(balance-st.start_balance)/base:+.2%}）")
    _write_stats_csv(st, balance)


def _write_stats_csv(st, balance):
    os.makedirs(RESULT_DIR, exist_ok=True)
    with open(STATS_CSV, "w", encoding="utf-8-sig") as fp:
        fp.write("交易对,笔数,胜率,累计盈亏U\n")
        for sym in st.symbols:
            stt = st.symbols[sym]["stats"]
            wr = (stt["wins"] / stt["trades"]) if stt["trades"] else 0.0
            fp.write(f"{sym},{stt['trades']},{wr:.4f},{stt['pnl']:.4f}\n")
        fp.write(f"钱包余额U,{balance:.2f},,\n")


# ---- 每根收盘后：按信号开/平/反手（逻辑与模拟盘一致）----
def live_on_new_closed(client, st, symbol, ref_price):
    s = st.symbols[symbol]
    sig, atr_last, emas = last_signal_and_atr(s["df"])
    s["atr"] = atr_last
    pos = s["pos"]

    if pos is None:
        if sig == "long":
            live_open(client, st, symbol, 1, ref_price, emas)
        elif sig == "short":
            live_open(client, st, symbol, -1, ref_price, emas)
    else:
        if pos["side"] == 1 and sig == "short":
            live_close_market(client, st, symbol, "reverse_signal")
            live_open(client, st, symbol, -1, ref_price, emas)   # 反手开空（保证金按平仓后的新余额×10%动态计算）
        elif pos["side"] == -1 and sig == "long":
            live_close_market(client, st, symbol, "reverse_signal")
            live_open(client, st, symbol, 1, ref_price, emas)    # 反手开多


# ---- 实时维护移动止盈：随盘中新高/新低移动服务器端止损单 ----
def live_update_trail(client, st, symbol, cur_high, cur_low, last_price):
    s = st.symbols[symbol]
    pos = s["pos"]
    if pos is None:
        return
    atr = s["atr"]
    f = s["filters"]
    side = pos["side"]
    qty = pos["qty"]

    # 计算新的止损候选价，并判断是否朝有利方向移动（多头只上移，空头只下移）
    if side == 1:
        if cur_high > pos["ext"]:
            pos["ext"] = cur_high
        cand = price_value(pos["ext"] - ATR_MULT * atr, f)
        improve = cand > pos["stop"]
        arrow = "↑"
    else:
        if cur_low < pos["ext"]:
            pos["ext"] = cur_low
        cand = price_value(pos["ext"] + ATR_MULT * atr, f)
        improve = cand < pos["stop"]
        arrow = "↓"

    if not improve:
        return

    # —— 节流（本次修复关键）：过滤掉"过于频繁"和"过于微小"的移动，避免撤旧挂新把委托堆到交易所上限 ——
    now = time.time()
    if now - pos.get("last_move_ts", 0.0) < TRAIL_MIN_INTERVAL:
        return                                       # 距上次移动止损还不到最小间隔，先不动
    ref = pos["stop"] if pos["stop"] else last_price
    if ref and abs(cand - pos["stop"]) < TRAIL_MIN_IMPROVE * ref:
        return                                       # 改善幅度太小（基本是 1 个 tick 的抖动），不值得撤挂

    # 挂新止损 = "先撤掉该交易对全部旧挂单、再挂新止损"，这套撤旧挂新现在都在 place_stop 内部完成
    # （紧贴下单、整体撤、覆盖条件单通道），所以这里不必、也不再单独调 cancel_all_orders 预撤。
    # 若新单最终没挂上（place_stop 返回 None），下面会先尝试补回原止损，仍失败才为避免裸仓平仓。
    old_level = pos["stop"]
    new_id = place_stop(client, symbol, side, cand, qty, f)

    if new_id == "IMMEDIATE":                     # 新止损价已被现价穿过 → 立刻平
        live_close_market(client, st, symbol, "atr_trail")
        return
    if new_id:
        pos["stop"], pos["stop_id"], pos["raised"] = cand, new_id, True
        pos["last_move_ts"] = now
        print(f"  [{now_str()}] {arrow} {symbol} 移动止损更新至 {cand:.6g}")
        return

    # 新单没挂上：把旧级别补回去，保证仓位仍有止损
    back_id = place_stop(client, symbol, side, old_level, qty, f)
    if back_id and back_id != "IMMEDIATE":
        pos["stop_id"] = back_id
        print(f"  ⚠ {symbol} 移动止损更新失败，已恢复原止损 {old_level:.6g}")
    else:
        print(f"  ⚠⚠ {symbol} 止损更新失败且无法恢复，为避免裸仓立即平仓")
        live_close_market(client, st, symbol, "stop_update_failed")


# ---- 对账：检测服务器端止损是否已成交（仓位被交易所平掉）----
def reconcile(client, st, symbol):
    s = st.symbols[symbol]
    pos = s["pos"]
    if pos is None:
        return
    amt, _ = get_position(client, symbol)
    if abs(amt) < 1e-12:
        reason = "atr_trail" if pos["raised"] else "stop_loss"
        cancel_all_orders(client, symbol)  # 仓位已被交易所平掉，全撤清掉可能残留的孤儿止损单
        _finalize_close(client, st, symbol, reason)


# ===================================================================
#  启动自检 + 主循环
# ===================================================================
def preflight_clean(client, symbols):
    """启动自检：相关交易对不能有残留持仓；无持仓交易对上也不能有残留止损算法单。
       止损单是算法委托，必须用 futures_get_open_algo_orders 查（旧代码查 futures_get_open_orders
       一直返回 0，所以从来没拦住过堆积——这里改对）。"""
    dirty = []
    posmap = _all_positions_map(client)
    algo = _algo_open_orders(client)
    algo_by_sym = {}
    if algo:
        for o in algo:
            algo_by_sym[o.get("symbol")] = algo_by_sym.get(o.get("symbol"), 0) + 1
    for sym in symbols:
        amt = posmap.get(sym, 0.0) if posmap is not None else 0.0
        if abs(amt) > 0:
            dirty.append(f"{sym} 有持仓({amt})")
        else:
            n = algo_by_sym.get(sym, 0)   # 无持仓却还有止损算法单 = 残留孤儿单
            if n:
                dirty.append(f"{sym} 有残留止损算法单({n}个)")
    return dirty


def confirm_live():
    """实盘启动提示。已改为【非交互】：只打印警告，不再要求输入，方便后台/nohup/screen 持续运行。"""
    if USE_TESTNET:
        return True
    print("\n" + "=" * 60)
    print(f"⚠ 实盘模式（真金白银）：将使用真实资金下单，{LEVERAGE}x 杠杆，亏损不可逆。")
    print("  已关闭交互确认，程序将直接开始运行。停止请用 Ctrl+C（前台）或 kill <pid>（后台）。")
    print("  若只想测试流程，请把 config.py 里的 USE_TESTNET 改回 True。")
    print("=" * 60)
    return True


_running = True
def _stop(*_):
    global _running
    _running = False
    print("\n收到停止信号，准备退出...")
_signal.signal(_signal.SIGINT, _stop)
_signal.signal(_signal.SIGTERM, _stop)


def _acquire_single_instance_lock():
    """单实例锁：避免不小心同时跑了多份程序对同一账户下单/互相撤单
       —— 多实例会让"撤旧挂新"互相打架，是止损委托爆量的常见原因之一。"""
    try:
        if os.path.exists(LOCK_FILE):
            with open(LOCK_FILE) as fp:
                old = fp.read().strip()
            if old.isdigit():
                pid = int(old)
                alive = True
                try:
                    os.kill(pid, 0)        # 不发真实信号，仅探测该进程是否存在
                except OSError:
                    alive = False
                if alive:
                    print(f"⚠ 检测到已有实例在运行 (PID={pid})。为避免多实例同时下单/互撤，已拒绝启动。")
                    print(f"  若确认旧实例确实已退出，删除 {LOCK_FILE} 后再重试。")
                    return False
        with open(LOCK_FILE, "w") as fp:
            fp.write(str(os.getpid()))
        return True
    except Exception as e:
        print(f"  ⚠ 创建单实例锁失败（忽略并继续）: {short_err(e)}")
        return True


def main():
    global _client
    os.makedirs(RESULT_DIR, exist_ok=True)
    if not _acquire_single_instance_lock():
        return

    mode = "测试网(假钱)" if USE_TESTNET else "★真实盘(真钱)★"
    print(f"[{now_str()}] 准备启动实盘交易 | 环境={mode} | {LEVERAGE}x | "
          f"每单保证金=总余额×{MARGIN_RATIO:.0%} | 止损 {SL_PCT:.0%} | ATR止盈 {ATR_MULT:.0f}x")

    if not confirm_live():
        print("已取消。")
        return

    _client = make_client()

    # 连通性 & 密钥校验
    try:
        total, avail = get_balances(_client)
    except BinanceAPIException as e:
        print(f"连接/鉴权失败：{e}")
        print("请检查：API Key 是否正确、是否开启了合约权限、IP白名单、以及测试网开关是否与Key匹配。")
        return
    except Exception as e:
        print(f"连接失败：{short_err(e)}")
        print("若是网络/连接问题，请检查服务器到币安的连通性（必要时在 config.py 配置 PROXY 走代理）。")
        return
    print(f"账户连接成功：钱包余额 {total:.2f}U，可用 {avail:.2f}U")

    if not ensure_one_way(_client):
        return

    # 校验交易对 + 读取下单精度
    filters = build_symbol_filters(_client, SYMBOLS)
    symbols = list(filters.keys())
    if not symbols:
        print("没有可交易的交易对，退出。")
        return

    # 启动先做一次【全账户清扫】：撤掉历史遗留的"无持仓残留止损单"
    # （就是你现在挂着的那 200 张孤儿单），避免下方自检因这些残留挂单而拒绝启动。
    # 注意：只清"无持仓"交易对上的单；若某交易对还有持仓，其挂单不会被动，仍会被下方自检拦下。
    print("启动清理：撤销本程序交易对上『无持仓的残留止损单』...")
    try:
        sweep_orphan_stops(_client, symbols, reason="启动清理")
    except Exception as e:
        print(f"  ⚠ 启动清理时出错（可继续）: {short_err(e)}")

    # 启动自检：不能有残留持仓；无持仓交易对也不能有残留止损算法单
    dirty = preflight_clean(_client, symbols)
    if dirty:
        print("⚠ 检测到相关交易对存在残留持仓或残留止损单，为避免冲突已停止启动：")
        for d in dirty:
            print("   -", d)
        print("说明：『残留止损算法单』本应被上面的启动清理撤掉；若仍列出，多半是查询接口本轮抖动，")
        print("      可重跑本程序，或用 clean_algo_stops.py 清理。『有持仓』则需你先在币安手动平仓后再启动。")
        return

    # 设置杠杆/保证金模式
    print("设置杠杆与保证金模式...")
    for sym in symbols:
        setup_symbol_exchange(_client, sym)

    st = LiveState(start_balance=total)
    print("预热历史K线中（每个交易对约 %d 根，从最新往回拉）..." % WARMUP_BARS)
    for sym in symbols:
        df = warmup_df(sym)
        if len(df) < EMA_SLOW2 + 100:
            print(f"  {sym} 历史数据不足({len(df)})，跳过")
            continue
        st.init_symbol(sym, df, filters[sym])
        t0 = pd.to_datetime(int(df["open_time"].iloc[0]), unit="ms", utc=True)
        t1 = pd.to_datetime(int(df["open_time"].iloc[-1]), unit="ms", utc=True)
        span = (int(df["open_time"].iloc[-1]) - int(df["open_time"].iloc[0])) // 60_000 + 1
        gap_note = "" if span == len(df) else f" ⚠缺口({span-len(df)}根)"
        _, _, (e1, e2, e3, e4) = last_signal_and_atr(df)
        print(f"  {sym} 预热 {len(df)} 根 | {t0:%m-%d %H:%M}~{t1:%m-%d %H:%M} UTC{gap_note}")
        print(f"      EMA144={e1:.6g} 169={e2:.6g} 576={e3:.6g} 676={e4:.6g}")
    if not st.symbols:
        print("无可用交易对，退出。")
        return

    print(f"\n[{now_str()}] 开始实时监控（每 {POLL_SECONDS}s 轮询一次，Ctrl+C 退出）\n")
    net_down = False
    last_sweep = 0.0          # 上次全账户清扫的时间戳（配合 ORPHAN_SWEEP_INTERVAL 节流）
    while _running:
        n_sym = len(st.symbols)
        round_fail = 0
        printed_err = False
        for sym in list(st.symbols.keys()):
            if not _running:
                break
            try:
                kl = fetch_klines(sym, limit=5)
            except Exception as e:
                round_fail += 1
                if not net_down and not printed_err:   # 断网时整轮只提示一次，避免刷屏
                    print(f"  [{now_str()}] ⚠ 行情/网络异常：{short_err(e)}")
                    printed_err = True
                continue
            if len(kl) < 2:
                continue
            forming = kl[-1]                  # 当前未收盘K线
            forming_open, f_high, f_low, last_price = forming[1], forming[2], forming[3], forming[4]
            s = st.symbols[sym]

            # 1) 处理新收盘的K线：更新指标、开/平/反手
            new_closed = [k for k in kl[:-1] if k[0] > s["last_closed_ot"]]
            if new_closed:
                if new_closed[0][0] != s["last_closed_ot"] + 60_000:
                    print(f"  ⚠ {sym} 检测到K线缺口，重新预热以保证EMA准确")
                    try:
                        df2 = warmup_df(sym)
                        if len(df2) >= EMA_SLOW2 + 100:
                            s["df"] = df2
                            s["last_closed_ot"] = int(df2["open_time"].iloc[-1])
                        reconcile(_client, st, sym)
                        live_update_trail(_client, st, sym, f_high, f_low, last_price)
                    except NET_ERRORS as e:
                        print(f"  ⚠ {sym} 接口异常: {short_err(e)}")
                    continue
                add = pd.DataFrame(new_closed, columns=KLINE_COLS)
                s["df"] = pd.concat([s["df"], add], ignore_index=True)
                if len(s["df"]) > MAX_DF_BARS:
                    s["df"] = s["df"].iloc[-MAX_DF_BARS:].reset_index(drop=True)
                s["last_closed_ot"] = int(new_closed[-1][0])
                # 入场参考价用最新价（市价单实际在现价附近成交）
                try:
                    live_on_new_closed(_client, st, sym, last_price)
                except NET_ERRORS as e:
                    print(f"  ⚠ {sym} 接口异常: {short_err(e)}")

            # 2) 对账(检测止损是否成交) + 维护移动止盈
            try:
                reconcile(_client, st, sym)
                live_update_trail(_client, st, sym, f_high, f_low, last_price)
            except NET_ERRORS as e:
                print(f"  ⚠ {sym} 接口异常: {short_err(e)}")

        # —— 周期性【全账户】清扫：撤掉"已无持仓却还残留"的止损单 ——
        # 这是防止止损委托积压到 -4045 全账户上限(200)的主动安全网：
        # 哪怕某条路径漏撤了一张、或仓位被交易所止损平掉后残留了止损单，这里都会兜底清掉。
        if _running and not net_down and (time.time() - last_sweep >= ORPHAN_SWEEP_INTERVAL):
            try:
                sweep_orphan_stops(_client, list(st.symbols.keys()), reason="周期清扫")
            except NET_ERRORS as e:
                print(f"  ⚠ 周期清扫接口异常: {short_err(e)}")
            last_sweep = time.time()

        # —— 网络健康判断 + 退避 ——
        if n_sym > 0 and round_fail >= n_sym:           # 整轮全挂 → 判定断网
            if not net_down:
                print(f"  [{now_str()}] ⚠ 与币安连接中断，后台自动重试中。"
                      f"已开仓的仓位仍由交易所端止损单保护；若长期中断请检查网络，或在 config.py 配置 PROXY 走代理。")
                net_down = True
            wait = min(60, max(POLL_SECONDS, 15))       # 断网时拉长重试间隔
        else:
            if net_down:
                print(f"  [{now_str()}] ✓ 与币安的连接已恢复，继续正常运行。")
                net_down = False
            wait = POLL_SECONDS
        # 分段 sleep，保证 Ctrl+C / kill 能及时退出
        slept = 0
        while _running and slept < wait:
            time.sleep(1)
            slept += 1

    # 退出前打印一次总览（注意：退出后仓位与服务器止损单仍在交易所挂着，按需手动处理）
    try:
        total, _ = get_balances(_client)
    except Exception:
        total = st.start_balance
    print(f"\n[{now_str()}] 已停止。最终统计：")
    print_stats(st, total)
    print("提示：程序退出后，已有持仓与服务器端止损单仍然有效，如需平仓请到币安手动操作。")
    try:
        os.remove(LOCK_FILE)
    except OSError:
        pass


if __name__ == "__main__":
    main()