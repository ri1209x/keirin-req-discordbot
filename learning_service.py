"""
Railway単体運用向け 学習/保存サービス

- SQLite に実行ログ/実着順を保存
- 過去ログから戦略重みを再学習し JSON に保存
- recommender 側は JSON を読み込んで重みへ反映
"""
from __future__ import annotations

import json
import os
import sqlite3
from dataclasses import asdict
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

from scraper import Player, RaceInfo

JST = ZoneInfo("Asia/Tokyo")


def _data_dir() -> str:
    # Railway Volume を /data にマウントする想定。
    # 未設定時はプロジェクト配下へフォールバック。
    p = os.getenv("KEIRIN_DATA_DIR")
    if p:
        os.makedirs(p, exist_ok=True)
        return p
    if os.path.isdir("/data"):
        os.makedirs("/data", exist_ok=True)
        return "/data"
    p = os.path.join(os.getcwd(), ".data")
    os.makedirs(p, exist_ok=True)
    return p


def _db_path() -> str:
    return os.path.join(_data_dir(), "keirin_learning.db")


def _weights_path() -> str:
    return os.path.join(_data_dir(), "learned_weights.json")


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(_db_path())
    conn.row_factory = sqlite3.Row
    return conn


def init_storage() -> None:
    with _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS race_logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                logged_at TEXT NOT NULL,
                venue TEXT NOT NULL,
                race_number INTEGER NOT NULL,
                race_date TEXT NOT NULL,
                strategy TEXT NOT NULL,
                ticket_type TEXT NOT NULL,
                budget INTEGER NOT NULL,
                is_mock INTEGER NOT NULL,
                players_json TEXT NOT NULL,
                odds_json TEXT NOT NULL,
                recommendation_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS race_results (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                venue TEXT NOT NULL,
                race_number INTEGER NOT NULL,
                race_date TEXT NOT NULL,
                ticket_type TEXT NOT NULL,
                result_combo TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(venue, race_number, race_date, ticket_type)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS train_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        conn.commit()


def _player_to_dict(p: Player) -> dict:
    return asdict(p)


def _bet_to_dict(bet) -> dict:
    return {
        "numbers": bet.numbers,
        "amount": bet.amount,
        "stars": bet.stars,
        "current_odds": bet.current_odds,
    }


def log_race_snapshot(race_info: RaceInfo, recommendation) -> None:
    players = [_player_to_dict(p) for p in race_info.players]
    odds_json = race_info.odds_map or {}
    rec_json = {
        "strategy": recommendation.strategy,
        "ticket_type": recommendation.ticket_type,
        "budget": recommendation.budget,
        "bets": [_bet_to_dict(b) for b in recommendation.bets],
    }
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO race_logs (
                logged_at, venue, race_number, race_date, strategy, ticket_type, budget,
                is_mock, players_json, odds_json, recommendation_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(JST).isoformat(),
                race_info.venue,
                race_info.race_number,
                race_info.race_date.isoformat(),
                recommendation.strategy,
                recommendation.ticket_type,
                recommendation.budget,
                1 if race_info.is_mock else 0,
                json.dumps(players, ensure_ascii=False),
                json.dumps(odds_json, ensure_ascii=False),
                json.dumps(rec_json, ensure_ascii=False),
            ),
        )
        conn.commit()


def save_race_result(
    venue: str,
    race_number: int,
    race_date: str,
    ticket_type: str,
    result_combo: Tuple[int, int, int],
) -> None:
    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO race_results (
                venue, race_number, race_date, ticket_type, result_combo, created_at
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(venue, race_number, race_date, ticket_type)
            DO UPDATE SET result_combo=excluded.result_combo, created_at=excluded.created_at
            """,
            (
                venue,
                race_number,
                race_date,
                ticket_type,
                "-".join(map(str, result_combo)),
                datetime.now(JST).isoformat(),
            ),
        )
        conn.commit()


def _safe_norm(x: float, lo: float, hi: float) -> float:
    if hi <= lo:
        return 0.5
    r = (x - lo) / (hi - lo)
    if r < 0.0:
        return 0.0
    if r > 1.0:
        return 1.0
    return r


def _strategy_odds_bounds(strategy: str) -> Tuple[float, float]:
    if strategy == "本命":
        return 3.0, 15.0
    if strategy == "中穴":
        return 20.0, 100.0
    return 50.0, 300.0


def _player_strength_map(players: List[dict]) -> Dict[int, float]:
    if not players:
        return {}
    scores = [float(p.get("score", 0.0)) for p in players]
    backs = [float(p.get("back_count", 0.0)) for p in players]
    s_lo, s_hi = min(scores), max(scores)
    b_lo, b_hi = min(backs), max(backs)
    out: Dict[int, float] = {}
    for p in players:
        n = int(p.get("car_number"))
        sc = _safe_norm(float(p.get("score", 0.0)), s_lo, s_hi)
        bc = _safe_norm(float(p.get("back_count", 0.0)), b_lo, b_hi)
        esc = float(p.get("escape_count", 0.0))
        mak = float(p.get("makuri_count", 0.0))
        sas = float(p.get("sashi_count", 0.0))
        mar = float(p.get("mark_count", 0.0))
        total = esc + mak + sas + mar
        kim = ((esc + mak) * 0.55 + (sas + mar) * 0.45) / total if total > 0 else 0.5
        role = p.get("line_role", "不明")
        role_v = 1.0 if role == "先頭" else (0.9 if role == "番手" else (0.75 if role == "短期" else 0.6))
        out[n] = sc * 0.45 + bc * 0.20 + kim * 0.20 + role_v * 0.15
    return out


def _line_component(combo: Tuple[int, int, int], role_map: Dict[int, str], strategy: str) -> float:
    r1 = role_map.get(combo[0], "不明")
    r2 = role_map.get(combo[1], "不明")
    if strategy in ("本命", "中穴"):
        if r1 == "先頭" and r2 == "番手":
            return 1.0
        if r1 == "先頭" or r2 == "番手":
            return 0.7
        return 0.45
    # 大穴は筋違い重視
    if r1 == "先頭" and r2 == "番手":
        return 0.35
    return 0.75


def _tactic_component(combo: Tuple[int, int, int], players_map: Dict[int, dict]) -> float:
    vals = []
    for n in combo:
        p = players_map.get(n, {})
        esc = float(p.get("escape_count", 0.0))
        mak = float(p.get("makuri_count", 0.0))
        sas = float(p.get("sashi_count", 0.0))
        mar = float(p.get("mark_count", 0.0))
        total = esc + mak + sas + mar
        if total <= 0:
            vals.append(0.5)
        else:
            vals.append(((esc + mak) * 0.5 + (sas + mar) * 0.5) / total)
    return sum(vals) / len(vals) if vals else 0.5


def _odds_component(od: float, strategy: str) -> float:
    lo, hi = _strategy_odds_bounds(strategy)
    center = (lo + hi) / 2.0
    spread = max(1.0, (hi - lo) / 2.0)
    return max(0.0, 1.0 - abs(od - center) / (spread * 2.5))


def _combo_score(
    combo: Tuple[int, int, int],
    od: float,
    strategy: str,
    strength: Dict[int, float],
    role_map: Dict[int, str],
    players_map: Dict[int, dict],
    w: Dict[str, float],
) -> float:
    form = (strength.get(combo[0], 0.5) + strength.get(combo[1], 0.5) + strength.get(combo[2], 0.5)) / 3.0
    line = _line_component(combo, role_map, strategy)
    tac = _tactic_component(combo, players_map)
    odd = _odds_component(od, strategy)
    return form * w["form"] + line * w["line"] + tac * w["tactic"] + odd * w["odds"]


def _parse_odds_map(raw: dict, ticket_type: str) -> Dict[Tuple[int, int, int], float]:
    tmap = raw.get(ticket_type, {})
    out: Dict[Tuple[int, int, int], float] = {}
    for k, v in tmap.items():
        if isinstance(k, str):
            # JSON化で "(1, 2, 3)" 形式になるケースに対応
            digits = [int(x) for x in k.replace("(", "").replace(")", "").replace(" ", "").split(",") if x]
            if len(digits) != 3:
                continue
            combo = (digits[0], digits[1], digits[2])
        elif isinstance(k, (list, tuple)) and len(k) == 3:
            combo = (int(k[0]), int(k[1]), int(k[2]))
        else:
            continue
        out[combo] = float(v)
    return out


def train_weights_from_logs(
    lookback_days: int = 90,
    min_samples_per_strategy: int = 20,
) -> dict:
    since = (datetime.now(JST) - timedelta(days=lookback_days)).isoformat()
    with _connect() as conn:
        rows = conn.execute(
            """
            SELECT l.*, r.result_combo
            FROM race_logs l
            LEFT JOIN race_results r
              ON r.venue = l.venue
             AND r.race_number = l.race_number
             AND r.race_date = l.race_date
             AND r.ticket_type = l.ticket_type
            WHERE l.logged_at >= ?
            ORDER BY l.id DESC
            """,
            (since,),
        ).fetchall()

    by_strategy: Dict[str, List[sqlite3.Row]] = {"本命": [], "中穴": [], "大穴": []}
    for r in rows:
        s = r["strategy"]
        if s in by_strategy:
            by_strategy[s].append(r)

    learned: Dict[str, Dict[str, float]] = {}
    stats: Dict[str, int] = {}
    candidates = [
        {"odds": 0.05, "form": 0.50, "line": 0.27, "tactic": 0.18},
        {"odds": 0.06, "form": 0.52, "line": 0.24, "tactic": 0.18},
        {"odds": 0.07, "form": 0.48, "line": 0.27, "tactic": 0.18},
        {"odds": 0.08, "form": 0.50, "line": 0.24, "tactic": 0.18},
        {"odds": 0.10, "form": 0.46, "line": 0.26, "tactic": 0.18},
    ]

    for strategy, samples in by_strategy.items():
        usable = [s for s in samples if int(s["is_mock"]) == 0]
        if len(usable) < min_samples_per_strategy:
            usable = samples[: min(len(samples), min_samples_per_strategy)]
        stats[strategy] = len(usable)
        if not usable:
            continue

        best_w = candidates[0]
        best_score = -1.0
        for w in candidates:
            total = 0.0
            n = 0
            for row in usable:
                players = json.loads(row["players_json"])
                raw_odds = json.loads(row["odds_json"])
                ticket_type = row["ticket_type"]
                odds_map = _parse_odds_map(raw_odds, ticket_type)
                if not odds_map:
                    continue
                players_map = {int(p["car_number"]): p for p in players}
                role_map = {int(p["car_number"]): p.get("line_role", "不明") for p in players}
                strength = _player_strength_map(players)
                lo, hi = _strategy_odds_bounds(strategy)
                pool = [(c, od) for c, od in odds_map.items() if lo <= od <= hi]
                if not pool:
                    pool = list(odds_map.items())
                scored = sorted(
                    ((c, od, _combo_score(c, od, strategy, strength, role_map, players_map, w)) for c, od in pool),
                    key=lambda x: x[2],
                    reverse=True,
                )
                pred = scored[0][0]

                result_combo = row["result_combo"]
                if result_combo:
                    rs = tuple(int(x) for x in str(result_combo).split("-"))
                    if pred == rs:
                        total += 1.0
                    else:
                        # 上位5以内なら部分点
                        top5 = [x[0] for x in scored[:5]]
                        total += 0.35 if rs in top5 else 0.0
                else:
                    # ラベル不足時は自己教師あり（能力中心スコアに近いほど加点）
                    proxy = sorted(
                        (
                            (
                                c,
                                (strength.get(c[0], 0.5) + strength.get(c[1], 0.5) + strength.get(c[2], 0.5)) / 3.0
                                + _line_component(c, role_map, strategy) * 0.35
                                + _tactic_component(c, players_map) * 0.25,
                            )
                            for c, _ in pool
                        ),
                        key=lambda x: x[1],
                        reverse=True,
                    )
                    proxy_top = [x[0] for x in proxy[:5]]
                    total += 0.6 if pred in proxy_top else 0.0
                n += 1
            score = total / max(1, n)
            if score > best_score:
                best_score = score
                best_w = w
        learned[strategy] = best_w

    payload = {
        "trained_at": datetime.now(JST).isoformat(),
        "lookback_days": lookback_days,
        "sample_counts": stats,
        "weights": learned,
    }
    with open(_weights_path(), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)

    with _connect() as conn:
        conn.execute(
            """
            INSERT INTO train_meta(key, value) VALUES('last_trained_at', ?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            """,
            (payload["trained_at"],),
        )
        conn.commit()
    return payload


def maybe_retrain() -> Optional[dict]:
    """
    学習間隔に応じて再学習。
    環境変数:
      LEARN_INTERVAL_MINUTES (default: 360)
      LEARN_LOOKBACK_DAYS (default: 90)
      LEARN_MIN_SAMPLES (default: 20)
    """
    interval_min = int(os.getenv("LEARN_INTERVAL_MINUTES", "360"))
    lookback = int(os.getenv("LEARN_LOOKBACK_DAYS", "90"))
    min_samples = int(os.getenv("LEARN_MIN_SAMPLES", "20"))
    now = datetime.now(JST)
    with _connect() as conn:
        row = conn.execute("SELECT value FROM train_meta WHERE key='last_trained_at'").fetchone()
    if row and row["value"]:
        try:
            last = datetime.fromisoformat(row["value"])
            if now - last < timedelta(minutes=interval_min):
                return None
        except Exception:
            pass
    return train_weights_from_logs(lookback_days=lookback, min_samples_per_strategy=min_samples)


_WEIGHTS_CACHE: Tuple[float, Dict[str, Dict[str, float]]] = (0.0, {})


def load_runtime_weights() -> Dict[str, Dict[str, float]]:
    global _WEIGHTS_CACHE
    path = _weights_path()
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return {}
    if mtime == _WEIGHTS_CACHE[0]:
        return _WEIGHTS_CACHE[1]
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        weights = data.get("weights", {})
        if isinstance(weights, dict):
            _WEIGHTS_CACHE = (mtime, weights)
            return weights
    except Exception:
        return {}
    return {}

