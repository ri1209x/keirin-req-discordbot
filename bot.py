"""
競輪買い目レコメンド Discord Bot
メインエントリーポイント
"""
import discord
from discord.ext import commands
import logging
import os
from dotenv import load_dotenv

load_dotenv()

# ロギング設定
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger('keirin_bot')

# Bot設定
intents = discord.Intents.default()
# スラッシュコマンド運用では message content intent は不要。
# これを True にすると Developer Portal 側での「Message Content Intent」有効化が必要。
intents.message_content = os.getenv("DISCORD_ENABLE_MESSAGE_CONTENT", "0") == "1"

bot = commands.Bot(command_prefix='!', intents=intents)


@bot.event
async def on_ready():
    logger.info(f'✅ Bot起動完了: {bot.user} (ID: {bot.user.id})')
    try:
        synced = await bot.tree.sync()
        logger.info(f'📡 {len(synced)}個のスラッシュコマンドを同期しました')
    except Exception as e:
        logger.error(f'コマンド同期エラー: {e}')

    await bot.change_presence(
        activity=discord.Activity(
            type=discord.ActivityType.watching,
            name="🚴 競輪レコメンド稼働中"
        )
    )


async def load_extensions():
    await bot.load_extension('keirin')
    logger.info('✅ Cogのロード完了')


async def main():
    token = os.getenv('DISCORD_TOKEN')
    if not token:
        raise ValueError("DISCORD_TOKEN が .env に設定されていません")

    async with bot:
        await load_extensions()
        await bot.start(token)


if __name__ == '__main__':
    import asyncio
    asyncio.run(main())
