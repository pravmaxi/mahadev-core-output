
import json
import math
import threading
from pathlib import Path
from datetime import datetime, timezone

from flask import Blueprint, jsonify, request, render_template

import backtest
from data import get_live_ltp, _SYMBOL_MAP


paper_trading_bp = Blueprint("paper_trading", __name__)

STATE_FILE = Path(__file__).resolve().parent / "data" / "paper_trading_state.json"
STATE_FILE.parent.mkdir(parents=True, exist_ok=True)

LOCK = threading.RLock()


def _now():
    return datetime.now(timezone.utc).isoformat()


def _load():
    if not STATE_FILE.exists():
        return {
            "running": False,
            "config": {},
            "started_at": None,
            "processed_events": [],
            "position": None,
            "trades": [],
            "last_scan": None,
            "error": None,
        }

    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {
            "running": False,
            "config": {},
            "started_at": None,
            "processed_events": [],
            "position": None,
            "trades": [],
            "last_scan": None,
            "error": "Could not read paper trading state",
        }


def _save(state):
    tmp = STATE_FILE.with_suffix(".tmp")
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(STATE_FILE)


STATE = _load()


def _yf_symbol(symbol, exchange):
    symbol = symbol.upper().strip()
    exchange = exchange.upper().strip()

    mapped = _SYMBOL_MAP.get(symbol, symbol)

    if mapped.startswith("^") or "." in mapped or "=" in mapped:
        return mapped

    if exchange == "NSE":
        return mapped + ".NS"
    if exchange == "BSE":
        return mapped + ".BO"

    return mapped


def _ltp(symbol, exchange):
    ticker = _yf_symbol(symbol, exchange)

    try:
        prices = get_live_ltp([ticker])
        value = prices.get(ticker)

        if value is not None:
            return float(value)

    except Exception as exc:
        print("Paper Trading LTP error:", exc)

    return None


def _event_key(event):
    return "|".join([
        str(event.get("source", "wt")),
        str(event.get("direction")),
        str(event.get("valid_t")),
        str(event.get("trigger_t")),
        str(event.get("level")),
    ])


def _event_is_after_start(event, started_at):
    try:
        event_time = datetime.fromisoformat(
            str(event["trigger_t"]).replace("Z", "+00:00")
        )

        start_time = datetime.fromisoformat(
            str(started_at).replace("Z", "+00:00")
        )

        if event_time.tzinfo is None:
            event_time = event_time.replace(tzinfo=timezone.utc)

        if start_time.tzinfo is None:
            start_time = start_time.replace(tzinfo=timezone.utc)

        return event_time > start_time

    except Exception:
        return False


def _close_position(state, exit_price, reason, exit_time=None):
    position = state.get("position")

    if not position:
        return

    entry = float(position["entry_price"])
    qty = int(position["qty"])
    direction = position["direction"]

    if direction == "buy":
        pnl = (exit_price - entry) * qty
    else:
        pnl = (entry - exit_price) * qty

    trade = {
        "symbol": position["symbol"],
        "direction": direction,
        "qty": qty,
        "entry_price": round(entry, 4),
        "exit_price": round(float(exit_price), 4),
        "pnl": round(pnl, 2),
        "reason": reason,
        "entry_time": position["entry_time"],
        "exit_time": exit_time or _now(),
    }

    state.setdefault("trades", []).append(trade)
    state["position"] = None


def _open_position(state, event):
    cfg = state["config"]

    direction = event["direction"]

    if cfg["side_mode"] == "long" and direction != "buy":
        return

    if cfg["side_mode"] == "short" and direction != "sell":
        return

    if state.get("position"):
        return

    level = float(event["level"])
    buffer_pts = float(cfg["buffer_pts"])

    sign = 1 if direction == "buy" else -1

    raw_entry = level + sign * buffer_pts

    slippage = float(cfg["slippage_pct"]) / 100
    entry = raw_entry * (1 + sign * slippage)

    allocation = float(cfg["allocation"])

    qty = math.floor(allocation / entry) if entry > 0 else 0

    if qty <= 0:
        return

    target_pct = float(cfg["target_pct"])
    stop_pct = float(cfg["stop_pct"])

    if direction == "buy":
        target = entry * (1 + target_pct / 100)
        stop = entry * (1 - stop_pct / 100) if stop_pct > 0 else None
    else:
        target = entry * (1 - target_pct / 100)
        stop = entry * (1 + stop_pct / 100) if stop_pct > 0 else None

    state["position"] = {
        "symbol": cfg["symbol"],
        "exchange": cfg["exchange"],
        "direction": direction,
        "qty": qty,
        "allocation": round(qty * entry, 2),
        "entry_price": round(entry, 4),
        "target": round(target, 4),
        "stop": round(stop, 4) if stop is not None else None,
        "entry_time": event["trigger_t"],
        "valid_time": event["valid_t"],
        "trigger_level": level,
        "source": event.get("source", "wt"),
    }


def _check_exit(state, events, ltp):
    position = state.get("position")

    if not position:
        return

    if ltp is None:
        return

    direction = position["direction"]
    target = float(position["target"])
    stop = position.get("stop")

    # Stop is checked before target, matching the backtest's
    # conservative same-candle stop/target handling.
    if stop is not None:
        stop = float(stop)

        if direction == "buy" and ltp <= stop:
            _close_position(state, stop, "stop_loss")
            return

        if direction == "sell" and ltp >= stop:
            _close_position(state, stop, "stop_loss")
            return

    if direction == "buy" and ltp >= target:
        _close_position(state, target, "target")
        return

    if direction == "sell" and ltp <= target:
        _close_position(state, target, "target")
        return

    if state["config"].get("exit_on_opposite"):
        opposite = "sell" if direction == "buy" else "buy"

        for event in events:
            if event.get("direction") == opposite and not event.get("filtered"):
                _close_position(state, ltp, "opposite_signal")
                return


def _run_backtest(cfg):
    return backtest.run(
        symbol=cfg["data_symbol"],
        interval=cfg["interval"],
        period=cfg["period"],

        indicators={
            "wt": True,
            "ce": False,
            "st": False,
        },

        params={
            "wt": {
                "n1": int(cfg["wt_n1"]),
                "n2": int(cfg["wt_n2"]),
            }
        },

        capital=float(cfg["capital"]),
        position_amount=float(cfg["allocation"]),
        position_pct=100,
        fixed_qty=1,
        sizing_mode="fixed",

        commission_pct=float(cfg["commission_pct"]),
        slippage_pct=float(cfg["slippage_pct"]),
        buffer_pts=float(cfg["buffer_pts"]),
        wait_bars=int(cfg["wait_bars"]),

        profit_target_pct=float(cfg["target_pct"]),
        stop_loss_pct=float(cfg["stop_pct"]),
        exit_on_opposite=bool(cfg["exit_on_opposite"]),

        side_mode=cfg["side_mode"],
        wt_pending_rule=cfg["wt_pending_rule"],

        heikin_ashi_flag=bool(cfg["heikin_ashi"]),

        strategy_mode="simple",
    )


def _public_state(state):
    position = state.get("position")
    cfg = state.get("config", {})

    if position:
        ltp = _ltp(position["symbol"], position["exchange"])

        if ltp is not None:
            position["ltp"] = round(ltp, 4)

            if position["direction"] == "buy":
                position["unrealized_pnl"] = round(
                    (ltp - position["entry_price"]) * position["qty"], 2
                )
            else:
                position["unrealized_pnl"] = round(
                    (position["entry_price"] - ltp) * position["qty"], 2
                )

    return {
        "ok": True,
        "running": state.get("running", False),
        "config": cfg,
        "position": position,
        "trades": state.get("trades", []),
        "last_scan": state.get("last_scan"),
        "error": state.get("error"),
        "processed_events": len(state.get("processed_events", [])),
    }


@paper_trading_bp.route("/paper-trading")
def paper_trading_page():
    return render_template("paper_trading.html", active="paper-trading")


@paper_trading_bp.route("/api/paper-trading/start", methods=["POST"])
def paper_trading_start():
    body = request.get_json(silent=True) or {}

    try:
        symbol = str(body.get("symbol", "")).strip().upper()

        if not symbol:
            return jsonify({"ok": False, "error": "Enter a symbol"})

        exchange = str(body.get("exchange", "NSE")).upper()

        suffix = {
            "NSE": ".NS",
            "BSE": ".BO",
            "US": "",
            "FX": "",
        }.get(exchange, ".NS")

        data_symbol = symbol

        if suffix and not symbol.endswith(suffix):
            data_symbol = symbol + suffix

        cfg = {
            "symbol": symbol,
            "data_symbol": data_symbol,
            "exchange": exchange,
            "interval": body.get("interval", "1h"),
            "period": body.get("period", "6mo"),

            "capital": float(body.get("capital", 10000)),
            "allocation": float(body.get("allocation", 5000)),

            "side_mode": body.get("side_mode", "long"),

            "target_pct": float(body.get("target_pct", 10)),
            "stop_pct": float(body.get("stop_pct", 0)),

            "buffer_pts": float(body.get("buffer_pts", 0.01)),
            "wait_bars": int(body.get("wait_bars", 100)),

            "commission_pct": float(body.get("commission_pct", 0)),
            "slippage_pct": float(body.get("slippage_pct", 0)),

            "wt_n1": int(body.get("wt_n1", 15)),
            "wt_n2": int(body.get("wt_n2", 27)),

            "wt_pending_rule": body.get("wt_pending_rule", "same_dir"),
            "heikin_ashi": bool(body.get("heikin_ashi", True)),
            "exit_on_opposite": bool(body.get("exit_on_opposite", True)),
        }

        if cfg["capital"] <= 0 or cfg["allocation"] <= 0:
            return jsonify({"ok": False, "error": "Capital and allocation must be greater than zero"})

        if cfg["allocation"] > cfg["capital"]:
            return jsonify({"ok": False, "error": "Allocation cannot exceed total capital"})

        if cfg["wt_pending_rule"] not in ("every", "same_dir", "any_dir"):
            return jsonify({"ok": False, "error": "Invalid WT pending rule"})

        with LOCK:
            STATE.clear()
            STATE.update({
                "running": True,
                "config": cfg,
                "started_at": _now(),
                "processed_events": [],
                "position": None,
                "trades": [],
                "last_scan": None,
                "error": None,
            })
            _save(STATE)

        return jsonify({"ok": True, "message": "Paper Trading started"})

    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)})


@paper_trading_bp.route("/api/paper-trading/scan", methods=["POST"])
def paper_trading_scan():
    with LOCK:
        if not STATE.get("running"):
            return jsonify({"ok": False, "error": "Paper Trading is stopped"})

        cfg = dict(STATE["config"])

    try:
        result = _run_backtest(cfg)

        if not result.get("ok"):
            with LOCK:
                STATE["error"] = result.get("error")
                _save(STATE)

            return jsonify({"ok": False, "error": result.get("error")})

        candles = result.get("candles", [])
        events = result.get("events", [])

        # Do not process an event on the currently forming candle.
        completed_time = candles[-2]["t"] if len(candles) >= 2 else None

        if completed_time is None:
            return jsonify({"ok": False, "error": "Waiting for completed candles"})

        completed_events = [
            e for e in events
            if e.get("trigger_t") <= completed_time
            and not e.get("filtered", False)
        ]

        ltp = _ltp(cfg["symbol"], cfg["exchange"])

        with LOCK:
            if not STATE.get("running"):
                return jsonify({"ok": False, "error": "Paper Trading stopped"})

            processed = set(STATE.get("processed_events", []))

            fresh_events = []

            for event in completed_events:
                key = _event_key(event)

                if key in processed:
                    continue

                processed.add(key)

                if not _event_is_after_start(event, STATE["started_at"]):
                    continue

                fresh_events.append(event)

            STATE["processed_events"] = list(processed)

            _check_exit(STATE, fresh_events, ltp)

            for event in fresh_events:
                if STATE.get("position") is None:
                    _open_position(STATE, event)

            STATE["last_scan"] = {
                "time": _now(),
                "last_candle": completed_time,
                "ltp": ltp,
                "new_events": len(fresh_events),
                "bars": result.get("bars"),
            }

            STATE["error"] = None
            _save(STATE)

            response = _public_state(STATE)

        return jsonify(response)

    except Exception as exc:
        with LOCK:
            STATE["error"] = str(exc)
            _save(STATE)

        return jsonify({"ok": False, "error": str(exc)})


@paper_trading_bp.route("/api/paper-trading/status")
def paper_trading_status():
    with LOCK:
        return jsonify(_public_state(STATE))


@paper_trading_bp.route("/api/paper-trading/stop", methods=["POST"])
def paper_trading_stop():
    with LOCK:
        STATE["running"] = False
        _save(STATE)

    return jsonify({"ok": True, "message": "Paper Trading stopped"})


@paper_trading_bp.route("/api/paper-trading/reset", methods=["POST"])
def paper_trading_reset():
    with LOCK:
        STATE.clear()
        STATE.update({
            "running": False,
            "config": {},
            "started_at": None,
            "processed_events": [],
            "position": None,
            "trades": [],
            "last_scan": None,
            "error": None,
        })
        _save(STATE)

    return jsonify({"ok": True, "message": "Paper Trading data reset"})
