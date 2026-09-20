"""
実発注の台帳・監査ログ・停止状態(kill switch / halt)

- 台帳(logs/live_orders.json): 「同じ日・同じ銘柄・同じ売買方向」の発注は1回だけ。
  スクリプトの再実行・二重起動でも同じ注文を二度出さない(冪等性)。発注「前」に予約する。
- 監査ログ(logs/live_audit.jsonl): 全リクエスト/レスポンスを追記。パスワード類は記録しない。
- kill switch(STOP_TRADING ファイル): あれば即座に全発注を止める。手動で作る/消す。
- halt(logs/live_halt.json): 異常検知(残高不整合・結果不明の注文・連続損切り等)で自動作成。
  人間が原因を確認して `live_trade.py resume` するまで発注しない。
"""

from __future__ import annotations

import datetime as dt
import json
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"

LEDGER_PATH = LOG_DIR / "live_orders.json"
AUDIT_PATH = LOG_DIR / "live_audit.jsonl"
HALT_PATH = LOG_DIR / "live_halt.json"
KILL_SWITCH_PATH = BASE_DIR / "STOP_TRADING"

SECRET_KEYS = {"Password", "APIPassword", "X-API-KEY", "Token"}


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _redact(obj):
    if isinstance(obj, dict):
        return {k: ("***" if k in SECRET_KEYS else _redact(v)) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_redact(v) for v in obj]
    return obj


def audit(kind: str, payload: dict, path: Path = AUDIT_PATH) -> None:
    path.parent.mkdir(exist_ok=True)
    rec = {"ts": dt.datetime.now().isoformat(timespec="seconds"), "kind": kind, "data": _redact(payload)}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


class OrderLedger:
    def __init__(self, path: Path = LEDGER_PATH):
        self.path = path

    def _load(self) -> list[dict]:
        if not self.path.exists():
            return []
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, entries: list[dict]) -> None:
        _atomic_write(self.path, json.dumps(entries, ensure_ascii=False, indent=2))

    @staticmethod
    def make_key(date: str, ticker: str, side: str) -> str:
        return f"{date}:{ticker}:{side}"

    def has(self, key: str) -> bool:
        return any(e["key"] == key for e in self._load())

    def reserve(self, key: str, **fields) -> bool:
        """未使用のキーなら予約して True。既にあれば False(=二重発注になるので出さない)。"""
        entries = self._load()
        if any(e["key"] == key for e in entries):
            return False
        entries.append({"key": key, "status": "RESERVED", "ts": dt.datetime.now().isoformat(timespec="seconds"), **fields})
        self._save(entries)
        return True

    def update(self, key: str, **fields) -> None:
        entries = self._load()
        for e in entries:
            if e["key"] == key:
                e.update(fields)
        self._save(entries)

    def entries_on(self, date: str) -> list[dict]:
        return [e for e in self._load() if e["key"].startswith(date + ":")]

    def notional_on(self, date: str) -> float:
        """その日に予約・発注済みの金額合計(上限管理用。約定前でも予約額を数える)。"""
        return sum(float(e.get("notional", 0)) for e in self.entries_on(date) if e.get("status") != "REJECTED")

    def count_on(self, date: str) -> int:
        return len(self.entries_on(date))

    def recent_stop_losses(self, since_date: str) -> int:
        return sum(1 for e in self._load() if e.get("stop_loss") and e.get("status") in {"FILLED", "PARTIAL"} and e["key"][:10] >= since_date)


def kill_switch_active(path: Path = KILL_SWITCH_PATH) -> bool:
    return path.exists()


def halt_reason(path: Path = HALT_PATH) -> str | None:
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8")).get("reason", "(理由不明)")


def set_halt(reason: str, path: Path = HALT_PATH) -> None:
    _atomic_write(path, json.dumps({"reason": reason, "ts": dt.datetime.now().isoformat(timespec="seconds")}, ensure_ascii=False, indent=2))


def clear_halt(path: Path = HALT_PATH) -> None:
    if path.exists():
        path.unlink()
