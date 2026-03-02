"""
競輪買い目レコメンドサービス
実際の出走表データ（スクレイプ or モック）＋戦略ロジックで買い目を生成
"""
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

from scraper import RaceInfo, Player

# ─── 定数 ────────────────────────────────────────────────────────────────────

STRATEGIES = {
    "本命": {
        "description": "オッズと出走表指標を重み付けし、堅めの組み合わせを優先",
        "min_bets": 2,
        "max_bets": 8,
        "top_n": 3,
        "confidence": "高",
        "expected_odds": "3〜10倍",
        "rank_range": (1, 5),
        "odds_range": (3.0, 10.0),
        "target_portfolio_odds": (3.0, 10.0),
        # オッズ依存を抑え、競走得点/B本数/決まり手/ラインを重視
        "weights": {"odds": 0.10, "form": 0.50, "line": 0.22, "tactic": 0.18},
    },
    "中穴": {
        "description": "オッズ帯と選手能力のバランスで中穴帯を狙う",
        "min_bets": 5,
        "max_bets": 24,
        "top_n": 5,
        "confidence": "中",
        "expected_odds": "20〜50倍",
        "rank_range": (6, 40),
        "odds_range": (20.0, 50.0),
        "target_portfolio_odds": (20.0, 50.0),
        "weights": {"odds": 0.08, "form": 0.52, "line": 0.22, "tactic": 0.18},
    },
    "大穴": {
        "description": "高配当帯の中で展開要素を重視して選抜",
        "min_bets": 7,
        "max_bets": 100,
        "top_n": 9,
        "confidence": "低",
        "expected_odds": "100〜300倍",
        "rank_range": (41, 120),
        "odds_range": (100.0, 300.0),
        "target_portfolio_odds": (100.0, 300.0),
        "weights": {"odds": 0.07, "form": 0.48, "line": 0.25, "tactic": 0.20},
    },
}

TICKET_TYPES = {
    "三連単": {"players": 3, "ordered": True,  "description": "1〜3着を順番通り当てる"},
    "三連複": {"players": 3, "ordered": False, "description": "1〜3着の選手を順不同で当てる"},
}


@dataclass
class BetRecommendation:
    numbers: List[int]
    amount: int
    stars: int
    reason: str
    current_odds: Optional[float] = None


@dataclass
class RaceRecommendation:
    venue: str
    race_number: int
    strategy: str
    ticket_type: str
    budget: int
    bets: List[BetRecommendation]
    advice: str
    total_amount: int
    is_mock: bool
    source_url: str
    odds_fetched_at: Optional[str]
    ticket_odds_count: int
    matched_odds_count: int


def _rank_players(players: List[Player], strategy: str) -> List[Player]:
    def score_fn(p: Player) -> float:
        base = p.score * 0.4 + p.triple_rate * 30 + p.win_rate * 20
        grade_bonus = {"SS": 20, "S1": 15, "S2": 10, "A1": 5, "A2": 0}.get(p.grade, 0)
        return base + grade_bonus

    ranked = sorted(players, key=score_fn, reverse=True)

    if strategy == "大穴" and len(ranked) >= 6:
        import random
        top3 = ranked[:3]
        rest = ranked[3:]
        random.shuffle(top3)
        ranked = rest[:3] + top3 + rest[3:]

    return ranked


def _generate_combinations(ranked, strategy, ticket_type, num_bets):
    import itertools
    config = STRATEGIES[strategy]
    top_n = min(config["top_n"], len(ranked))
    pool = ranked[:top_n]
    n_in_bet = TICKET_TYPES[ticket_type]["players"]
    ordered = TICKET_TYPES[ticket_type]["ordered"]

    combos = []
    seen = set()

    if ordered:
        for perm in itertools.permutations(pool, n_in_bet):
            nums = [p.car_number for p in perm]
            key = tuple(nums)
            if key not in seen:
                seen.add(key)
                combos.append(nums)
    else:
        for combo in itertools.combinations(pool, n_in_bet):
            nums = sorted(p.car_number for p in combo)
            key = tuple(nums)
            if key not in seen:
                seen.add(key)
                combos.append(nums)

    top1_num = ranked[0].car_number if ranked else None
    combos.sort(key=lambda x: (0 if top1_num in x else 1, x))
    return combos[:num_bets]


def _calc_weighted_avg_odds(odds_list: List[float], amounts: List[int]) -> Optional[float]:
    usable = [(od, am) for od, am in zip(odds_list, amounts) if od is not None]
    if not usable:
        return None
    total_amount = sum(am for _, am in usable)
    if total_amount <= 0:
        return None
    return sum(od * am for od, am in usable) / total_amount


def _calc_synthetic_odds(avg_odds: Optional[float], bet_count: int) -> Optional[float]:
    """
    合成オッズを簡易近似で算出。
    三連単のような多点買いでは、同一予算に対して点数が増えるほど
    実効リターンが薄まるため、平均オッズを点数で割って評価する。
    """
    if avg_odds is None or bet_count <= 0:
        return None
    return avg_odds / bet_count


def _safe_norm(val: float, min_v: float, max_v: float) -> float:
    if max_v <= min_v:
        return 0.5
    return max(0.0, min(1.0, (val - min_v) / (max_v - min_v)))


def _distance_to_range(value: float, low: float, high: float) -> float:
    if low <= value <= high:
        return 0.0
    if value < low:
        return low - value
    return value - high


def _parse_recent_results_stats(recent_results: str) -> Tuple[float, float]:
    """
    直近成績文字列（例: 1-3-2-1-5）から、
    - 平均着順の良さ（0-1, 高いほど良い）
    - 3着内率（0-1）
    を返す。
    """
    if not recent_results:
        return 0.5, 0.5
    nums: List[int] = []
    for t in recent_results.replace(" ", "").split("-"):
        if not t:
            continue
        if t.isdigit():
            v = int(t)
            if 1 <= v <= 9:
                nums.append(v)
    if not nums:
        return 0.5, 0.5
    avg_rank = sum(nums) / len(nums)
    rank_score = max(0.0, min(1.0, (10.0 - avg_rank) / 9.0))
    top3_rate = sum(1 for n in nums if n <= 3) / len(nums)
    return rank_score, top3_rate


def _pearson_corr(xs: List[float], ys: List[float]) -> float:
    n = min(len(xs), len(ys))
    if n < 2:
        return 0.0
    mx = sum(xs[:n]) / n
    my = sum(ys[:n]) / n
    num = 0.0
    dx2 = 0.0
    dy2 = 0.0
    for i in range(n):
        dx = xs[i] - mx
        dy = ys[i] - my
        num += dx * dy
        dx2 += dx * dx
        dy2 += dy * dy
    if dx2 <= 1e-12 or dy2 <= 1e-12:
        return 0.0
    return num / ((dx2 ** 0.5) * (dy2 ** 0.5))


def _learn_feature_weights(players: List[Player]) -> Dict[str, float]:
    """
    出走表に含まれる過去指標（直近成績/勝率/3連対率）に対して、
    説明力が高くなるよう score/B/決まり手/ライン の重みを簡易学習する。
    """
    if not players:
        return {"score": 0.35, "back": 0.20, "kimarite": 0.25, "line": 0.20}

    score_min, score_max = min(p.score for p in players), max(p.score for p in players)
    back_min, back_max = min(p.back_count for p in players), max(p.back_count for p in players)
    win_min, win_max = min(p.win_rate for p in players), max(p.win_rate for p in players)
    tri_min, tri_max = min(p.triple_rate for p in players), max(p.triple_rate for p in players)

    role_val = {"先頭": 1.0, "番手": 0.9, "短期": 0.75, "不明": 0.6}
    features = []
    targets = []
    for p in players:
        attack = p.escape_count + p.makuri_count
        finish = p.sashi_count + p.mark_count
        total = attack + finish
        kimarite = (attack / total) * 0.55 + (finish / total) * 0.45 if total > 0 else 0.5

        f_score = _safe_norm(p.score, score_min, score_max)
        f_back = _safe_norm(float(p.back_count), float(back_min), float(back_max))
        f_kimarite = max(0.0, min(1.0, kimarite))
        f_line = role_val.get(p.line_role, role_val["不明"])
        features.append((f_score, f_back, f_kimarite, f_line))

        recent_rank_score, recent_top3 = _parse_recent_results_stats(p.recent_results)
        win_norm = _safe_norm(p.win_rate, win_min, win_max)
        tri_norm = _safe_norm(p.triple_rate, tri_min, tri_max)
        # 過去実績ターゲット（直近成績をやや重視）
        y = recent_rank_score * 0.35 + recent_top3 * 0.25 + tri_norm * 0.25 + win_norm * 0.15
        targets.append(y)

    best = {"score": 0.35, "back": 0.20, "kimarite": 0.25, "line": 0.20}
    best_obj = -10.0
    # 離散探索（学習データが少ない前提で過学習しにくいよう粗め）
    steps = [0.10, 0.15, 0.20, 0.25, 0.30, 0.35, 0.40]
    for ws in steps:
        for wb in steps:
            for wk in steps:
                wl = 1.0 - (ws + wb + wk)
                if wl < 0.10 or wl > 0.45:
                    continue
                preds = [
                    f[0] * ws + f[1] * wb + f[2] * wk + f[3] * wl
                    for f in features
                ]
                corr = _pearson_corr(preds, targets)
                # 競走得点/決まり手/ライン優先の事前制約を軽く加える
                prior = 0.08 * ws + 0.06 * wk + 0.05 * wl - 0.02 * wb
                obj = corr + prior
                if obj > best_obj:
                    best_obj = obj
                    best = {"score": ws, "back": wb, "kimarite": wk, "line": wl}
    return best


def _learn_strategy_weights(players: List[Player], strategy: str) -> Dict[str, float]:
    """
    レース単位で戦略重みを再学習する。
    直近成績やライン情報が十分なときは、odds重みを下げて能力系を強める。
    """
    base = dict(STRATEGIES[strategy]["weights"])
    if not players:
        return base

    known_roles = sum(1 for p in players if p.line_role != "不明")
    role_ratio = known_roles / max(1, len(players))
    lead_cnt = sum(1 for p in players if p.line_role == "先頭")
    follow_cnt = sum(1 for p in players if p.line_role == "番手")
    line_signal = min(1.0, (min(lead_cnt, follow_cnt) / 3.0))

    recent_scores = []
    top3_rates = []
    for p in players:
        r_score, r_top3 = _parse_recent_results_stats(p.recent_results)
        recent_scores.append(r_score)
        top3_rates.append(r_top3)
    recent_signal = (sum(recent_scores) / len(recent_scores) + sum(top3_rates) / len(top3_rates)) / 2.0 if recent_scores else 0.5

    # 能力系信号が強いほど odds を下げる
    data_conf = role_ratio * 0.45 + line_signal * 0.35 + recent_signal * 0.20

    odds_scale = max(0.25, 0.95 - data_conf * 0.65)
    form_scale = 1.00 + data_conf * 0.35
    line_scale = 1.00 + (role_ratio * 0.6 + line_signal * 0.4) * 0.45
    tactic_scale = 1.00 + data_conf * 0.20

    learned = {
        "odds": base["odds"] * odds_scale,
        "form": base["form"] * form_scale,
        "line": base["line"] * line_scale,
        "tactic": base["tactic"] * tactic_scale,
    }

    # 本命/中穴は同一ライン重視をさらに上乗せ
    if strategy in ("本命", "中穴"):
        learned["line"] *= 1.08
    # 大穴は筋違い（別線）評価のため tactic も少し増やす
    if strategy == "大穴":
        learned["tactic"] *= 1.10

    total = sum(learned.values())
    if total <= 0:
        return base
    learned = {k: v / total for k, v in learned.items()}
    # odds重みの上限を厳しめに固定
    odds_cap = 0.10 if strategy == "本命" else 0.08
    if learned["odds"] > odds_cap:
        excess = learned["odds"] - odds_cap
        learned["odds"] = odds_cap
        redistribute_keys = ["form", "line", "tactic"]
        rsum = sum(learned[k] for k in redistribute_keys)
        if rsum > 0:
            for k in redistribute_keys:
                learned[k] += excess * (learned[k] / rsum)
    return learned


def _sorted_odds_items(race_info: RaceInfo, ticket_type: str):
    odds_map = race_info.odds_map.get(ticket_type, {})
    items = sorted(odds_map.items(), key=lambda x: (x[1], x[0]))
    return [
        {
            "numbers": list(combo),
            "odds": odds,
            "rank": idx + 1,
        }
        for idx, (combo, odds) in enumerate(items)
    ]


def _rebalance_longshot_low_budget_combos(
    race_info: RaceInfo,
    ticket_type: str,
    combos: List[List[int]],
    budget: int,
) -> List[List[int]]:
    """
    大穴×低予算帯（<=1万円）は 100円刻みで広く買う前提を強制する。
    - 目標点数: budget/100
    - ボリュームゾーン: 100〜300倍
    - 50〜100倍は上限（約25%）まで
    """
    target_count = max(1, budget // 100)
    odds_map = race_info.odds_map.get(ticket_type, {})
    if not odds_map:
        return combos[:target_count]

    def to_key(nums: List[int]) -> Tuple[int, int, int]:
        return tuple(nums) if ticket_type == "三連単" else tuple(sorted(nums))

    def key_to_nums(key: Tuple[int, int, int]) -> List[int]:
        return list(key) if ticket_type == "三連単" else sorted(list(key))

    bridge_cap = max(2, min(6, target_count // 4))

    # 既存買い目をベースに、重複除去して保持
    selected_keys: List[Tuple[int, int, int]] = []
    seen = set()
    for nums in combos:
        k = to_key(nums)
        if k in odds_map and k not in seen:
            seen.add(k)
            selected_keys.append(k)

    # 50〜100帯が多すぎる場合は低オッズ側から削る
    bridge_keys = [k for k in selected_keys if 50.0 < odds_map.get(k, 0.0) <= 100.0]
    if len(bridge_keys) > bridge_cap:
        removable = sorted(bridge_keys, key=lambda k: (odds_map.get(k, 0.0), k))
        remove_n = len(bridge_keys) - bridge_cap
        remove_set = set(removable[:remove_n])
        selected_keys = [k for k in selected_keys if k not in remove_set]
        seen = set(selected_keys)

    def add_from(pool: List[Tuple[int, int, int]]) -> None:
        for k in pool:
            if len(selected_keys) >= target_count:
                return
            if k in seen:
                continue
            seen.add(k)
            selected_keys.append(k)

    items = _sorted_odds_items(race_info, ticket_type)
    pool_100_300 = [tuple(x["numbers"]) for x in items if 100.0 < x["odds"] <= 300.0]
    pool_300_500 = [tuple(x["numbers"]) for x in items if 300.0 < x["odds"] <= 500.0]
    pool_50_100 = [tuple(x["numbers"]) for x in items if 50.0 < x["odds"] <= 100.0]
    pool_under_500 = [tuple(x["numbers"]) for x in items if x["odds"] <= 500.0]

    # まず 100〜300 を優先して目標点数まで埋める
    add_from(pool_100_300)
    # 不足時のみ 300〜500 を補完
    add_from(pool_300_500)
    # さらに不足時のみ 50〜100 を上限付きで補完
    if len(selected_keys) < target_count:
        current_bridge = sum(1 for k in selected_keys if 50.0 < odds_map.get(k, 0.0) <= 100.0)
        bridge_room = max(0, bridge_cap - current_bridge)
        if bridge_room > 0:
            add_from(pool_50_100[:bridge_room])
    # まだ不足する場合のみ <=500 全域から補完
    add_from(pool_under_500)

    return [key_to_nums(k) for k in selected_keys[:target_count]]


def _build_player_feature_map(players: List[Player]) -> Dict[int, Dict[str, float]]:
    if not players:
        return {}

    score_min, score_max = min(p.score for p in players), max(p.score for p in players)
    win_min, win_max = min(p.win_rate for p in players), max(p.win_rate for p in players)
    tri_min, tri_max = min(p.triple_rate for p in players), max(p.triple_rate for p in players)
    back_min, back_max = min(p.back_count for p in players), max(p.back_count for p in players)

    learned = _learn_feature_weights(players)

    role_value = {"先頭": 1.0, "番手": 0.90, "短期": 0.75, "不明": 0.60}
    style_value = {"逃": 1.0, "両": 0.85, "追": 0.70}

    feature_map: Dict[int, Dict[str, float]] = {}
    player_map = {p.car_number: p for p in players}

    # 同一ライン推定:
    # - 先頭は自車番をラインID
    # - 番手は「同地区 + 先頭の推進力 + 番手の追込み適性」で最も相性が高い先頭へ接続
    leaders = [p for p in players if p.line_role == "先頭"]
    line_id_map: Dict[int, int] = {}
    for p in leaders:
        line_id_map[p.car_number] = p.car_number

    if leaders:
        for p in players:
            if p.line_role != "番手":
                continue
            best_leader = leaders[0]
            best_score = -1.0
            for ld in leaders:
                same_pref_bonus = 0.25 if p.prefecture and p.prefecture == ld.prefecture else 0.0
                ld_power = p.score * 0.0 + ld.score * 0.55 + ld.back_count * 0.20 + (ld.escape_count + ld.makuri_count) * 0.25
                follower_fit = p.mark_count * 0.45 + p.sashi_count * 0.35 + p.triple_rate * 10.0 * 0.20
                score = same_pref_bonus + ld_power * 0.01 + follower_fit * 0.01
                if score > best_score:
                    best_score = score
                    best_leader = ld
            line_id_map[p.car_number] = best_leader.car_number

    for p in players:
        if p.car_number not in line_id_map:
            # 短期/不明は独立ライン扱い
            line_id_map[p.car_number] = p.car_number
    for p in players:
        attack_count = p.escape_count + p.makuri_count
        finish_count = p.sashi_count + p.mark_count
        move_total = attack_count + finish_count
        attack_ratio = (attack_count / move_total) if move_total > 0 else (0.55 if p.style in ("逃", "両") else 0.35)
        finish_ratio = (finish_count / move_total) if move_total > 0 else (0.45 if p.style in ("追", "両") else 0.35)
        recent_rank_score, recent_top3 = _parse_recent_results_stats(p.recent_results)

        score_norm = _safe_norm(p.score, score_min, score_max)
        back_norm = _safe_norm(float(p.back_count), float(back_min), float(back_max))
        kimarite_score = attack_ratio * 0.55 + finish_ratio * 0.45
        line_score = role_value.get(p.line_role, role_value["不明"])

        learned_power = (
            score_norm * learned["score"]
            + back_norm * learned["back"]
            + kimarite_score * learned["kimarite"]
            + line_score * learned["line"]
        )

        form_score = (
            learned_power * 0.60
            + _safe_norm(p.win_rate, win_min, win_max) * 0.10
            + _safe_norm(p.triple_rate, tri_min, tri_max) * 0.12
            + recent_rank_score * 0.10
            + recent_top3 * 0.08
        )

        tactic_score = (
            attack_ratio * 0.50
            + finish_ratio * 0.35
            + style_value.get(p.style, 0.60) * 0.15
        )

        feature_map[p.car_number] = {
            "form": form_score,
            "tactic": tactic_score,
            "role": role_value.get(p.line_role, role_value["不明"]),
            "attack_ratio": attack_ratio,
            "finish_ratio": finish_ratio,
            "line_role": p.line_role,
            "score_norm": score_norm,
            "back_norm": back_norm,
            "line_score": line_score,
            # 先頭の推進力を番手評価へ伝播させるための指標
            "leader_power": score_norm * 0.60 + back_norm * 0.25 + attack_ratio * 0.15,
            "recent_rank_score": recent_rank_score,
            "recent_top3": recent_top3,
            "line_id": line_id_map.get(p.car_number, p.car_number),
        }

    return feature_map


def _calc_line_component(
    combo: List[int],
    ticket_type: str,
    strategy: str,
    feature_map: Dict[int, Dict[str, float]],
) -> float:
    feats = [feature_map.get(n, {}) for n in combo]
    roles = [f.get("line_role", "不明") for f in feats]

    if ticket_type == "三連単":
        first = roles[0] if len(roles) > 0 else "不明"
        second = roles[1] if len(roles) > 1 else "不明"
        first_f = feats[0] if len(feats) > 0 else {}
        second_f = feats[1] if len(feats) > 1 else {}
        third_f = feats[2] if len(feats) > 2 else {}
        line_ids = [f.get("line_id", n) for f, n in zip(feats, combo)]
        same_line_12 = len(line_ids) > 1 and line_ids[0] == line_ids[1]
        distinct_lines = len(set(line_ids)) if line_ids else 0

        base = 0.35
        if first == "先頭":
            base += 0.15
        if second == "番手":
            base += 0.10
        if "短期" in roles:
            base += 0.05

        # 先頭が強いほど番手の連対期待を引き上げる
        leader_power = first_f.get("leader_power", first_f.get("form", 0.5))
        second_form = second_f.get("form", 0.5)
        third_form = third_f.get("form", 0.5)
        synergy = 0.0

        if first == "先頭" and second == "番手" and same_line_12:
            synergy += 0.17 + leader_power * 0.22 + second_form * 0.10
            # 同一ラインの先頭-番手が強いと3着も連れやすい
            synergy += third_form * 0.05
        elif first == "先頭" and second == "番手":
            # 異ラインの先頭-番手は加点を抑制
            synergy += 0.03 + leader_power * 0.05 + second_form * 0.03
        elif first == "先頭":
            synergy += 0.05 + leader_power * 0.10
        elif second == "番手":
            synergy += 0.04 + second_form * 0.06

        # 戦略別のライン重み:
        # 本命/中穴: 同一ライン(先頭-番手)を強く評価
        # 大穴: 別線決着(筋違い)を評価
        strategy_adj = 0.0
        if strategy in ("本命", "中穴"):
            if same_line_12 and first == "先頭" and second == "番手":
                strategy_adj += 0.24
            elif same_line_12:
                strategy_adj += 0.08
            else:
                strategy_adj -= 0.10
        else:
            # 大穴は筋違い決着を加点、同線ワンツーは減点
            if same_line_12 and first == "先頭" and second == "番手":
                strategy_adj -= 0.10
            if distinct_lines >= 2:
                strategy_adj += 0.10
            if distinct_lines == 3:
                strategy_adj += 0.06

        return min(1.0, max(0.0, base + synergy + strategy_adj))

    # 三連複は並び順がないため、先頭/番手の同居を評価
    has_lead = "先頭" in roles
    has_follow = "番手" in roles
    has_short = "短期" in roles
    score = 0.40 + (0.15 if has_lead and has_follow else 0.0) + (0.05 if has_short else 0.0)
    if has_lead:
        lead_power = max((f.get("leader_power", f.get("form", 0.5)) for f in feats), default=0.5)
        score += lead_power * 0.15
    if has_follow:
        follow_form = max((f.get("form", 0.5) for f, r in zip(feats, roles) if r == "番手"), default=0.5)
        score += follow_form * 0.10
    line_ids = [f.get("line_id", n) for f, n in zip(feats, combo)]
    distinct_lines = len(set(line_ids)) if line_ids else 0
    if strategy in ("本命", "中穴"):
        if distinct_lines <= 2:
            score += 0.08
    else:
        if distinct_lines >= 2:
            score += 0.10
        if distinct_lines == 3:
            score += 0.05
    return min(1.0, max(0.0, score))


def _calc_tactic_component(combo: List[int], ticket_type: str, feature_map: Dict[int, Dict[str, float]]) -> float:
    f = [feature_map.get(n, {}) for n in combo]
    if not f:
        return 0.5

    if ticket_type == "三連単":
        first_attack = f[0].get("attack_ratio", 0.5) if len(f) > 0 else 0.5
        second_finish = f[1].get("finish_ratio", 0.5) if len(f) > 1 else 0.5
        mean_tactic = sum(x.get("tactic", 0.5) for x in f) / len(f)
        return min(1.0, first_attack * 0.45 + second_finish * 0.30 + mean_tactic * 0.25)

    return min(1.0, sum(x.get("tactic", 0.5) for x in f) / len(f))


def _calc_odds_component(odds: float, rank: int, strategy: str) -> float:
    cfg = STRATEGIES[strategy]
    low, high = cfg["target_portfolio_odds"]
    rank_min, rank_max = cfg["rank_range"]
    center = (low + high) / 2
    spread = max((high - low) / 2, 1.0)
    odds_closeness = max(0.0, 1.0 - abs(odds - center) / (spread * 3.0))

    if rank_max <= rank_min:
        rank_fit = 0.5
    elif rank < rank_min:
        rank_fit = max(0.0, 1.0 - (rank_min - rank) / max(rank_min, 1))
    elif rank > rank_max:
        rank_fit = max(0.0, 1.0 - (rank - rank_max) / max(rank_max, 1))
    else:
        rank_fit = 1.0

    # オッズは補助情報として扱う（過度な最適化を避ける）
    return odds_closeness * 0.55 + rank_fit * 0.45


def _calc_combo_score(
    combo: List[int],
    odds: float,
    rank: int,
    strategy: str,
    ticket_type: str,
    feature_map: Dict[int, Dict[str, float]],
    strategy_weights: Optional[Dict[str, float]] = None,
) -> float:
    weights = strategy_weights or STRATEGIES[strategy]["weights"]
    player_features = [feature_map.get(n, {"form": 0.5}) for n in combo]
    if player_features:
        form_component = sum(f.get("form", 0.5) for f in player_features) / len(player_features)
        recent_component = sum(
            (f.get("recent_rank_score", 0.5) * 0.55 + f.get("recent_top3", 0.5) * 0.45)
            for f in player_features
        ) / len(player_features)
        form_component = form_component * 0.85 + recent_component * 0.15
    else:
        form_component = 0.5
    line_component = _calc_line_component(combo, ticket_type, strategy, feature_map)
    tactic_component = _calc_tactic_component(combo, ticket_type, feature_map)
    odds_component = _calc_odds_component(odds, rank, strategy)
    return (
        odds_component * weights["odds"]
        + form_component * weights["form"]
        + line_component * weights["line"]
        + tactic_component * weights["tactic"]
    )


def _get_budget_target_count(strategy: str, budget: int, min_bets: int, max_bets: int) -> int:
    if strategy == "本命":
        base = max(2, budget // 500)
    elif strategy == "中穴":
        if budget <= 1000:
            base = 7
        elif budget <= 1500:
            base = 10
        elif budget <= 2000:
            base = 13
        elif budget <= 3000:
            base = 14
        elif budget <= 5000:
            base = 16
        elif budget <= 8000:
            base = 19
        else:
            base = 22
    else:
        # 大穴は低予算帯では100円刻みで広く買う方針
        if budget <= 10000:
            base = max(7, budget // 100)
        else:
            base = max(7, budget // 150)
    return max(min_bets, min(max_bets, base))


def _get_middle_count_window(budget: int) -> Tuple[int, int]:
    if budget <= 1000:
        return 6, 8
    if budget <= 1500:
        return 9, 12
    if budget <= 2000:
        return 12, 15
    if budget <= 5000:
        return 12, 16
    if budget <= 8000:
        return 14, 20
    return 16, 24


def _calc_diversity_score(selected: List[dict], ticket_type: str) -> float:
    if not selected:
        return 0.0
    combos = [s["numbers"] for s in selected]
    unique_numbers = len({n for c in combos for n in c})
    number_div = unique_numbers / 9.0

    if ticket_type == "三連単":
        first_div = len({c[0] for c in combos}) / max(1, min(9, len(combos)))
    else:
        first_div = len({min(c) for c in combos}) / max(1, min(9, len(combos)))

    overlap_vals = []
    for i in range(len(combos)):
        a = set(combos[i])
        for j in range(i + 1, len(combos)):
            b = set(combos[j])
            overlap_vals.append(len(a & b) / 3.0)
    overlap = (sum(overlap_vals) / len(overlap_vals)) if overlap_vals else 0.0
    overlap_score = 1.0 - overlap
    return number_div * 0.40 + first_div * 0.30 + overlap_score * 0.30


def _estimate_race_chaos(players: List[Player]) -> bool:
    """
    荒れやすさを簡易判定。
    - 先頭役が多い
    - バック本数が全体的に多い
    - 上位の3連対率差が小さい（混戦）
    """
    if not players:
        return False
    lead_cnt = sum(1 for p in players if p.line_role == "先頭")
    avg_back = sum(p.back_count for p in players) / max(1, len(players))
    tri_rates = sorted((p.triple_rate for p in players), reverse=True)
    top_spread = (tri_rates[0] - tri_rates[min(2, len(tri_rates) - 1)]) if tri_rates else 1.0
    chaos_score = 0.0
    chaos_score += 1.0 if lead_cnt >= 3 else 0.0
    chaos_score += 1.0 if avg_back >= 2.0 else 0.0
    chaos_score += 1.0 if top_spread <= 0.12 else 0.0
    return chaos_score >= 2.0


def _decide_bridge_owner(scored: List[dict], budget: int) -> str:
    """
    50〜100倍帯（ブリッジ帯）を中穴/大穴どちらに寄せるかを、
    全体オッズ分布と必要点数の圧迫度で決定する。
    """
    c_mid = sum(1 for x in scored if 20.0 <= x["odds"] <= 50.0)
    c_bridge = sum(1 for x in scored if 50.0 < x["odds"] <= 100.0)
    c_long = sum(1 for x in scored if 100.0 < x["odds"] <= 300.0)
    if c_bridge == 0:
        return "none"

    affordable = max(1, budget // 100)
    mid_min = min(STRATEGIES["中穴"].get("min_bets", 5), affordable, len(scored))
    mid_max = min(STRATEGIES["中穴"].get("max_bets", 18), affordable, len(scored))
    long_min = min(STRATEGIES["大穴"].get("min_bets", 7), affordable, len(scored))
    long_max = min(STRATEGIES["大穴"].get("max_bets", 100), affordable, len(scored))
    if mid_max < mid_min:
        mid_min = mid_max
    if long_max < long_min:
        long_min = long_max

    req_mid = _get_budget_target_count("中穴", budget, mid_min, mid_max) if mid_max > 0 else 0
    req_long = _get_budget_target_count("大穴", budget, long_min, long_max) if long_max > 0 else 0

    mid_pressure = req_mid / max(c_mid, 1)
    long_pressure = req_long / max(c_long, 1)

    # 供給不足が強い方に 50〜100帯を割り当てる
    return "大穴" if long_pressure > mid_pressure else "中穴"


def _filter_candidates_by_strategy(
    scored: List[dict],
    strategy: str,
    required_count: int = 0,
    allow_high_odds_extension: bool = False,
    bridge_owner: str = "none",
) -> List[dict]:
    cfg = STRATEGIES[strategy]
    rank_min, rank_max = cfg.get("rank_range", (1, 10**9))
    odds_min, odds_max = cfg.get("odds_range", (0.0, float("inf")))
    odds_center = (odds_min + odds_max) / 2.0

    if strategy == "中穴" and bridge_owner == "中穴":
        bridge = [
            x for x in scored
            if rank_min <= x["rank"] <= rank_max and 50.0 < x["odds"] <= 100.0
        ]
        bridge.sort(key=lambda x: (abs(x["odds"] - 75.0), x["rank"]))

        strict = [
            x for x in scored
            if rank_min <= x["rank"] <= rank_max and (
                odds_min <= x["odds"] <= odds_max
                or (50.0 < x["odds"] <= 100.0)
            )
        ]

        if strict:
            must = []
            need_bridge = min(2, len(bridge))
            for x in bridge[:need_bridge]:
                must.append(x)
            merged = must + [x for x in strict if x not in must]
            if required_count > 0 and len(merged) < required_count:
                remain = [x for x in scored if rank_min <= x["rank"] <= rank_max and x not in merged]
                remain.sort(key=lambda x: (abs(x["odds"] - odds_center), x["rank"]))
                for x in remain:
                    merged.append(x)
                    if len(merged) >= required_count:
                        break
            if required_count > 0:
                return merged[:required_count]
            return merged

        if strict and (required_count <= 0 or len(strict) >= required_count):
            return strict

    if strategy == "大穴":
        # 大穴でも 500倍超は常時除外
        capped = [x for x in scored if x["odds"] <= 500.0]
        # 50〜100倍帯（ブリッジ帯）は大穴では入れ過ぎない
        # 目安: 全体の25%まで（最低2点、最大6点）
        bridge_cap = 2
        if required_count > 0:
            bridge_cap = max(2, min(6, required_count // 4))
        strict = [
            x for x in capped
            if rank_min <= x["rank"] <= rank_max and odds_min <= x["odds"] <= odds_max
        ]
        if required_count <= 0:
            required_count = len(strict)

        # 基本は 100〜300 倍で構成
        result = strict[:]

        # 50〜100帯を大穴側に割り当てる場合は、最低1〜2点を優先採用
        if bridge_owner == "大穴":
            bridge = [
                x for x in capped
                if 50.0 < x["odds"] <= 100.0
            ]
            bridge.sort(key=lambda x: (abs(x["odds"] - 75.0), x["rank"]))
            need_bridge = min(1, len(bridge), required_count if required_count > 0 else 1, bridge_cap)
            must = []
            for x in bridge[:need_bridge]:
                if x not in must:
                    must.append(x)
            result = must + [x for x in result if x not in must]
            if required_count > 0 and len(result) > required_count:
                result = result[:required_count]

        # 荒れる判定時のみ 300〜500 倍を最大2点まで補充
        if allow_high_odds_extension and len(result) < required_count:
            ext = [
                x for x in capped
                if rank_min <= x["rank"] <= rank_max and 300.0 < x["odds"] <= 500.0
            ]
            ext.sort(key=lambda x: (abs(x["odds"] - 400.0), x["rank"]))
            extra_limit = min(2, required_count - len(result))
            for x in ext[:extra_limit]:
                if x not in result:
                    result.append(x)

        # 不足時は rank帯を外しても 100〜300 倍を優先補完（大穴の主戦場）
        if len(result) < required_count:
            broad_main_pool = [
                x for x in capped
                if odds_min <= x["odds"] <= odds_max and x not in result
            ]
            broad_main_pool.sort(key=lambda x: (abs(x["odds"] - odds_center), x["rank"]))
            for x in broad_main_pool:
                result.append(x)
                if len(result) >= required_count:
                    break

        # さらに不足する場合は rank帯内から補完（まず 100倍超を優先）
        if len(result) < required_count:
            if allow_high_odds_extension:
                rank_pool = [
                    x for x in capped
                    if rank_min <= x["rank"] <= rank_max and x not in result and x["odds"] > 100.0
                ]
            else:
                rank_pool = [
                    x for x in capped
                    if rank_min <= x["rank"] <= rank_max and x not in result and 100.0 < x["odds"] <= 300.0
                ]
            rank_pool.sort(key=lambda x: (abs(x["odds"] - odds_center), x["rank"]))
            for x in rank_pool:
                result.append(x)
                if len(result) >= required_count:
                    break

        # なお不足時のみ、50〜100（ブリッジ帯）を最後の補完として使用
        if len(result) < required_count:
            current_bridge = sum(1 for x in result if 50.0 < x["odds"] <= 100.0)
            bridge_fallback = [x for x in capped if 50.0 < x["odds"] <= 100.0 and x not in result]
            bridge_fallback.sort(key=lambda x: (abs(x["odds"] - 75.0), x["rank"]))
            for x in bridge_fallback:
                if current_bridge >= bridge_cap:
                    break
                result.append(x)
                current_bridge += 1
                if len(result) >= required_count:
                    break

        # 最終ガード: 50〜100倍の比率を上限内に抑える
        bridge_items = [x for x in result if 50.0 < x["odds"] <= 100.0]
        if len(bridge_items) > bridge_cap:
            keep_bridge = sorted(
                bridge_items,
                key=lambda x: (abs(x["odds"] - 95.0), x["rank"])
            )[:bridge_cap]
            non_bridge = [x for x in result if not (50.0 < x["odds"] <= 100.0)]
            result = non_bridge + keep_bridge

            if len(result) < required_count:
                refill_pool = [
                    x for x in capped
                    if x not in result and x["odds"] > 100.0
                ]
                if not allow_high_odds_extension:
                    refill_pool = [x for x in refill_pool if x["odds"] <= 300.0]
                refill_pool.sort(key=lambda x: (abs(x["odds"] - odds_center), x["rank"]))
                for x in refill_pool:
                    result.append(x)
                    if len(result) >= required_count:
                        break

        if result:
            return result

    strict = [
        x for x in scored
        if rank_min <= x["rank"] <= rank_max and odds_min <= x["odds"] <= odds_max
    ]
    if strict and (required_count <= 0 or len(strict) >= required_count):
        return strict

    rank_only = [x for x in scored if rank_min <= x["rank"] <= rank_max]
    if rank_only:
        if not strict:
            if required_count <= 0:
                return rank_only
            # 目標オッズに近い順で必要件数まで採用
            ranked = sorted(rank_only, key=lambda x: (abs(x["odds"] - odds_center), x["rank"]))
            return ranked[:required_count]

        # strictが不足する場合はrank帯からオッズ近傍を補完
        remain = [x for x in rank_only if x not in strict]
        remain.sort(key=lambda x: (abs(x["odds"] - odds_center), x["rank"]))
        merged = strict[:]
        for x in remain:
            if x in merged:
                continue
            merged.append(x)
            if required_count > 0 and len(merged) >= required_count:
                break
        return merged

    if strict:
        # rank帯が取れないが strict はあるケース
        return strict

    odds_only = [x for x in scored if odds_min <= x["odds"] <= odds_max]
    if odds_only:
        if required_count <= 0:
            return odds_only
        ranked = sorted(odds_only, key=lambda x: (abs(x["odds"] - odds_center), x["rank"]))
        return ranked[:required_count]

    if required_count > 0:
        return scored[:required_count]

    return scored


def _inject_bridge_tickets(
    selected: List[dict],
    scoped: List[dict],
    strategy: str,
    bridge_owner: str,
) -> List[dict]:
    if bridge_owner != strategy or not selected:
        return selected

    bridge = [x for x in scoped if 50.0 < x["odds"] <= 100.0]
    if not bridge:
        return selected

    need = min(2, len(bridge), len(selected))
    have = [x for x in selected if 50.0 < x["odds"] <= 100.0]
    if len(have) >= need:
        return selected

    selected_new = selected[:]
    # 差し込む候補
    add_pool = [x for x in bridge if x not in selected_new]
    if not add_pool:
        return selected
    add_pool.sort(key=lambda x: (abs(x["odds"] - 75.0), x["rank"]))

    # 置換対象は「スコアが低く、50〜100でもない」買い目を優先
    replace_idx = sorted(
        range(len(selected_new)),
        key=lambda i: (selected_new[i]["score"], abs(selected_new[i]["odds"] - 75.0)),
    )

    deficit = need - len(have)
    for cand in add_pool:
        if deficit <= 0:
            break
        target = next((i for i in replace_idx if not (50.0 < selected_new[i]["odds"] <= 100.0)), None)
        if target is None:
            break
        selected_new[target] = cand
        deficit -= 1

    return selected_new


def _inject_same_line_axis_candidates(
    scored: List[dict],
    scoped: List[dict],
    strategy: str,
    feature_map: Dict[int, Dict[str, float]],
) -> List[dict]:
    """
    本命/中穴では、同一ラインの先頭-番手軸を候補に差し込む。
    オッズ帯フィルタで本線が抜け落ちるのを防ぐための補正。
    """
    if strategy not in ("本命", "中穴"):
        return scoped

    axis = []
    for x in scored:
        nums = x["numbers"]
        if len(nums) < 2:
            continue
        f1 = feature_map.get(nums[0], {})
        f2 = feature_map.get(nums[1], {})
        if f1.get("line_role") != "先頭" or f2.get("line_role") != "番手":
            continue
        if f1.get("line_id") != f2.get("line_id"):
            continue
        axis.append(x)

    if not axis:
        return scoped

    # 軸候補は少数精鋭で先頭側に配置
    axis.sort(key=lambda x: (-x["score"], x["rank"], x["odds"]))
    top_n = 3 if strategy == "本命" else 4
    head = axis[:top_n]
    merged = head + [x for x in scoped if x not in head]
    return merged


def _is_same_line_lead_follow(item: dict, feature_map: Dict[int, Dict[str, float]]) -> bool:
    nums = item.get("numbers", [])
    if len(nums) < 2:
        return False
    f1 = feature_map.get(nums[0], {})
    f2 = feature_map.get(nums[1], {})
    return (
        f1.get("line_role") == "先頭"
        and f2.get("line_role") == "番手"
        and f1.get("line_id") == f2.get("line_id")
    )


def _inject_axis_and_crossline_constraints(
    selected: List[dict],
    scoped: List[dict],
    strategy: str,
    feature_map: Dict[int, Dict[str, float]],
) -> List[dict]:
    if not selected:
        return selected

    selected_new = selected[:]
    if strategy in ("本命", "中穴"):
        need_axis = 2 if strategy == "本命" else 1
        axis_count = sum(1 for x in selected_new if _is_same_line_lead_follow(x, feature_map))
        if axis_count < need_axis:
            axis_pool = [x for x in scoped if _is_same_line_lead_follow(x, feature_map) and x not in selected_new]
            axis_pool.sort(key=lambda x: (-x["score"], x["rank"], x["odds"]))
            replace_idx = sorted(
                range(len(selected_new)),
                key=lambda i: (selected_new[i]["score"], selected_new[i]["odds"])
            )
            for cand in axis_pool:
                if axis_count >= need_axis:
                    break
                target = next(
                    (i for i in replace_idx if not _is_same_line_lead_follow(selected_new[i], feature_map)),
                    None
                )
                if target is None:
                    break
                selected_new[target] = cand
                axis_count += 1

    if strategy == "大穴":
        # 大穴は別線決着を重視。先頭-番手の同線ワンツーを抑える。
        cross_pool = [
            x for x in scoped
            if not _is_same_line_lead_follow(x, feature_map) and x not in selected_new
        ]
        cross_pool.sort(key=lambda x: (-x["score"], x["rank"], x["odds"]))
        for i in range(len(selected_new)):
            if not _is_same_line_lead_follow(selected_new[i], feature_map):
                continue
            if not cross_pool:
                break
            selected_new[i] = cross_pool.pop(0)

    return selected_new


def _select_combos_from_odds(
    race_info: RaceInfo,
    strategy: str,
    ticket_type: str,
    budget: int,
) -> List[List[int]]:
    config = STRATEGIES[strategy]
    ranked_odds = _sorted_odds_items(race_info, ticket_type)
    candidates = ranked_odds[:]
    if not candidates:
        return []

    feature_map = _build_player_feature_map(race_info.players)
    learned_strategy_weights = _learn_strategy_weights(race_info.players, strategy)
    scored = []
    for item in candidates:
        combo = item["numbers"]
        score = _calc_combo_score(
            combo=combo,
            odds=item["odds"],
            rank=item["rank"],
            strategy=strategy,
            ticket_type=ticket_type,
            feature_map=feature_map,
            strategy_weights=learned_strategy_weights,
        )
        scored.append({**item, "score": score})

    scored.sort(key=lambda x: (-x["score"], x["rank"], x["odds"]))
    affordable = max(1, budget // 100)
    min_bets = min(config.get("min_bets", 2), affordable, len(scored))
    max_bets = min(config.get("max_bets", 12), affordable, len(scored))
    if max_bets < min_bets:
        min_bets = max_bets

    target_low, target_high = config.get("target_portfolio_odds", (3.0, 5.0))
    synth_low, synth_high = (3.0, 5.0)
    target_center = (target_low + target_high) / 2.0

    candidates_packs = []
    desired_count = _get_budget_target_count(strategy, budget, min_bets, max_bets)
    required_count = desired_count if strategy in ("中穴", "大穴") else min_bets
    chaos = _estimate_race_chaos(race_info.players)
    bridge_owner = _decide_bridge_owner(scored, budget)
    scoped = _filter_candidates_by_strategy(
        scored,
        strategy,
        required_count=required_count,
        allow_high_odds_extension=(strategy == "大穴" and chaos),
        bridge_owner=bridge_owner,
    )
    scoped = _inject_same_line_axis_candidates(scored, scoped, strategy, feature_map)

    # scoped件数に合わせて点数上限を再計算
    min_bets = min(min_bets, len(scoped))
    max_bets = min(max_bets, len(scoped))
    if max_bets < min_bets:
        min_bets = max_bets
    desired_count = min(max(desired_count, min_bets), max_bets)
    required_count = min(max(required_count, min_bets), max_bets)

    if strategy == "中穴":
        w_min, w_max = _get_middle_count_window(budget)
        search_min = max(min_bets, w_min)
        search_max = min(max_bets, w_max)
        required_count = max(search_min, min(required_count, search_max))
    else:
        search_min = max(min_bets, desired_count - 2)
        search_max = min(max_bets, desired_count + 2)
        search_min = max(search_min, required_count)

    for bet_count in range(search_min, search_max + 1):
        selected = sorted(
            scoped,
            key=lambda x: (abs(x["odds"] - target_center), x["rank"], -x["score"])
        )[:bet_count]
        selected = _inject_bridge_tickets(selected, scoped, strategy, bridge_owner)
        selected = _inject_axis_and_crossline_constraints(selected, scoped, strategy, feature_map)
        amounts = _calc_amounts(budget, len(selected), strategy)
        remaining = [x for x in scoped if x not in selected]
        remaining.sort(key=lambda x: x["odds"])

        for _ in range(25):
            avg_odds = _calc_weighted_avg_odds([x["odds"] for x in selected], amounts)
            if avg_odds is None:
                break
            if target_low <= avg_odds <= target_high:
                break

            if avg_odds < target_low:
                selected.sort(key=lambda x: x["odds"])
                low_item = selected[0]
                replacement = next((x for x in reversed(remaining) if x["odds"] > low_item["odds"]), None)
            else:
                selected.sort(key=lambda x: x["odds"], reverse=True)
                low_item = selected[0]
                replacement = next((x for x in remaining if x["odds"] < low_item["odds"]), None)

            if replacement is None:
                break
            selected[0] = replacement
            remaining.remove(replacement)
            remaining.append(low_item)

        amounts = _calc_amounts(budget, len(selected), strategy)
        avg_odds = _calc_weighted_avg_odds([x["odds"] for x in selected], amounts)
        if avg_odds is None:
            continue
        synthetic_odds = _calc_synthetic_odds(avg_odds, len(selected))
        range_distance = _distance_to_range(avg_odds, target_low, target_high)
        center_distance = abs(avg_odds - target_center)
        synth_distance = (
            _distance_to_range(synthetic_odds, synth_low, synth_high)
            if synthetic_odds is not None else 0.0
        )
        # 合成オッズ・点数・分散のバランスで評価
        quality_bonus = -sum(x["score"] for x in selected) / max(len(selected), 1) * 0.05
        diversity = _calc_diversity_score(selected, ticket_type)

        if strategy == "本命":
            count_penalty = abs(bet_count - desired_count) * 0.45
            diversity_penalty = (1.0 - diversity) * 0.8
            range_weight = 20
            center_weight = 2
            synth_weight = 10
        elif strategy == "中穴":
            count_penalty = abs(bet_count - desired_count) * 0.70
            diversity_penalty = (1.0 - diversity) * 2.2
            range_weight = 12
            center_weight = 1.5
            synth_weight = 12
        else:
            count_penalty = abs(bet_count - desired_count) * 0.90
            diversity_penalty = (1.0 - diversity) * 2.8
            range_weight = 10
            center_weight = 1.2
            synth_weight = 10

        objective = (
            range_distance * range_weight
            + center_distance * center_weight
            + synth_distance * synth_weight
            + count_penalty
            + diversity_penalty
            + quality_bonus
        )
        candidates_packs.append(
            {
                "objective": objective,
                "selected": selected[:],
                "bet_count": bet_count,
                "avg_odds": avg_odds,
            }
        )

    if not candidates_packs:
        final_selected = scoped[:min_bets]
    else:
        chosen = min(candidates_packs, key=lambda p: p["objective"])
        chosen_drift = _distance_to_range(chosen["avg_odds"], target_low, target_high)

        # 予算寄り探索で合成オッズが大きく外れた場合は、全点数帯から再探索して補正する。
        if chosen_drift > 1.0:
            global_packs = []
            global_min = max(min_bets, required_count)
            for bet_count in range(global_min, max_bets + 1):
                selected = sorted(
                    scoped,
                    key=lambda x: (abs(x["odds"] - target_center), x["rank"], -x["score"])
                )[:bet_count]
                selected = _inject_bridge_tickets(selected, scoped, strategy, bridge_owner)
                selected = _inject_axis_and_crossline_constraints(selected, scoped, strategy, feature_map)
                amounts = _calc_amounts(budget, len(selected), strategy)
                remaining = [x for x in scoped if x not in selected]
                remaining.sort(key=lambda x: x["odds"])

                for _ in range(25):
                    avg_odds = _calc_weighted_avg_odds([x["odds"] for x in selected], amounts)
                    if avg_odds is None:
                        break
                    if target_low <= avg_odds <= target_high:
                        break
                    if avg_odds < target_low:
                        selected.sort(key=lambda x: x["odds"])
                        edge_item = selected[0]
                        replacement = next((x for x in reversed(remaining) if x["odds"] > edge_item["odds"]), None)
                    else:
                        selected.sort(key=lambda x: x["odds"], reverse=True)
                        edge_item = selected[0]
                        replacement = next((x for x in remaining if x["odds"] < edge_item["odds"]), None)
                    if replacement is None:
                        break
                    selected[0] = replacement
                    remaining.remove(replacement)
                    remaining.append(edge_item)

                amounts = _calc_amounts(budget, len(selected), strategy)
                avg_odds = _calc_weighted_avg_odds([x["odds"] for x in selected], amounts)
                if avg_odds is None:
                    continue
                synthetic_odds = _calc_synthetic_odds(avg_odds, len(selected))
                range_distance = _distance_to_range(avg_odds, target_low, target_high)
                center_distance = abs(avg_odds - target_center)
                synth_distance = (
                    _distance_to_range(synthetic_odds, synth_low, synth_high)
                    if synthetic_odds is not None else 0.0
                )
                quality_bonus = -sum(x["score"] for x in selected) / max(len(selected), 1) * 0.05
                diversity = _calc_diversity_score(selected, ticket_type)
                if strategy == "本命":
                    count_penalty = abs(bet_count - desired_count) * 0.45
                    diversity_penalty = (1.0 - diversity) * 0.8
                    range_weight = 20
                    center_weight = 2
                    synth_weight = 10
                elif strategy == "中穴":
                    count_penalty = abs(bet_count - desired_count) * 0.70
                    diversity_penalty = (1.0 - diversity) * 2.2
                    range_weight = 12
                    center_weight = 1.5
                    synth_weight = 12
                else:
                    count_penalty = abs(bet_count - desired_count) * 0.90
                    diversity_penalty = (1.0 - diversity) * 2.8
                    range_weight = 10
                    center_weight = 1.2
                    synth_weight = 10
                objective = (
                    range_distance * range_weight
                    + center_distance * center_weight
                    + synth_distance * synth_weight
                    + count_penalty
                    + diversity_penalty
                    + quality_bonus
                )
                global_packs.append(
                    {
                        "objective": objective,
                        "selected": selected[:],
                        "avg_odds": avg_odds,
                        "drift": range_distance,
                    }
                )

            if global_packs:
                global_best = min(global_packs, key=lambda p: p["objective"])
                if global_best["drift"] < chosen_drift:
                    chosen = global_best

        final_selected = chosen["selected"]

    final_selected.sort(key=lambda x: (-x["score"], x["rank"], x["odds"]))
    return [w["numbers"] for w in final_selected]


def _calc_amounts(budget, num_bets, strategy):
    unit = 100
    if strategy == "本命":
        weights = [4, 2, 1][:num_bets]
    elif strategy == "中穴":
        weights = [4, 2, 2, 1, 1][:num_bets]
    else:
        weights = [1] * num_bets
    while len(weights) < num_bets:
        weights.append(1)
    total_w = sum(weights)
    amounts = [max(unit, int((budget * w / total_w) / unit) * unit) for w in weights]
    diff = budget - sum(amounts)
    if diff > 0:
        amounts[0] += (diff // unit) * unit
    return amounts


def _calc_amounts_with_odds(
    budget: int,
    strategy: str,
    combo_odds: List[Optional[float]],
    target_range: Tuple[float, float],
) -> List[int]:
    """
    オッズ連動の金額配分。
    - 目標合成オッズに近づくように低オッズ側へ厚めに配分
    - 先頭1点への過集中を抑制（中穴/大穴は特に均し気味）
    """
    num_bets = len(combo_odds)
    if num_bets == 0:
        return []
    unit = 100

    # 大穴は低予算帯では均等100円配分を優先
    if strategy == "大穴" and budget <= 10000:
        amounts = [unit] * num_bets
        total = unit * num_bets
        if total < budget:
            # 端数は先頭から100円ずつ加算（通常は total==budget）
            i = 0
            while total < budget:
                amounts[i % num_bets] += unit
                total += unit
                i += 1
        elif total > budget:
            # 念のため過剰分を後ろから削る
            i = num_bets - 1
            while total > budget and i >= 0:
                if amounts[i] > unit:
                    amounts[i] -= unit
                    total -= unit
                i -= 1
        return amounts

    # 中穴の低〜中予算帯は広めに買いつつ、上位1〜2点を少し厚くする
    if strategy == "中穴" and budget <= 2000:
        amounts = [unit] * num_bets
        extra_units = max(0, (budget - unit * num_bets) // unit)
        if extra_units <= 0:
            return amounts

        ordered_idx = sorted(
            range(num_bets),
            key=lambda i: combo_odds[i] if combo_odds[i] is not None else 10**9
        )
        max_per_ticket = 300

        # 第1段: まず広く 200円化して、100/300 の二択化を避ける
        first_wave_slots = min(num_bets, extra_units)
        for i in range(first_wave_slots):
            amounts[ordered_idx[i]] += unit
            extra_units -= 1
            if extra_units <= 0:
                break

        # 第2段: 余剰のみ上位へ追加し 300円化
        if budget <= 1000:
            boost_slots = min(2, num_bets)
        elif budget <= 1500:
            boost_slots = min(3, num_bets)
        else:
            boost_slots = min(4, num_bets)

        slot_idx = 0
        while extra_units > 0 and boost_slots > 0:
            idx = ordered_idx[slot_idx % boost_slots]
            if amounts[idx] + unit <= max_per_ticket:
                amounts[idx] += unit
                extra_units -= 1
            slot_idx += 1
            if slot_idx > 2000:
                break

        return amounts

    # まずは従来配分を土台にする
    base = _calc_amounts(budget, num_bets, strategy)

    # オッズが取れない場合は従来配分
    if not any(o is not None for o in combo_odds):
        return base

    low, high = target_range
    target = (low + high) / 2.0

    # 低オッズほど重くなる係数（目標から遠い高オッズは軽くする）
    factors = []
    for i, od in enumerate(combo_odds):
        if od is None:
            factors.append((i, 1.0))
            continue
        # target を超えるほど係数を落とす（下限 0.35）
        ratio = target / max(od, 0.1)
        factor = max(0.35, min(2.2, ratio))
        factors.append((i, factor))

    weighted = [max(unit, int((base[i] * f) / unit) * unit) for i, f in factors]
    total = sum(weighted)
    if total <= 0:
        return base

    # 予算へ正規化
    scale = budget / total
    amounts = [max(unit, int((a * scale) / unit) * unit) for a in weighted]

    # 予算差分を低オッズ順に埋める
    diff = budget - sum(amounts)
    order = sorted(
        range(num_bets),
        key=lambda i: combo_odds[i] if combo_odds[i] is not None else 10**9
    )
    ptr = 0
    while diff >= unit and order:
        amounts[order[ptr % len(order)]] += unit
        diff -= unit
        ptr += 1

    # 一点集中抑制（本命は厚張り許容、中穴はやや分散、大穴は分散優先）
    if strategy == "本命":
        cap_ratio = 0.60
    elif strategy == "中穴":
        cap_ratio = 0.25
    else:
        cap_ratio = 0.40
    cap = max(unit, int((budget * cap_ratio) / unit) * unit)
    for _ in range(20):
        max_idx = max(range(num_bets), key=lambda i: amounts[i])
        if amounts[max_idx] <= cap:
            break
        excess = amounts[max_idx] - cap
        move = max(unit, (excess // unit) * unit)
        amounts[max_idx] -= move
        # 低オッズ側へ再配分
        for i in order:
            if i == max_idx:
                continue
            add = min(move, unit * 2)
            amounts[i] += add
            move -= add
            if move <= 0:
                break
        if move > 0:
            amounts[max_idx] += move
            break

    # 合計誤差の最終調整
    total = sum(amounts)
    if total < budget and order:
        i = 0
        while total < budget:
            amounts[order[i % len(order)]] += unit
            total += unit
            i += 1
    elif total > budget:
        desc = sorted(range(num_bets), key=lambda i: amounts[i], reverse=True)
        i = 0
        while total > budget and desc:
            idx = desc[i % len(desc)]
            if amounts[idx] > unit:
                amounts[idx] -= unit
                total -= unit
            i += 1
            if i > 500:
                break

    return amounts


def _make_reason(player_nums, ranked, strategy, idx):
    p_map = {p.car_number: p for p in ranked}
    main_num = player_nums[0]
    main = p_map.get(main_num)
    if main is None:
        return f"{main_num}号車を軸にした買い目"

    grade_label = {"SS": "SS級", "S1": "S1級", "S2": "S2級", "A1": "A1級", "A2": "A2級"}.get(main.grade, "")
    style_label = {"逃": "逃げ", "追": "追込み", "両": "自在型"}.get(main.style, main.style)
    line_label = f"・{main.line_role}" if main.line_role != "不明" else ""
    b_label = f"・B{main.back_count}" if main.back_count > 0 else ""

    if strategy == "本命":
        reasons = [
            f"{main_num}号車({main.name})は{grade_label}・{style_label}{line_label}{b_label}。競走得点{main.score:.1f}で安定感あり",
            f"3連対率{main.triple_rate:.1%}とB{main.back_count}を評価し、{main_num}号車({main.name})中心で構成",
            f"勝率{main.win_rate:.1%}の{main_num}号車({main.name})を軸に、並び適性も加味した1点",
        ]
    elif strategy == "中穴":
        reasons = [
            f"{main_num}号車({main.name})の{style_label}{line_label}とオッズ帯のバランスから中穴期待",
            f"ライン崩れ時の伸びとB{main.back_count}を加味し、{main_num}号車({main.name})を評価",
            f"{grade_label}の{main_num}号車({main.name})を、勝率/3連対率/戦法内訳の重みで選抜",
            f"前団争い激化で{main_num}号車({main.name})の差し込みに期待",
            f"展開次第で{main_num}号車({main.name})がチャンスを掴む",
        ]
    else:
        reasons = [
            f"番手選手競り合い時に{main_num}号車({main.name})が漁夫の利を得る可能性",
            f"大波乱なら{main_num}号車({main.name})も台頭。高配当を夢見る一点",
            f"{main_num}号車({main.name})の独走力に賭ける大穴狙い",
            f"ライン外れた際の{main_num}号車({main.name})の単騎逃げに期待",
            f"穴狙い。{main_num}号車({main.name})が荒れ展開で台頭するなら高配当",
        ]

    return reasons[idx % len(reasons)]


def _make_advice(ranked, strategy):
    if not ranked:
        return "出走表データが取得できませんでした。参考情報としてご活用ください。"
    top = ranked[0]
    second = ranked[1] if len(ranked) > 1 else None
    grade_label = {"SS": "SS級", "S1": "S1級", "S2": "S2級", "A1": "A1級", "A2": "A2級"}.get(top.grade, "")
    style_label = {"逃": "逃げ", "追": "追込み", "両": "自在型"}.get(top.style, top.style)

    if strategy == "本命":
        return (f"競走得点トップの{top.car_number}号車({top.name}/{grade_label}・{style_label})が軸。"
                f"3連対率{top.triple_rate:.1%}の安定感から堅い展開を期待。")
    elif strategy == "中穴":
        second_str = f"・{second.car_number}号車({second.name})" if second else ""
        return (f"{top.car_number}号車({top.name}){second_str}を中心に中穴を狙う。"
                f"ライン戦術の崩れが入れば10〜30倍の配当も。展開に注目。")
    else:
        return (f"大荒れ期待。{top.car_number}号車({top.name})中心だが、下位人気が台頭すれば高配当。"
                f"全車の動き出しと番手争いに注目。")


def generate_recommendation(race_info: RaceInfo, strategy: str, budget: int, ticket_type: str) -> RaceRecommendation:
    ranked = _rank_players(race_info.players, strategy)
    # まずはオッズ順位ベースで買い目を抽出し、不足分はスコアベースで補完する。
    combos = _select_combos_from_odds(race_info, strategy, ticket_type, budget)
    if not combos:
        target_max = min(
            STRATEGIES[strategy].get("max_bets", 12),
            max(1, budget // 100),
        )
        fallback = _generate_combinations(ranked, strategy, ticket_type, target_max)
        combos = fallback[:target_max]

    # 大穴×低予算帯は 100円刻みの広げ買いを最優先
    if strategy == "大穴" and budget <= 10000:
        combos = _rebalance_longshot_low_budget_combos(race_info, ticket_type, combos, budget)

    target_range = STRATEGIES[strategy].get("target_portfolio_odds", (3.0, 5.0))
    combo_odds = []
    for nums in combos:
        odds_key = tuple(nums) if ticket_type == "三連単" else tuple(sorted(nums))
        combo_odds.append(race_info.odds_map.get(ticket_type, {}).get(odds_key))
    amounts = _calc_amounts_with_odds(budget, strategy, combo_odds, target_range)

    bets = []
    for i, nums in enumerate(combos):
        stars = max(1, 3 - i) if strategy == "本命" else (3 if i == 0 else (2 if i <= 2 else 1))
        reason = _make_reason(nums, ranked, strategy, i)
        amount = amounts[i] if i < len(amounts) else amounts[-1]
        odds_key = tuple(nums) if ticket_type == "三連単" else tuple(sorted(nums))
        odds = race_info.odds_map.get(ticket_type, {}).get(odds_key)
        bets.append(BetRecommendation(numbers=nums, amount=amount, stars=stars, reason=reason, current_odds=odds))

    return RaceRecommendation(
        venue=race_info.venue,
        race_number=race_info.race_number,
        strategy=strategy,
        ticket_type=ticket_type,
        budget=budget,
        bets=bets,
        advice=_make_advice(ranked, strategy),
        total_amount=sum(b.amount for b in bets),
        is_mock=race_info.is_mock,
        source_url=race_info.source_url,
        odds_fetched_at=(
            race_info.odds_fetched_at.strftime("%Y-%m-%d %H:%M:%S")
            if race_info.odds_fetched_at else None
        ),
        ticket_odds_count=len(race_info.odds_map.get(ticket_type, {})),
        matched_odds_count=sum(1 for b in bets if b.current_odds is not None),
    )
