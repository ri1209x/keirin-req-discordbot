"""
競輪コマンド Cog（外部データ対応版）
"""
import asyncio
import logging
from datetime import date, datetime

import discord
from discord import app_commands
from discord.ext import commands

from scraper import VENUES, VENUE_MAP, fetch_race_card
from recommender import generate_recommendation, STRATEGIES, TICKET_TYPES
from google_ai_service import get_ai_advice
from formatter import build_recommendation_embed, build_help_embed, build_venues_embed

logger = logging.getLogger("keirin_bot.cog")


async def _safe_send_message(
    interaction: discord.Interaction,
    content: str = None,
    *,
    embed: discord.Embed = None,
    ephemeral: bool = False,
):
    try:
        if interaction.response.is_done():
            return await interaction.followup.send(content=content, embed=embed, ephemeral=ephemeral)
        return await interaction.response.send_message(content=content, embed=embed, ephemeral=ephemeral)
    except discord.NotFound:
        logger.warning("Interaction が無効になったためメッセージ送信できませんでした")
        return None


class KeirinCog(commands.Cog, name="競輪"):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="keirin", description="🚴 競輪の買い目をレコメンドします（出走表データ取得）")
    @app_commands.describe(
        venue="競輪場名（例: 川崎、松戸、立川）",
        race="レース番号（1〜12）",
        strategy="狙い方を選択してください",
        budget="投票予算（円単位・100円以上）",
        ticket_type="三連単（順番通り）または三連複（順不同）",
        race_date="開催日 YYYY-MM-DD形式（省略時: 本日）",
    )
    @app_commands.choices(
        strategy=[
            app_commands.Choice(name="🔵 本命狙い（3点・堅い・的中率重視）", value="本命"),
            app_commands.Choice(name="🟠 中穴狙い（5点・バランス型）",       value="中穴"),
            app_commands.Choice(name="🔴 大穴狙い（8点・高配当・夢狙い）",   value="大穴"),
        ],
        ticket_type=[
            app_commands.Choice(name="三連単（1〜3着を順番通り）", value="三連単"),
            app_commands.Choice(name="三連複（1〜3着を順不同）",   value="三連複"),
        ]
    )
    async def keirin(
        self,
        interaction: discord.Interaction,
        venue: str,
        race: int,
        strategy: str,
        budget: int,
        ticket_type: str,
        race_date: str = None,
    ):
        # ─── Discordタイムアウト回避のため即defer ───────────────────────────────
        try:
            await interaction.response.defer(thinking=True)
        except discord.NotFound:
            logger.warning("Interaction が無効になったため defer できませんでした")
            # defer失敗でも処理を続行（後でfollowup送信時に握り潰す）

        # ─── バリデーション ──────────────────────────────────────────────────
        errors = []

        # 競輪場チェック（部分一致）
        matched_venue = None
        for v in VENUES:
            if venue in v or v in venue:
                matched_venue = v
                break
        if not matched_venue:
            errors.append(f"❌ 競輪場「{venue}」が見つかりません。`/keirin_venues` で一覧を確認してください。")

        if not (1 <= race <= 12):
            errors.append("❌ レース番号は1〜12の範囲で入力してください。")

        if budget < 100:
            errors.append("❌ 投票予算は100円以上で入力してください。")

        # 開催日パース
        target_date = date.today()
        if race_date:
            try:
                target_date = datetime.strptime(race_date, "%Y-%m-%d").date()
            except ValueError:
                errors.append("❌ 日付は YYYY-MM-DD 形式で入力してください（例: 2026-02-28）")

        if errors:
            await _safe_send_message(interaction, "\n".join(errors), ephemeral=True)
            return

        try:
            # 1. 出走表スクレイプ（非同期で実行）
            race_info = await asyncio.wait_for(
                asyncio.to_thread(fetch_race_card, matched_venue, race, target_date),
                timeout=20,
            )

            # 2. 買い目生成
            rec = generate_recommendation(race_info, strategy, budget, ticket_type)

            # 3. AI アドバイス生成（非同期で実行）
            ai_advice = await asyncio.wait_for(
                asyncio.to_thread(get_ai_advice, rec, race_info),
                timeout=20,
            )

            # 4. Embed 構築・送信
            embed = build_recommendation_embed(rec, ai_advice)
            await _safe_send_message(interaction, embed=embed)

            data_type = "模擬データ" if race_info.is_mock else "実データ"
            logger.info(
                f"[keirin] user={interaction.user} venue={matched_venue} "
                f"race={race} strategy={strategy} budget={budget} "
                f"ticket={ticket_type} date={target_date} data={data_type}"
            )

        except Exception as e:
            logger.error(f"[keirin] エラー発生: {e}", exc_info=True)
            await _safe_send_message(
                interaction,
                f"⚠️ レコメンドの生成中にエラーが発生しました。\n理由: {e}",
                ephemeral=True,
            )

    @app_commands.command(name="keirin_venues", description="📍 対応している競輪場の一覧を表示します")
    async def keirin_venues(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=build_venues_embed(VENUES))

    @app_commands.command(name="keirin_help", description="📖 競輪Botの使い方を表示します")
    async def keirin_help(self, interaction: discord.Interaction):
        await interaction.response.send_message(embed=build_help_embed())


async def setup(bot: commands.Bot):
    await bot.add_cog(KeirinCog(bot))
