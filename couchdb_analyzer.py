#!/usr/bin/env python3
"""
CouchDB Log Analyzer — Parser + ASCII Dashboard + Power BI export
Usage: python couchdb_analyzer.py [logfile] [--export csv|json] [--top N] [--live]
"""

import re
import sys
import csv
import json
import argparse
import random
from datetime import datetime, timedelta
from collections import defaultdict, Counter
from pathlib import Path
from dataclasses import dataclass, field, asdict
from typing import Optional

# ─── CouchDB log line regex ─────────────────────────────────────────────────
LOG_RE = re.compile(
    r'\[(?P<level>\w+)\]\s+'
    r'(?P<ts>\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})?)\s+'
    r'(?:(?P<node>\S+)\s+)?'
    r'(?P<pid><\d+\.\d+\.\d+>)\s+'
    r'(?P<db>\w[\w._-]*)?\s*'
    r'(?P<method>GET|POST|PUT|DELETE|HEAD|COPY)?\s*'
    r'(?P<path>/[^\s]*)?\s*'
    r'(?:(?P<status>\d{3})\s+)?'
    r'(?:ok=(?P<ok>true|false)\s+)?'
    r'(?P<msg>.*)?'
)

HTTP_RE = re.compile(
    r'"(?P<method>GET|POST|PUT|DELETE|HEAD|COPY)\s+(?P<path>[^\s"]+)[^"]*"\s+'
    r'(?P<status>\d{3})\s+'
    r'(?P<duration>[\d.]+)'
)

@dataclass
class LogEntry:
    ts: datetime
    level: str
    method: Optional[str]
    path: Optional[str]
    status: Optional[int]
    duration_ms: Optional[float]
    db: Optional[str]
    raw: str

@dataclass
class Stats:
    total: int = 0
    errors: int = 0
    by_level: Counter = field(default_factory=Counter)
    by_method: Counter = field(default_factory=Counter)
    by_status: Counter = field(default_factory=Counter)
    by_db: Counter = field(default_factory=Counter)
    slowest: list = field(default_factory=list)
    timeline: defaultdict = field(default_factory=lambda: defaultdict(int))
    durations: list = field(default_factory=list)

# ─── Parser ──────────────────────────────────────────────────────────────────

def parse_line(line: str) -> Optional[LogEntry]:
    """Try to parse a CouchDB log line (multiple formats supported)."""
    ts = method = path = status = duration = db = None
    level = "info"

    # Standard CouchDB format
    m = LOG_RE.search(line)
    if m:
        g = m.groupdict()
        level = g.get("level", "info").lower()
        db = g.get("db")
        method = g.get("method")
        path = g.get("path")
        raw_status = g.get("status")
        status = int(raw_status) if raw_status else None
        raw_ts = g.get("ts", "")
        try:
            raw_ts = raw_ts.rstrip("Z").replace("T", " ")[:19]
            ts = datetime.strptime(raw_ts, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            ts = datetime.now()

    # HTTP access log within the line
    h = HTTP_RE.search(line)
    if h:
        method = method or h.group("method")
        path = path or h.group("path")
        status = status or int(h.group("status"))
        try:
            duration = float(h.group("duration"))
        except ValueError:
            duration = None

    if ts is None:
        # Try to extract a timestamp of any kind
        ts_m = re.search(r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2})', line)
        if ts_m:
            try:
                ts = datetime.strptime(ts_m.group(1).replace("T", " "), "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass

    if ts is None:
        return None

    return LogEntry(ts=ts, level=level, method=method, path=path,
                    status=status, duration_ms=duration, db=db, raw=line.strip())

def analyze(entries: list[LogEntry]) -> Stats:
    s = Stats()
    for e in entries:
        s.total += 1
        s.by_level[e.level] += 1
        if e.level in ("error", "critical", "warning"):
            s.errors += 1
        if e.method:
            s.by_method[e.method] += 1
        if e.status:
            s.by_status[e.status] += 1
            if e.status >= 500:
                s.errors += 1
        if e.db:
            s.by_db[e.db] += 1
        if e.duration_ms is not None:
            s.durations.append(e.duration_ms)
            s.slowest.append((e.duration_ms, e.method, e.path, str(e.ts)))
        minute = e.ts.strftime("%H:%M")
        s.timeline[minute] += 1

    s.slowest.sort(reverse=True)
    s.slowest = s.slowest[:10]
    return s

# ─── ASCII Dashboard ──────────────────────────────────────────────────────────

COLORS = {
    "reset": "\033[0m", "bold": "\033[1m",
    "red": "\033[91m", "green": "\033[92m", "yellow": "\033[93m",
    "blue": "\033[94m", "cyan": "\033[96m", "gray": "\033[90m",
    "white": "\033[97m", "magenta": "\033[95m",
}

def c(color: str, text: str) -> str:
    return f"{COLORS.get(color,'')}{text}{COLORS['reset']}"

def bar(value: int, max_val: int, width: int = 30, color: str = "cyan") -> str:
    filled = int(width * value / max_val) if max_val else 0
    return c(color, "█" * filled) + c("gray", "░" * (width - filled))

def sparkline(values: list[int]) -> str:
    chars = " ▁▂▃▄▅▆▇█"
    if not values:
        return ""
    mx = max(values) or 1
    return "".join(chars[int(v / mx * 8)] for v in values)

def print_dashboard(stats: Stats, filename: str):
    w = 70
    print()
    print(c("bold", c("cyan", "╔" + "═" * (w-2) + "╗")))
    title = " CouchDB Log Analyzer — Dashboard "
    pad = (w - 2 - len(title)) // 2
    print(c("cyan", "║") + " " * pad + c("bold", c("white", title)) + " " * (w - 2 - pad - len(title)) + c("cyan", "║"))
    print(c("cyan", "║") + c("gray", f"  File: {filename:<{w-10}}") + c("cyan", "║"))
    print(c("bold", c("cyan", "╠" + "═" * (w-2) + "╣")))

    # Summary row
    err_rate = stats.errors / stats.total * 100 if stats.total else 0
    err_color = "red" if err_rate > 5 else "yellow" if err_rate > 1 else "green"
    avg_ms = sum(stats.durations) / len(stats.durations) if stats.durations else 0
    p95 = sorted(stats.durations)[int(len(stats.durations) * 0.95)] if stats.durations else 0

    summary_items = [
        ("Requests", c("bold", c("white", str(stats.total)))),
        ("Errors", c("bold", c(err_color, f"{stats.errors} ({err_rate:.1f}%)"))),
        ("Avg latency", c("bold", c("cyan", f"{avg_ms:.1f}ms"))),
        ("p95 latency", c("bold", c("yellow", f"{p95:.1f}ms"))),
    ]
    row = "  ".join(f"{k}: {v}" for k, v in summary_items)
    print(c("cyan", "║") + f"  {row}")
    print(c("bold", c("cyan", "╠" + "═" * (w-2) + "╣")))

    # HTTP Methods
    print(c("cyan", "║") + c("bold", "  HTTP Methods") + c("gray", "  (top verbs)"))
    max_m = max(stats.by_method.values(), default=1)
    for method, cnt in stats.by_method.most_common(5):
        color = {"GET": "green", "POST": "cyan", "PUT": "blue",
                 "DELETE": "red", "HEAD": "gray"}.get(method, "white")
        b = bar(cnt, max_m, 25, color)
        print(c("cyan", "║") + f"  {c(color, method):<18} {b} {c('white', str(cnt))}")
    print(c("cyan", "║"))

    # Status codes
    print(c("cyan", "║") + c("bold", "  HTTP Status Codes"))
    max_s = max(stats.by_status.values(), default=1)
    for code, cnt in sorted(stats.by_status.items()):
        color = "green" if code < 300 else "yellow" if code < 400 else "red"
        b = bar(cnt, max_s, 25, color)
        print(c("cyan", "║") + f"  {c(color, str(code))}  {b} {c('white', str(cnt))}")
    print(c("cyan", "║"))

    # Top databases
    if stats.by_db:
        print(c("cyan", "║") + c("bold", "  Top Databases"))
        max_d = max(stats.by_db.values(), default=1)
        for db, cnt in stats.by_db.most_common(5):
            b = bar(cnt, max_d, 25, "magenta")
            print(c("cyan", "║") + f"  {c('magenta', db[:20]):<22} {b} {c('white', str(cnt))}")
        print(c("cyan", "║"))

    # Slowest requests
    if stats.slowest:
        print(c("cyan", "║") + c("bold", "  Top 10 Slowest Requests"))
        for i, (ms, method, path, ts) in enumerate(stats.slowest[:10], 1):
            path_str = (path or "/")[:35]
            ms_color = "red" if ms > 1000 else "yellow" if ms > 200 else "green"
            verb = (method or 'UNK').ljust(7)
            print(c("cyan", "║") + f"  {c('gray', str(i)+'.'):>4} {c(ms_color, f'{ms:>8.1f}ms')}  "
                  f"{c('cyan', verb)} {c('white', path_str)}")
        print(c("cyan", "║"))

    # Timeline sparkline
    if stats.timeline:
        times = sorted(stats.timeline.keys())
        values = [stats.timeline[t] for t in times]
        spark = sparkline(values)
        print(c("cyan", "║") + c("bold", "  Request Timeline") + c("gray", f"  [{times[0]} → {times[-1]}]"))
        print(c("cyan", "║") + f"  {c('cyan', spark)}")
        print(c("cyan", "║"))

    # Log levels
    print(c("cyan", "║") + c("bold", "  Log Levels"))
    level_colors = {"info": "blue", "warning": "yellow", "error": "red",
                    "debug": "gray", "critical": "red"}
    max_l = max(stats.by_level.values(), default=1)
    for lvl, cnt in stats.by_level.most_common():
        col = level_colors.get(lvl, "white")
        b = bar(cnt, max_l, 25, col)
        print(c("cyan", "║") + f"  {c(col, lvl.upper()+'  '):<20} {b} {c('white', str(cnt))}")

    print(c("bold", c("cyan", "╚" + "═" * (w-2) + "╝")))
    print()

# ─── Export ───────────────────────────────────────────────────────────────────

def export_csv(entries: list[LogEntry], path: str):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["ts", "level", "method", "path", "status", "duration_ms", "db"])
        w.writeheader()
        for e in entries:
            w.writerow({"ts": e.ts.isoformat(), "level": e.level, "method": e.method,
                        "path": e.path, "status": e.status, "duration_ms": e.duration_ms, "db": e.db})
    print(c("green", f"  CSV exported → {path}"))

def export_json(entries: list[LogEntry], path: str):
    data = [{"ts": e.ts.isoformat(), "level": e.level, "method": e.method,
             "path": e.path, "status": e.status, "duration_ms": e.duration_ms, "db": e.db}
            for e in entries]
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    print(c("green", f"  JSON exported → {path}"))

# ─── Demo log generator ───────────────────────────────────────────────────────

def generate_demo_log(n: int = 500) -> list[str]:
    """Generate realistic fake CouchDB logs for demo."""
    methods = ["GET", "POST", "PUT", "DELETE"]
    weights = [60, 20, 15, 5]
    dbs = ["products", "users", "orders", "sessions", "analytics"]
    paths = ["/db/_all_docs", "/db/_find", "/db/doc", "/db/_bulk_docs", "/db/_changes"]
    statuses = [200, 200, 200, 201, 304, 400, 401, 404, 500, 503]
    status_weights = [50, 30, 20, 10, 5, 2, 2, 4, 1, 1]
    levels = ["info", "info", "info", "warning", "error"]
    lines = []
    base = datetime.now() - timedelta(hours=2)
    for i in range(n):
        ts = base + timedelta(seconds=i * 14 + random.randint(0, 13))
        method = random.choices(methods, weights)[0]
        db = random.choice(dbs)
        path = random.choice(paths).replace("db", db)
        status = random.choices(statuses, status_weights)[0]
        duration = round(random.lognormvariate(3.5, 1.2), 2)
        level = random.choices(levels)[0]
        pid = f"<{random.randint(100,999)}.{random.randint(1,99)}.{random.randint(1,9)}>"
        lines.append(
            f'[{level.upper()}] {ts.strftime("%Y-%m-%dT%H:%M:%S")}Z node1@127.0.0.1 {pid} '
            f'{db} "{method} {path} HTTP/1.1" {status} {duration}'
        )
    return lines

# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="CouchDB Log Analyzer")
    parser.add_argument("logfile", nargs="?", help="Path to CouchDB log file (omit for demo)")
    parser.add_argument("--export", choices=["csv", "json", "both"], help="Export format for Power BI")
    parser.add_argument("--top", type=int, default=10, help="Top N items to show")
    parser.add_argument("--demo", action="store_true", help="Run with generated demo data")
    args = parser.parse_args()

    if args.logfile and not args.demo:
        p = Path(args.logfile)
        if not p.exists():
            print(c("red", f"File not found: {args.logfile}"))
            sys.exit(1)
        lines = p.read_text(errors="replace").splitlines()
        filename = args.logfile
    else:
        print(c("yellow", "  No log file provided — running with demo data (500 generated entries)"))
        lines = generate_demo_log(500)
        filename = "demo_couchdb.log"

    entries = [e for line in lines if (e := parse_line(line))]
    if not entries:
        print(c("red", "  No parseable log entries found."))
        sys.exit(1)

    print(c("gray", f"  Parsed {len(entries)}/{len(lines)} lines"))

    stats = analyze(entries)
    print_dashboard(stats, filename)

    if args.export in ("csv", "both"):
        export_csv(entries, filename.replace(".log", "") + "_powerbi.csv")
    if args.export in ("json", "both"):
        export_json(entries, filename.replace(".log", "") + "_powerbi.json")

if __name__ == "__main__":
    main()
