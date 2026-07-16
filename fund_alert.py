#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Fund intraday change alert - skill edition.

Queries Tiantian Fund intraday estimates (direct funds) and estimates from
disclosed top holdings + real-time quotes (estimated funds). Prints the alert
message to stdout. Does NOT send anything - piping to send_feishu.py handles
delivery. Fund codes come from CLI args (configured in the autopilot task prompt).

Resilience: a single fund failing (e.g. 007355's real-time quote API down) does
NOT abort the whole alert. Successful funds are still reported; failed ones are
listed as "估算不可用" with the reason. Only if EVERY fund fails does it exit 1.

Trading day -> stdout is the alert message, exit 0.
Non-trading day -> stdout is "SKIP_NON_TRADING: ...", exit 0 (send_feishu.py skips).
Error (all funds failed) -> stderr has the reason, exit 1.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import sys
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Any


@dataclass
class DirectFundChange:
    code: str
    name: str
    change_pct: float
    estimate_value: str
    nav_date: str
    quote_time: dt.datetime


@dataclass
class Holding:
    secid: str
    code: str
    name: str
    weight_pct: float


@dataclass
class HoldingQuote:
    secid: str
    code: str
    name: str
    change_pct: float
    quote_time: dt.datetime


@dataclass
class EstimatedFundChange:
    code: str
    name: str
    estimate_pct: float
    normalized_top_holdings_pct: float
    covered_weight_pct: float
    holdings_date: str
    quote_time: dt.datetime
    holdings: list[tuple[Holding, HoldingQuote]]


@dataclass
class FailedFund:
    code: str
    name: str
    reason: str


class FundAlertError(RuntimeError):
    pass


REQUEST_TIMEOUT = 12  # seconds


def now_cn() -> dt.datetime:
    return dt.datetime.now(dt.timezone(dt.timedelta(hours=8)))


def http_get(url: str, timeout: int = REQUEST_TIMEOUT) -> str:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/126.0 Safari/537.36"
            ),
            "Referer": "https://fund.eastmoney.com/",
        },
    )
    last_error: Exception | None = None
    raw = b""
    encoding = "utf-8"
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read()
                encoding = response.headers.get_content_charset() or "utf-8"
            break
        except Exception as exc:
            last_error = exc
            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))
    else:
        raise FundAlertError(f"请求失败: {url} -> {last_error}")

    candidates = [encoding]
    if "," in encoding:
        candidates.extend(part.strip() for part in encoding.split(",") if part.strip())
    candidates.extend(["utf-8", "gb18030"])
    for candidate in candidates:
        try:
            return raw.decode(candidate)
        except (LookupError, UnicodeDecodeError):
            continue
    return raw.decode("utf-8", errors="replace")


def parse_cn_time(value: str) -> dt.datetime:
    parsed = dt.datetime.strptime(value, "%Y-%m-%d %H:%M")
    return parsed.replace(tzinfo=dt.timezone(dt.timedelta(hours=8)))


def parse_epoch_cn(value: int | float | str) -> dt.datetime:
    return dt.datetime.fromtimestamp(int(value), tz=dt.timezone(dt.timedelta(hours=8)))


def signed_pct(value: float) -> str:
    return f"{value:+.2f}%"


def get_direct_fund_change(code: str, fallback_name: str) -> DirectFundChange:
    url = f"https://fundgz.1234567.com.cn/js/{code}.js?rt={int(time.time() * 1000)}"
    text = http_get(url)
    match = re.search(r"jsonpgz\((\{.*\})\);?", text, flags=re.S)
    if not match:
        raise FundAlertError(f"{code} 实时估值接口返回格式异常")
    data = json.loads(match.group(1))
    quote_time = parse_cn_time(data["gztime"])
    return DirectFundChange(
        code=code,
        name=fallback_name or data.get("name") or code,
        change_pct=float(data["gszzl"]),
        estimate_value=str(data.get("gsz", "")),
        nav_date=str(data.get("jzrq", "")),
        quote_time=quote_time,
    )


def extract_f10_content(text: str) -> str:
    match = re.search(r'content:"(?P<content>.*)",arryear:', text, flags=re.S)
    if not match:
        raise FundAlertError("持仓接口返回格式异常")
    content = match.group("content")
    content = content.replace(r"\/", "/")
    content = content.replace(r"\"", '"')
    return html.unescape(content)


def parse_holdings(content: str) -> tuple[str, list[Holding]]:
    date_match = re.search(r"截止至：<font[^>]*>([^<]+)</font>", content)
    holdings_date = date_match.group(1).strip() if date_match else "未知"
    rows = re.findall(r"<tr>(.*?)</tr>", content, flags=re.S)
    holdings: list[Holding] = []
    for row in rows:
        secid_match = re.search(r"quote\.eastmoney\.com/unify/r/([0-9]+\.[0-9A-Za-z]+)", row)
        if not secid_match:
            continue
        name_match = re.search(r"<td class='tol'>\s*<a [^>]*>([^<]+)</a>", row, flags=re.S)
        weight_match = re.search(r"<td class='tor'>([0-9,.]+)%</td>", row)
        if not name_match or not weight_match:
            continue
        secid = secid_match.group(1)
        code = secid.split(".", 1)[1]
        holdings.append(
            Holding(
                secid=secid,
                code=code,
                name=html.unescape(name_match.group(1)).strip(),
                weight_pct=float(weight_match.group(1).replace(",", "")),
            )
        )
    if not holdings:
        raise FundAlertError("未解析到可用于估算的持仓")
    return holdings_date, holdings


def get_fund_holdings(code: str) -> tuple[str, list[Holding]]:
    url = (
        "https://fundf10.eastmoney.com/FundArchivesDatas.aspx?"
        f"type=jjcc&code={urllib.parse.quote(code)}&topline=10&year=&month=&rt={time.time()}"
    )
    return parse_holdings(extract_f10_content(http_get(url)))


def get_single_quote(secid: str) -> HoldingQuote | None:
    fields = "f57,f58,f170,f86"
    url = (
        "https://push2.eastmoney.com/api/qt/stock/get?"
        f"secid={urllib.parse.quote(secid, safe='.')}&fltt=2&fields={fields}"
    )
    data = json.loads(http_get(url))
    row = data.get("data") or {}
    if row.get("f170") in (None, "-") or row.get("f86") in (None, 0, "-"):
        return None
    return HoldingQuote(
        secid=secid,
        code=str(row.get("f57") or secid.split(".", 1)[1]),
        name=str(row.get("f58") or secid),
        change_pct=float(row["f170"]),
        quote_time=parse_epoch_cn(row["f86"]),
    )


def get_quotes(secids: list[str]) -> dict[str, HoldingQuote]:
    if not secids:
        return {}
    fields = "f12,f14,f3,f124"
    url = (
        "https://push2.eastmoney.com/api/qt/ulist.np/get?"
        f"fltt=2&fields={fields}&secids={urllib.parse.quote(','.join(secids), safe=',.')}"
    )
    try:
        data = json.loads(http_get(url))
        rows = data.get("data", {}).get("diff", [])
        quotes: dict[str, HoldingQuote] = {}
        by_code = {secid.split(".", 1)[1]: secid for secid in secids}
        for row in rows:
            code = str(row.get("f12"))
            secid = by_code.get(code)
            if not secid or row.get("f3") in (None, "-"):
                continue
            quotes[secid] = HoldingQuote(
                secid=secid,
                code=code,
                name=str(row.get("f14") or code),
                change_pct=float(row["f3"]),
                quote_time=parse_epoch_cn(row["f124"]),
            )
        if quotes:
            return quotes
    except FundAlertError:
        pass

    quotes = {}
    for secid in secids:
        quote = get_single_quote(secid)
        if quote:
            quotes[secid] = quote
        time.sleep(0.15)
    return quotes


def estimate_fund_from_holdings(code: str, name: str) -> EstimatedFundChange:
    holdings_date, holdings = get_fund_holdings(code)
    quotes = get_quotes([holding.secid for holding in holdings])
    pairs: list[tuple[Holding, HoldingQuote]] = []
    for holding in holdings:
        quote = quotes.get(holding.secid)
        if quote:
            pairs.append((holding, quote))
    if not pairs:
        raise FundAlertError(f"{code} 未获取到持仓实时行情")
    covered_weight = sum(holding.weight_pct for holding, _ in pairs)
    estimate = sum(holding.weight_pct * quote.change_pct for holding, quote in pairs) / 100.0
    normalized = (
        sum(holding.weight_pct * quote.change_pct for holding, quote in pairs) / covered_weight
        if covered_weight
        else 0.0
    )
    quote_time = min(quote.quote_time for _, quote in pairs)
    return EstimatedFundChange(
        code=code,
        name=name,
        estimate_pct=estimate,
        normalized_top_holdings_pct=normalized,
        covered_weight_pct=covered_weight,
        holdings_date=holdings_date,
        quote_time=quote_time,
        holdings=pairs,
    )


def is_trading_day_for_payload(changes: list[DirectFundChange | EstimatedFundChange]) -> bool:
    today = now_cn().date()
    return any(change.quote_time.date() == today for change in changes)


def build_message(
    direct_changes: list[DirectFundChange],
    estimated_changes: list[EstimatedFundChange],
    failed: list[FailedFund] | None = None,
) -> str:
    now_label = now_cn().strftime("%Y-%m-%d %H:%M")
    lines = [f"基金盘中涨跌幅提醒 {now_label}", ""]

    for item in direct_changes:
        lines.append(
            f"{item.code} {item.name}: {signed_pct(item.change_pct)} "
            f"(估值 {item.estimate_value}, 行情 {item.quote_time.strftime('%H:%M')})"
        )

    for item in estimated_changes:
        lines.append(
            f"{item.code} {item.name}: {signed_pct(item.estimate_pct)} 估算 "
            f"(前十大覆盖 {item.covered_weight_pct:.2f}%, 持仓 {item.holdings_date}, "
            f"行情 {item.quote_time.strftime('%H:%M')})"
        )
        lines.append(
            f"  前十大归一化涨跌: {signed_pct(item.normalized_top_holdings_pct)}"
        )

    if failed:
        lines.append("")
        for f in failed:
            lines.append(f"{f.code} {f.name}: 估算不可用（{f.reason}）")

    if estimated_changes:
        lines.append("")
        lines.append("说明: 007355 按披露前十大持仓净值占比乘实时涨跌幅估算，未披露持仓和仓位变化按 0 处理。")
    return "\n".join(lines)


def parse_direct(value: str | None) -> list[tuple[str, str]]:
    if not value:
        return []
    pairs: list[tuple[str, str]] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            code, name = item.split(":", 1)
            pairs.append((code.strip(), name.strip()))
        else:
            pairs.append((item, item))
    return pairs


def parse_estimated(value: str | None) -> list[tuple[str, str]]:
    if not value:
        return []
    pairs: list[tuple[str, str]] = []
    for item in value.split(","):
        item = item.strip()
        if not item:
            continue
        if ":" in item:
            code, name = item.split(":", 1)
            pairs.append((code.strip(), name.strip()))
        else:
            pairs.append((item, item))
    return pairs


def collect(
    direct_funds: list[tuple[str, str]], estimated_funds: list[tuple[str, str]]
) -> tuple[list[DirectFundChange], list[EstimatedFundChange], list[FailedFund], bool]:
    """Resilient gather for programmatic use (e.g. the e-ink renderer).

    Same per-fund fault tolerance as run() but returns structured results
    instead of printing. is_trading is False when no fund quote is dated today
    (non-trading day) or when every fund failed.
    """
    direct_changes: list[DirectFundChange] = []
    estimated_changes: list[EstimatedFundChange] = []
    failed: list[FailedFund] = []

    for code, name in direct_funds:
        try:
            direct_changes.append(get_direct_fund_change(code, name))
        except Exception as exc:
            failed.append(FailedFund(code, name, str(exc)))

    for code, name in estimated_funds:
        try:
            estimated_changes.append(estimate_fund_from_holdings(code, name))
        except Exception as exc:
            failed.append(FailedFund(code, name, str(exc)))

    all_changes: list[DirectFundChange | EstimatedFundChange] = [*direct_changes, *estimated_changes]
    is_trading = bool(all_changes) and is_trading_day_for_payload(all_changes)
    return direct_changes, estimated_changes, failed, is_trading


def run(direct_funds: list[tuple[str, str]], estimated_funds: list[tuple[str, str]]) -> int:
    direct_changes: list[DirectFundChange] = []
    estimated_changes: list[EstimatedFundChange] = []
    failed: list[FailedFund] = []

    # Direct funds (fundgz) - resilient: one failing doesn't abort the rest.
    for code, name in direct_funds:
        try:
            direct_changes.append(get_direct_fund_change(code, name))
        except Exception as exc:
            failed.append(FailedFund(code, name, str(exc)))

    # Estimated funds (fundf10 + push2 quotes) - resilient: push2 being down
    # for one fund must not kill the whole alert.
    for code, name in estimated_funds:
        try:
            estimated_changes.append(estimate_fund_from_holdings(code, name))
        except Exception as exc:
            failed.append(FailedFund(code, name, str(exc)))

    all_changes: list[DirectFundChange | EstimatedFundChange] = [*direct_changes, *estimated_changes]

    # Nothing succeeded at all -> genuine failure, do not send.
    if not all_changes:
        reasons = "; ".join(f"{f.code}: {f.reason}" for f in failed) if failed else "no funds configured"
        print(f"运行失败: 全部基金数据源获取失败 -> {reasons}", file=sys.stderr)
        return 1

    if not is_trading_day_for_payload(all_changes):
        print("SKIP_NON_TRADING: 行情时间不是今天，判断为非证券交易日，跳过发送。")
        return 0

    print(build_message(direct_changes, estimated_changes, failed))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Fund intraday change alert (prints message, does not send).")
    parser.add_argument("--direct", help="Comma-separated CODE:NAME for direct-estimate funds, e.g. 160419:金鹰信息产业股票A,003015:中海医疗保健股票A")
    parser.add_argument("--estimated", help="Comma-separated CODE:NAME for holdings-estimated funds, e.g. 007355:汇添富科技创新混合A")
    args = parser.parse_args()

    direct_funds = parse_direct(args.direct)
    estimated_funds = parse_estimated(args.estimated)
    if not direct_funds and not estimated_funds:
        print("error: provide at least --direct or --estimated", file=sys.stderr)
        return 2

    try:
        return run(direct_funds, estimated_funds)
    except Exception as exc:
        print(f"运行失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
