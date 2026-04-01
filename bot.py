import os
import requests
import json
import time
import logging
from datetime import datetime, timedelta
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading

DHAN_TOKEN = os.environ.get("DHAN_ACCESS_TOKEN", "")
DHAN_CLIENT = os.environ.get("DHAN_CLIENT_ID", "1110090989")
DHAN_BASE = "https://api.dhan.co/v2"

SETTINGS = {
    "lot_size": 75,
    "max_lots": 1,
    "normal_sl_buffer": 20,
    "pp_sl_buffer": 15,
    "tp1_points": 50,
    "tp2_points": 100,
    "tp1_exit_pct": 50,
    "vix_max": 20,
    "max_gap": 200,
    "min_candle_body": 60,
    "min_bounce_pct": 50,
    "min_score": 6,
    "major_tests": 3,
    "pivot_lookback": 5,
    "hard_exit": "10:30",
    "entry_time": "09:20",
    "shadow_mode": True,
}

PORT = int(os.environ.get("PORT", 8080))

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s", handlers=[logging.StreamHandler()])
log = logging.getLogger("NiftyAlgo")

def dhan_headers():
    return {"access-token": DHAN_TOKEN, "Content-Type": "application/json", "Accept": "application/json"}

def dhan_post(endpoint, data):
    try:
        r = requests.post(DHAN_BASE + "/" + endpoint, json=data, headers=dhan_headers(), timeout=15)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        log.error("Dhan POST %s: %s" % (endpoint, e))
        return None

def dhan_get(endpoint):
    try:
        r = requests.get(DHAN_BASE + "/" + endpoint, headers=dhan_headers(), timeout=15)
        return r.json() if r.status_code == 200 else None
    except Exception as e:
        log.error("Dhan GET %s: %s" % (endpoint, e))
        return None

def get_candles(timeframe, days_back=30):
    tf_map = {"1D": "D", "4H": "240", "1H": "60", "15M": "15", "5M": "5"}
    today = datetime.now().strftime("%Y-%m-%d")
    start = (datetime.now() - timedelta(days=days_back)).strftime("%Y-%m-%d")
    data = dhan_post("charts/historical", {
        "securityId": "13",
        "exchangeSegment": "IDX_I",
        "instrument": "INDEX",
        "interval": tf_map.get(timeframe, "D"),
        "fromDate": start,
        "toDate": today
    })
    if data and "open" in data:
        candles = []
        for i in range(len(data["open"])):
            candles.append({
                "o": data["open"][i],
                "h": data["high"][i],
                "l": data["low"][i],
                "c": data["close"][i],
                "v": data.get("volume", [0] * len(data["open"]))[i]
            })
        return candles
    return []

def get_nifty_ltp():
    data = dhan_post("marketfeed/ltp", {"IDX_I": ["13"]})
    if data and "data" in data:
        return float(data["data"].get("IDX_I:13", {}).get("last_price", 0))
    return 0

def get_vix():
    data = dhan_post("marketfeed/ltp", {"IDX_I": ["26"]})
    if data and "data" in data:
        return float(data["data"].get("IDX_I:26", {}).get("last_price", 0))
    return 0

def get_oi_data():
    data = dhan_get("optionchain?UnderlyingScrip=NIFTY")
    if not data or "data" not in data:
        return 0, 0
    max_ce_oi = 0
    max_ce_strike = 0
    max_pe_oi = 0
    max_pe_strike = 0
    for item in data["data"]:
        oi = item.get("openInterest", 0)
        if item.get("optionType") == "CE" and oi > max_ce_oi:
            max_ce_oi = oi
            max_ce_strike = item.get("strikePrice", 0)
        elif item.get("optionType") == "PE" and oi > max_pe_oi:
            max_pe_oi = oi
            max_pe_strike = item.get("strikePrice", 0)
    return max_ce_strike, max_pe_strike

def find_atm_option(price, option_type="CE"):
    data = dhan_get("optionchain?UnderlyingScrip=NIFTY")
    strike = round(price / 50) * 50
    if data and "data" in data:
        for item in data["data"]:
            if item.get("strikePrice") == strike and item.get("optionType") == option_type:
                return item.get("securityId", ""), strike
    return "", strike

def place_order(security_id, txn_type, quantity):
    if SETTINGS["shadow_mode"]:
        log.info("[SHADOW] %s %sx %s" % (txn_type, quantity, security_id))
        return {"orderId": "SHADOW_" + str(int(time.time())), "status": "SHADOW"}
    return dhan_post("orders", {
        "dhanClientId": DHAN_CLIENT,
        "transactionType": txn_type,
        "exchangeSegment": "NSE_FNO",
        "productType": "INTRADAY",
        "orderType": "MARKET",
        "securityId": security_id,
        "quantity": quantity,
        "price": 0,
        "validity": "DAY"
    })

def find_pivot_highs(candles, lb=5):
    pivots = []
    for i in range(lb, len(candles) - lb):
        h = candles[i]["h"]
        is_pivot = True
        for j in range(1, lb + 1):
            if candles[i - j]["h"] >= h or candles[i + j]["h"] >= h:
                is_pivot = False
                break
        if is_pivot:
            merged = False
            for p in pivots:
                if abs(p["price"] - h) / max(h, 1) < 0.003:
                    p["tests"] += 1
                    p["price"] = max(p["price"], h)
                    merged = True
                    break
            if not merged:
                pivots.append({"price": h, "tests": 1, "index": i})
    return pivots

def find_pivot_lows(candles, lb=5):
    pivots = []
    for i in range(lb, len(candles) - lb):
        l = candles[i]["l"]
        is_pivot = True
        for j in range(1, lb + 1):
            if candles[i - j]["l"] <= l or candles[i + j]["l"] <= l:
                is_pivot = False
                break
        if is_pivot:
            merged = False
            for p in pivots:
                if abs(p["price"] - l) / max(l, 1) < 0.003:
                    p["tests"] += 1
                    p["price"] = min(p["price"], l)
                    merged = True
                    break
            if not merged:
                pivots.append({"price": l, "tests": 1, "index": i})
    return pivots

def check_sweep(candles, level, direction):
    for c in candles[-10:]:
        if direction == "bsl" and c["h"] > level:
            return {"swept": True, "sweep_price": c["h"]}
        if direction == "ssl" and c["l"] < level:
            return {"swept": True, "sweep_price": c["l"]}
    return {"swept": False}

def get_sweep_direction(bsl_swept, ssl_swept):
    if ssl_swept and not bsl_swept:
        return "PE"
    if bsl_swept and not ssl_swept:
        return "CE"
    return None

def get_trend(candles):
    if len(candles) < 6:
        return "MIXED"
    recent = candles[-10:]
    highs = []
    lows = []
    for i in range(1, len(recent) - 1):
        if recent[i]["h"] > recent[i - 1]["h"] and recent[i]["h"] > recent[i + 1]["h"]:
            highs.append(recent[i]["h"])
        if recent[i]["l"] < recent[i - 1]["l"] and recent[i]["l"] < recent[i + 1]["l"]:
            lows.append(recent[i]["l"])
    if len(highs) < 2 or len(lows) < 2:
        return "MIXED"
    if highs[-1] > highs[-2] and lows[-1] > lows[-2]:
        return "BULL"
    if highs[-1] < highs[-2] and lows[-1] < lows[-2]:
        return "BEAR"
    return "MIXED"

def find_bos(candles):
    if len(candles) < 10:
        return []
    breaks = []
    recent = candles[-15:]
    swing_lows = []
    swing_highs = []
    for i in range(2, len(recent) - 2):
        if (recent[i]["l"] <= recent[i - 1]["l"] and recent[i]["l"] <= recent[i - 2]["l"] and
                recent[i]["l"] <= recent[i + 1]["l"] and recent[i]["l"] <= recent[i + 2]["l"]):
            swing_lows.append({"price": recent[i]["l"], "idx": i})
        if (recent[i]["h"] >= recent[i - 1]["h"] and recent[i]["h"] >= recent[i - 2]["h"] and
                recent[i]["h"] >= recent[i + 1]["h"] and recent[i]["h"] >= recent[i + 2]["h"]):
            swing_highs.append({"price": recent[i]["h"], "idx": i})
    for i in range(len(recent) - 1, max(0, len(recent) - 5), -1):
        for sl in swing_lows:
            if sl["idx"] < i and recent[i]["c"] < sl["price"]:
                breaks.append({"type": "BOS", "dir": "BEAR", "price": sl["price"]})
                break
        for sh in swing_highs:
            if sh["idx"] < i and recent[i]["c"] > sh["price"]:
                breaks.append({"type": "BOS", "dir": "BULL", "price": sh["price"]})
                break
    return breaks

def find_choch(candles, bsl_swept, ssl_swept):
    if len(candles) < 10:
        return []
    breaks = []
    recent = candles[-15:]
    swing_lows = []
    swing_highs = []
    for i in range(2, len(recent) - 2):
        if (recent[i]["l"] <= recent[i - 1]["l"] and recent[i]["l"] <= recent[i - 2]["l"] and
                recent[i]["l"] <= recent[i + 1]["l"] and recent[i]["l"] <= recent[i + 2]["l"]):
            swing_lows.append(recent[i]["l"])
        if (recent[i]["h"] >= recent[i - 1]["h"] and recent[i]["h"] >= recent[i - 2]["h"] and
                recent[i]["h"] >= recent[i + 1]["h"] and recent[i]["h"] >= recent[i + 2]["h"]):
            swing_highs.append(recent[i]["h"])
    if ssl_swept and len(swing_highs) >= 2:
        if recent[-1]["c"] > swing_highs[-2]:
            breaks.append({"type": "CHoCH", "dir": "BULL"})
    if bsl_swept and len(swing_lows) >= 2:
        if recent[-1]["c"] < swing_lows[-2]:
            breaks.append({"type": "CHoCH", "dir": "BEAR"})
    return breaks

def find_fvg(candles):
    if len(candles) < 3:
        return []
    fvgs = []
    for i in range(len(candles) - 2):
        c1 = candles[i]
        c2 = candles[i + 1]
        c3 = candles[i + 2]
        body = abs(c2["c"] - c2["o"])
        if i >= 5:
            avg = sum(abs(candles[i - j]["c"] - candles[i - j]["o"]) for j in range(5)) / 5
        else:
            avg = body
        if body < avg * 1.2:
            continue
        if c1["l"] > c3["h"] and c2["c"] < c2["o"]:
            fvgs.append({"top": c1["l"], "bot": c3["h"], "type": "BEAR"})
        if c1["h"] < c3["l"] and c2["c"] > c2["o"]:
            fvgs.append({"top": c3["l"], "bot": c1["h"], "type": "BULL"})
    return fvgs[-3:]

def build_scorecard(analysis):
    return {
        "major_zone": analysis.get("near_major", False),
        "sweep": analysis.get("bsl_swept", False) or analysis.get("ssl_swept", False),
        "tf_aligned": analysis.get("alignment") != "MIXED",
        "pp": analysis.get("pp_eligible", False),
        "bos": analysis.get("bos_count", 0) > 0,
        "choch": analysis.get("choch_count", 0) > 0,
        "fvg": analysis.get("fvg_count", 0) > 0,
        "big_candle": False,
        "bounce": False,
    }

def score_total(sc):
    return sum(1 for v in sc.values() if v)

def score_confidence(s):
    m = {0: 0, 1: 30, 2: 40, 3: 50, 4: 55, 5: 65, 6: 72, 7: 80, 8: 88, 9: 92}
    return m.get(s, 0)


class NiftyAlgoBot:
    def __init__(self):
        self.analysis = {}
        self.scorecard = {}
        self.position = None
        self.traded_today = False
        self.sl_hit_today = False
        self.last_date = None
        self.logs = []

    def log(self, msg):
        ts = datetime.now().strftime("%H:%M:%S")
        self.logs.append("[%s] %s" % (ts, msg))
        log.info(msg)
        if len(self.logs) > 100:
            self.logs = self.logs[-100:]

    def reset_daily(self):
        today = datetime.now().date()
        if self.last_date != today:
            self.position = None
            self.traded_today = False
            self.sl_hit_today = False
            self.scorecard = {}
            self.analysis = {}
            self.logs = []
            self.last_date = today
            self.log("Daily reset done")

    def run_analysis(self):
        self.log("Running analysis...")
        c_1d = get_candles("1D", 30)
        c_4h = get_candles("4H", 10)
        c_1h = get_candles("1H", 7)
        self.log("Data: 1D=%d 4H=%d 1H=%d" % (len(c_1d), len(c_4h), len(c_1h)))
        if not c_1d:
            self.log("No 1D data!")
            return
        bsl_levels = sorted(find_pivot_highs(c_1d, SETTINGS["pivot_lookback"]), key=lambda x: x["price"], reverse=True)[:3]
        ssl_levels = sorted(find_pivot_lows(c_1d, SETTINGS["pivot_lookback"]), key=lambda x: x["price"])[:3]
        cmp = c_1d[-1]["c"]
        bsl_swept_any = False
        ssl_swept_any = False
        for b in bsl_levels:
            sw = check_sweep(c_1d, b["price"], "bsl")
            b["swept"] = sw["swept"]
            if sw["swept"]:
                bsl_swept_any = True
        for s in ssl_levels:
            sw = check_sweep(c_1d, s["price"], "ssl")
            s["swept"] = sw["swept"]
            if sw["swept"]:
                ssl_swept_any = True
        direction = get_sweep_direction(bsl_swept_any, ssl_swept_any)
        t_1d = get_trend(c_1d)
        t_4h = get_trend(c_4h) if c_4h else "MIXED"
        t_1h = get_trend(c_1h) if c_1h else "MIXED"
        all_bull = all(t == "BULL" for t in [t_1d, t_4h, t_1h])
        all_bear = all(t == "BEAR" for t in [t_1d, t_4h, t_1h])
        alignment = "BULL" if all_bull else ("BEAR" if all_bear else "MIXED")
        bos = find_bos(c_4h) if c_4h else []
        choch = find_choch(c_4h, bsl_swept_any, ssl_swept_any) if c_4h else []
        fvg = find_fvg(c_4h) if c_4h else []
        oi_ssl, oi_bsl = get_oi_data()
        near_major = False
        pp_eligible = False
        if direction == "CE" and ssl_levels:
            nearest = min(ssl_levels, key=lambda x: abs(x["price"] - cmp))
            if abs(nearest["price"] - cmp) / cmp < 0.01:
                near_major = nearest["tests"] >= SETTINGS["major_tests"]
                pp_eligible = near_major
        elif direction == "PE" and bsl_levels:
            nearest = min(bsl_levels, key=lambda x: abs(x["price"] - cmp))
            if abs(nearest["price"] - cmp) / cmp < 0.01:
                near_major = nearest["tests"] >= SETTINGS["major_tests"]
                pp_eligible = near_major
        self.analysis = {
            "cmp": cmp,
            "bsl": bsl_levels,
            "ssl": ssl_levels,
            "bsl_swept": bsl_swept_any,
            "ssl_swept": ssl_swept_any,
            "direction": direction,
            "trends": {"1D": t_1d, "4H": t_4h, "1H": t_1h},
            "alignment": alignment,
            "bos_count": len(bos),
            "choch_count": len(choch),
            "fvg_count": len(fvg),
            "oi_ssl": oi_ssl,
            "oi_bsl": oi_bsl,
            "near_major": near_major,
            "pp_eligible": pp_eligible,
            "pdh": c_1d[-1]["h"],
            "pdl": c_1d[-1]["l"],
            "pre_score": 0,
            "vix": 0,
        }
        self.scorecard = build_scorecard(self.analysis)
        self.analysis["pre_score"] = score_total(self.scorecard)
        self.log("BSL: %s" % str([(b["price"], b["tests"], b.get("swept")) for b in bsl_levels]))
        self.log("SSL: %s" % str([(s["price"], s["tests"], s.get("swept")) for s in ssl_levels]))
        self.log("Dir: %s | Align: %s | BOS=%d CHoCH=%d FVG=%d" % (direction, alignment, len(bos), len(choch), len(fvg)))
        self.log("Pre-Score: %d/9" % score_total(self.scorecard))

    def check_entry(self):
        if self.traded_today:
            self.log("Already traded. SKIP.")
            return
        if self.sl_hit_today:
            self.log("SL hit today. SKIP.")
            return
        vix = get_vix()
        self.analysis["vix"] = vix
        if vix > SETTINGS["vix_max"]:
            self.log("VIX %.1f > %d. NO TRADE." % (vix, SETTINGS["vix_max"]))
            return
        nifty = get_nifty_ltp()
        prev_close = self.analysis.get("cmp", nifty)
        gap = nifty - prev_close
        if abs(gap) > SETTINGS["max_gap"]:
            self.log("Gap %.0f > %d. NO TRADE." % (gap, SETTINGS["max_gap"]))
            return
        c5m = get_candles("5M", 1)
        if not c5m:
            self.log("No 5M data. SKIP.")
            return
        fc = c5m[0]
        body = abs(fc["c"] - fc["o"])
        rng = fc["h"] - fc["l"]
        bounce_pct = (body / rng * 100) if rng > 0 else 0
        is_green = fc["c"] > fc["o"]
        is_red = fc["c"] < fc["o"]
        color = "GREEN" if is_green else "RED"
        self.log("Candle: body=%.0f range=%.0f bounce=%.0f%% %s" % (body, rng, bounce_pct, color))
        if body < SETTINGS["min_candle_body"]:
            self.log("Body %.0f < %d. SKIP." % (body, SETTINGS["min_candle_body"]))
            return
        if bounce_pct < SETTINGS["min_bounce_pct"]:
            self.log("Bounce %.0f%% < %d%%. SKIP." % (bounce_pct, SETTINGS["min_bounce_pct"]))
            return
        self.scorecard["big_candle"] = True
        self.scorecard["bounce"] = True
        total = score_total(self.scorecard)
        self.log("Score: %d/9 | Confidence: %d%%" % (total, score_confidence(total)))
        if total < SETTINGS["min_score"]:
            self.log("Score %d < %d. NO TRADE." % (total, SETTINGS["min_score"]))
            return
        direction = self.analysis.get("direction")
        if not direction:
            self.log("No direction. SKIP.")
            return
        if direction == "CE" and not is_green:
            self.log("CE but RED candle. SKIP.")
            return
        if direction == "PE" and not is_red:
            self.log("PE but GREEN candle. SKIP.")
            return
        self.log("*** ALL CONDITIONS MET - %s TRADE! ***" % direction)
        entry = fc["c"]
        pp = self.analysis.get("pp_eligible", False) and total >= 8
        if pp:
            if direction == "CE":
                sl = entry - SETTINGS["pp_sl_buffer"]
            else:
                sl = entry + SETTINGS["pp_sl_buffer"]
            sl_type = "PP"
        else:
            if direction == "CE":
                sl = fc["l"] - SETTINGS["normal_sl_buffer"]
            else:
                sl = fc["h"] + SETTINGS["normal_sl_buffer"]
            sl_type = "NORMAL"
        if direction == "CE":
            tp1 = entry + SETTINGS["tp1_points"]
            tp2 = entry + SETTINGS["tp2_points"]
        else:
            tp1 = entry - SETTINGS["tp1_points"]
            tp2 = entry - SETTINGS["tp2_points"]
        opt_id, strike = find_atm_option(entry, direction)
        self.log("Entry:%.0f SL:%.0f(%s) TP1:%.0f TP2:%.0f" % (entry, sl, sl_type, tp1, tp2))
        result = place_order(opt_id, "BUY", SETTINGS["lot_size"])
        order_id = result.get("orderId", "") if result else ""
        self.position = {
            "type": direction,
            "entry": entry,
            "sl": sl,
            "sl_type": sl_type,
            "tp1": tp1,
            "tp2": tp2,
            "tp1_hit": False,
            "qty": SETTINGS["lot_size"],
            "remaining": SETTINGS["lot_size"],
            "opt_id": opt_id,
            "strike": strike,
            "order_id": order_id,
            "pnl": 0,
            "status": "OPEN",
            "entry_time": datetime.now().strftime("%H:%M:%S"),
        }
        self.traded_today = True
        mode = "SHADOW" if SETTINGS["shadow_mode"] else "LIVE"
        self.log("ORDER [%s]: %s@%.0f" % (mode, direction, entry))

    def monitor_position(self):
        if not self.position or self.position["status"] == "CLOSED":
            return
        pos = self.position
        now = datetime.now().strftime("%H:%M")
        nifty = get_nifty_ltp()
        if nifty <= 0:
            return
        if now >= SETTINGS["hard_exit"]:
            self._close(nifty, "10:30 HARD EXIT")
            return
        if pos["type"] == "CE":
            if nifty <= pos["sl"]:
                self._close(pos["sl"], "SL HIT")
                self.sl_hit_today = True
            elif nifty >= pos["tp1"] and not pos["tp1_hit"]:
                pos["tp1_hit"] = True
                pos["remaining"] = pos["qty"] // 2
                pos["sl"] = pos["entry"]
                self.log("TP1 HIT! 50%% exit. SL->BE %.0f" % pos["entry"])
                if not SETTINGS["shadow_mode"]:
                    place_order(pos["opt_id"], "SELL", pos["qty"] // 2)
            elif nifty >= pos["tp2"] and pos["tp1_hit"]:
                self._close(pos["tp2"], "TP2 HIT!")
        elif pos["type"] == "PE":
            if nifty >= pos["sl"]:
                self._close(pos["sl"], "SL HIT")
                self.sl_hit_today = True
            elif nifty <= pos["tp1"] and not pos["tp1_hit"]:
                pos["tp1_hit"] = True
                pos["remaining"] = pos["qty"] // 2
                pos["sl"] = pos["entry"]
                self.log("TP1 HIT! 50%% exit. SL->BE %.0f" % pos["entry"])
                if not SETTINGS["shadow_mode"]:
                    place_order(pos["opt_id"], "SELL", pos["qty"] // 2)
            elif nifty <= pos["tp2"] and pos["tp1_hit"]:
                self._close(pos["tp2"], "TP2 HIT!")

    def _close(self, exit_price, reason):
        pos = self.position
        if pos["type"] == "CE":
            pts = exit_price - pos["entry"]
        else:
            pts = pos["entry"] - exit_price
        pos["pnl"] = pts * 0.5 * pos["remaining"]
        pos["status"] = "CLOSED"
        if not SETTINGS["shadow_mode"] and pos["remaining"] > 0:
            place_order(pos["opt_id"], "SELL", pos["remaining"])
        self.log("EXIT: %s | Pts:%.0f | PnL:Rs.%.0f" % (reason, pts, pos["pnl"]))

    def run(self):
        self.log("=" * 50)
        self.log("RISHABH NIFTY ALGO BOT STARTED")
        self.log("Mode: %s" % ("SHADOW" if SETTINGS["shadow_mode"] else "LIVE"))
        self.log("Rules: 1 trade/day | VIX<%d | 10:30 exit | Score>=%d" % (SETTINGS["vix_max"], SETTINGS["min_score"]))
        self.log("Strategy: L1(BSL/SSL+Sweep) + L2(PP) + L3(BOS/CHoCH/FVG)")
        self.log("=" * 50)
        while True:
            try:
                self.reset_daily()
                t = datetime.now().strftime("%H:%M")
                if t == "09:00":
                    self.run_analysis()
                    vix = get_vix()
                    self.analysis["vix"] = vix
                    if vix > SETTINGS["vix_max"]:
                        self.log("VIX %.1f > %d. NO TRADE today." % (vix, SETTINGS["vix_max"]))
                if t == SETTINGS["entry_time"] and not self.traded_today:
                    if not self.analysis:
                        self.run_analysis()
                    self.check_entry()
                if self.position and self.position["status"] != "CLOSED":
                    self.monitor_position()
                time.sleep(5)
            except KeyboardInterrupt:
                self.log("Stopped.")
                break
            except Exception as e:
                self.log("ERROR: %s" % str(e))
                time.sleep(10)


bot = NiftyAlgoBot()

DASHBOARD_PAGE = (
    '<!DOCTYPE html><html><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">'
    '<title>Rishabh Nifty Algo</title><style>'
    '*{margin:0;padding:0;box-sizing:border-box}'
    'body{font-family:system-ui;background:#0a0a0a;color:#e0e0e0;padding:16px}'
    'h1{font-size:20px;color:#f0b90b;margin-bottom:16px}'
    '.g{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px}'
    '.c{background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:12px}'
    '.c h3{font-size:13px;color:#888;margin-bottom:8px}'
    '.v{font-size:22px;font-weight:600}'
    '.gr{color:#0ecb81}.rd{color:#f6465d}.yl{color:#f0b90b}'
    '.tg{display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;background:#f0b90b22;color:#f0b90b;border:1px solid #f0b90b44}'
    '.sg{display:grid;grid-template-columns:1fr 1fr 1fr;gap:6px}'
    '.si{padding:6px;border-radius:4px;font-size:11px}'
    '.sy{background:#0ecb8118;color:#0ecb81}'
    '.sn{background:#f6465d18;color:#f6465d}'
    '.lg{background:#111;border-radius:8px;padding:12px;max-height:300px;overflow-y:auto;font-family:monospace;font-size:11px;line-height:1.8}'
    '.btn{background:#f0b90b;color:#000;border:none;padding:10px 20px;border-radius:6px;font-weight:600;cursor:pointer}'
    'table{width:100%;font-size:13px}td{padding:4px 8px;border-bottom:1px solid #222}td:first-child{color:#888}'
    '.pos{background:#0ecb8110;border:1px solid #0ecb8133;border-radius:8px;padding:12px;margin-bottom:12px}'
    '</style></head><body>'
    '<div style="display:flex;align-items:center;gap:12px;margin-bottom:16px">'
    '<h1>Rishabh Nifty Algo</h1><span class=tg id=mt>-</span></div>'
    '<div class=g>'
    '<div class=c><h3>Score</h3><div class=v id=sc>-</div></div>'
    '<div class=c><h3>Direction</h3><div class=v id=dr>-</div></div>'
    '<div class=c><h3>VIX</h3><div class=v id=vx>-</div></div>'
    '<div class=c><h3>Status</h3><div class=v id=st>-</div></div>'
    '</div>'
    '<div class=c style=margin-bottom:12px><h3>Levels</h3><table>'
    '<tr><td>BSL</td><td id=bl>-</td></tr>'
    '<tr><td>SSL</td><td id=sl>-</td></tr>'
    '<tr><td>Trends</td><td id=tr>-</td></tr>'
    '<tr><td>Alignment</td><td id=al>-</td></tr>'
    '<tr><td>OI</td><td id=oi>-</td></tr>'
    '</table></div>'
    '<div class=c style=margin-bottom:12px><h3>Scorecard</h3><div class=sg id=scd></div></div>'
    '<div id=pb></div>'
    '<div style=margin-bottom:12px>'
    '<button class=btn onclick=az()>Run Analysis</button> '
    '<button class=btn style="background:#333;color:#fff" onclick=rf()>Refresh</button>'
    '</div>'
    '<div class=c><h3>Logs</h3><div class=lg id=lg></div></div>'
    '<script>'
    'var L={major_zone:"L1:Major",sweep:"L1:Sweep",tf_aligned:"L1:TF",pp:"L2:PP",bos:"L3:BOS",choch:"L3:CHoCH",fvg:"L3:FVG",big_candle:"Candle",bounce:"Bounce"};'
    'async function rf(){'
    'try{var r=await fetch("/api/status"),d=await r.json();'
    'document.getElementById("mt").textContent=d.mode;'
    'document.getElementById("sc").textContent=d.score+"/9";'
    'document.getElementById("sc").className="v "+(d.score>=6?"gr":d.score>=4?"yl":"rd");'
    'var a=d.analysis||{};'
    'document.getElementById("dr").textContent=a.direction||"WAIT";'
    'document.getElementById("dr").className="v "+(a.direction==="CE"?"gr":a.direction==="PE"?"rd":"yl");'
    'document.getElementById("vx").textContent=(a.vix||0).toFixed(1);'
    'document.getElementById("vx").className="v "+((a.vix||0)>20?"rd":"gr");'
    'document.getElementById("st").textContent=d.traded?"TRADED":"WAITING";'
    'document.getElementById("bl").innerHTML=(a.bsl||[]).map(function(b){return b.price.toFixed(0)+"("+b.tests+"x)"+(b.swept?" SWEPT":"")}).join(" | ")||"-";'
    'document.getElementById("sl").innerHTML=(a.ssl||[]).map(function(s){return s.price.toFixed(0)+"("+s.tests+"x)"+(s.swept?" SWEPT":"")}).join(" | ")||"-";'
    'var t=a.trends||{};document.getElementById("tr").textContent=(t["1D"]||"-")+"/"+(t["4H"]||"-")+"/"+(t["1H"]||"-");'
    'document.getElementById("al").textContent=a.alignment||"-";'
    'document.getElementById("oi").textContent="SSL="+(a.oi_ssl||0)+" BSL="+(a.oi_bsl||0);'
    'var sc=d.scorecard||{};var keys=Object.keys(L);var h="";'
    'for(var i=0;i<keys.length;i++){var k=keys[i];h+="<div class=\\"si "+(sc[k]?"sy":"sn")+"\\">"+(sc[k]?"Y":"N")+" "+L[k]+"</div>"}'
    'document.getElementById("scd").innerHTML=h;'
    'if(d.position&&d.position.status){'
    'var p=d.position;document.getElementById("pb").innerHTML="<div class=pos><b>"+p.type+"@"+p.entry.toFixed(0)+"</b> SL:"+p.sl.toFixed(0)+" TP1:"+p.tp1.toFixed(0)+(p.tp1_hit?" HIT":"")+" TP2:"+p.tp2.toFixed(0)+" PnL:Rs."+p.pnl.toFixed(0)+" "+p.status+"</div>"'
    '}else{document.getElementById("pb").innerHTML=""}'
    'var logs=d.logs||[];var lh="";for(var i=logs.length-1;i>=0;i--){lh+="<div>"+logs[i]+"</div>"}'
    'document.getElementById("lg").innerHTML=lh;'
    '}catch(e){console.error(e)}}'
    'async function az(){await fetch("/api/analyze");setTimeout(rf,1000)}'
    'rf();setInterval(rf,5000);'
    '</script></body></html>'
)


class Dashboard(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/api/status":
            data = {
                "mode": "SHADOW" if SETTINGS["shadow_mode"] else "LIVE",
                "traded": bot.traded_today,
                "position": bot.position,
                "analysis": bot.analysis,
                "scorecard": bot.scorecard,
                "score": score_total(bot.scorecard) if bot.scorecard else 0,
                "logs": bot.logs[-30:],
            }
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps(data, default=str).encode())
        elif self.path == "/api/analyze":
            bot.run_analysis()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"ok": True, "score": score_total(bot.scorecard)}).encode())
        else:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(DASHBOARD_PAGE.encode())

    def log_message(self, format, *args):
        pass


def main():
    server = HTTPServer(("0.0.0.0", PORT), Dashboard)
    t = threading.Thread(target=server.serve_forever, daemon=True)
    t.start()
    log.info("Dashboard: http://localhost:%d" % PORT)
    bot.run()


if __name__ == "__main__":
    main()
