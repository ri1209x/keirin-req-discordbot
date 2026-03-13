"""
楽天Kドリームス 出走表スクレイパー
keirin.kdreams.jp から当日の出走表・選手データを取得する

URL構造:
  日付一覧: https://keirin.kdreams.jp/racecard/{year}/{month}/{day}/
  レース詳細: https://keirin.kdreams.jp/{venue_slug}/racedetail/{race_id}/

レースID形式: {場コード}{年月日}{節何日目か}{レース番号}
例: 1220260222010001 → 松戸 2026/02/22 1日目 1R
"""
import re
import time
import random
import hashlib
import logging
from datetime import date, datetime
from zoneinfo import ZoneInfo
from dataclasses import dataclass, field
from typing import List, Optional, Dict, Tuple

import os

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("keirin_bot.scraper")
JST = ZoneInfo("Asia/Tokyo")

PREFECTURES = [
    "北海道",
    "青森", "岩手", "宮城", "秋田", "山形", "福島",
    "茨城", "栃木", "群馬", "埼玉", "千葉", "東京", "神奈川",
    "新潟", "富山", "石川", "福井", "山梨", "長野",
    "岐阜", "静岡", "愛知", "三重",
    "滋賀", "京都", "大阪", "兵庫", "奈良", "和歌山",
    "鳥取", "島根", "岡山", "広島", "山口",
    "徳島", "香川", "愛媛", "高知",
    "福岡", "佐賀", "長崎", "熊本", "大分", "宮崎", "鹿児島",
    "沖縄",
]

# ─── 競輪場コード・スラグ対応表 ──────────────────────────────────────────────
VENUE_MAP: Dict[str, dict] = {
    "函館":  {"code": "01", "slug": "hakodate"},
    "青森":  {"code": "02", "slug": "aomori"},
    "いわき平": {"code": "03", "slug": "iwakitaira"},
    "弥彦":  {"code": "04", "slug": "yahiko"},
    "前橋":  {"code": "05", "slug": "maebashi"},
    "取手":  {"code": "06", "slug": "toride"},
    "宇都宮": {"code": "07", "slug": "utsunomiya"},
    "大宮":  {"code": "08", "slug": "omiya"},
    "西武園": {"code": "09", "slug": "seibuen"},
    "京王閣": {"code": "10", "slug": "keiokaku"},
    "立川":  {"code": "11", "slug": "tachikawa"},
    "松戸":  {"code": "12", "slug": "matsudo"},
    "千葉":  {"code": "13", "slug": "chiba"},
    "川崎":  {"code": "14", "slug": "kawasaki"},
    "平塚":  {"code": "15", "slug": "hiratsuka"},
    "小田原": {"code": "16", "slug": "odawara"},
    "伊東":  {"code": "17", "slug": "ito"},
    "静岡":  {"code": "18", "slug": "shizuoka"},
    "名古屋": {"code": "19", "slug": "nagoya"},
    "岐阜":  {"code": "20", "slug": "gifu"},
    "大垣":  {"code": "21", "slug": "ogaki"},
    "豊橋":  {"code": "22", "slug": "toyohashi"},
    "富山":  {"code": "23", "slug": "toyama"},
    "松阪":  {"code": "24", "slug": "matsusaka"},
    "四日市": {"code": "25", "slug": "yokkaichi"},
    "福井":  {"code": "26", "slug": "fukui"},
    "奈良":  {"code": "27", "slug": "nara"},
    "向日町": {"code": "28", "slug": "mukomachi"},
    "和歌山": {"code": "29", "slug": "wakayama"},
    "岸和田": {"code": "30", "slug": "kishiwada"},
    "玉野":  {"code": "31", "slug": "tamano"},
    "広島":  {"code": "32", "slug": "hiroshima"},
    "防府":  {"code": "33", "slug": "hofu"},
    "高松":  {"code": "34", "slug": "takamatsu"},
    "小松島": {"code": "35", "slug": "komatsushima"},
    "高知":  {"code": "36", "slug": "kochi"},
    "松山":  {"code": "37", "slug": "matsuyama"},
    "小倉":  {"code": "38", "slug": "kokura"},
    "久留米": {"code": "39", "slug": "kurume"},
    "武雄":  {"code": "40", "slug": "takeo"},
    "佐世保": {"code": "41", "slug": "sasebo"},
    "熊本":  {"code": "42", "slug": "kumamoto"},
    "別府":  {"code": "43", "slug": "beppu"},
    "佐伯":  {"code": "44", "slug": "saiki"},
}

VENUES = list(VENUE_MAP.keys())


@dataclass
class Player:
    """出走選手データ"""
    car_number: int          # 車番
    name: str                # 選手名
    prefecture: str          # 登録府県
    age: int                 # 年齢
    grade: str               # 級班（SS/S1/S2/A1/A2）
    style: str               # 脚質（逃/両/追）
    score: float             # 競走得点
    win_rate: float          # 勝率
    double_rate: float       # 2連対率
    triple_rate: float       # 3連対率
    recent_results: str      # 直近成績（例: "1-3-2-1-5"）
    gear: str                # ギア倍数
    back_count: int = 0      # B本数
    escape_count: int = 0    # 逃げ回数
    makuri_count: int = 0    # 捲り回数
    sashi_count: int = 0     # 差し回数
    mark_count: int = 0      # マーク回数
    line_role: str = "不明"   # 先頭/番手/短期/不明


@dataclass
class RaceInfo:
    """レース情報"""
    venue: str
    race_number: int
    race_date: date
    race_id: str
    players: List[Player] = field(default_factory=list)
    source_url: str = ""
    is_mock: bool = False    # スクレイプ失敗時のモックデータフラグ
    odds_map: Dict[str, Dict[Tuple[int, int, int], float]] = field(default_factory=dict)
    odds_fetched_at: Optional[datetime] = None


# ─── スクレイパー ─────────────────────────────────────────────────────────────

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/121.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "ja-JP,ja;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


def _safe_float(val, default=0.0) -> float:
    try:
        return float(str(val).replace("%", "").replace("－", "0").strip())
    except (ValueError, TypeError):
        return default


def _safe_int(val, default=0) -> int:
    try:
        return int(str(val).strip())
    except (ValueError, TypeError):
        return default


def _normalize_ticket_type(text: str) -> Optional[str]:
    compact = re.sub(r"\s+", "", text)
    if "三連単" in compact or "3連単" in compact:
        return "三連単"
    if "三連複" in compact or "3連複" in compact:
        return "三連複"
    return None


def _extract_combo_from_text(text: str) -> Optional[Tuple[int, int, int]]:
    m = re.search(r"([1-9])\s*(?:[-=→＞>])\s*([1-9])\s*(?:[-=→＞>])\s*([1-9])", text)
    if not m:
        return None
    return int(m.group(1)), int(m.group(2)), int(m.group(3))


def _extract_odds_value(text: str) -> Optional[float]:
    # 例: "12.5倍", "1,240.0倍", "999倍"
    m = re.search(r"(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*倍", text)
    if not m:
        return None
    return _safe_float(m.group(1).replace(",", ""), default=None)


def _extract_odds_from_odds_contents(
    soup: BeautifulSoup, ticket_type: str
) -> Dict[Tuple[int, int, int], float]:
    """
    オッズ領域（JS_ODDSCONTENTS_xxx）の「人気順/高配当順」テキストから抽出。
    Kドリームスでは「7-5-1 12.4」「1=5=7 5.1」のような形式で並ぶ。
    """
    block_id = "JS_ODDSCONTENTS_3rentan" if ticket_type == "三連単" else "JS_ODDSCONTENTS_3renhuku"
    block = soup.find(id=block_id)
    if block is None:
        return {}

    text = block.get_text(" ", strip=True)
    result: Dict[Tuple[int, int, int], float] = {}

    if ticket_type == "三連単":
        pattern = re.compile(r"([1-9])\s*[-→＞>]\s*([1-9])\s*[-→＞>]\s*([1-9])\s+(\d{1,4}(?:\.\d+)?)")
    else:
        pattern = re.compile(r"([1-9])\s*[=＝]\s*([1-9])\s*[=＝]\s*([1-9])\s+(\d{1,4}(?:\.\d+)?)")

    for m in pattern.finditer(text):
        combo = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
        odds = _safe_float(m.group(4), default=None)
        if odds is None:
            continue

        key = combo if ticket_type == "三連単" else tuple(sorted(combo))
        # 同一組番が複数回出る（人気順/高配当順）ため、低い方を採用。
        if key not in result or odds < result[key]:
            result[key] = odds

    return result


def _extract_odds_map(soup: BeautifulSoup) -> Dict[str, Dict[Tuple[int, int, int], float]]:
    """
    三連単/三連複オッズを抽出して返す。
    戻り値:
      {
        "三連単": {(1, 2, 3): 12.5, ...},
        "三連複": {(1, 2, 3): 5.2, ...},
      }
    """
    odds_map: Dict[str, Dict[Tuple[int, int, int], float]] = {"三連単": {}, "三連複": {}}

    # まず専用オッズ領域から抽出（最優先）。
    for ticket_type in ("三連単", "三連複"):
        odds_map[ticket_type].update(_extract_odds_from_odds_contents(soup, ticket_type))

    # テーブル主体で抽出。票種は table 前後テキストまたは table 内ヘッダから推定。
    for table in soup.find_all("table"):
        table_text = table.get_text(" ", strip=True)
        ticket_type = _normalize_ticket_type(table_text)
        if ticket_type is None:
            # 直前の見出しテキストも見る
            prev_text = ""
            prev = table.find_previous(["h1", "h2", "h3", "h4", "h5", "p", "div"])
            if prev is not None:
                prev_text = prev.get_text(" ", strip=True)
            ticket_type = _normalize_ticket_type(prev_text)
        if ticket_type is None:
            continue

        for row in table.find_all("tr"):
            row_text = row.get_text(" ", strip=True)
            combo = _extract_combo_from_text(row_text)
            odds = _extract_odds_value(row_text)
            if combo is None or odds is None:
                continue

            key = combo if ticket_type == "三連単" else tuple(sorted(combo))
            odds_map[ticket_type][key] = odds

    # script埋め込みのテキストにも一部オッズが含まれる場合があるため補完抽出
    for script in soup.find_all("script"):
        text = script.string or script.get_text(" ", strip=True)
        if not text:
            continue
        if "odds" not in text.lower() and "オッズ" not in text:
            continue
        ticket_type = _normalize_ticket_type(text)
        if ticket_type is None:
            continue

        for m in re.finditer(
            r"([1-9])\s*(?:[-=→＞>])\s*([1-9])\s*(?:[-=→＞>])\s*([1-9]).{0,30}?(\d{1,3}(?:,\d{3})*(?:\.\d+)?)\s*倍",
            text,
        ):
            combo = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
            odds = _safe_float(m.group(4).replace(",", ""), default=None)
            if odds is None:
                continue
            key = combo if ticket_type == "三連単" else tuple(sorted(combo))
            odds_map[ticket_type][key] = odds

    return {k: v for k, v in odds_map.items() if v}


def _infer_line_role_from_comment(comment: str) -> str:
    c = (comment or "").strip()
    if not c:
        return "不明"
    if "単騎" in c:
        return "短期"
    if "自力" in c or "前で" in c:
        return "先頭"
    if "君" in c or "さん" in c:
        return "番手"
    return "不明"


def _parse_line_roles(soup: BeautifulSoup) -> Dict[int, str]:
    """
    選手コメント欄から先頭/番手/短期を推定して返す。
    {car_number: role}
    """
    roles: Dict[int, str] = {}
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header_text = rows[0].get_text(" ", strip=True)
        if "選手コメント" not in header_text:
            continue

        for row in rows[1:]:
            texts = [c.get_text(" ", strip=True) for c in row.find_all(["td", "th"])]
            if len(texts) < 10:
                continue

            # 形式A: [..., 枠番, 車番, 選手名, ...]（len>=15）
            # 形式B: [..., 枠番, 車番, 選手名, ...]（末尾側の空セルが省略され len=14）
            car_number = None
            candidates = []
            if len(texts) > 4:
                candidates.append(texts[4])
            if len(texts) > 3:
                candidates.append(texts[3])
            for c in candidates:
                n = _safe_int(c, default=-1)
                if 1 <= n <= 9:
                    car_number = n
                    break
            if car_number is None:
                continue

            comment = next((t for t in texts if "。" in t and len(t) <= 20), "")
            role = _infer_line_role_from_comment(comment)
            roles[car_number] = role
        break

    return roles


def fetch_race_list_for_date(target_date: date) -> List[dict]:
    """
    指定日の全レース一覧を取得
    Returns: [{"venue": "川崎", "race_ids": ["1420260228010001", ...], "url": ...}, ...]
    """
    url = f"https://keirin.kdreams.jp/racecard/{target_date.year}/{target_date.month:02d}/{target_date.day:02d}/"
    try:
        res = requests.get(url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.content, "html.parser")

        race_list = []
        # レースカードの各開催競輪場を探索
        for a_tag in soup.find_all("a", href=True):
            href = a_tag.get("href", "")
            if "racedetail" in href:
                race_id_match = re.search(r"/racedetail/(\d+)/", href)
                if race_id_match:
                    race_id = race_id_match.group(1)
                    full_url = href if href.startswith("http") else "https://keirin.kdreams.jp" + href
                    race_list.append({"race_id": race_id, "url": full_url})

        return race_list
    except Exception as e:
        logger.warning(f"レース一覧取得失敗: {e}")
        return []


def fetch_race_card(venue: str, race_number: int, target_date: Optional[date] = None) -> RaceInfo:
    """
    出走表をスクレイプして RaceInfo を返す
    取得失敗時はモックデータを返す（Botが止まらないように）
    """
    if target_date is None:
        target_date = datetime.now(JST).date()

    venue_info = VENUE_MAP.get(venue)
    if not venue_info:
        logger.warning(f"不明な競輪場: {venue}")
        if _allow_mock():
            return _make_mock_race(venue, race_number, target_date)
        raise RuntimeError(f"不明な競輪場: {venue}")

    slug = venue_info["slug"]

    # まず当日のレース一覧から対象レースIDを探す
    race_url = _find_race_url(slug, race_number, target_date)
    if not race_url:
        logger.warning(f"{venue} {race_number}R のURLが見つかりません → モックデータ使用")
        if _allow_mock():
            return _make_mock_race(venue, race_number, target_date)
        raise RuntimeError(f"{venue} {race_number}R の出走表URLが見つかりません（開催なし/サイト構造変更の可能性）")

    return _scrape_race_card(venue, race_number, target_date, race_url)


def _find_race_url(slug: str, race_number: int, target_date: date) -> Optional[str]:
    """当日の開催一覧から対象競輪場・レース番号のURLを探す"""
    list_url = f"https://keirin.kdreams.jp/racecard/{target_date.year}/{target_date.month:02d}/{target_date.day:02d}/"

    try:
        res = requests.get(list_url, headers=HEADERS, timeout=10)
        res.raise_for_status()
        soup = BeautifulSoup(res.content, "html.parser")

        # 各レースリンクを走査
        for a_tag in soup.find_all("a", href=True):
            href = a_tag.get("href", "")
            if slug in href and "racedetail" in href:
                # URLからレース番号を確認
                race_num_match = re.search(r"(\d{2})$", href.rstrip("/").split("/")[-1])
                if race_num_match:
                    r_num = int(race_num_match.group(1))
                    if r_num == race_number:
                        return href if href.startswith("http") else "https://keirin.kdreams.jp" + href

        logger.debug(f"slug={slug} race={race_number} が一覧に見当たらない")
        return None

    except Exception as e:
        logger.warning(f"レース一覧取得エラー: {e}")
        return None


def _scrape_race_card(venue: str, race_number: int, target_date: date, url: str) -> RaceInfo:
    """出走表ページをスクレイプして選手データを返す"""
    try:
        time.sleep(1.5)  # サーバー負荷軽減
        res = requests.get(url, headers=HEADERS, timeout=15)
        res.raise_for_status()
        soup = BeautifulSoup(res.content, "html.parser")

        players = _parse_players(soup)
        odds_map = _extract_odds_map(soup)
        odds_fetched_at = datetime.now(JST)
        if not players:
            logger.warning(f"選手データ取得失敗 ({url}) → モックデータ使用")
            if _allow_mock():
                return _make_mock_race(venue, race_number, target_date)
            raise RuntimeError(f"選手データ取得失敗: {venue} {race_number}R ({target_date})")

        race_id_match = re.search(r"/racedetail/(\d+)/", url)
        race_id = race_id_match.group(1) if race_id_match else ""

        return RaceInfo(
            venue=venue,
            race_number=race_number,
            race_date=target_date,
            race_id=race_id,
            players=players,
            source_url=url,
            is_mock=False,
            odds_map=odds_map,
            odds_fetched_at=odds_fetched_at,
        )

    except Exception as e:
        logger.error(f"出走表スクレイプ失敗: {e} ({url})")
        if _allow_mock():
            return _make_mock_race(venue, race_number, target_date)
        raise RuntimeError(f"出走表スクレイプ失敗: {venue} {race_number}R ({target_date})") from e


def _allow_mock() -> bool:
    """環境変数でモックフォールバックを制御する"""
    return os.getenv("KEIRIN_ALLOW_MOCK", "1") != "0"


def _parse_players(soup: BeautifulSoup) -> List[Player]:
    """BeautifulSoup から選手情報をパース"""
    players = []
    line_roles = _parse_line_roles(soup)

    # Kドリームスの出走表テーブルを探す
    table = soup.find("table", class_=re.compile(r"race.*card|racecard|entry", re.IGNORECASE))
    if table is None:
        # テーブルを全探索
        tables = soup.find_all("table")
        for t in tables:
            rows = t.find_all("tr")
            if len(rows) >= 4:
                # 車番1〜9が含まれるテーブルを特定
                text = t.get_text()
                if any(str(i) in text for i in range(1, 10)):
                    table = t
                    break

    if table is None:
        return []

    rows = table.find_all("tr")
    for row in rows:
        cols = row.find_all(["td", "th"])
        if len(cols) < 5:
            continue

        texts = [c.get_text(strip=True) for c in cols]

        # racecard_table は列数が 22/23 など揺れるため、ヒューリスティックに抽出する
        # 目標: 車番1-9 + "...府県/年齢/期別" を含む行を Player に変換
        if len(texts) >= 15 and any("/" in t and re.search(r"/\d+/\d+", t) for t in texts):
            # 車番（1-9）を行内から探す（枠番が先に来るので、後ろ側優先）
            car_num = None
            for t in texts[:8][::-1]:
                n = _safe_int(t, default=-1)
                if 1 <= n <= 9:
                    car_num = n
                    break
            if car_num is None:
                continue

            # 選手名+府県/年齢/期別
            name_pref_age = next((t for t in texts if "/" in t and re.search(r"/\d+/\d+", t)), "")
            before = name_pref_age.split("/")[0].strip()
            compact = re.sub(r"[\s　]+", "", before)

            prefecture = ""
            name = before
            for pref in sorted(PREFECTURES, key=len, reverse=True):
                if compact.endswith(pref):
                    prefecture = pref
                    name_compact = compact[: -len(pref)]
                    name = name_compact
                    break

            age = 25
            m_age = re.search(r"/(\d+)/(\d+)", name_pref_age)
            if m_age:
                age = _safe_int(m_age.group(1), default=25)

            grade = next((t for t in texts if re.fullmatch(r"SS|S1|S2|A1|A2", t)), "A1")
            style = next((t for t in texts if t in ("逃", "両", "追")), "両")
            gear = next((t for t in texts if re.fullmatch(r"\d\.\d{2}", t)), "3.50")
            score = next((_safe_float(t, default=None) for t in texts if re.fullmatch(r"\d{2,3}\.\d{1,2}", t)), None)
            score = 70.0 if score is None else float(score)

            # レートは末尾に並ぶことが多い（勝率/2連対率/3連対率）
            win_rate = _safe_float(texts[-3], default=0.0) if len(texts) >= 3 else 0.0
            d_rate = _safe_float(texts[-2], default=0.0) if len(texts) >= 2 else 0.0
            t_rate = _safe_float(texts[-1], default=0.0) if len(texts) >= 1 else 0.0
            s_count = _safe_int(texts[10], default=0) if len(texts) > 10 else 0
            b_count = _safe_int(texts[11], default=0) if len(texts) > 11 else 0
            nige_count = _safe_int(texts[12], default=0) if len(texts) > 12 else 0
            makuri_count = _safe_int(texts[13], default=0) if len(texts) > 13 else 0
            sashi_count = _safe_int(texts[14], default=0) if len(texts) > 14 else 0
            mark_count = _safe_int(texts[15], default=0) if len(texts) > 15 else 0

            players.append(Player(
                car_number=car_num,
                name=name or "不明",
                prefecture=prefecture,
                age=age,
                grade=grade,
                style=style,
                score=score,
                win_rate=win_rate / 100 if win_rate > 1 else win_rate,
                double_rate=d_rate / 100 if d_rate > 1 else d_rate,
                triple_rate=t_rate / 100 if t_rate > 1 else t_rate,
                recent_results="",
                gear=gear,
                back_count=b_count if b_count >= 0 else max(0, s_count),
                escape_count=nige_count,
                makuri_count=makuri_count,
                sashi_count=sashi_count,
                mark_count=mark_count,
                line_role=line_roles.get(car_num, "不明"),
            ))
            continue

        # 車番が1〜9の行のみ処理
        try:
            car_num = int(texts[0])
            if not (1 <= car_num <= 9):
                continue
        except ValueError:
            continue

        try:
            # カラム位置はサイト構造に依存するため柔軟に対応
            name = texts[1] if len(texts) > 1 else "不明"
            prefecture = texts[2] if len(texts) > 2 else ""
            age_grade = texts[3] if len(texts) > 3 else "0"
            style = texts[4] if len(texts) > 4 else "両"
            score = _safe_float(texts[5]) if len(texts) > 5 else 70.0
            win_rate = _safe_float(texts[6]) if len(texts) > 6 else 0.0
            d_rate = _safe_float(texts[7]) if len(texts) > 7 else 0.0
            t_rate = _safe_float(texts[8]) if len(texts) > 8 else 0.0
            recent = texts[9] if len(texts) > 9 else ""
            gear = texts[10] if len(texts) > 10 else "3.50"

            # 年齢・級班を分離（例: "32 S1" や "32" など）
            age_match = re.search(r"(\d+)", age_grade)
            age = int(age_match.group(1)) if age_match else 25
            grade_match = re.search(r"(SS|S1|S2|A1|A2)", age_grade)
            grade = grade_match.group(1) if grade_match else "A1"

            players.append(Player(
                car_number=car_num,
                name=name,
                prefecture=prefecture,
                age=age,
                grade=grade,
                style=style,
                score=score,
                win_rate=win_rate,
                double_rate=d_rate,
                triple_rate=t_rate,
                recent_results=recent,
                gear=gear,
                line_role=line_roles.get(car_num, "不明"),
            ))
        except Exception as e:
            logger.debug(f"選手行パースエラー: {e} - {texts}")
            continue

    return players


# ─── モックデータ生成（スクレイプ失敗時のフォールバック） ──────────────────────

_MOCK_NAMES = [
    "田中一郎", "鈴木二郎", "佐藤三郎", "高橋四郎", "伊藤五郎",
    "渡辺六郎", "山本七郎", "中村八郎", "小林九郎",
]

_MOCK_GRADES = ["S1", "S1", "S2", "A1", "A1", "A1", "A1", "A2", "A2"]
_MOCK_STYLES = ["逃", "両", "追", "逃", "両", "追", "逃", "両", "追"]
_MOCK_PREFS = ["東京", "大阪", "神奈川", "埼玉", "千葉", "愛知", "福岡", "北海", "兵庫"]


def _make_mock_race(venue: str, race_number: int, race_date: date) -> RaceInfo:
    """APIが取得できない場合の疑似データ（再現性あり）"""
    seed_str = f"{venue}-{race_number}-{race_date}"
    rng = random.Random(int(hashlib.md5(seed_str.encode()).hexdigest(), 16) % (2**32))

    # 競走得点を人気順にシャッフル
    scores = sorted([rng.uniform(70, 115) for _ in range(9)], reverse=True)
    order = list(range(9))
    rng.shuffle(order)

    players = []
    for i, idx in enumerate(order):
        players.append(Player(
            car_number=i + 1,
            name=_MOCK_NAMES[i],
            prefecture=_MOCK_PREFS[i],
            age=rng.randint(22, 45),
            grade=_MOCK_GRADES[idx],
            style=_MOCK_STYLES[idx],
            score=round(scores[idx], 2),
            win_rate=round(rng.uniform(0.05, 0.35), 3),
            double_rate=round(rng.uniform(0.15, 0.65), 3),
            triple_rate=round(rng.uniform(0.30, 0.80), 3),
            recent_results="-".join(str(rng.randint(1, 9)) for _ in range(5)),
            gear=f"{rng.choice([3, 3, 3, 4])}.{rng.choice([25, 50, 75])}",
        ))

    return RaceInfo(
        venue=venue,
        race_number=race_number,
        race_date=race_date,
        race_id="",
        players=players,
        source_url="",
        is_mock=True,
        odds_map={},
        odds_fetched_at=None,
    )
