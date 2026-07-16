import os
import requests
from PIL import Image, ImageDraw, ImageFont

from fund_alert import collect, parse_direct, parse_estimated, signed_pct, now_cn

# =====================================================================
# 🌟 第一部分：用户自定义区（想改什么，直接在这里改） 🌟
# =====================================================================

# 1. 直接估值基金（天天基金官方实时估值）。
#    格式：代码:名称，多只用逗号分隔。
FUND_DIRECT = "160419:中证证券公司A,017513:北证50成份C"

# 2. 持仓估算基金（按披露的前十大持仓 × 实时行情估算，用于没有官方实时估值的基金）。
#    格式：代码:名称，多只用逗号分隔。
FUND_ESTIMATED = "007355:汇添科技创新A"

# 3. 屏幕顶部标题
PANEL_TITLE = "基金盘中估值"


# =====================================================================
# 🔒 第二部分：核心密钥区（⚠️绝对不要改这里，请在 GitHub Secrets 里配置） 🔒
# =====================================================================
API_KEY = os.environ.get("ZECTRIX_API_KEY")
MAC_ADDRESS = os.environ.get("ZECTRIX_MAC")

# 本地渲染验证用：设置 DRY_RUN=1 时只生成 page_*.png、不推送、也不校验密钥。
DRY_RUN = bool(os.environ.get("DRY_RUN"))

# 接口地址（自动拼接）
PUSH_URL = f"https://cloud.zectrix.com/open/v1/devices/{MAC_ADDRESS}/display/image"


# =====================================================================
# ⚙️ 第三部分：底层运行逻辑（如果没有报错，不需要修改以下代码） ⚙️
# =====================================================================

FONT_PATH = "font.ttf"
try:
    font_title = ImageFont.truetype(FONT_PATH, 24)
    font_item = ImageFont.truetype(FONT_PATH, 18)
    font_small = ImageFont.truetype(FONT_PATH, 14)
    font_big = ImageFont.truetype(FONT_PATH, 34)
except Exception:
    print("❌ 错误: 找不到 font.ttf")
    exit(1)

SCREEN_W, SCREEN_H = 400, 300
CONTENT_TOP = 55       # 标题栏下方，第一个基金块的起始 y
BLOCK_H = 60           # 每只基金一个块
PER_PAGE = (SCREEN_H - CONTENT_TOP) // BLOCK_H   # 单页可容纳的基金数（=4）


def push_image(img, page_id):
    img.save(f"page_{page_id}.png")
    if DRY_RUN:
        print(f"📝 [dry-run] 已保存 page_{page_id}.png，跳过推送。")
        return
    api_headers = {"X-API-Key": API_KEY}
    files = {"images": (f"page_{page_id}.png", open(f"page_{page_id}.png", "rb"), "image/png")}
    data = {"dither": "true", "pageId": str(page_id)}
    try:
        res = requests.post(PUSH_URL, headers=api_headers, files=files, data=data)
        print(f"✅ Page {page_id} 推送成功: {res.status_code}")
    except Exception as e:
        print(f"❌ Page {page_id} 推送失败: {e}")


def _text_width(draw, text, font):
    try:
        return draw.textlength(text, font=font)
    except AttributeError:
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]


def _truncate(draw, text, font, max_width):
    if _text_width(draw, text, font) <= max_width:
        return text
    while text and _text_width(draw, text + "…", font) > max_width:
        text = text[:-1]
    return (text + "…") if text else ""


def build_rows(direct_changes, estimated_changes, failed):
    """把三类结果拍平成统一的行结构：{name, code, pct, detail}。pct 为 None 表示估算不可用。"""
    rows = []
    for c in direct_changes:
        rows.append({
            "name": c.name, "code": c.code, "pct": c.change_pct,
            "detail": f"行情 {c.quote_time.strftime('%H:%M')}",
        })
    for c in estimated_changes:
        rows.append({
            "name": c.name, "code": c.code, "pct": c.estimate_pct,
            "detail": f"行情 {c.quote_time.strftime('%H:%M')}",
        })
    for f in failed:
        rows.append({
            "name": f.name, "code": f.code, "pct": None,
            "detail": "估算不可用",
        })
    return rows


def draw_fund_page(rows, title, time_label):
    img = Image.new('1', (SCREEN_W, SCREEN_H), color=255)
    draw = ImageDraw.Draw(img)

    # 标题栏：黑底白字，左标题右时间
    draw.rounded_rectangle([(10, 10), (390, 45)], radius=8, fill=0)
    draw.text((20, 14), title, font=font_title, fill=255)
    tw = _text_width(draw, time_label, font_small)
    draw.text((388 - tw, 22), time_label, font=font_small, fill=255)

    y = CONTENT_TOP
    for idx, row in enumerate(rows):
        change_w = 0
        if row["pct"] is not None:
            # 1-bit 无颜色，用 ▲/▼ 区分涨跌
            change = ("▲" if row["pct"] >= 0 else "▼") + signed_pct(row["pct"])
            change_w = _text_width(draw, change, font_big)
            draw.text((388 - change_w, y + 6), change, font=font_big, fill=0)

        label = _truncate(draw, f"{row['name']} {row['code']}", font_item,
                          SCREEN_W - 20 - change_w - 12 - 20)
        draw.text((20, y + 10), label, font=font_item, fill=0)

        detail = _truncate(draw, row["detail"], font_small, SCREEN_W - 40)
        draw.text((20, y + 36), detail, font=font_small, fill=0)

        if idx != len(rows) - 1:
            draw.line([(20, y + BLOCK_H - 8), (380, y + BLOCK_H - 8)], fill=0, width=1)
        y += BLOCK_H

    return img


def task_fund():
    direct, estimated, failed, is_trading = collect(
        parse_direct(FUND_DIRECT), parse_estimated(FUND_ESTIMATED)
    )
    # 非交易日、或全部基金数据源失败（无当日行情）→ 不推送，保留墨水屏上一次画面。
    if not is_trading:
        print("⏩ 非交易日或无盘中数据，跳过推送。")
        return

    rows = build_rows(direct, estimated, failed)
    time_label = now_cn().strftime("%m-%d %H:%M")
    pages = [rows[i:i + PER_PAGE] for i in range(0, len(rows), PER_PAGE)]
    for page_id, subset in enumerate(pages, start=1):
        print(f"生成 Page {page_id}: {len(subset)} 只基金...")
        img = draw_fund_page(subset, PANEL_TITLE, time_label)
        push_image(img, page_id)


# ================= 主程序 =================
if __name__ == "__main__":
    if not DRY_RUN and (not API_KEY or not MAC_ADDRESS):
        print("❌ 错误: 请先在 GitHub Secrets 中配置 ZECTRIX_API_KEY 和 ZECTRIX_MAC")
        exit(1)

    print("🚀 开始执行基金盘中估值推送任务...")
    task_fund()
    print("🎉 任务执行完毕！")
