"""
Claude AI連携サービス
実際の出走表データを元に、AIが詳細なワンポイントアドバイスを生成する
"""
import os
import logging
from typing import Optional

from recommender import RaceRecommendation
from scraper import RaceInfo

logger = logging.getLogger("keirin_bot.claude")


def _build_race_context(race_info: RaceInfo) -> str:
    if not race_info.players:
        return "（出走表データなし）"
    lines = ["【出走選手データ】"]
    for p in sorted(race_info.players, key=lambda x: x.car_number):
        lines.append(
            f"{p.car_number}号車 {p.name}({p.prefecture}/{p.age}歳/{p.grade}) "
            f"脚質:{p.style} 得点:{p.score} 勝率:{p.win_rate:.1%} "
            f"3連対率:{p.triple_rate:.1%} 直近:{p.recent_results}"
        )
    return "\n".join(lines)


def _build_prompt(rec: RaceRecommendation, race_info: Optional[RaceInfo]) -> str:
    bets_text = "\n".join([
        f"  {i+1}点目: {'→'.join(map(str, b.numbers))} {b.amount:,}円"
        for i, b in enumerate(rec.bets)
    ])
    race_context = _build_race_context(race_info) if race_info else "（出走表データなし）"
    mock_note = "※このデータは模擬データです。" if rec.is_mock else ""

    return f"""あなたは競輪の専門解説者です。以下の出走表データと買い目レコメンドを踏まえ、
簡潔で的確なワンポイントアドバイスを日本語で2〜3文で生成してください。
選手の脚質・得点・直近成績を活用し、専門的かつ初心者にも伝わる表現にしてください。
{mock_note}

競輪場: {rec.venue}競輪 第{rec.race_number}R
戦略: {rec.strategy}狙い / 車券: {rec.ticket_type} / 予算: {rec.budget:,}円

{race_context}

推奨買い目:
{bets_text}

アドバイスは「💡」で始めてください。免責事項は含めないでください。"""


def get_ai_advice(rec: RaceRecommendation, race_info: Optional[RaceInfo] = None) -> str:
    api_key = os.getenv("ANTHROPIC_API_KEY")
    if not api_key:
        return rec.advice
    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=300,
            messages=[{"role": "user", "content": _build_prompt(rec, race_info)}]
        )
        return message.content[0].text
    except ImportError:
        logger.warning("Claude API を使うには anthropic パッケージのインストールが必要です")
        return rec.advice
    except Exception as e:
        logger.warning(f"Claude API エラー: {e}")
        return rec.advice
