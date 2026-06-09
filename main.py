import argparse
import csv
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from io import BytesIO
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup
from PIL import Image, ImageDraw, ImageFont
from slack_sdk import WebClient


# =========================
# 基本設定
# =========================

SITE_NAME = "andST"
BASE_URL = "https://www.dot-st.com"
RANKING_URL = os.environ.get("ANDST_RANKING_URL", "https://www.dot-st.com/disp/ranking/").strip()

DATA_FILE = "previous.json"
CSV_FILE = "data.csv"
REPORT_FILE = "report.png"

DAILY_TOP_N = 10

SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")
SLACK_CHANNEL_IDS = [
    channel_id.strip()
    for channel_id in os.environ.get(
        "SLACK_CHANNEL_IDS",
        os.environ.get("SLACK_CHANNEL_ID", "")
    ).split(",")
    if channel_id.strip()
]

GOOGLE_SHEET_ID = os.environ.get("GOOGLE_SHEET_ID", "").strip()
GOOGLE_SERVICE_ACCOUNT_JSON = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()


CATEGORY_KEYWORDS = {
    "ワンピース": ["ワンピ", "ジャンスカ", "ドレス"],
    "トップス": ["トップス", "ブラウス", "シャツ", "Tシャツ", "TEE", "カットソー", "ニット", "カーデ", "ベスト", "プルオーバー"],
    "ボトムス": ["パンツ", "デニム", "スカート", "ショートパンツ"],
    "アウター": ["ジャケット", "コート", "ブルゾン", "パーカー", "ジレ"],
    "シューズ": ["サンダル", "パンプス", "ブーツ", "スニーカー", "シューズ", "ミュール"],
    "バッグ": ["バッグ", "トート", "ショルダー", "ポーチ", "リュック"],
    "アクセサリー": ["ピアス", "ネックレス", "リング", "イヤリング", "ヘア", "ベルト", "キャップ", "ハット"],
}


# =========================
# Slack設定
# =========================

def load_slack_targets():
    targets_json = os.environ.get("SLACK_TARGETS_JSON", "").strip()

    if targets_json:
        try:
            raw_targets = json.loads(targets_json)
        except json.JSONDecodeError as exc:
            raise RuntimeError("SLACK_TARGETS_JSON のJSON形式が正しくありません") from exc

        targets = []
        for index, target in enumerate(raw_targets, start=1):
            bot_token = str(target.get("bot_token", "")).strip()
            channel_ids = target.get("channel_ids", [])

            if isinstance(channel_ids, str):
                channel_ids = [
                    channel_id.strip()
                    for channel_id in channel_ids.split(",")
                    if channel_id.strip()
                ]

            channel_ids = [
                str(channel_id).strip()
                for channel_id in channel_ids
                if str(channel_id).strip()
            ]

            if not bot_token:
                raise RuntimeError(f"SLACK_TARGETS_JSON の {index}件目に bot_token がありません")
            if not channel_ids:
                raise RuntimeError(f"SLACK_TARGETS_JSON の {index}件目に channel_ids がありません")

            targets.append({
                "name": str(target.get("name", f"workspace_{index}")),
                "bot_token": bot_token,
                "channel_ids": channel_ids,
            })

        if targets:
            return targets

    if SLACK_BOT_TOKEN and SLACK_CHANNEL_IDS:
        return [{
            "name": "default",
            "bot_token": SLACK_BOT_TOKEN,
            "channel_ids": SLACK_CHANNEL_IDS,
        }]

    raise RuntimeError("SLACK_TARGETS_JSON、または SLACK_BOT_TOKEN + SLACK_CHANNEL_IDS / SLACK_CHANNEL_ID を設定してください")


SLACK_TARGETS = load_slack_targets()


# =========================
# 共通関数
# =========================

def today():
    return datetime.now().strftime("%Y-%m-%d")


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def get_font(size):
    font_candidates = [
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.otf",
    ]
    for font_path in font_candidates:
        if os.path.exists(font_path):
            return ImageFont.truetype(font_path, size)
    return ImageFont.load_default()


def clean_text(text):
    text = re.sub(r"\s+", " ", text or "")
    return text.strip()


def extract_category(name):
    for category, keywords in CATEGORY_KEYWORDS.items():
        if any(keyword.lower() in name.lower() for keyword in keywords):
            return category
    return "その他"


def extract_brand_from_name(name):
    match = re.search(r"【([^】]+)】", name)
    if match:
        return match.group(1).strip()[:30]

    match = re.search(r"\[([^\]]+)\]", name)
    if match:
        return match.group(1).strip()[:30]

    return "andST"


def normalize_price(text):
    match = re.search(r"[¥￥]\s*([\d,]+)", text or "")
    if not match:
        return 0
    return int(match.group(1).replace(",", ""))


def absolute_url(url):
    if not url:
        return ""
    if url.startswith("//"):
        return "https:" + url
    return urljoin(BASE_URL, url)


def request_html(url):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }
    res = requests.get(url, headers=headers, timeout=25)
    res.raise_for_status()
    return res.text


# =========================
# andST取得
# =========================

def fetch_andst_image_from_product(product_url):
    try:
        html = request_html(product_url)
        soup = BeautifulSoup(html, "html.parser")

        for tag in soup.select("meta[property='og:image'], meta[name='twitter:image']"):
            content = tag.get("content")
            if content:
                return absolute_url(content)

        for img in soup.select("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-original")
            if src and ("goods" in src or "item" in src or "image" in src):
                return absolute_url(src)

    except Exception:
        return ""

    return ""


def fetch_andst():
    html = request_html(RANKING_URL)
    soup = BeautifulSoup(html, "html.parser")

    candidates = []

    for link in soup.select("a[href]"):
        href = link.get("href", "")
        text = clean_text(link.get_text(" ", strip=True))

        if not href:
            continue

        full_url = absolute_url(href)

        if not any(key in full_url for key in ["/disp/item/", "/ap/item/", "/item/"]):
            continue

        if not text:
            parent = link.find_parent()
            text = clean_text(parent.get_text(" ", strip=True)) if parent else ""

        if not text:
            continue

        price = normalize_price(text)

        name = re.sub(r"[¥￥]\s*[\d,]+", "", text)
        name = re.sub(r"\bNEW\b|\bSALE\b|\b予約\b|\bWEB限定\b", "", name, flags=re.IGNORECASE)
        name = clean_text(name)

        if len(name) < 3:
            continue

        image_url = ""
        img = link.select_one("img")
        if img:
            image_url = absolute_url(
                img.get("src") or img.get("data-src") or img.get("data-original") or ""
            )

        label_parts = []
        if re.search(r"\bNEW\b", text, re.IGNORECASE):
            label_parts.append("NEW")
        if re.search(r"\bSALE\b", text, re.IGNORECASE):
            label_parts.append("SALE")
        if "予約" in text:
            label_parts.append("予約")
        if "WEB限定" in text:
            label_parts.append("WEB限定")

        candidates.append({
            "name": name,
            "price": price,
            "url": full_url,
            "image_url": image_url,
            "label": " / ".join(label_parts),
        })

    items = []
    seen_urls = set()

    for candidate in candidates:
        url = candidate["url"]

        if url in seen_urls:
            continue

        seen_urls.add(url)

        if not candidate.get("image_url"):
            candidate["image_url"] = fetch_andst_image_from_product(url)

        item = {
            "rank": len(items) + 1,
            "name": candidate["name"],
            "price": candidate["price"],
            "url": url,
            "image_url": candidate.get("image_url", ""),
            "category": extract_category(candidate["name"]),
            "brand": extract_brand_from_name(candidate["name"]),
            "label": candidate.get("label", ""),
        }

        items.append(item)

        if len(items) >= DAILY_TOP_N:
            break

    if not items:
        raise RuntimeError("andSTランキングの商品情報を取得できませんでした。HTML構造が変更された可能性があります。")

    return items


# =========================
# データ保存・読み込み
# =========================

def load_previous():
    if not os.path.exists(DATA_FILE):
        return []
    with open(DATA_FILE, "r", encoding="utf-8") as f:
        return json.load(f)


def save_current(items):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)


def save_csv(items):
    file_exists = os.path.exists(CSV_FILE)
    with open(CSV_FILE, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)

        if not file_exists:
            writer.writerow([
                "date",
                "rank",
                "name",
                "price",
                "url",
                "image_url",
                "category",
                "brand",
                "label",
            ])

        for item in items:
            writer.writerow([
                today(),
                item["rank"],
                item["name"],
                item["price"],
                item["url"],
                item.get("image_url", ""),
                item.get("category", ""),
                item.get("brand", ""),
                item.get("label", ""),
            ])


def load_csv_rows():
    if not os.path.exists(CSV_FILE):
        return []

    rows = []
    with open(CSV_FILE, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        for row in reader:
            try:
                row["date_obj"] = parse_date(row["date"])
                row["rank"] = int(row["rank"])
                row["price"] = int(row.get("price") or 0)
                row["category"] = row.get("category") or extract_category(row.get("name", ""))
                row["brand"] = row.get("brand") or extract_brand_from_name(row.get("name", ""))
                row["label"] = row.get("label", "")
            except Exception:
                continue

            rows.append(row)

    return rows


# =========================
# 分析
# =========================

def analyze_ranking(current, previous):
    previous_map = {item["url"]: item for item in previous}
    new_items = []
    rising_items = []

    for item in current:
        old = previous_map.get(item["url"])

        if old is None:
            new_items.append(item)
        else:
            diff = old["rank"] - item["rank"]
            if diff > 0:
                rising_items.append({
                    **item,
                    "old_rank": old["rank"],
                    "rank_diff": diff,
                })

    rising_items.sort(key=lambda x: x["rank_diff"], reverse=True)

    return new_items, rising_items


def price_range_name(price):
    if price <= 0:
        return "価格不明"
    if price <= 2999:
        return "〜2,999円"
    if price <= 4999:
        return "3,000〜4,999円"
    if price <= 6999:
        return "5,000〜6,999円"
    if price <= 9999:
        return "7,000〜9,999円"
    return "10,000円〜"


def count_price_ranges(items):
    counter = Counter({
        "〜2,999円": 0,
        "3,000〜4,999円": 0,
        "5,000〜6,999円": 0,
        "7,000〜9,999円": 0,
        "10,000円〜": 0,
        "価格不明": 0,
    })

    for item in items:
        counter[price_range_name(item.get("price", 0))] += 1

    return counter


def price_analysis_text(items):
    if not items:
        return "価格帯データがありません。"

    price_ranges = count_price_ranges(items)
    top_range = max(price_ranges, key=price_ranges.get)

    return f"価格帯は「{top_range}」が最多です。"


def auto_analysis(items, new_items, rising_items):
    if not items:
        return "ランキングデータがありません。"

    top_names = "、".join([item["name"] for item in items[:3]])
    top_category = Counter(item.get("category") or extract_category(item["name"]) for item in items).most_common(1)[0][0]
    top_brand = Counter(item.get("brand") or extract_brand_from_name(item["name"]) for item in items).most_common(1)[0][0]

    return "\n".join([
        f"上位は「{top_names}」が中心です。",
        price_analysis_text(items),
        f"カテゴリは「{top_category}」、ブランドは「{top_brand}」が目立ちます。",
        f"新規ランクインは{len(new_items)}件、急上昇は{len(rising_items)}件です。",
    ])


def filter_rows_by_period(rows, report_type):
    today_date = datetime.now().date()

    if report_type == "weekly":
        start_date = today_date - timedelta(days=7)
        title = f"週次レポート {start_date.strftime('%Y-%m-%d')}〜{today_date.strftime('%Y-%m-%d')}"
    elif report_type == "monthly":
        start_date = today_date - timedelta(days=30)
        title = f"月次レポート {start_date.strftime('%Y-%m-%d')}〜{today_date.strftime('%Y-%m-%d')}"
    else:
        raise ValueError("report_type must be weekly or monthly")

    return [row for row in rows if start_date <= row["date_obj"] <= today_date], title


def analyze_period(rows):
    product_map = defaultdict(lambda: {
        "name": "",
        "url": "",
        "image_url": "",
        "category": "その他",
        "brand": "andST",
        "price": 0,
        "label": "",
        "appearances": 0,
        "rank_total": 0,
        "best_rank": 999,
        "first_rank": None,
        "last_rank": None,
        "first_date": None,
        "last_date": None,
        "rank1_count": 0,
    })

    dates = sorted({row["date_obj"] for row in rows})

    for row in sorted(rows, key=lambda r: (r["date_obj"], r["rank"])):
        item = product_map[row["url"]]

        item.update({
            "name": row["name"],
            "url": row["url"],
            "image_url": row.get("image_url", ""),
            "category": row.get("category") or extract_category(row["name"]),
            "brand": row.get("brand") or extract_brand_from_name(row["name"]),
            "price": row["price"],
            "label": row.get("label", ""),
        })

        item["appearances"] += 1
        item["rank_total"] += row["rank"]
        item["best_rank"] = min(item["best_rank"], row["rank"])

        if row["rank"] == 1:
            item["rank1_count"] += 1

        if item["first_date"] is None or row["date_obj"] < item["first_date"]:
            item["first_date"] = row["date_obj"]
            item["first_rank"] = row["rank"]

        if item["last_date"] is None or row["date_obj"] > item["last_date"]:
            item["last_date"] = row["date_obj"]
            item["last_rank"] = row["rank"]

    products = []

    for item in product_map.values():
        item["avg_rank"] = item["rank_total"] / item["appearances"]
        item["rank_change"] = (item["first_rank"] or 0) - (item["last_rank"] or 0)
        products.append(item)

    category_counts = Counter(p["category"] for p in products)
    brand_counts = Counter(p["brand"] for p in products)
    price_counts = count_price_ranges([p for p in products if p["price"]])

    popularity = sorted(products, key=lambda x: (-x["appearances"], x["avg_rank"]))[:10]
    rising = sorted(
        [p for p in products if p["rank_change"] > 0],
        key=lambda x: x["rank_change"],
        reverse=True,
    )[:10]
    champions = sorted(products, key=lambda x: (-x["rank1_count"], x["avg_rank"]))[:10]
    new_count = sum(1 for p in products if dates and p["first_date"] == min(dates))

    return {
        "days": len(dates),
        "product_count": len(products),
        "new_count": new_count,
        "popularity": popularity,
        "rising": rising,
        "champions": champions,
        "category_counts": category_counts,
        "brand_counts": brand_counts,
        "price_counts": price_counts,
        "top_categories": category_counts.most_common(5),
        "top_brands": brand_counts.most_common(5),
        "price_comment": price_analysis_text([p for p in products if p["price"]]) if products else "集計対象データがありません。",
    }


# =========================
# 画像生成
# =========================

def download_image(image_url):
    try:
        if not image_url:
            return None

        res = requests.get(
            image_url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        res.raise_for_status()

        return Image.open(BytesIO(res.content)).convert("RGB")

    except Exception:
        return None


def draw_text_wrap(draw, text, position, font, fill, max_width, line_height, max_lines=None):
    x, y = position
    line = ""
    lines = []

    for char in text:
        test_line = line + char
        bbox = draw.textbbox((x, y), test_line, font=font)

        if bbox[2] - bbox[0] <= max_width:
            line = test_line
        else:
            if line:
                lines.append(line)
            line = char

    if line:
        lines.append(line)

    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:max(0, len(lines[-1]) - 1)] + "…"

    for line in lines:
        draw.text((x, y), line, fill=fill, font=font)
        y += line_height

    return y


def draw_bar_chart(draw, title, data, x, y, w, h, font_title, font_small):
    draw.rounded_rectangle(
        (x, y, x + w, y + h),
        radius=18,
        fill="#f2f8ff",
        outline="#dce8f5",
    )

    draw.text((x + 20, y + 16), title, fill="#111111", font=font_title)

    items = list(data.items()) if isinstance(data, Counter) else list(data)
    items = items[:5]

    max_value = max([value for _, value in items], default=1)

    bar_x = x + 165
    bar_y = y + 65
    bar_max_w = w - 210

    for label, value in items:
        draw.text((x + 20, bar_y), str(label)[:12], fill="#111111", font=font_small)

        bar_w = int(bar_max_w * (value / max_value)) if max_value else 0

        draw.rounded_rectangle(
            (bar_x, bar_y + 3, bar_x + bar_w, bar_y + 21),
            radius=8,
            fill="#cfe8ff",
        )

        draw.text((bar_x + bar_w + 8, bar_y), str(value), fill="#555555", font=font_small)

        bar_y += 36


def create_daily_report_image(items, analysis, new_items, rising_items):
    width = 1200
    height = 2100

    img = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(img)

    font_title = get_font(42)
    font_subtitle = get_font(28)
    font_body = get_font(22)
    font_small = get_font(18)
    font_rank = get_font(20)

    draw.rectangle((0, 0, width, 110), fill="#dceeff")
    draw.text((40, 28), f"andSTランキング画像レポート / {today()}", fill="#111111", font=font_title)
    draw.text((905, 40), "自動分析レポート", fill="#333333", font=font_subtitle)

    draw.text((40, 140), "🏆 TOP10 ランキング", fill="#111111", font=font_title)

    card_w = 210
    card_h = 335
    gap = 20

    start_x = 40
    start_y = 210

    for idx, item in enumerate(items[:10]):
        row = idx // 5
        col = idx % 5

        x = start_x + col * (card_w + gap)
        y = start_y + row * (card_h + 28)

        draw.rounded_rectangle(
            (x, y, x + card_w, y + card_h),
            radius=18,
            fill="#ffffff",
            outline="#d5e7f7",
            width=2,
        )

        draw.rounded_rectangle((x + 12, y + 12, x + 72, y + 52), radius=10, fill="#4aa3df")
        draw.text((x + 22, y + 18), f'{item["rank"]}位', fill="white", font=font_rank)

        if item.get("label"):
            draw.rounded_rectangle((x + 128, y + 16, x + 195, y + 45), radius=8, fill="#ff8bbd")
            draw.text((x + 138, y + 20), item["label"][:7], fill="white", font=font_small)

        product_img = download_image(item.get("image_url", ""))

        if product_img:
            product_img.thumbnail((card_w - 24, 155))
            img.paste(product_img, (x + (card_w - product_img.width) // 2, y + 62))
        else:
            draw.rectangle((x + 20, y + 62, x + card_w - 20, y + 215), fill="#eeeeee")
            draw.text((x + 55, y + 128), "画像なし", fill="#999999", font=font_body)

        draw_text_wrap(
            draw,
            item["name"],
            (x + 14, y + 230),
            font_small,
            "#111111",
            card_w - 28,
            23,
            max_lines=3,
        )

        price_text = f'¥{item["price"]:,}' if item["price"] else "価格不明"
        draw.text((x + 14, y + 304), price_text, fill="#111111", font=font_body)

    y2 = 940

    draw.line((40, y2, width - 40, y2), fill="#dddddd", width=2)
    draw.text((40, y2 + 35), "📈 自動分析", fill="#111111", font=font_title)

    box_x, box_y, box_w, box_h = 40, y2 + 100, 540, 330

    draw.rounded_rectangle(
        (box_x, box_y, box_x + box_w, box_y + box_h),
        radius=18,
        fill="#f2f8ff",
        outline="#dce8f5",
    )

    text_y = box_y + 30
    for line in analysis.split("\n"):
        if line.strip():
            text_y = draw_text_wrap(
                draw,
                "・" + line.strip(),
                (box_x + 25, text_y),
                font_body,
                "#111111",
                box_w - 50,
                34,
            )
            text_y += 8

    draw.text((620, y2 + 100), "🔥 急上昇", fill="#111111", font=font_subtitle)
    ry = y2 + 150

    if rising_items:
        for item in rising_items[:5]:
            draw_text_wrap(
                draw,
                f'↑{item["rank_diff"]}：{item["old_rank"]}位 → {item["rank"]}位 {item["name"]}',
                (620, ry),
                font_small,
                "#111111",
                520,
                28,
                max_lines=1,
            )
            ry += 32
    else:
        draw.text((620, ry), "急上昇アイテムはありません", fill="#555555", font=font_small)

    draw.text((620, y2 + 360), "🆕 新規ランクイン", fill="#111111", font=font_subtitle)
    ny = y2 + 410

    if new_items:
        for item in new_items[:5]:
            draw_text_wrap(
                draw,
                f'NEW {item["rank"]}位：{item["name"]}',
                (620, ny),
                font_small,
                "#111111",
                520,
                28,
                max_lines=1,
            )
            ny += 32
    else:
        draw.text((620, ny), "新規ランクインはありません", fill="#555555", font=font_small)

    chart_y = 1510

    category_counts = Counter(item.get("category") or extract_category(item["name"]) for item in items)
    brand_counts = Counter(item.get("brand") or extract_brand_from_name(item["name"]) for item in items)

    draw_bar_chart(draw, "カテゴリ別TOP10構成", category_counts, 40, chart_y, 540, 250, font_subtitle, font_small)
    draw_bar_chart(draw, "ブランド別TOP10構成", brand_counts, 620, chart_y, 540, 250, font_subtitle, font_small)
    draw_bar_chart(draw, "価格帯構成", count_price_ranges(items), 40, chart_y + 280, 540, 230, font_subtitle, font_small)

    draw.line((40, 2020, width - 40, 2020), fill="#dddddd", width=2)
    draw.text((40, 2045), "ランキング元：andST", fill="#555555", font=font_small)
    draw.text((260, 2045), RANKING_URL, fill="#2a6fba", font=font_small)
    draw.text((880, 2045), "毎日 9:15 自動投稿", fill="#555555", font=font_small)

    img.save(REPORT_FILE)

    return REPORT_FILE


def create_period_report_image(report_type, period_title, summary):
    width = 1200
    height = 1900

    img = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(img)

    font_title = get_font(42)
    font_subtitle = get_font(30)
    font_body = get_font(23)
    font_small = get_font(18)

    report_label = "週次" if report_type == "weekly" else "月次"

    draw.rectangle((0, 0, width, 110), fill="#dceeff")
    draw.text((40, 28), f"andSTランキング{report_label}レポート", fill="#111111", font=font_title)
    draw.text((760, 40), today(), fill="#333333", font=font_subtitle)

    draw.text((40, 145), period_title, fill="#111111", font=font_subtitle)

    stat_y = 210
    stats = [
        ("集計日数", f"{summary['days']}日"),
        ("登場商品数", f"{summary['product_count']}商品"),
        ("初日新規数", f"{summary['new_count']}商品"),
    ]

    for idx, (label, value) in enumerate(stats):
        x = 40 + idx * 370
        draw.rounded_rectangle(
            (x, stat_y, x + 330, stat_y + 110),
            radius=18,
            fill="#f2f8ff",
            outline="#dce8f5",
        )
        draw.text((x + 24, stat_y + 20), label, fill="#555555", font=font_body)
        draw.text((x + 24, stat_y + 58), value, fill="#111111", font=font_subtitle)

    draw.text((40, 370), "🏆 人気継続 TOP10", fill="#111111", font=font_subtitle)

    y = 420
    for idx, item in enumerate(summary["popularity"][:10], start=1):
        text = f"{idx}. {item['name']} / 登場{item['appearances']}回 / 平均{item['avg_rank']:.1f}位 / 最高{item['best_rank']}位"
        draw_text_wrap(draw, text, (60, y), font_small, "#111111", 1080, 28, max_lines=1)
        y += 34

    draw.text((40, 790), "🔥 急上昇 TOP10", fill="#111111", font=font_subtitle)

    y = 840
    if summary["rising"]:
        for idx, item in enumerate(summary["rising"][:10], start=1):
            text = f"{idx}. ↑{item['rank_change']}：{item['first_rank']}位 → {item['last_rank']}位 {item['name']}"
            draw_text_wrap(draw, text, (60, y), font_small, "#111111", 1080, 28, max_lines=1)
            y += 34
    else:
        draw.text((60, y), "急上昇アイテムはありません", fill="#555555", font=font_small)

    draw.text((40, 1130), "👑 1位獲得・上位安定", fill="#111111", font=font_subtitle)

    y = 1180
    for idx, item in enumerate(summary["champions"][:5], start=1):
        text = f"{idx}. 1位{item['rank1_count']}回 / 平均{item['avg_rank']:.1f}位：{item['name']}"
        draw_text_wrap(draw, text, (60, y), font_small, "#111111", 1080, 28, max_lines=1)
        y += 36

    draw_bar_chart(draw, "カテゴリ別トレンド", summary["category_counts"], 40, 1400, 540, 240, font_subtitle, font_small)
    draw_bar_chart(draw, "ブランド別トレンド", summary["brand_counts"], 620, 1400, 540, 240, font_subtitle, font_small)

    draw.rounded_rectangle((40, 1690, width - 40, 1750), radius=16, fill="#f2f8ff", outline="#dce8f5")
    draw.text((65, 1708), summary["price_comment"], fill="#111111", font=font_body)

    img.save(REPORT_FILE)

    return REPORT_FILE


# =========================
# Slack投稿文
# =========================

def make_daily_link_text(items):
    if not items:
        return f"📊 *andSTランキング画像レポート / {today()}*\nランキングデータがありません。"

    first = items[0]
    first_price = f'¥{first["price"]:,}' if first["price"] else "価格不明"

    return "\n".join([
        f"📊 *andSTランキング画像レポート / {today()}*",
        "",
        f"1位：<{first['url']}|{first['name']}> {first_price}",
        "",
        "詳細は画像レポートを確認してください。",
    ])


def make_period_link_text(report_type, period_title, summary):
    label = "週次" if report_type == "weekly" else "月次"

    lines = [
        f"📊 *andSTランキング{label}レポート / {today()}*",
        period_title,
        "",
        f"集計日数：{summary['days']}日 / 登場商品数：{summary['product_count']}商品",
        "",
        "🏆 *人気継続 TOP10*",
    ]

    for idx, item in enumerate(summary["popularity"][:10], start=1):
        lines.append(
            f"{idx}位：<{item['url']}|{item['name']}> 登場{item['appearances']}回 / 平均{item['avg_rank']:.1f}位"
        )

    if summary["rising"]:
        lines += ["", "🔥 *急上昇 TOP5*"]
        for idx, item in enumerate(summary["rising"][:5], start=1):
            lines.append(
                f"{idx}位：↑{item['rank_change']} {item['first_rank']}位→{item['last_rank']}位 <{item['url']}|{item['name']}>"
            )

    return "\n".join(lines)


def post_to_slack(image_path, comment, title):
    for target in SLACK_TARGETS:
        slack = WebClient(token=target["bot_token"])

        for channel_id in target["channel_ids"]:
            slack.files_upload_v2(
                channel=channel_id,
                file=image_path,
                title=title,
                initial_comment=comment,
            )


# =========================
# Google Sheets同期
# =========================

def open_google_sheet():
    if not GOOGLE_SHEET_ID or not GOOGLE_SERVICE_ACCOUNT_JSON:
        return None

    try:
        import gspread

        credentials = json.loads(GOOGLE_SERVICE_ACCOUNT_JSON)
        client = gspread.service_account_from_dict(credentials)

        return client.open_by_key(GOOGLE_SHEET_ID)

    except Exception as exc:
        print(f"Google Sheets sync skipped: {exc}")
        return None


def get_or_create_worksheet(sheet, title, rows=1000, cols=20):
    try:
        return sheet.worksheet(title)
    except Exception:
        return sheet.add_worksheet(title=title, rows=rows, cols=cols)


def sync_daily_to_google_sheets(items):
    sheet = open_google_sheet()

    if sheet is None:
        return

    ws = get_or_create_worksheet(sheet, "andst_daily_ranking")

    if not ws.get_all_values():
        ws.append_row(
            ["date", "rank", "name", "price", "url", "image_url", "category", "brand", "label"],
            value_input_option="USER_ENTERED",
        )

    rows = [
        [
            today(),
            item["rank"],
            item["name"],
            item["price"],
            item["url"],
            item.get("image_url", ""),
            item.get("category", ""),
            item.get("brand", ""),
            item.get("label", ""),
        ]
        for item in items
    ]

    ws.append_rows(rows, value_input_option="USER_ENTERED")


def sync_period_to_google_sheets(report_type, period_title, summary):
    sheet = open_google_sheet()

    if sheet is None:
        return

    ws = get_or_create_worksheet(sheet, "andst_period_reports")

    if not ws.get_all_values():
        ws.append_row(
            ["created_at", "report_type", "period", "days", "product_count", "top_category", "top_brand", "top_item"],
            value_input_option="USER_ENTERED",
        )

    top_category = summary["top_categories"][0][0] if summary["top_categories"] else ""
    top_brand = summary["top_brands"][0][0] if summary["top_brands"] else ""
    top_item = summary["popularity"][0]["name"] if summary["popularity"] else ""

    ws.append_row(
        [
            today(),
            report_type,
            period_title,
            summary["days"],
            summary["product_count"],
            top_category,
            top_brand,
            top_item,
        ],
        value_input_option="USER_ENTERED",
    )


# =========================
# 実行処理
# =========================

def run_daily():
    current = fetch_andst()
    previous = load_previous()

    new_items, rising_items = analyze_ranking(current, previous)
    analysis = auto_analysis(current, new_items, rising_items)

    image_path = create_daily_report_image(current, analysis, new_items, rising_items)

    post_to_slack(
        image_path,
        make_daily_link_text(current),
        f"andSTランキング画像レポート {today()}",
    )

    save_current(current)
    save_csv(current)
    sync_daily_to_google_sheets(current)


def run_period(report_type):
    rows = load_csv_rows()
    period_rows, period_title = filter_rows_by_period(rows, report_type)

    summary = analyze_period(period_rows)

    image_path = create_period_report_image(report_type, period_title, summary)

    label = "週次" if report_type == "weekly" else "月次"

    post_to_slack(
        image_path,
        make_period_link_text(report_type, period_title, summary),
        f"andSTランキング{label}レポート {today()}",
    )

    sync_period_to_google_sheets(report_type, period_title, summary)


def main():
    parser = argparse.ArgumentParser(description="andST ranking Slack report bot")

    parser.add_argument(
        "--report-type",
        choices=["daily", "weekly", "monthly"],
        default="daily",
        help="投稿するレポート種別",
    )

    args = parser.parse_args()

    if args.report_type == "daily":
        run_daily()
    else:
        run_period(args.report_type)


if __name__ == "__main__":
    main()
