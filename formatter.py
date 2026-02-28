"""
Discord埋め込みメッセージフォーマッター（外部データ対応版）
"""
import discord
from recommender import RaceRecommendation, STRATEGIES, TICKET_TYPES

STRATEGY_COLORS = {"本命": 0x3498DB, "中穴": 0xE67E22, "大穴": 0xE74C3C}
STRATEGY_EMOJIS = {"本命": "🔵", "中穴": "🟠", "大穴": "🔴"}
STAR_EMOJIS = {3: "⭐⭐⭐", 2: "⭐⭐", 1: "⭐"}
EMBED_FIELD_LIMIT = 1024


def format_numbers(numbers: list, ticket_type: str) -> str:
    if ticket_type == "三連複":
        return "=".join(map(str, sorted(numbers)))
    return "→".join(map(str, numbers))


def build_recommendation_embed(rec: RaceRecommendation, ai_advice: str = None) -> discord.Embed:
    color = STRATEGY_COLORS.get(rec.strategy, 0x95A5A6)
    strategy_emoji = STRATEGY_EMOJIS.get(rec.strategy, "⚪")
    strategy_info = STRATEGIES[rec.strategy]

    # モックデータの場合は色をグレーに変更してわかりやすく
    if rec.is_mock:
        title = "🚴 競輪買い目レコメンド ⚠️ (模擬データ)"
    else:
        title = "🚴 競輪買い目レコメンド"

    embed = discord.Embed(title=title, color=color)

    # レース情報
    venue_line = rec.venue + "競輪"
    if rec.source_url:
        venue_line = f"[{rec.venue}競輪]({rec.source_url})"

    embed.add_field(
        name="📍 レース情報",
        value=(
            f"**競輪場:** {venue_line}\n"
            f"**レース:** 第{rec.race_number}R\n"
            f"**車券種別:** {rec.ticket_type}"
        ),
        inline=True
    )

    embed.add_field(
        name=f"{strategy_emoji} 戦略",
        value=(
            f"**{rec.strategy}狙い**\n"
            f"{strategy_info['description']}\n"
            f"期待配当: {strategy_info['expected_odds']}"
        ),
        inline=True
    )

    embed.add_field(
        name="💰 予算情報",
        value=(
            f"**投票予算:** {rec.budget:,}円\n"
            f"**総投票額:** {rec.total_amount:,}円\n"
            f"**買い目数:** {len(rec.bets)}点"
        ),
        inline=True
    )

    embed.add_field(name="─" * 20, value="", inline=False)

    # 買い目一覧
    bet_lines = []
    for i, bet in enumerate(rec.bets):
        stars = STAR_EMOJIS.get(bet.stars, "⭐")
        nums = format_numbers(bet.numbers, rec.ticket_type)
        odds_suffix = f"（現在オッズ: {bet.current_odds:.1f}倍）" if bet.current_odds is not None else ""
        bet_lines.append(f"`{i+1:2d}.` {stars} **{nums}**　{bet.amount:,}円 {odds_suffix}".rstrip())

    # Discord Embed field value は最大1024文字
    if bet_lines:
        visible_lines = []
        for line in bet_lines:
            trial = "\n".join(visible_lines + [line])
            if len(trial) > EMBED_FIELD_LIMIT - 32:
                break
            visible_lines.append(line)
        omitted = len(bet_lines) - len(visible_lines)
        if omitted > 0:
            summary = f"... 他 {omitted}点"
            trial = "\n".join(visible_lines + [summary])
            if len(trial) <= EMBED_FIELD_LIMIT:
                visible_lines.append(summary)
            elif visible_lines:
                visible_lines[-1] = summary
        bet_value = "\n".join(visible_lines) if visible_lines else "買い目なし"
    else:
        bet_value = "買い目なし"

    embed.add_field(
        name="🎯 推奨買い目",
        value=bet_value,
        inline=False
    )

    # 推奨理由（上位3点）
    if rec.bets:
        reason_lines = []
        for i, bet in enumerate(rec.bets[:3]):
            nums = format_numbers(bet.numbers, rec.ticket_type)
            reason_lines.append(f"**{nums}:** {bet.reason}")
        embed.add_field(
            name="📝 推奨理由（上位3点）",
            value="\n".join(reason_lines),
            inline=False
        )

    # AIアドバイス
    embed.add_field(
        name="💡 ワンポイントアドバイス",
        value=ai_advice or rec.advice,
        inline=False
    )

    # データソース表示
    if rec.is_mock:
        data_note = "⚠️ 本日の出走表が取得できなかったため模擬データを使用しています"
    else:
        data_note = f"✅ Kドリームス出走表データを使用"
    if rec.odds_fetched_at:
        data_note += f"\n🕒 オッズ取得時刻: {rec.odds_fetched_at}"
    else:
        data_note += "\n🕒 オッズ取得時刻: 取得できませんでした"
    data_note += (
        f"\n📈 {rec.ticket_type}オッズ取得件数: {rec.ticket_odds_count}件"
        f"\n✅ 推奨買い目との一致: {rec.matched_odds_count}/{len(rec.bets)}点"
    )
    embed.add_field(name="📊 データソース", value=data_note, inline=False)

    embed.set_footer(
        text="⚠️ このレコメンドは参考情報です。投票は自己責任で。公営競技は20歳以上が対象です。"
    )
    return embed


def build_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🚴 競輪Bot ヘルプ",
        description="Kドリームスの出走表データを取得して、買い目をレコメンドするBotです。",
        color=0x2ECC71
    )
    embed.add_field(
        name="📌 メインコマンド `/keirin`",
        value=(
            "**パラメータ:**\n"
            "• `venue` - 競輪場名（例: 川崎、松戸、立川）\n"
            "• `race` - レース番号（1〜12）\n"
            "• `strategy` - 狙い（本命/中穴/大穴）\n"
            "• `budget` - 投票予算（100円以上）\n"
            "• `ticket_type` - 三連単 or 三連複\n"
            "• `date` - 開催日（省略時: 本日）"
        ),
        inline=False
    )
    embed.add_field(
        name="📋 その他コマンド",
        value=(
            "`/keirin_venues` - 対応競輪場一覧\n"
            "`/keirin_help` - このヘルプを表示"
        ),
        inline=False
    )
    embed.add_field(
        name="🎯 戦略の違い",
        value=(
            "🔵 **本命** - 上位3選手中心。3点買い。的中率重視\n"
            "🟠 **中穴** - バランス型。10点買い。10〜30位人気帯狙い\n"
            "🔴 **大穴** - 高配当狙い。8点買い。荒れたレースに強い"
        ),
        inline=False
    )
    embed.add_field(
        name="⚙️ データ取得について",
        value=(
            "楽天Kドリームス（keirin.kdreams.jp）から当日の出走表を取得します。\n"
            " `/keirin` 実行時点のオッズを取得して表示します。\n"
            "レースが開催されていない場合や取得失敗時は模擬データを使用します。"
        ),
        inline=False
    )
    embed.set_footer(text="⚠️ 投票は20歳以上・自己責任でお願いします")
    return embed


def build_venues_embed(venues: list) -> discord.Embed:
    embed = discord.Embed(title="📍 対応競輪場一覧（44場）", color=0x9B59B6)
    chunk_size = len(venues) // 3 + 1
    chunks = [venues[i:i+chunk_size] for i in range(0, len(venues), chunk_size)]
    for i, chunk in enumerate(chunks):
        embed.add_field(name=f"競輪場 {i+1}", value="\n".join(chunk), inline=True)
    return embed
