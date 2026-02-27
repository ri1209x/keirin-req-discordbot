"""
競輪買い目レコメンドサービス
実際の出走表データ（スクレイプ or モック）＋戦略ロジックで買い目を生成
"""
from dataclasses import dataclass
from typing import List

from scraper import RaceInfo, Player, VENUES

# ─── 定数 ────────────────────────────────────────────────────────────────────

STRATEGIES = {
    "本命": {
        "description": "1〜3番人気の選手を中心に堅い組み合わせ",
        "num_bets": 3,
        "top_n": 3,
        "confidence": "高",
        "expected_odds": "2〜5倍",
    },
    "中穴": {
        "description": "2〜5番人気を絡めたバランス型",
        "num_bets": 5,
        "top_n": 5,
        "confidence": "中",
        "expected_odds": "10〜30倍",
    },
    "大穴": {
        "description": "高配当狙い。下位人気選手を積極的に絡める",
        "num_bets": 8,
        "top_n": 9,
        "confidence": "低",
        "expected_odds": "50〜500倍",
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


def _make_reason(player_nums, ranked, strategy, idx):
    p_map = {p.car_number: p for p in ranked}
    main_num = player_nums[0]
    main = p_map.get(main_num)
    if main is None:
        return f"{main_num}号車を軸にした買い目"

    grade_label = {"SS": "SS級", "S1": "S1級", "S2": "S2級", "A1": "A1級", "A2": "A2級"}.get(main.grade, "")
    style_label = {"逃": "逃げ", "追": "追込み", "両": "自在型"}.get(main.style, main.style)

    if strategy == "本命":
        reasons = [
            f"{main_num}号車({main.name})は{grade_label}・{style_label}。競走得点{main.score:.1f}で安定感あり",
            f"3連対率{main.triple_rate:.1%}の{main_num}号車({main.name})が中心の堅い組み合わせ",
            f"勝率{main.win_rate:.1%}の{main_num}号車({main.name})を軸に押さえの1点",
        ]
    elif strategy == "中穴":
        reasons = [
            f"{main_num}号車({main.name})の{style_label}が炸裂すると中穴配当が期待できる",
            f"ライン崩れが起きた際に{main_num}号車({main.name})が台頭する可能性",
            f"{grade_label}の{main_num}号車({main.name})がコース適性を発揮できれば好走",
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
    num_bets = STRATEGIES[strategy]["num_bets"]
    combos = _generate_combinations(ranked, strategy, ticket_type, num_bets)
    amounts = _calc_amounts(budget, len(combos), strategy)

    bets = []
    for i, nums in enumerate(combos):
        stars = max(1, 3 - i) if strategy == "本命" else (3 if i == 0 else (2 if i <= 2 else 1))
        reason = _make_reason(nums, ranked, strategy, i)
        amount = amounts[i] if i < len(amounts) else amounts[-1]
        bets.append(BetRecommendation(numbers=nums, amount=amount, stars=stars, reason=reason))

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
    )
