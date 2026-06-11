import argparse
import csv
import json
import os
import re
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

DATA_FILE = "previous.json"
CSV_FILE = "data.csv"
REPORT_FILE = "report.png"

DAILY_TOP_N = 10

# WOMEN > トップス / 前日ランキング / 年代別
AGE_RANKING_TARGETS = [
    {
        "label": "20代後半",
        "age_range": "25-29",
        "url": "https://www.dot-st.com/disp/ranking/?dispNo=001001003&periodTp=3&ageRange=25-29",
    },
    {
        "label": "30代前半",
        "age_range": "30-34",
        "url": "https://www.dot-st.com/disp/ranking/?dispNo=001001003&periodTp=3&ageRange=30-34",
    },
    {
        "label": "30代後半",
        "age_range": "35-39",
        "url": "https://www.dot-st.com/disp/ranking/?dispNo=001001003&periodTp=3&ageRange=35-39",
    },
]

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

    raise RuntimeError("SLACK_TARGETS_JSON、または SLACK_BOT_TOKEN + SLACK_CHANNEL_ID を設定してください")


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
    return re.sub(r"\s+", " ", text or "").strip()


def absolute_url(url):
    if not url:
        return ""

    if url.startswith("//"):
        return "https:" + url

    return urljoin(BASE_URL, url)


def normalize_price(text):
    match = re.search(r"[¥￥]\s*([\d,]+)", text or "")

    if not match:
        return 0

    return int(match.group(1).replace(",", ""))


def request_html(url):
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
    }

    response = requests.get(url, headers=headers, timeout=25)
    response.raise_for_status()

    return response.text


def extract_brand_and_name(raw_name):
    name = clean_text(raw_name)

    # 先頭の順位を削除
    name = re.sub(r"^\d+\s+", "", name)

    # 価格以降を削除
    name = re.sub(r"[¥￥]\s*[\d,]+.*$", "", name)

    # ラベル系を削除
    name = re.sub(r"\bNEW\b|\bSALE\b|再入荷|予約|先行予約|WEB限定|ポイント\d+倍", "", name, flags=re.IGNORECASE)
    name = clean_text(name)

    parts = name.split(" ", 1)

    if len(parts) == 2:
        brand = parts[0].strip()
        product_name = parts[1].strip()
    else:
        brand = "andST"
        product_name = name

    return brand[:30], product_name


def extract_label(text):
    labels = []

    if re.search(r"\bNEW\b", text, re.IGNORECASE):
        labels.append("NEW")

    if re.search(r"\bSALE\b", text, re.IGNORECASE):
        labels.append("SALE")

    if "再入荷" in text:
        labels.append("再入荷")

    if "予約" in text:
        labels.append("予約")

    if "WEB限定" in text:
        labels.append("WEB限定")

    return " / ".join(labels)


# =========================
# andST取得
# =========================

def fetch_product_image(product_url):
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


def fetch_andst_ranking(target):
    html = request_html(target["url"])
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
        brand, name = extract_brand_and_name(text)

        if len(name) < 3:
            continue

        image_url = ""

        img = link.select_one("img")
        if img:
            image_url = absolute_url(
                img.get("src") or img.get("data-src") or img.get("data-original") or ""
            )

        candidates.append({
            "name": name,
            "brand": brand,
            "price": price,
            "url": full_url,
            "image_url": image_url,
            "label": extract_label(text),
        })

    items = []
    seen_urls = set()

    for candidate in candidates:
        url = candidate["url"]

        if url in seen_urls:
            continue

        seen_urls.add(url)

        if not candidate.get("image_url"):
            candidate["image_url"] = fetch_product_image(url)

        item = {
            "age_label": target["label"],
            "age_range": target["age_range"],
            "rank": len(items) + 1,
            "name": candidate["name"],
            "brand": candidate["brand"],
            "price": candidate["price"],
            "url": candidate["url"],
            "image_url": candidate.get("image_url", ""),
            "label": candidate.get("label", ""),
            "source_url": target["url"],
        }

        items.append(item)

        if len(items) >= DAILY_TOP_N:
            break

    if not items:
        raise RuntimeError(f"{target['label']} のランキング商品情報を取得できませんでした。URLまたはHTML構造を確認してください。")

    return items


def fetch_all_age_rankings():
    result = {}

    for target in AGE_RANKING_TARGETS:
        result[target["label"]] = fetch_andst_ranking(target)

    return result


# =========================
# データ保存
# =========================

def load_previous():
    if not os.path.exists(DATA_FILE):
        return {}

    with open(DATA_FILE, "r", encoding="utf-8") as f:
        data = json.load(f)

    if isinstance(data, list):
        return {"旧データ": data}

    return data


def save_current(grouped_items):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(grouped_items, f, ensure_ascii=False, indent=2)


def flatten_grouped_items(grouped_items):
    rows = []

    for age_label, items in grouped_items.items():
        for item in items:
            rows.append(item)

    return rows


def save_csv(grouped_items):
    file_exists = os.path.exists(CSV_FILE)

    with open(CSV_FILE, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)

        if not file_exists:
            writer.writerow([
                "date",
                "age_label",
                "age_range",
                "rank",
                "brand",
                "name",
                "price",
                "url",
                "image_url",
                "label",
                "source_url",
            ])

        for item in flatten_grouped_items(grouped_items):
            writer.writerow([
                today(),
                item.get("age_label", ""),
                item.get("age_range", ""),
                item.get("rank", ""),
                item.get("brand", ""),
                item.get("name", ""),
                item.get("price", ""),
                item.get("url", ""),
                item.get("image_url", ""),
                item.get("label", ""),
                item.get("source_url", ""),
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
            except Exception:
                continue

            rows.append(row)

    return rows


# =========================
# 分析
# =========================

def analyze_ranking_by_age(current_grouped, previous_grouped):
    result = {}

    for age_label, current_items in current_grouped.items():
        previous_items = previous_grouped.get(age_label, [])
        previous_map = {item["url"]: item for item in previous_items}

        new_items = []
        rising_items = []

        for item in current_items:
            old = previous_map.get(item["url"])

            if old is None:
                new_items.append(item)
            else:
                diff = int(old.get("rank", 0)) - int(item.get("rank", 0))
                if diff > 0:
                    rising_items.append({
                        **item,
                        "old_rank": old.get("rank"),
                        "rank_diff": diff,
                    })

        rising_items.sort(key=lambda x: x["rank_diff"], reverse=True)

        result[age_label] = {
            "new_items": new_items,
            "rising_items": rising_items,
        }

    return result


def make_summary_text(grouped_items, ranking_analysis):
    lines = []

    for age_label, items in grouped_items.items():
        if not items:
            lines.append(f"{age_label}：データなし")
            continue

        first = items[0]
        new_count = len(ranking_analysis.get(age_label, {}).get("new_items", []))
        rising_count = len(ranking_analysis.get(age_label, {}).get("rising_items", []))

        lines.append(
            f"{age_label}：1位は「{first['brand']} {first['name']}」。新規{new_count}件、急上昇{rising_count}件。"
        )

    return "\n".join(lines)


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
    summary = {}

    for target in AGE_RANKING_TARGETS:
        age_label = target["label"]
        age_rows = [row for row in rows if row.get("age_label") == age_label]

        product_map = {}

        for row in age_rows:
            url = row["url"]

            if url not in product_map:
                product_map[url] = {
                    "age_label": age_label,
                    "brand": row.get("brand", ""),
                    "name": row.get("name", ""),
                    "url": url,
                    "appearances": 0,
                    "rank_total": 0,
                    "best_rank": 999,
                    "first_date": None,
                    "last_date": None,
                    "first_rank": None,
                    "last_rank": None,
                }

            item = product_map[url]
            item["appearances"] += 1
            item["rank_total"] += row["rank"]
            item["best_rank"] = min(item["best_rank"], row["rank"])

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

        popularity = sorted(products, key=lambda x: (-x["appearances"], x["avg_rank"]))[:10]
        rising = sorted(
            [p for p in products if p["rank_change"] > 0],
            key=lambda x: x["rank_change"],
            reverse=True,
        )[:10]

        summary[age_label] = {
            "product_count": len(products),
            "popularity": popularity,
            "rising": rising,
        }

    return summary


# =========================
# 画像生成
# =========================

def download_image(image_url):
    try:
        if not image_url:
            return None

        response = requests.get(
            image_url,
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=20,
        )
        response.raise_for_status()

        return Image.open(BytesIO(response.content)).convert("RGB")

    except Exception:
        return None


def draw_text_wrap(draw, text, position, font, fill, max_width, line_height, max_lines=None):
    x, y = position
    line = ""
    lines = []

    for char in str(text):
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


def draw_item_row(draw, base_img, item, x, y, w, font_rank, font_body, font_small):
    row_h = 84

    draw.rounded_rectangle(
        (x, y, x + w, y + row_h),
        radius=12,
        fill="#ffffff",
        outline="#dce8f5",
        width=1,
    )

    draw.rounded_rectangle(
        (x + 12, y + 18, x + 58, y + 52),
        radius=8,
        fill="#4aa3df",
    )
    draw.text((x + 22, y + 23), f"{item['rank']}", fill="#ffffff", font=font_rank)

    thumb = download_image(item.get("image_url", ""))

    if thumb:
        thumb.thumbnail((58, 58))
        base_img.paste(thumb, (x + 72, y + 13))
    else:
        draw.rectangle((x + 72, y + 13, x + 130, y + 71), fill="#eeeeee")

    text_x = x + 145
    text_w = w - 165

    brand = item.get("brand", "")
    name = item.get("name", "")
    label = item.get("label", "")
    price = item.get("price", 0)

    title = f"{brand} {name}".strip()
    draw_text_wrap(draw, title, (text_x, y + 12), font_small, "#111111", text_w, 22, max_lines=2)

    price_text = f"¥{price:,}" if price else "価格不明"

    if label:
        price_text += f" / {label}"

    draw.text((text_x, y + 56), price_text, fill="#555555", font=font_small)

    return y + row_h + 8


def draw_age_compare_cell(draw, base_img, item, x, y, w, h, font_rank, font_body, font_small):
    draw.rounded_rectangle(
        (x, y, x + w, y + h),
        radius=12,
        fill="#ffffff",
        outline="#dce8f5",
        width=1,
    )

    if not item:
        draw.text((x + 20, y + 40), "データなし", fill="#999999", font=font_body)
        return

    rank = item.get("rank", "")
    brand = item.get("brand", "")
    name = item.get("name", "")
    price = item.get("price", 0)
    label = item.get("label", "")

    draw.rounded_rectangle(
        (x + 12, y + 14, x + 58, y + 48),
        radius=8,
        fill="#4aa3df",
    )
    draw.text((x + 22, y + 19), str(rank), fill="#ffffff", font=font_rank)

    thumb = download_image(item.get("image_url", ""))

    if thumb:
        thumb.thumbnail((70, 70))
        base_img.paste(thumb, (x + 72, y + 14))
    else:
        draw.rectangle((x + 72, y + 14, x + 142, y + 84), fill="#eeeeee")

    text_x = x + 155
    text_w = w - 170

    title = f"{brand} {name}".strip()
    draw_text_wrap(
        draw,
        title,
        (text_x, y + 14),
        font_small,
        "#111111",
        text_w,
        22,
        max_lines=2,
    )

    price_text = f"¥{price:,}" if price else "価格不明"

    if label:
        price_text += f" / {label}"

    draw_text_wrap(
        draw,
        price_text,
        (text_x, y + 62),
        font_small,
        "#555555",
        text_w,
        20,
        max_lines=1,
    )


def create_daily_report_image(grouped_items, ranking_analysis):
    width = 1600
    height = 1850

    img = Image.new("RGB", (width, height), "#ffffff")
    draw = ImageDraw.Draw(img)

    font_title = get_font(42)
    font_subtitle = get_font(28)
    font_body = get_font(22)
    font_small = get_font(17)
    font_rank = get_font(20)

    draw.rectangle((0, 0, width, 110), fill="#dceeff")
    draw.text(
        (40, 28),
        f"andST WOMENトップス 年代別ランキング / {today()}",
        fill="#111111",
        font=font_title,
    )
    draw.text(
        (1120, 42),
        "20代後半 / 30代前半 / 30代後半",
        fill="#333333",
        font=font_body,
    )

    summary_y = 135
    draw.rounded_rectangle(
        (40, summary_y, width - 40, summary_y + 145),
        radius=18,
        fill="#f2f8ff",
        outline="#dce8f5",
    )
    draw.text((65, summary_y + 22), "本日の概要", fill="#111111", font=font_subtitle)

    summary_text = make_summary_text(grouped_items, ranking_analysis)
    text_y = summary_y + 68

    for line in summary_text.split("\n"):
        draw.text((70, text_y), "・" + line, fill="#111111", font=font_small)
        text_y += 28

    table_x = 40
    table_y = 320
    rank_col_w = 80
    col_w = 480
    header_h = 70
    row_h = 130

    age_labels = [target["label"] for target in AGE_RANKING_TARGETS]

    table_w = rank_col_w + col_w * len(age_labels)
    table_h = header_h + row_h * DAILY_TOP_N

    draw.rounded_rectangle(
        (table_x, table_y, table_x + table_w, table_y + table_h),
        radius=22,
        fill="#fbfdff",
        outline="#dce8f5",
        width=2,
    )

    draw.rectangle(
        (table_x, table_y, table_x + table_w, table_y + header_h),
        fill="#eaf5ff",
    )

    draw.text(
        (table_x + 18, table_y + 22),
        "順位",
        fill="#111111",
        font=font_body,
    )

    for col_index, age_label in enumerate(age_labels):
        x = table_x + rank_col_w + col_index * col_w
        draw.line(
            (x, table_y, x, table_y + table_h),
            fill="#dce8f5",
            width=2,
        )
        draw.text(
            (x + 24, table_y + 20),
            age_label,
            fill="#111111",
            font=font_subtitle,
        )

    for rank_index in range(DAILY_TOP_N):
        y = table_y + header_h + rank_index * row_h

        draw.line(
            (table_x, y, table_x + table_w, y),
            fill="#dce8f5",
            width=1,
        )

        draw.text(
            (table_x + 22, y + 48),
            f"{rank_index + 1}位",
            fill="#111111",
            font=font_body,
        )

        for col_index, age_label in enumerate(age_labels):
            items = grouped_items.get(age_label, [])
            item = items[rank_index] if rank_index < len(items) else None

            cell_x = table_x + rank_col_w + col_index * col_w + 10
            cell_y = y + 10

            draw_age_compare_cell(
                draw,
                img,
                item,
                cell_x,
                cell_y,
                col_w - 20,
                row_h - 20,
                font_rank,
                font_body,
                font_small,
            )

    footer_y = table_y + table_h + 50
    draw.line((40, footer_y, width - 40, footer_y), fill="#dddddd", width=2)
    draw.text(
        (40, footer_y + 25),
        "ランキング元：andST / WOMEN > トップス / 年代別",
        fill="#555555",
        font=font_small,
    )
    draw.text(
        (1240, footer_y + 25),
        "毎日 9:15 自動投稿",
        fill="#555555",
        font=font_small,
    )

    img.save(REPORT_FILE)

    return REPORT_FILE


# =========================
# Slack投稿文
# =========================

def make_daily_link_text(grouped_items):
    lines = [
        f"📊 *andST WOMENトップス 年代別ランキング / {today()}*",
        "",
    ]

    for age_label, items in grouped_items.items():
        lines.append(f"*{age_label} TOP10*")

        for item in items[:10]:
            price_text = f'¥{item["price"]:,}' if item.get("price") else "価格不明"
            label_text = f' / {item["label"]}' if item.get("label") else ""
            lines.append(
                f'{item["rank"]}位：<{item["url"]}|{item["brand"]} {item["name"]}> {price_text}{label_text}'
            )

        lines.append("")

    lines.append("詳細は画像レポートを確認してください。")

    return "\n".join(lines)


def make_period_link_text(report_type, period_title, summary):
    label = "週次" if report_type == "weekly" else "月次"

    lines = [
        f"📊 *andST WOMENトップス 年代別{label}レポート / {today()}*",
        period_title,
        "",
    ]

    for age_label, data in summary.items():
        lines.append(f"*{age_label} 人気継続 TOP5*")

        if data["popularity"]:
            for idx, item in enumerate(data["popularity"][:5], start=1):
                lines.append(
                    f"{idx}位：<{item['url']}|{item['brand']} {item['name']}> 登場{item['appearances']}回 / 平均{item['avg_rank']:.1f}位"
                )
        else:
            lines.append("データなし")

        lines.append("")

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


def sync_daily_to_google_sheets(grouped_items):
    sheet = open_google_sheet()

    if sheet is None:
        return

    ws = get_or_create_worksheet(sheet, "andst_age_daily_ranking")

    if not ws.get_all_values():
        ws.append_row(
            [
                "date",
                "age_label",
                "age_range",
                "rank",
                "brand",
                "name",
                "price",
                "url",
                "image_url",
                "label",
                "source_url",
            ],
            value_input_option="USER_ENTERED",
        )

    rows = []

    for item in flatten_grouped_items(grouped_items):
        rows.append([
            today(),
            item.get("age_label", ""),
            item.get("age_range", ""),
            item.get("rank", ""),
            item.get("brand", ""),
            item.get("name", ""),
            item.get("price", ""),
            item.get("url", ""),
            item.get("image_url", ""),
            item.get("label", ""),
            item.get("source_url", ""),
        ])

    ws.append_rows(rows, value_input_option="USER_ENTERED")


def sync_period_to_google_sheets(report_type, period_title, summary):
    sheet = open_google_sheet()

    if sheet is None:
        return

    ws = get_or_create_worksheet(sheet, "andst_age_period_reports")

    if not ws.get_all_values():
        ws.append_row(
            [
                "created_at",
                "report_type",
                "period",
                "age_label",
                "product_count",
                "top_item",
            ],
            value_input_option="USER_ENTERED",
        )

    rows = []

    for age_label, data in summary.items():
        top_item = ""

        if data["popularity"]:
            top = data["popularity"][0]
            top_item = f"{top['brand']} {top['name']}"

        rows.append([
            today(),
            report_type,
            period_title,
            age_label,
            data["product_count"],
            top_item,
        ])

    ws.append_rows(rows, value_input_option="USER_ENTERED")


# =========================
# 実行処理
# =========================

def run_daily():
    current_grouped = fetch_all_age_rankings()
    previous_grouped = load_previous()

    ranking_analysis = analyze_ranking_by_age(current_grouped, previous_grouped)

    image_path = create_daily_report_image(current_grouped, ranking_analysis)

    post_to_slack(
        image_path,
        make_daily_link_text(current_grouped),
        f"andST WOMENトップス 年代別ランキング {today()}",
    )

    save_current(current_grouped)
    save_csv(current_grouped)
    sync_daily_to_google_sheets(current_grouped)


def run_period(report_type):
    rows = load_csv_rows()
    period_rows, period_title = filter_rows_by_period(rows, report_type)

    summary = analyze_period(period_rows)

    image_path = create_period_report_image(report_type, period_title, summary)

    label = "週次" if report_type == "weekly" else "月次"

    post_to_slack(
        image_path,
        make_period_link_text(report_type, period_title, summary),
        f"andST WOMENトップス 年代別{label}レポート {today()}",
    )

    sync_period_to_google_sheets(report_type, period_title, summary)


def main():
    parser = argparse.ArgumentParser(description="andST age ranking Slack report bot")

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
