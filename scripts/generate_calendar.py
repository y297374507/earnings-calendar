#!/usr/bin/env python3
"""
Generate earnings-calendar ICS + a beautiful index.html
- US stocks via Finnhub (+ yfinance fallback)
- A-shares via AKShare
"""

import os
import sys
from datetime import date, timedelta, datetime, timezone
from pathlib import Path
import pandas as pd
import requests

# ────────────────────────────────────────────────────────────────────────────────
# Config
API = "https://finnhub.io/api/v1/calendar/earnings"
TOKEN = os.getenv("FINNHUB_TOKEN")
WATCHLIST_FILE = Path(__file__).parent.parent / "watchlist.txt"
WATCHLIST_CN_FILE = Path(__file__).parent.parent / "watchlist_cn.txt"
LOOKBEHIND_DAYS = 15
LOOKAHEAD_DAYS = 90
TODAY = date.today()
FROM = (TODAY - timedelta(days=LOOKBEHIND_DAYS)).isoformat()
TO = (TODAY + timedelta(days=LOOKAHEAD_DAYS)).isoformat()

# 你的仓库固定链接（已写死，方便直接用）
RAW_ICS_URL = "https://raw.githubusercontent.com/y297374507/earnings-calendar/main/earnings_calendar.ics"
WEBCAL_URL = "webcal://raw.githubusercontent.com/y297374507/earnings-calendar/main/earnings_calendar.ics"

def get_cn_periods() -> list[str]:
    year = TODAY.year
    month = TODAY.month
    periods = []
    if month <= 4:
        periods.append(f"{year - 1}年报")
        periods.append(f"{year}一季")
    elif month <= 8:
        periods.append(f"{year}一季")
        periods.append(f"{year - 1}年报")
    elif month <= 10:
        periods.append(f"{year}三季")
    else:
        periods.append(f"{year}三季")
        periods.append(f"{year}年报")
    return periods

def load_watchlist() -> set[str]:
    if not WATCHLIST_FILE.exists():
        print(f"⚠️ Watchlist file not found: {WATCHLIST_FILE}")
        return set()
    symbols = set()
    with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                symbols.add(line.upper())
    return symbols

def load_watchlist_cn() -> set[str]:
    if not WATCHLIST_CN_FILE.exists():
        print(f"⚠️ A-share watchlist file not found: {WATCHLIST_CN_FILE}")
        return set()
    symbols = set()
    with open(WATCHLIST_CN_FILE, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                code = line.replace("sh", "").replace("sz", "").replace("bj", "")
                symbols.add(code)
    return symbols

def fmt_number(num):
    if num in (None, 0, "0"):
        return "-"
    try:
        n = float(num)
    except (ValueError, TypeError):
        return "-"
    if n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f} B"
    if n >= 1_000_000:
        return f"{n / 1_000_000:.0f} M"
    return f"{n:.0f}"

def fetch_earnings() -> list[dict]:
    if not TOKEN:
        raise RuntimeError("FINNHUB_TOKEN env-var is missing.")
    chunk_size = 15
    start_date = date.fromisoformat(FROM)
    end_date = date.fromisoformat(TO)
    all_records = []
    current = start_date
    while current < end_date:
        chunk_end = min(current + timedelta(days=chunk_size), end_date)
        params = {"from": current.isoformat(), "to": chunk_end.isoformat(), "token": TOKEN}
        resp = requests.get(API, params=params, timeout=30)
        resp.raise_for_status()
        records = resp.json().get("earningsCalendar", [])
        all_records.extend(records)
        print(f" 📥 {current.isoformat()} ~ {chunk_end.isoformat()}: {len(records)} records")
        current = chunk_end
    seen = set()
    unique_records = []
    for r in all_records:
        key = (r.get("symbol"), r.get("date"))
        if key not in seen:
            seen.add(key)
            unique_records.append(r)
    return unique_records

def fetch_yfinance_earnings(watchlist: set[str], existing_symbols: set[str]) -> list[dict]:
    import time as _time
    import yfinance as yf
    missing = watchlist - existing_symbols
    if not missing:
        return []
    from_date = TODAY - timedelta(days=LOOKBEHIND_DAYS)
    to_date = TODAY + timedelta(days=LOOKAHEAD_DAYS)
    records: list[dict] = []
    print(f"\n 🐍 yfinance fallback for {len(missing)} tickers…")
    for i, symbol in enumerate(sorted(missing), 1):
        try:
            ticker = yf.Ticker(symbol)
            cal = ticker.calendar
        except Exception as e:
            print(f" [!] {symbol}: {e}")
            _time.sleep(0.5)
            continue
        if not cal:
            print(f" [!] {symbol}: empty calendar data")
            _time.sleep(0.5)
            continue
        raw_dates = cal.get("Earnings Date")
        if not raw_dates:
            _time.sleep(0.5)
            continue
        event_date = raw_dates[0]
        if isinstance(event_date, datetime):
            event_date = event_date.date()
        if not (from_date <= event_date <= to_date):
            _time.sleep(0.5)
            continue
        records.append({
            "symbol": symbol,
            "date": event_date.isoformat(),
            "hour": "",
            "quarter": "",
            "epsEstimate": cal.get("Earnings Average"),
            "revenueEstimate": cal.get("Revenue Average"),
            "source": "yf",
        })
        print(f" [{i}/{len(missing)}] {symbol}: {event_date}")
        _time.sleep(0.5)
    print(f" 🐍 yfinance found {len(records)} tickers")
    return records

def fetch_cn_earnings(watchlist_cn: set[str]) -> list[dict]:
    if not watchlist_cn:
        print(" 🇨🇳 No A-share watchlist configured")
        return []
    try:
        import akshare as ak
    except ImportError:
        print(" ⚠️ AKShare not installed, skipping A-share data")
        return []
    periods = get_cn_periods()
    all_records = []
    for period in periods:
        try:
            print(f" 🇨🇳 获取 {period} 财报披露时间...")
            df = ak.stock_report_disclosure(market="沪深京", period=period)
            df_filtered = df[df["股票代码"].isin(watchlist_cn)]
            for _, row in df_filtered.iterrows():
                disclosure_date = row.get("实际披露")
                if pd.isna(disclosure_date):
                    disclosure_date = row.get("首次预约")
                if pd.isna(disclosure_date):
                    continue
                if isinstance(disclosure_date, date):
                    event_date = disclosure_date
                else:
                    try:
                        event_date = pd.to_datetime(disclosure_date).date()
                    except:
                        continue
                event_date = event_date - timedelta(days=1)  # A股通常前一晚发布
                from_date = TODAY - timedelta(days=LOOKBEHIND_DAYS)
                to_date = TODAY + timedelta(days=LOOKAHEAD_DAYS)
                if event_date < from_date or event_date > to_date:
                    continue
                report_type = period.replace("年", "年").replace("季", "季报")
                if "报" not in report_type:
                    report_type += "报"
                record = {
                    "symbol": row["股票代码"],
                    "name": row["股票简称"],
                    "date": event_date.isoformat(),
                    "period": period,
                    "report_type": report_type,
                    "source": "cn",
                }
                all_records.append(record)
            print(f" {period}: {len(df_filtered)} 条匹配")
        except Exception as e:
            print(f" {period}: 错误 - {e}")
    return all_records

def escape_ics_text(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace(";", r"\;")
        .replace(",", r"\,")
        .replace("\n", r"\n")
    )

def fold_ics_line(line: str, width: int = 75) -> list[str]:
    if len(line) <= width:
        return [line]
    folded = [line[:width]]
    rest = line[width:]
    while rest:
        folded.append(f" {rest[: width - 1]}")
        rest = rest[width - 1 :]
    return folded

def to_event_lines(item: dict, dtstamp: str) -> list[str]:
    symbol = item.get("symbol", "UNKNOWN")
    event_date = datetime.fromisoformat(item["date"]).date()
    end_date = event_date + timedelta(days=1)
    uid = f"{symbol}-{event_date.isoformat()}@earning-calendar-ics"
    hour = item.get("hour", "")
    hour_map = {"bmo": "盘前", "amc": "盘后", "": ""}
    timing = hour_map.get(hour, "")
    summary = f"{symbol} Earnings"
    if timing:
        summary = f"{symbol} Earnings ({timing})"
    source_label = "yfinance" if item.get("source") == "yf" else "Finnhub (non-GAAP)"
    description = "\n".join([
        f"Ticker: {symbol}",
        f"Fiscal Qtr: {item.get('quarter', '-')}",
        f"Timing: {timing if timing else '未指定'}",
        f"Estimate EPS: {item.get('epsEstimate') if item.get('epsEstimate') is not None else '-'}",
        f"Est. Revenue: {fmt_number(item.get('revenueEstimate'))}",
        f"Source: {source_label}",
    ])
    return [
        "BEGIN:VEVENT",
        f"UID:{escape_ics_text(uid)}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART;VALUE=DATE:{event_date.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{end_date.strftime('%Y%m%d')}",
        f"SUMMARY:{escape_ics_text(summary)}",
        f"DESCRIPTION:{escape_ics_text(description)}",
        "END:VEVENT",
    ]

def to_cn_event_lines(item: dict, dtstamp: str) -> list[str]:
    symbol = item.get("symbol", "UNKNOWN")
    name = item.get("name", "")
    event_date = datetime.fromisoformat(item["date"]).date()
    end_date = event_date + timedelta(days=1)
    uid = f"CN-{symbol}-{event_date.isoformat()}@earning-calendar-ics"
    report_type = item.get("report_type", "财报")
    summary = f"{name} {report_type}"
    description = "\n".join([
        f"股票代码: {symbol}",
        f"股票简称: {name}",
        f"报告类型: {report_type}",
        f"披露日期: {event_date.isoformat()}",
        "Source: AKShare (东方财富)",
    ])
    return [
        "BEGIN:VEVENT",
        f"UID:{escape_ics_text(uid)}",
        f"DTSTAMP:{dtstamp}",
        f"DTSTART;VALUE=DATE:{event_date.strftime('%Y%m%d')}",
        f"DTEND;VALUE=DATE:{end_date.strftime('%Y%m%d')}",
        f"SUMMARY:{escape_ics_text(summary)}",
        f"DESCRIPTION:{escape_ics_text(description)}",
        "END:VEVENT",
    ]

def build_calendar(records: list[dict]) -> str:
    dtstamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//earning-calendar-ics//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:Earnings Calendar",
    ]
    for rec in sorted(records, key=lambda r: (r.get("date", ""), r.get("symbol", ""))):
        if not rec.get("date"):
            continue
        if rec.get("source") == "cn":
            lines.extend(to_cn_event_lines(rec, dtstamp))
        else:
            lines.extend(to_event_lines(rec, dtstamp))
    lines.append("END:VCALENDAR")
    folded_lines: list[str] = []
    for line in lines:
        folded_lines.extend(fold_ics_line(line))
    return "\r\n".join(folded_lines) + "\r\n"

def build_index_html(records: list[dict]) -> str:
    us_count = len([r for r in records if r.get("source") != "cn"])
    cn_count = len([r for r in records if r.get("source") == "cn"])
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    sorted_recs = sorted(
        [r for r in records if r.get("date")],
        key=lambda r: (r["date"], r.get("symbol", ""))
    )

    rows = []
    for r in sorted_recs:
        d = r["date"]
        if r.get("source") == "cn":
            name = r.get("name", "")
            symbol = r.get("symbol", "")
            report = r.get("report_type", "财报")
            timing = ""
            eps = "-"
            rev = "-"
            market = "A股"
            badge = "cn"
        else:
            name = r.get("symbol", "")
            symbol = r.get("symbol", "")
            report = f"Q{r.get('quarter', '-')}" if r.get("quarter") else "Earnings"
            hour = r.get("hour", "")
            timing = {"bmo": "盘前", "amc": "盘后"}.get(hour, "")
            eps = r.get("epsEstimate") if r.get("epsEstimate") is not None else "-"
            rev = fmt_number(r.get("revenueEstimate"))
            market = "US"
            badge = "us"

        rows.append(f"""
        <tr class="event-row" data-market="{badge}" data-date="{d}">
            <td class="date">{d}</td>
            <td>
                <span class="badge {badge}">{market}</span>
                <strong>{name}</strong>
                <span class="symbol">{symbol}</span>
            </td>
            <td>{report}</td>
            <td>{timing or "-"}</td>
            <td class="num">{eps}</td>
            <td class="num">{rev}</td>
        </tr>""")

    table_body = "\n".join(rows) if rows else '<tr><td colspan="6" style="text-align:center;padding:2rem;">暂无事件</td></tr>'

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Earnings Calendar · 财报日历</title>
<meta name="description" content="自动更新的美股 + A股财报日历">
<style>
  :root {{
    --bg: #0f1117;
    --card: #1a1d27;
    --border: #2a2e3a;
    --text: #e6e8ef;
    --muted: #8b90a0;
    --accent: #3b82f6;
    --us: #22c55e;
    --cn: #f59e0b;
    --hover: #252836;
  }}
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
    background: var(--bg);
    color: var(--text);
    line-height: 1.5;
    min-height: 100vh;
  }}
  .container {{ max-width: 1100px; margin: 0 auto; padding: 1.5rem; }}
  header {{
    display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center;
    gap: 1rem; margin-bottom: 1.5rem; padding-bottom: 1rem; border-bottom: 1px solid var(--border);
  }}
  h1 {{ font-size: 1.6rem; font-weight: 700; }}
  .meta {{ color: var(--muted); font-size: 0.9rem; }}
  .stats {{ display: flex; gap: 1rem; flex-wrap: wrap; }}
  .stat {{
    background: var(--card); border: 1px solid var(--border); border-radius: 10px;
    padding: 0.6rem 1rem; font-size: 0.9rem;
  }}
  .stat strong {{ color: var(--accent); }}
  .actions {{ display: flex; gap: 0.75rem; flex-wrap: wrap; margin: 1.2rem 0; }}
  a.btn {{
    display: inline-flex; align-items: center; gap: 0.4rem;
    background: var(--accent); color: white; text-decoration: none;
    padding: 0.55rem 1rem; border-radius: 8px; font-weight: 600; font-size: 0.9rem;
    transition: opacity 0.15s;
  }}
  a.btn:hover {{ opacity: 0.9; }}
  a.btn.secondary {{ background: var(--card); border: 1px solid var(--border); color: var(--text); }}
  .filters {{
    display: flex; gap: 0.5rem; margin-bottom: 1rem; flex-wrap: wrap;
  }}
  .filter-btn {{
    background: var(--card); border: 1px solid var(--border); color: var(--muted);
    padding: 0.35rem 0.85rem; border-radius: 20px; cursor: pointer; font-size: 0.85rem;
  }}
  .filter-btn.active {{ background: var(--accent); color: white; border-color: var(--accent); }}
  .table-wrap {{
    background: var(--card); border: 1px solid var(--border); border-radius: 12px; overflow: hidden;
  }}
  table {{ width: 100%; border-collapse: collapse; font-size: 0.92rem; }}
  th {{
    text-align: left; padding: 0.85rem 1rem; background: #151820;
    color: var(--muted); font-weight: 600; font-size: 0.8rem; text-transform: uppercase;
    letter-spacing: 0.03em; border-bottom: 1px solid var(--border);
  }}
  td {{ padding: 0.75rem 1rem; border-bottom: 1px solid var(--border); }}
  tr:last-child td {{ border-bottom: none; }}
  tr:hover td {{ background: var(--hover); }}
  .badge {{
    display: inline-block; font-size: 0.7rem; font-weight: 700;
    padding: 0.15rem 0.45rem; border-radius: 4px; margin-right: 0.4rem; vertical-align: middle;
  }}
  .badge.us {{ background: rgba(34,197,94,0.15); color: var(--us); }}
  .badge.cn {{ background: rgba(245,158,11,0.15); color: var(--cn); }}
  .symbol {{ color: var(--muted); font-size: 0.85rem; margin-left: 0.3rem; }}
  .num {{ font-variant-numeric: tabular-nums; font-family: ui-monospace, monospace; }}
  .date {{ font-weight: 600; white-space: nowrap; }}
  footer {{
    margin-top: 2rem; text-align: center; color: var(--muted); font-size: 0.85rem;
  }}
  @media (max-width: 700px) {{
    th:nth-child(4), td:nth-child(4),
    th:nth-child(5), td:nth-child(5),
    th:nth-child(6), td:nth-child(6) {{ display: none; }}
    h1 {{ font-size: 1.3rem; }}
  }}
</style>
</head>
<body>
  <div class="container">
    <header>
      <div>
        <h1>📅 Earnings Calendar</h1>
        <div class="meta">美股 + A股财报日历 · 自动更新</div>
      </div>
      <div class="stats">
        <div class="stat">总计 <strong>{len(sorted_recs)}</strong> 事件</div>
        <div class="stat">美股 <strong>{us_count}</strong></div>
        <div class="stat">A股 <strong>{cn_count}</strong></div>
      </div>
    </header>

    <div class="actions">
      <a class="btn" href="earnings_calendar.ics" download>⬇️ 下载 .ics</a>
      <a class="btn secondary" href="{WEBCAL_URL}">📱 订阅到日历</a>
      <a class="btn secondary" href="{RAW_ICS_URL}" target="_blank">🔗 原始 .ics 链接</a>
    </div>

    <div class="filters">
      <button class="filter-btn active" data-filter="all">全部</button>
      <button class="filter-btn" data-filter="us">美股</button>
      <button class="filter-btn" data-filter="cn">A股</button>
    </div>

    <div class="table-wrap">
      <table>
        <thead>
          <tr>
            <th>日期</th>
            <th>公司</th>
            <th>类型</th>
            <th>时段</th>
            <th>EPS 预估</th>
            <th>营收预估</th>
          </tr>
        </thead>
        <tbody id="tbody">
          {table_body}
        </tbody>
      </table>
    </div>

    <footer>
      最后更新：{updated}<br>
      数据来源：Finnhub / yfinance / AKShare · 仅供参考
    </footer>
  </div>

<script>
  const btns = document.querySelectorAll('.filter-btn');
  const rows = document.querySelectorAll('.event-row');
  btns.forEach(btn => {{
    btn.addEventListener('click', () => {{
      btns.forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      const f = btn.dataset.filter;
      rows.forEach(row => {{
        row.style.display = (f === 'all' || row.dataset.market === f) ? '' : 'none';
      }});
    }});
  }});
</script>
</body>
</html>"""

def main() -> None:
    all_records = []

    print("🇺🇸 获取美股财报...")
    watchlist = load_watchlist()
    us_records = fetch_earnings()
    if watchlist:
        filtered = [r for r in us_records if r.get("symbol", "").upper() in watchlist]
        found_symbols = {r.get("symbol", "").upper() for r in filtered}
        print(f"📋 美股 Watchlist: {len(watchlist)} symbols, matched {len(filtered)} events")
        all_records.extend(filtered)
        yf_records = fetch_yfinance_earnings(watchlist, found_symbols)
        yf_seen = set()
        for r in yf_records:
            key = (r.get("symbol"), r.get("date"))
            if key not in yf_seen:
                yf_seen.add(key)
                all_records.append(r)
    else:
        print(f"📋 No US watchlist configured, using all {len(us_records)} events")
        all_records.extend(us_records)

    print()
    print("🇨🇳 获取A股财报...")
    watchlist_cn = load_watchlist_cn()
    cn_records = fetch_cn_earnings(watchlist_cn)
    print(f"📋 A股 Watchlist: {len(watchlist_cn)} symbols, matched {len(cn_records)} events")
    all_records.extend(cn_records)

    # 输出 ICS
    with open("earnings_calendar.ics", "w", encoding="utf-8") as f:
        f.write(build_calendar(all_records))

    # 输出 Index.html（重点！）
    with open("index.html", "w", encoding="utf-8") as f:
        f.write(build_index_html(all_records))

    us_cnt = len([r for r in all_records if r.get("source") != "cn"])
    cn_cnt = len([r for r in all_records if r.get("source") == "cn"])
    print()
    print(f"✅ Calendar refreshed ({len(all_records)} events: {us_cnt} US + {cn_cnt} CN)")
    print(f"   → earnings_calendar.ics")
    print(f"   → index.html")

if __name__ == "__main__":
    try:
        main()
    except Exception as exc:
        print("💥 Script failed:", exc)
        sys.exit(1)
