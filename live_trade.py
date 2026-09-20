"""
実発注用のオーケストレータ(仮想の rebalance_auto.py を実弾に載せるための別系統)

rebalance.py / rebalance_auto.py は「自動発注しない」前提のまま残してあり、こちらが実発注を担当する。
売買判断のロジックは同じ rebalance.compute_plan を使う(仮想で検証した戦略をそのまま実弾へ)。

3つのモード(trading_config.json の mode):
    notify  : 提案をLINEに送るだけ。発注は自分で証券会社アプリから(まずここから始める)
    approve : 提案を保存し、`python live_trade.py approve <ID>` したときだけ発注
    auto    : 提案をそのまま自動発注(十分な実績が出てから)

コマンド:
    python live_trade.py run            # 日次実行(通常はこれをスケジューラから)
    python live_trade.py approve <ID>   # 承認待ちの提案を発注
    python live_trade.py init [--yes]   # 証券会社の現在の保有を基準状態として取り込む(初回・照合ズレの解消時)
    python live_trade.py status         # 現在の設定・停止状態・本日の発注を表示
    python live_trade.py stop           # kill switch(STOP_TRADINGを作る)
    python live_trade.py resume         # kill switchとhaltを解除(原因確認後に人間が行う)

安全装置(詳細は trade_guard.py / order_ledger.py):
    - 実発注は 設定 live=true かつ 環境変数 LIVE_TRADING=1 の両方が必要(kabuブローカー時)
    - 指値のみ・1注文/1日の上限金額・件数上限・価格乖離チェック・保有超過売却の禁止
    - 同日・同銘柄・同方向の発注は1回だけ(台帳で二重発注防止)
    - 発注結果が不明(タイムアウト等)なら再送せず即停止(halt)。人間が証券会社の画面で確認する
    - 残高照合: 発注後に証券会社の実残高と一致しなければ停止。approve/auto では毎回の開始時にも照合
    - 連続損切りが規定回数に達したら停止
    - 管理対象は WATCHLIST(+既に取り込んだ保有)のみ。ほかの保有株には触らない。
      戦略が使う資金は capital_limit まで(口座に余分な資金があっても使わない)
    - 取引時間外・休場日・データ取得失敗(保有銘柄の株価欠損)のときは何もしない

前提: 売却代金は約定・受渡まで買付余力に数えない(保守的)ので、入れ替えが翌日に持ち越されることがある。
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path

import console_utf8
import market_calendar
import order_ledger as ol
import rebalance as rb
import rebalance_auto
from broker import (
    Broker,
    BrokerError,
    KabuStationBroker,
    OrderRequest,
    OrderResult,
    PaperBroker,
    Position,
    Quote,
)
from line_alert import send_line_alert
from trade_guard import build_orders, check_execution_allowed
from trading_config import TradingConfig, load_config
from watchlist import LOG_DIR, WATCHLIST

console_utf8.setup()

BASE_DIR = Path(__file__).resolve().parent


@dataclass
class Env:
    state_path: Path
    pending_path: Path
    paper_path: Path
    ledger: ol.OrderLedger
    halt_path: Path
    kill_path: Path
    audit_path: Path


def default_env() -> Env:
    return Env(
        state_path=BASE_DIR / "portfolio_live.json",
        pending_path=LOG_DIR / "pending_plan.json",
        paper_path=LOG_DIR / "paper_broker.json",
        ledger=ol.OrderLedger(ol.LEDGER_PATH),
        halt_path=ol.HALT_PATH,
        kill_path=ol.KILL_SWITCH_PATH,
        audit_path=ol.AUDIT_PATH,
    )


# ---------------------------------------------------------------- 状態ファイル

def load_state(env: Env) -> dict | None:
    if not env.state_path.exists():
        return None
    return json.loads(env.state_path.read_text(encoding="utf-8"))


def save_state(env: Env, state: dict) -> None:
    ol._atomic_write(env.state_path, json.dumps(state, ensure_ascii=False, indent=2))


def managed_positions(positions: list[Position], state: dict | None) -> dict[str, Position]:
    universe = set(WATCHLIST) | set((state or {}).get("holdings", {}))
    return {p.ticker: p for p in positions if p.ticker in universe and p.shares > 0}


def reconcile(state: dict, positions: dict[str, Position], cash: float, cfg: TradingConfig) -> list[str]:
    """記録上の状態と証券会社の実残高の食い違い(空ならOK)。管理対象外の保有は見ない。"""
    problems = []
    local = state.get("holdings", {})
    for t in sorted(set(local) | set(positions)):
        a, b = local.get(t, 0), positions[t].shares if t in positions else 0
        if a != b:
            problems.append(f"{t}: 記録{a}株 / 証券会社{b}株")
    last_cash = state.get("lastBrokerCash")
    if last_cash is not None and cash < last_cash - cfg.cash_tolerance_yen:
        problems.append(f"現金が想定より減っている: 前回{last_cash:,.0f}円 → 現在{cash:,.0f}円(許容{cfg.cash_tolerance_yen:,.0f}円)")
    return problems


def apply_fill(state: dict, ticker: str, side: str, qty: int, price: float | None) -> None:
    holdings, basis = state.setdefault("holdings", {}), state.setdefault("costBasis", {})
    old = holdings.get(ticker, 0)
    if side == "buy":
        new = old + qty
        if price:
            prev = basis.get(ticker, price)
            basis[ticker] = round((old * prev + qty * price) / new, 2) if new else price
        holdings[ticker] = new
    else:
        new = old - qty
        if new <= 0:
            holdings.pop(ticker, None)
            basis.pop(ticker, None)
        else:
            holdings[ticker] = new


def adopt_broker_state(state: dict, positions: dict[str, Position], cash: float) -> None:
    """証券会社の実残高を正として状態を上書きする(notifyモード・init用)。取得単価は分かるものだけ更新。"""
    old_basis = state.get("costBasis", {})
    state["holdings"] = {t: p.shares for t, p in positions.items()}
    state["costBasis"] = {
        t: round(p.avg_price, 2) if p.avg_price else old_basis[t]
        for t, p in positions.items() if p.avg_price or t in old_basis
    }
    state["lastBrokerCash"] = cash


# ---------------------------------------------------------------- ブローカー構築

def make_broker(cfg: TradingConfig, env: Env, environ: dict | None = None) -> Broker:
    environ = os.environ if environ is None else environ
    if cfg.broker == "kabu":
        return KabuStationBroker(
            cfg.kabu_base_url,
            environ.get("KABU_API_PASSWORD", ""),
            environ.get("KABU_ORDER_PASSWORD", ""),
            account_type=cfg.account_type,
            exchange=cfg.kabu_exchange,
            audit=lambda kind, payload: ol.audit(kind, payload, env.audit_path),
        )
    if env.paper_path.exists():
        d = json.loads(env.paper_path.read_text(encoding="utf-8"))
        return PaperBroker(d["cash"], d["positions"], {})
    return PaperBroker(cfg.paper_cash, {}, {})


def save_paper_broker(env: Env, broker: Broker) -> None:
    if isinstance(broker, PaperBroker):
        ol._atomic_write(env.paper_path, json.dumps({"cash": broker.cash, "positions": broker.positions}))


# ---------------------------------------------------------------- 約定の追跡

def wait_for_fill(broker: Broker, res: OrderResult, cfg: TradingConfig, sleep=time.sleep) -> OrderResult:
    deadline = time.monotonic() + cfg.fill_poll_seconds
    while res.status == "SUBMITTED" and time.monotonic() < deadline:
        sleep(cfg.fill_poll_interval)
        try:
            res = broker.get_order(res.order_id)
        except BrokerError as e:
            print(f"  [警告] 注文照会失敗: {e}")
    return res


def settle_working_orders(broker: Broker, env: Env, state: dict) -> list[str]:
    """前回までに未約定だった(WORKING)注文を照会し、その後の約定を状態に反映する。"""
    notes = []
    for e in env.ledger._load():
        if e.get("status") != "WORKING" or not e.get("order_id"):
            continue
        try:
            res = broker.get_order(e["order_id"])
        except BrokerError as ex:
            notes.append(f"{e['key']}: 注文照会失敗({ex})")
            continue
        delta = res.filled_qty - int(e.get("applied_qty", 0))
        if delta > 0:
            apply_fill(state, e["ticker"], e["side"], delta, res.avg_price)
            notes.append(f"{e['key']}: 遅れて{delta}株約定")
        new_status = "WORKING" if res.status == "SUBMITTED" else res.status
        env.ledger.update(e["key"], status=new_status, filled_qty=res.filled_qty, applied_qty=res.filled_qty)
    return notes


def execute_orders(orders: list[OrderRequest], cfg: TradingConfig, broker: Broker, env: Env,
                   state: dict, today: str, notify=send_line_alert, sleep=time.sleep) -> list[str]:
    """発注して約定を状態へ反映する。戻り値は結果の説明行。異常時は halt を立てて中断する。"""
    lines = []
    for o in orders:
        key = env.ledger.make_key(today, o.ticker, o.side)
        if not env.ledger.reserve(key, ticker=o.ticker, side=o.side, qty=o.qty, limit=o.limit_price,
                                  notional=o.qty * o.limit_price, stop_loss=o.stop_loss, applied_qty=0):
            lines.append(f"[{o.ticker}] {o.side} は本日発注済みのためスキップ")
            continue
        ol.audit("ORDER_REQUEST", asdict(o), env.audit_path)
        res = broker.place_order(o)
        ol.audit("ORDER_RESULT", {"ticker": o.ticker, "status": res.status, "order_id": res.order_id, "message": res.message}, env.audit_path)

        if res.status == "UNKNOWN":
            env.ledger.update(key, status="UNKNOWN", message=res.message)
            reason = f"{o.ticker} {o.side} {o.qty}株の発注結果が不明({res.message})。証券会社の画面で注文状況を確認してください"
            ol.set_halt(reason, env.halt_path)
            notify(f"🚨【自動売買】発注結果不明のため全停止\n{reason}")
            lines.append(f"[{o.ticker}] 結果不明 → 停止")
            break
        if res.status == "REJECTED":
            env.ledger.update(key, status="REJECTED", message=res.message)
            lines.append(f"[{o.ticker}] 拒否: {res.message}")
            continue

        res = wait_for_fill(broker, res, cfg, sleep)
        status = "WORKING" if res.status == "SUBMITTED" else res.status
        env.ledger.update(key, status=status, order_id=res.order_id, filled_qty=res.filled_qty, applied_qty=res.filled_qty)
        if res.filled_qty > 0:
            apply_fill(state, o.ticker, o.side, res.filled_qty, res.avg_price)
            if o.stop_loss and o.side == "sell":
                state.setdefault("cooldown", {})[o.ticker] = rebalance_auto.STOP_LOSS_COOLDOWN_RUNS
        verb = "買い" if o.side == "buy" else "売り"
        lines.append(f"[{o.ticker}] {verb}{o.qty}株 指値{o.limit_price:,.0f}円 → {status}(約定{res.filled_qty}株)")
    return lines


def finalize_after_trades(cfg: TradingConfig, broker: Broker, env: Env, state: dict, today: dt.date,
                          notify=send_line_alert) -> str | None:
    """発注後に実残高を取り直して照合し、状態を実残高で確定する。異常時は halt して理由を返す。"""
    cash = broker.get_cash()
    actual = managed_positions(broker.get_positions(), state)
    problems = reconcile({**state, "lastBrokerCash": None}, actual, cash, cfg)
    reason = None
    if problems:
        reason = "約定反映後の残高が証券会社と一致しません: " + " / ".join(problems)
        ol.set_halt(reason, env.halt_path)
        notify(f"🚨【自動売買】残高不整合のため停止\n{reason}")
    adopt_broker_state(state, actual, cash)
    since = (today - dt.timedelta(days=cfg.circuit_breaker_window_days)).isoformat()
    n = env.ledger.recent_stop_losses(since)
    if n >= cfg.circuit_breaker_stop_losses and not reason:
        reason = f"直近{cfg.circuit_breaker_window_days}日で損切りが{n}回発生(上限{cfg.circuit_breaker_stop_losses}回)。相場・戦略を確認してください"
        ol.set_halt(reason, env.halt_path)
        notify(f"🚨【自動売買】連続損切りのため停止\n{reason}")
    return reason


# ---------------------------------------------------------------- 本体

def fetch_quotes(broker: Broker, tickers: list[str]) -> dict[str, Quote]:
    quotes = {}
    for t in tickers:
        try:
            quotes[t] = broker.get_quote(t)
        except BrokerError as e:
            print(f"  [警告] {t}: 現在値取得失敗: {e}")
    return quotes


def format_proposal(orders: list[OrderRequest], rejected: list[tuple[str, str]], header: str) -> str:
    lines = [header]
    for o in orders:
        tag = "【損切り】" if o.stop_loss else ""
        lines.append(f"{tag}{'買い' if o.side == 'buy' else '売り'} {o.ticker} {o.qty}株 指値{o.limit_price:,.0f}円(約{o.qty * o.limit_price:,.0f}円)")
    if rejected:
        lines.append("--- 見送り ---")
        lines += [f"{t}: {why}" for t, why in rejected]
    return "\n".join(lines)


def run_once(cfg: TradingConfig, broker: Broker, env: Env, now: dt.datetime, *,
             fetch_snapshots=rb.fetch_universe_snapshots, notify=send_line_alert,
             environ: dict | None = None, sleep=time.sleep) -> dict:
    today = now.strftime("%Y-%m-%d")
    will_place = cfg.mode == "auto"
    blocked = check_execution_allowed(
        now, cfg, kill_switch=env.kill_path.exists(), halt_reason=ol.halt_reason(env.halt_path),
        will_place_orders=will_place, environ=environ,
    )
    if blocked:
        print(f"[見送り] {blocked}")
        if "停止中(halt)" in blocked:
            notify(f"⚠【自動売買】停止中のため何もしません\n{blocked}")
        return {"status": "blocked", "reason": blocked}

    state = load_state(env)
    if state is None:
        msg = "portfolio_live.json がありません。`python live_trade.py init` で証券会社の保有を取り込んでから実行してください。"
        print(msg)
        return {"status": "no_state", "reason": msg}

    notes = settle_working_orders(broker, env, state)
    save_state(env, state)   # 台帳のapplied_qtyは更新済みなので、途中で中断しても状態への反映を失わない
    cash = broker.get_cash()
    actual = managed_positions(broker.get_positions(), state)
    if cfg.mode == "notify":
        adopt_broker_state(state, actual, cash)   # 手動売買した結果に追随する
    else:
        problems = reconcile(state, actual, cash, cfg)
        if problems:
            reason = "記録と証券会社の残高が一致しません: " + " / ".join(problems) + "(手動売買・配当・分割などの可能性。確認後 `init --yes` で取り込み直し)"
            ol.set_halt(reason, env.halt_path)
            notify(f"🚨【自動売買】残高不整合のため停止\n{reason}")
            return {"status": "halted", "reason": reason}
    holdings = dict(state.get("holdings", {}))
    cost_basis = dict(state.get("costBasis", {}))
    cooldown = {t: d - 1 for t, d in state.get("cooldown", {}).items() if d - 1 > 0}

    snapshots = fetch_snapshots(holdings)
    missing = rb.find_missing_holdings(holdings, snapshots)
    if not snapshots or missing:
        msg = f"株価データを取得できない銘柄があるため今回は見送ります: {missing or '全銘柄'}"
        print(msg)
        return {"status": "data_unavailable", "reason": msg}
    if isinstance(broker, PaperBroker):
        broker.prices.update({s.ticker: s.price for s in snapshots})

    price = {s.ticker: s.price for s in snapshots}
    managed_value = sum(n * price[t] for t, n in holdings.items())
    available = max(0.0, min(cash, cfg.capital_limit - managed_value))
    plan = rb.compute_plan(available, holdings, snapshots, cost_basis, set(cooldown))
    action_rows = [r for r in plan["rows"] if r["action_shares"] != 0]
    quotes = fetch_quotes(broker, [r["ticker"] for r in action_rows])
    guard = build_orders(action_rows, quotes=quotes, cash=available, holdings=holdings,
                         total_value=plan["total_value"], cfg=cfg, ledger=env.ledger, today=today)

    state["cooldown"] = cooldown
    summary = {"status": "ok", "orders": guard.orders, "rejected": guard.rejected, "notes": notes, "mode": cfg.mode}
    header = f"【自動売買・{cfg.mode}】{now:%Y-%m-%d %H:%M}"

    if not guard.orders and not guard.rejected:
        print("本日の売買提案はありません。")
        save_state(env, state)
        return summary

    if cfg.mode == "notify":
        notify(format_proposal(guard.orders, guard.rejected, header + " 提案(発注はしません。売買は自分で)"))
    elif cfg.mode == "approve":
        plan_id = uuid.uuid4().hex[:8]
        pending = {
            "id": plan_id,
            "created": now.isoformat(timespec="seconds"),
            "expires": (now + dt.timedelta(minutes=cfg.approval_ttl_minutes)).isoformat(timespec="seconds"),
            "total_value": plan["total_value"],
            "rows": [{k: r[k] for k in ("ticker", "action_shares", "price", "score", "stop_loss")} for r in action_rows],
        }
        ol._atomic_write(env.pending_path, json.dumps(pending, ensure_ascii=False, indent=2))
        summary["plan_id"] = plan_id
        notify(format_proposal(guard.orders, guard.rejected, header + f" 承認待ち(有効{cfg.approval_ttl_minutes}分)")
               + f"\n承認: python live_trade.py approve {plan_id}")
    else:
        lines = execute_orders(guard.orders, cfg, broker, env, state, today, notify, sleep)
        summary["results"] = lines
        reason = finalize_after_trades(cfg, broker, env, state, now.date(), notify)
        summary["halt"] = reason
        notify(format_proposal(guard.orders, guard.rejected, header + " 発注結果") + "\n" + "\n".join(lines))
    save_state(env, state)
    save_paper_broker(env, broker)
    return summary


def approve(plan_id: str, cfg: TradingConfig, broker: Broker, env: Env, now: dt.datetime, *,
            notify=send_line_alert, environ: dict | None = None, sleep=time.sleep) -> dict:
    if not env.pending_path.exists():
        return {"status": "error", "reason": "承認待ちの提案がありません"}
    pending = json.loads(env.pending_path.read_text(encoding="utf-8"))
    if pending["id"] != plan_id:
        return {"status": "error", "reason": f"IDが一致しません(承認待ちは {pending['id']})"}
    if now > dt.datetime.fromisoformat(pending["expires"]):
        return {"status": "error", "reason": "提案の有効期限が切れています。`run` をやり直してください"}
    blocked = check_execution_allowed(
        now, cfg, kill_switch=env.kill_path.exists(), halt_reason=ol.halt_reason(env.halt_path),
        will_place_orders=True, environ=environ,
    )
    if blocked:
        return {"status": "blocked", "reason": blocked}
    state = load_state(env)
    if state is None:
        return {"status": "error", "reason": "portfolio_live.json がありません(init が必要)"}

    settle_working_orders(broker, env, state)
    save_state(env, state)
    cash = broker.get_cash()
    actual = managed_positions(broker.get_positions(), state)
    problems = reconcile(state, actual, cash, cfg)
    if problems:
        reason = "承認時の照合で残高不一致: " + " / ".join(problems)
        ol.set_halt(reason, env.halt_path)
        return {"status": "halted", "reason": reason}

    env.pending_path.unlink()   # 使い切り(二重承認防止)。台帳も二重発注を防ぐ
    holdings = dict(state.get("holdings", {}))
    rows = pending["rows"]
    # 承認時点の現在値で再検証する(提案からの値動き・残高変化を反映)
    quotes = fetch_quotes(broker, [r["ticker"] for r in rows])
    guard = build_orders(rows, quotes=quotes, cash=cash, holdings=holdings, total_value=pending["total_value"],
                         cfg=cfg, ledger=env.ledger, today=now.strftime("%Y-%m-%d"))
    lines = execute_orders(guard.orders, cfg, broker, env, state, now.strftime("%Y-%m-%d"), notify, sleep)
    reason = finalize_after_trades(cfg, broker, env, state, now.date(), notify)
    save_state(env, state)
    save_paper_broker(env, broker)
    notify(format_proposal(guard.orders, guard.rejected, f"【自動売買・承認発注】{now:%Y-%m-%d %H:%M} 結果") + "\n" + "\n".join(lines))
    return {"status": "ok", "orders": guard.orders, "rejected": guard.rejected, "results": lines, "halt": reason}


# ---------------------------------------------------------------- CLI

def cmd_init(cfg: TradingConfig, broker: Broker, env: Env, yes: bool) -> None:
    prev = load_state(env) or {}
    positions_all = broker.get_positions()
    managed = managed_positions(positions_all, prev)
    ignored = [p.ticker for p in positions_all if p.ticker not in managed]
    cash = broker.get_cash()
    print(f"現金 {cash:,.0f}円 / 管理対象の保有: {', '.join(f'{t}:{p.shares}株' for t, p in managed.items()) or 'なし'}")
    print(f"管理対象外(触りません): {', '.join(ignored) or 'なし'} / 戦略が使う資金の上限: {cfg.capital_limit:,.0f}円")
    if not yes:
        print("内容を確認して問題なければ --yes を付けて再実行してください(保存はしていません)。")
        return
    state = {"cooldown": prev.get("cooldown", {}), "initializedAt": dt.datetime.now().isoformat(timespec="seconds")}
    state["costBasis"] = prev.get("costBasis", {})
    adopt_broker_state(state, managed, cash)
    save_state(env, state)
    print(f"[保存しました] {env.state_path}")


def cmd_status(cfg: TradingConfig, env: Env) -> None:
    now = dt.datetime.now()
    print(f"mode={cfg.mode} broker={cfg.broker} live設定={cfg.live} LIVE_TRADING={os.environ.get('LIVE_TRADING')!r} "
          f"→ 実発注{'有効' if cfg.live_enabled() else '無効'}")
    print(f"接続先: {cfg.kabu_base_url}{'  ★本番★' if cfg.is_production_endpoint else '(検証環境)'}" if cfg.broker == "kabu" else "接続先: 仮想ブローカー")
    print(f"kill switch: {'有効' if env.kill_path.exists() else 'なし'} / halt: {ol.halt_reason(env.halt_path) or 'なし'}")
    print(f"承認待ち: {json.loads(env.pending_path.read_text(encoding='utf-8'))['id'] if env.pending_path.exists() else 'なし'}")
    today = now.strftime("%Y-%m-%d")
    print(f"本日の発注: {env.ledger.count_on(today)}件 / {env.ledger.notional_on(today):,.0f}円")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="実発注オーケストレータ")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("run")
    p_ap = sub.add_parser("approve"); p_ap.add_argument("plan_id")
    p_init = sub.add_parser("init"); p_init.add_argument("--yes", action="store_true")
    for name in ("status", "stop", "resume"):
        sub.add_parser(name)
    args = ap.parse_args(argv)

    cfg, env = load_config(), default_env()
    if args.cmd == "stop":
        env.kill_path.write_text("stop", encoding="utf-8"); print(f"kill switch を有効にしました: {env.kill_path}"); return 0
    if args.cmd == "resume":
        env.kill_path.unlink(missing_ok=True); ol.clear_halt(env.halt_path); print("kill switch と halt を解除しました"); return 0
    if args.cmd == "status":
        cmd_status(cfg, env); return 0

    try:
        broker = make_broker(cfg, env)
        if args.cmd == "init":
            cmd_init(cfg, broker, env, args.yes)
        elif args.cmd == "run":
            result = run_once(cfg, broker, env, dt.datetime.now(market_calendar.JST))
            print(f"[結果] {result['status']}")
        elif args.cmd == "approve":
            result = approve(args.plan_id, cfg, broker, env, dt.datetime.now(market_calendar.JST))
            print(f"[結果] {result['status']} {result.get('reason', '')}")
            return 0 if result["status"] == "ok" else 1
    except BrokerError as e:
        print(f"[エラー] 証券会社との通信に失敗: {e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
