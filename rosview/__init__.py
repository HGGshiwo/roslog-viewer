#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rosview — terminal UI for browsing ROS log files, htop/vim style.

Views:
  sessions  pick a run directory under ~/.ros/log
  nodes     per-node log counts, Enter to open
  log       scrollable log view with search / level filter / follow / wrap

Keys are vim-like (j/k/g/G/Ctrl+d/u) plus arrow keys, PgUp/PgDn.
"""
import argparse
import bisect
import curses
import glob
import locale
import os
import re
import sys
import time
import unicodedata

VERSION = "1.0.3"
MAX_LINES_PER_FILE = 200_000      # keep last N lines per file
GETCH_TIMEOUT_MS = 400            # poll interval for follow mode
LOAD_FIRST_MS = 250               # parse budget before showing the UI
LOAD_STEP_MS = 120                # background parse budget per idle tick
VIS_BUDGET_MS = 70                # visual-line build budget per idle tick

SEV_DEBUG, SEV_INFO, SEV_WARN, SEV_ERROR, SEV_FATAL = 1, 2, 3, 4, 5
SEV_NAMES = {SEV_DEBUG: "DEBUG", SEV_INFO: "INFO", SEV_WARN: "WARN",
             SEV_ERROR: "ERROR", SEV_FATAL: "FATAL"}
SEV_BY_NAME = {"DEBUG": SEV_DEBUG, "INFO": SEV_INFO, "WARN": SEV_WARN,
               "WARNING": SEV_WARN, "ERROR": SEV_ERROR, "FATAL": SEV_FATAL,
               "ERR": SEV_ERROR, "FTL": SEV_FATAL}
ALL_NODE = "(all)"
UNKNOWN_NODE = "(unknown)"

ROS_LOG_ROOT = os.path.join(
    os.environ.get("ROS_HOME", os.path.expanduser("~")), ".ros", "log")

# ROS1/ROS2 rosout.log:  [INFO] [WallTime: 1234.5] [/node]: msg
ROSOUT_RE = re.compile(
    r'^\s*\[(?P<sev>DEBUG|INFO|WARN|WARNING|ERROR|ERR|FATAL|FTL)\]'
    r'\s*\[(?:WallTime:\s*)?(?P<ts>\d{9,13}\.\d{1,9})\]'
    r'(?:\s*\[(?P<node>[^\]]+)\])?\s*:?\s?(?P<msg>.*)$')
# ROS1 rosout mono format (rosout_agg written file):
#   1789018982.235271692 INFO /node [file:line(func)] [topics: ...] msg
# The integer part may be short (7 digits) when the device clock is not
# synced, so accept 1-13 digits but require a level or /node to follow.
MONO_RE = re.compile(
    r'^\s*(?P<ts>\d{1,13}\.\d{1,9})\s+'
    r'(?:(?P<sev>DEBUG|INFO|WARN|WARNING|ERROR|ERR|FATAL|FTL)\s+)?'
    r'(?P<node>/[^\s\[]+)?\s*'
    r'(?P<msg>(?:\[[^\n]*)|\S.*)$')
MONO_TOPICS_RE = re.compile(r'\s*\[topics:[^\]]*\]')
# "<ts>  Node Startup" — rosout restarts mark run boundaries
MONO_STARTUP_RE = re.compile(
    r'^\s*(?P<ts>\d{1,13}\.\d{1,9})\s+Node Startup\s*$')
#  [/node] [INFO] [1234.5]: msg
NODE_FIRST_RE = re.compile(
    r'^\s*\[(?P<node>[^\]\s][^\]]*)\]\s*'
    r'\[(?P<sev>DEBUG|INFO|WARN|WARNING|ERROR|ERR|FATAL|FTL)\]'
    r'\s*\[?(?P<ts>\d{9,13}\.\d{1,9})?\]?\s*:?\s?(?P<msg>.*)$')
# per-node console log:  [rospy.client][INFO] 2020-01-01 12:00:00,000: msg
NODEFILE_RE = re.compile(
    r'^\s*(?:\[(?P<logger>[^\]]+)\])?'
    r'\s*\[(?P<sev>DEBUG|INFO|WARN|WARNING|ERROR|ERR|FATAL|FTL)\]'
    r'\s*(?P<dt>\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}[,.]\d{1,6})\s*:?\s?'
    r'(?P<msg>.*)$')


# ---------------------------------------------------------------- helpers

def dwidth(s):
    """Display width of a string (east-asian aware)."""
    if hasattr(s, "isascii") and s.isascii():
        return len(s)
    w = 0
    for ch in s:
        w += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return w


def clip_to_width(s, width):
    """Return the longest prefix of s fitting in `width` display columns."""
    if width <= 0:
        return ""
    if len(s) <= width and (not s or s.isascii()):
        return s
    out, w = [], 0
    for ch in s:
        cw = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if w + cw > width:
            return "".join(out) + ("…" if dwidth(s) > width else "")
        out.append(ch)
        w += cw
    return "".join(out)


def clean(s):
    """Make a string safe for curses (tabs/control chars)."""
    return s.replace("\t", "    ").replace("\r", "")


def fmt_ts(ts):
    if ts is None:
        return "--:--:--"
    try:
        return time.strftime("%H:%M:%S", time.localtime(ts))
    except (ValueError, OverflowError, OSError):
        return "--:--:--"


def fmt_date_ts(ts):
    if ts is None:
        return "?"
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
    except (ValueError, OverflowError, OSError):
        return "?"


def parse_dt(s):
    """'2020-01-01 12:00:00,123' -> epoch seconds (local tz)."""
    s = s.replace("T", " ").replace(",", ".")
    for fmt in ("%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S"):
        try:
            st = time.strptime(s, fmt)
            return time.mktime(st)
        except ValueError:
            continue
    return None


def commify(n):
    return "{:,}".format(n)


def norm_node(name):
    return name.strip().lstrip("/").strip() or UNKNOWN_NODE


# ---------------------------------------------------------------- model

class Entry(object):
    __slots__ = ("ts", "sev", "node", "msg", "seq", "src")

    def __init__(self, ts, sev, node, msg, seq, src=0):
        self.ts = ts
        self.sev = sev
        self.node = node
        self.msg = msg
        self.seq = seq
        self.src = src               # loader id, for cross-file dedupe


class FileLoader(object):
    """Chunked incremental reader for one log file.

    read_chunk() decodes at most 1MB per call so the UI can parse large
    files in slices; `sent` tracks how many parsed entries the Session
    has already integrated, enabling append-only updates."""

    def __init__(self, path, default_node, counter, src=0):
        self.path = path
        self.default_node = default_node
        self.counter = counter          # shared itertools-like [n] box
        self.src = src
        self.f = open(path, "rb")
        try:
            self.file_size = os.fstat(self.f.fileno()).st_size
        except OSError:
            self.file_size = 0
        self.size = 0
        self.last = None                # last Entry, for continuation lines
        self.entries = []
        self.sent = 0                   # entries handed to the Session
        self.at_eof = False
        self.dropped_front = False      # cap truncation since last integrate
        self.truncated = False
        self._pending_b = b""

    def _new_entry(self, ts, sev, node, msg):
        e = Entry(ts, sev, node, msg, self.counter[0], self.src)
        self.counter[0] += 1
        self.entries.append(e)
        self.last = e

    def _line(self, line):
        line = clean(line.rstrip("\n"))
        m = ROSOUT_RE.match(line)
        if m:
            node = norm_node(m.group("node")) if m.group("node") \
                else self.default_node
            self._new_entry(float(m.group("ts")),
                            SEV_BY_NAME.get(m.group("sev").upper()), node,
                            m.group("msg"))
            return
        m = MONO_STARTUP_RE.match(line)
        if m:
            self._new_entry(float(m.group("ts")), None, UNKNOWN_NODE,
                            "Node Startup")
            return
        m = MONO_RE.match(line)
        if m and (m.group("sev") or m.group("node")):
            node = norm_node(m.group("node").rstrip(":")) \
                if m.group("node") else self.default_node
            msg = MONO_TOPICS_RE.sub("", m.group("msg"))
            self._new_entry(float(m.group("ts")),
                            SEV_BY_NAME.get(m.group("sev").upper(),
                                            SEV_INFO) if m.group("sev")
                            else None,
                            node, msg)
            return
        m = NODE_FIRST_RE.match(line)
        if m and m.group("ts"):
            self._new_entry(float(m.group("ts")),
                            SEV_BY_NAME.get(m.group("sev").upper()),
                            norm_node(m.group("node")), m.group("msg"))
            return
        m = NODEFILE_RE.match(line)
        if m:
            self._new_entry(parse_dt(m.group("dt")),
                            SEV_BY_NAME.get(m.group("sev").upper()),
                            self.default_node, m.group("msg"))
            return
        # continuation of a multi-line message, or unparsable raw line
        if self.last is not None:
            self.last.msg += "\n" + line
        elif line.strip():
            self._new_entry(None, None, self.default_node, line)

    def read_chunk(self):
        """Decode at most 1MB more of the file. True if it grew."""
        if self.at_eof:
            return False
        try:
            st = os.fstat(self.f.fileno())
        except OSError:
            self.at_eof = True
            return False
        if st.st_size <= self.size:
            self.at_eof = True       # caught up; poll() re-arms this
            return False
        chunk = self.f.read(1 << 20)
        if not chunk:
            self.at_eof = True
            return False
        self.size += len(chunk)
        self._feed(chunk)
        return True

    def _feed(self, raw):
        self._pending_b += raw
        lines = self._pending_b.split(b"\n")
        self._pending_b = lines.pop()
        for lb in lines:
            self._line(lb.decode("utf-8", "replace"))
        if len(self.entries) > MAX_LINES_PER_FILE:
            drop = len(self.entries) - MAX_LINES_PER_FILE
            del self.entries[:drop]
            self.sent = max(0, self.sent - drop)
            self.truncated = True
            self.dropped_front = True
            self.last = self.entries[-1] if self.entries else None

    def close(self):
        try:
            self.f.close()
        except OSError:
            pass


class Session(object):
    """One run directory: rosout.log + per-node *.log files.

    Parsing is incremental: load() opens the files and decodes a first
    slice; step() continues within a time budget so the UI stays
    responsive on huge logs. New entries are integrated append-only;
    a full re-sort happens only when timestamps jump backwards."""

    def __init__(self, rosout_path, include_node_files=True):
        self.rosout_path = os.path.abspath(rosout_path)
        self.dir = os.path.dirname(self.rosout_path)
        self.name = os.path.basename(self.dir) or self.rosout_path
        self.include_node_files = include_node_files
        self.loaders = []
        self.entries = []       # merged, sorted (grows incrementally)
        self.nodes = {}         # name -> {"counts": {}, "entries": []}
        self.truncated = False
        self.fully_loaded = False
        self.appended = []      # entries accepted since last drain
        self.dirty_order = False        # entries were re-sorted
        self._key_srcs = {}
        self._single = False    # single file: no cross-file dedupe needed
        self._last_ts = None
        self.load()

    # -- loading

    def load(self):
        for ld in self.loaders:
            ld.close()
        self.loaders = []
        self.entries = []
        self.nodes = {}
        self.truncated = False
        self.fully_loaded = False
        self.appended = []
        self.dirty_order = False
        self._key_srcs = {}
        self._last_ts = None
        counter = [0]
        src = 0
        ld = FileLoader(self.rosout_path, UNKNOWN_NODE, counter, src)
        self.loaders.append(ld)
        if self.include_node_files:
            for path in sorted(glob.glob(os.path.join(self.dir, "*.log"))):
                base = os.path.basename(path)
                if base.startswith("rosout") or base.startswith("launch"):
                    continue
                stem = base[:-4] if base.endswith(".log") else base
                stem = re.sub(r"(-\d+)+$", "", stem) or stem
                try:
                    src += 1
                    fl = FileLoader(path, norm_node(stem), counter, src)
                except OSError:
                    continue
                self.loaders.append(fl)
        self._single = len(self.loaders) == 1
        self.step(LOAD_FIRST_MS)

    def step(self, budget_ms=LOAD_STEP_MS):
        """Parse more data within a time budget. True if anything changed."""
        if self.fully_loaded:
            return False
        deadline = time.time() + budget_ms / 1000.0
        changed = False
        while True:
            progressed = False
            for ld in self.loaders:
                if ld.at_eof:
                    continue
                if ld.read_chunk():
                    changed = True
                    progressed = True
                self._integrate(ld)
            if not progressed:
                self.fully_loaded = True
                break
            if time.time() >= deadline:
                break
        return changed

    def poll(self):
        """Follow mode: pick up appended data. True if changed."""
        changed = False
        for ld in self.loaders:
            ld.at_eof = False
            if ld.read_chunk():
                changed = True
            self._integrate(ld)
        return changed

    def progress(self):
        """Fraction of the log files decoded so far (0..1)."""
        total = sum(ld.file_size for ld in self.loaders)
        if not total:
            return 1.0
        return min(1.0, sum(ld.size for ld in self.loaders) / float(total))

    def _integrate(self, ld):
        if ld.dropped_front:
            ld.dropped_front = False
            self.truncated = True
            self._rebuild()
            return
        new = ld.entries[ld.sent:]
        if not new:
            return
        ld.sent = len(ld.entries)
        if self._single:
            accepted = new
        else:
            # rosout.log already aggregates all nodes; per-node *.log files
            # duplicate it. Dedupe identical (node,sev,msg) only ACROSS
            # files — repeats inside one file are distinct events (e.g.
            # periodic diagnostics) and must be kept.
            accepted = []
            for e in new:
                key = (e.node, e.sev, e.msg)
                srcs = self._key_srcs.get(key)
                if srcs is None:
                    self._key_srcs[key] = {e.src}
                    accepted.append(e)
                elif e.src in srcs:
                    accepted.append(e)
                else:
                    srcs.add(e.src)
        if not accepted:
            return
        self.entries.extend(accepted)
        self.appended.extend(accepted)
        need_sort = False
        for e in accepted:
            slot = self.nodes.setdefault(e.node,
                                         {"counts": {}, "entries": []})
            slot["entries"].append(e)
            slot["counts"][e.sev] = slot["counts"].get(e.sev, 0) + 1
            if e.ts is None:
                continue
            if self._last_ts is not None and e.ts < self._last_ts:
                need_sort = True        # e.g. unsynced clock jumped back
            self._last_ts = e.ts
        if need_sort:
            self._resort()

    def _resort(self):
        self.entries.sort(key=lambda e: (e.ts if e.ts is not None
                                         else float("inf"), e.seq))
        self.nodes = {}
        self._last_ts = None
        for e in self.entries:
            slot = self.nodes.setdefault(e.node,
                                         {"counts": {}, "entries": []})
            slot["entries"].append(e)
            slot["counts"][e.sev] = slot["counts"].get(e.sev, 0) + 1
            if e.ts is not None:
                self._last_ts = e.ts
        self.dirty_order = True

    def _rebuild(self):
        """Fallback after per-file truncation: full eager merge."""
        ents = []
        for ld in self.loaders:
            ents.extend(ld.entries)
            ld.sent = len(ld.entries)
        self.entries = ents
        self._resort()
        self.appended = []

    def node_names(self):
        rows = [(n, len(s["entries"])) for n, s in self.nodes.items()
                if n != ALL_NODE]
        rows.sort(key=lambda r: (-r[1], r[0]))
        return [(ALL_NODE, len(self.entries))] + rows

    def node_entries(self, name):
        if name == ALL_NODE:
            return self.entries
        slot = self.nodes.get(name)
        return slot["entries"] if slot else []

    def node_counts(self, name):
        if name == ALL_NODE:
            counts = {}
            for slot in self.nodes.values():
                for k, v in slot["counts"].items():
                    counts[k] = counts.get(k, 0) + v
            return counts
        slot = self.nodes.get(name)
        return slot["counts"] if slot else {}


def find_sessions():
    """Run directories under ROS_LOG_ROOT, newest first."""
    out = []
    try:
        names = os.listdir(ROS_LOG_ROOT)
    except OSError:
        return out
    for name in names:
        if name == "latest":        # symlink to the newest run dir
            continue
        path = os.path.join(ROS_LOG_ROOT, name)
        if not os.path.isdir(path):
            continue
        rosout = os.path.join(path, "rosout.log")
        if not os.path.exists(rosout):
            logs = sorted(glob.glob(os.path.join(path, "*.log")),
                          key=os.path.getmtime, reverse=True)
            if not logs:
                continue
            rosout = logs[0]
        try:
            mtime = os.path.getmtime(rosout)
            size = os.path.getsize(rosout)
        except OSError:
            continue
        out.append({"name": name, "path": rosout, "mtime": mtime,
                    "size": size})
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def resolve_rosout(path):
    """A user-supplied file/dir -> (rosout-ish file, session_dir)."""
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.isfile(path):
        return path
    if os.path.isdir(path):
        cand = os.path.join(path, "rosout.log")
        if os.path.isfile(cand):
            return cand
        logs = sorted(glob.glob(os.path.join(path, "*.log")),
                      key=os.path.getmtime, reverse=True)
        if logs:
            return logs[0]
        raise SystemExit("error: no *.log found in %s" % path)
    raise SystemExit("error: no such file or directory: %s" % path)


# ---------------------------------------------------------------- text wrap

def wrap_text(s, width):
    """Word wrap; hard-breaks tokens longer than width."""
    if width <= 0:
        return [s]
    if len(s) <= width and s.isascii() and " " not in s.strip(" "):
        # fast path: short single token, fits on one line
        return [s] if s else [""]
    lines, cur, curw = [], "", 0
    for word in s.split(" "):
        ww = dwidth(word)
        if ww > width:
            if cur:
                lines.append(cur)
                cur, curw = "", 0
            while dwidth(word) > width:
                acc = ""
                for ch in word:
                    if dwidth(acc + ch) > width:
                        break
                    acc += ch
                lines.append(acc)
                word = word[len(acc):]
            cur, curw = word, dwidth(word)
            continue
        if curw == 0:
            cur, curw = word, ww
        elif curw + 1 + ww <= width:
            cur, curw = cur + " " + word, curw + 1 + ww
        else:
            lines.append(cur)
            cur, curw = word, ww
    if cur or not lines:
        lines.append(cur)
    return lines


NODE_COL = 24                 # node-name column width in the (all) log view


def log_prefix(e, with_node):
    p = "%s %-5s " % (fmt_ts(e.ts), SEV_NAMES.get(e.sev, "-"))
    if with_node:
        n = clip_to_width(e.node, NODE_COL)
        p += n + " " * (NODE_COL - dwidth(n)) + " "
    return p


def entry_visual_lines(e, width, wrap_on, with_node=False):
    """Composed display lines for one entry: 'HH:MM:SS LEVEL  msg'.

    With with_node, a fixed-width node column is inserted after the level."""
    prefix = log_prefix(e, with_node)
    avail = width - len(prefix)
    if avail < 4:
        avail = 4
    out = []
    if not wrap_on:
        flat = e.msg.replace("\n", " ⏎ ")
        out.append(prefix + clip_to_width(flat, avail))
        return out
    segs = e.msg.split("\n")
    if not segs:
        segs = [""]
    indent = " " * len(prefix)
    first = True
    for seg in segs:
        for ln in wrap_text(clean(seg), avail):
            out.append((prefix if first else indent) + ln)
            first = False
        first = False
    if not out:
        out.append(prefix)
    return out


def count_wrap_text(s, width):
    """Line count of wrap_text(s, width) without building any strings."""
    if width <= 0:
        return 1
    if len(s) <= width and s.isascii() and " " not in s.strip(" "):
        return 1
    n, curw = 0, 0
    for word in s.split(" "):
        ww = dwidth(word)
        if ww > width:
            if curw:
                n += 1
                curw = 0
            full = (ww - 1) // width
            n += full
            ww -= full * width
        if curw == 0:
            curw = ww
        elif curw + 1 + ww <= width:
            curw += 1 + ww
        else:
            n += 1
            curw = ww
    return n + 1 if (curw or n == 0) else n


def entry_line_count(e, width, wrap_on, with_node=False):
    """Number of display lines for one entry (count-only, no strings)."""
    prefix = log_prefix(e, with_node)
    avail = width - len(prefix)
    if avail < 4:
        avail = 4
    if not wrap_on:
        return 1
    segs = e.msg.split("\n") or [""]
    return max(1, sum(count_wrap_text(clean(seg), avail) for seg in segs))


# ---------------------------------------------------------------- UI

class Theme(object):
    def __init__(self):
        self.ok = False

    def init(self):
        self.ok = curses.has_colors()
        if self.ok:
            curses.start_color()
            try:
                curses.use_default_colors()
                bg = -1
            except curses.error:
                bg = curses.COLOR_BLACK
            curses.init_pair(1, curses.COLOR_WHITE, bg)      # DEBUG
            curses.init_pair(2, curses.COLOR_CYAN, bg)       # INFO
            curses.init_pair(3, curses.COLOR_YELLOW, bg)     # WARN
            curses.init_pair(4, curses.COLOR_RED, bg)        # ERROR
            curses.init_pair(5, curses.COLOR_MAGENTA, bg)    # FATAL
            curses.init_pair(6, curses.COLOR_BLACK, curses.COLOR_YELLOW)

    def sev(self, sev):
        if sev == SEV_FATAL:
            return curses.A_BOLD | curses.color_pair(5) if self.ok \
                else curses.A_BOLD
        if sev == SEV_ERROR:
            return curses.A_BOLD | curses.color_pair(4) if self.ok \
                else curses.A_BOLD
        if sev == SEV_WARN:
            return curses.color_pair(3) if self.ok else curses.A_NORMAL
        if sev == SEV_INFO:
            return curses.color_pair(2) if self.ok else curses.A_NORMAL
        if sev == SEV_DEBUG:
            return curses.A_DIM | curses.color_pair(1) if self.ok \
                else curses.A_DIM
        return curses.A_DIM

    @property
    def hi(self):
        return curses.color_pair(6) | curses.A_BOLD if self.ok \
            else curses.A_REVERSE

    @property
    def dim(self):
        return curses.A_DIM

    @property
    def head(self):
        return curses.A_BOLD

    @property
    def sel(self):
        return curses.A_REVERSE

    @property
    def status(self):
        return curses.A_BOLD


class Quit(Exception):
    pass


class App(object):
    def __init__(self, stdscr, args):
        self.stdscr = stdscr
        self.args = args
        self.theme = Theme()
        self.theme.init()
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        stdscr.keypad(True)
        stdscr.timeout(GETCH_TIMEOUT_MS)

        self.mode = "sessions"
        self.help_on = False
        self.input = None            # {"prompt","value","kind"}
        self.session = None
        self.sessions = []

        # sessions view state
        self.s_cur = 0
        self.s_scroll = 0

        # nodes view state
        self.n_cur = 0
        self.n_scroll = 0
        self.n_filter = ""

        # log view state — windowed/dynamic: visual lines exist only for a
        # small range around the viewport and are built on demand while
        # scrolling; nothing is pre-rendered for the whole file.
        self.l_node = ALL_NODE
        self.l_abs = 0            # absolute visual line index of the top row
        self.vis = []             # built lines [(text, sev, entry, plen)]
        self.vis_first = 0        # absolute index of self.vis[0]
        self.vis_hi = 0           # absolute index one past the window
        self.lo_ent = 0           # ents index of first entry in the window
        self.hi_ent = 0           # ents index one past the last window entry
        self.counts = []          # visual line count per entry (None unknown)
        self.prefix = [0]         # prefix[i] = visual lines before ents[i]
        self.prefix_valid = 0     # prefix is valid for ents[0:prefix_valid]
        self.follow = False
        self.wrap = True
        self.hidden = set()          # hidden severities
        self.needle = ""
        self.matches = []            # absolute visual line indexes
        self.match_i = -1
        self.vis_cache = {}          # (seq, w, wrap, with_node) -> [(line, plen)]

        if args.path:
            self.session = Session(resolve_rosout(args.path),
                                   include_node_files=not args.no_node_files)
            self.mode = "nodes"
        else:
            self.refresh_sessions()
            if not args.sessions:
                opened = False
                latest = os.path.join(ROS_LOG_ROOT, "latest")
                if os.path.isdir(latest):
                    try:
                        self.session = Session(
                            resolve_rosout(latest),
                            include_node_files=not args.no_node_files)
                        opened = True
                    except (SystemExit, OSError, IOError):
                        opened = False
                if not opened and self.sessions:
                    try:
                        self.session = Session(
                            self.sessions[0]["path"],
                            include_node_files=not args.no_node_files)
                        opened = True
                    except (OSError, IOError):
                        opened = False
                if opened:
                    self.mode = "nodes"

    # ---------------------------------------------------------- utilities

    def addstr(self, y, x, text, attr=0):
        h, w = self.stdscr.getmaxyx()
        if y < 0 or y >= h or x < 0 or x >= w:
            return
        text = clip_to_width(text, w - x)
        if not text:
            return
        try:
            self.stdscr.addstr(y, x, text, attr)
        except curses.error:
            pass

    def add_spans(self, y, text, needle, attr):
        """Re-draw needle occurrences in text with highlight attr."""
        if not needle:
            return
        hay = text.casefold()
        nd = needle.casefold()
        start = 0
        while True:
            i = hay.find(nd, start)
            if i < 0 or not nd:
                break
            col = dwidth(text[:i])
            self.addstr(y, col, text[i:i + len(nd)], attr)
            start = i + len(nd)

    def refresh_sessions(self):
        self.sessions = find_sessions()
        self.s_cur = min(self.s_cur, max(0, len(self.sessions) - 1))

    def open_session(self, path):
        self.session = Session(path,
                               include_node_files=not self.args.no_node_files)
        self.mode = "nodes"
        self.n_cur = 0
        self.n_scroll = 0
        self.n_filter = ""
        self.l_node = ALL_NODE
        self.needle = ""

    def reload_session(self):
        if self.session is None:
            return
        keep_node = self.l_node
        self.session.load()
        if keep_node not in self.session.nodes and keep_node != ALL_NODE:
            self.l_node = ALL_NODE
        self.reset_log_view()

    # ---------------------------------------------------------- log view

    def log_entries(self):
        if self.session is None:
            return []
        return self.session.node_entries(self.l_node)

    def reset_log_view(self):
        """Drop the built window (node switch / reload / re-sort / rewrap)."""
        self.vis = []
        self.vis_first = 0
        self.vis_hi = 0
        self.lo_ent = 0
        self.hi_ent = 0
        self.counts = []
        self.prefix = [0]
        self.prefix_valid = 0
        self.l_abs = 0
        self.matches = []
        self.match_i = -1

    def _wrap_lines(self, e, w, with_node):
        key = (e.seq, w, self.wrap, with_node)
        lines = self.vis_cache.get(key)
        if lines is None:
            plen = len(log_prefix(e, with_node))
            lines = [(ln, plen)
                     for ln in entry_visual_lines(e, w, self.wrap, with_node)]
            if len(self.vis_cache) > 150_000:
                self.vis_cache.clear()
            self.vis_cache[key] = lines
        return lines

    def _lines_of(self, ents, i, w, with_node):
        """Visual line count of ents[i]; hidden entries contribute zero."""
        if i < len(self.counts) and self.counts[i] is not None:
            return self.counts[i]
        e = ents[i]
        n = 0 if e.sev in self.hidden else entry_line_count(e, w, self.wrap,
                                                           with_node)
        while len(self.counts) <= i:
            self.counts.append(None)
        self.counts[i] = n
        return n

    def _extend_prefix(self, ents, upto, w, with_node):
        """prefix[i] = total visual lines before ents[i], up to `upto`."""
        while self.prefix_valid < upto:
            i = self.prefix_valid
            n = self._lines_of(ents, i, w, with_node)
            self.prefix.append(self.prefix[-1] + n)
            self.prefix_valid += 1

    def _total_lines(self, ents, w, with_node):
        self._extend_prefix(ents, len(ents), w, with_node)
        return self.prefix[-1]

    def ensure_window(self):
        """Build/trim visual lines so the viewport is covered, on demand.

        Small scroll deltas grow the window incrementally; big jumps
        relocate it by bisecting the count-only prefix array, so strings
        are only ever built for a few screens around the viewport."""
        h, w = self.stdscr.getmaxyx()
        body = max(1, h - 3)
        ents = self.log_entries()
        with_node = self._with_node()
        near = self.vis and (
            self.vis_first - body * 3 <= self.l_abs <= self.vis_hi + body * 3)
        if not near:
            self._relocate_window(ents, w, with_node, body)
        # grow upward (older lines) while scrolling near the window top
        want_top = self.l_abs - body
        while self.vis_first > want_top and self.lo_ent > 0:
            i = self.lo_ent - 1
            self.lo_ent = i
            if self._lines_of(ents, i, w, with_node) == 0:
                continue               # hidden: contributes no lines
            lines = self._wrap_lines(ents[i], w, with_node)
            wrapped = [(ln, ents[i].sev, ents[i], p) for ln, p in lines]
            self.vis[:0] = wrapped
            self.vis_first -= len(wrapped)
        # grow downward (newer lines)
        want_bot = self.l_abs + body * 2
        while self.vis_hi < want_bot and self.hi_ent < len(ents):
            i = self.hi_ent
            self.hi_ent = i + 1
            if self._lines_of(ents, i, w, with_node) == 0:
                continue
            lines = self._wrap_lines(ents[i], w, with_node)
            wrapped = [(ln, ents[i].sev, ents[i], p) for ln, p in lines]
            self.vis.extend(wrapped)
            self.vis_hi += len(wrapped)
        # clamp to available range
        if self.vis_hi < self.l_abs + body:
            self.l_abs = max(0, self.vis_hi - body)
        if self.vis_first > self.l_abs:
            self.l_abs = self.vis_first
        # trim far-off lines (keep two screens of slack)
        slack_top = self.l_abs - body
        while self.vis and self.vis_first < slack_top:
            self.vis.pop(0)
            self.vis_first += 1
        slack_bot = self.l_abs + body * 3
        while self.vis and self.vis_hi > slack_bot:
            self.vis.pop()
            self.vis_hi -= 1

    def _relocate_window(self, ents, w, with_node, body):
        """Jump: locate the entry containing the top viewport line via
        bisect over the count-only prefix, then build a fresh window.
        The prefix is extended only as far as the jump target needs."""
        if not ents:
            self.vis = []
            self.vis_first = self.vis_hi = 0
            self.lo_ent = self.hi_ent = 0
            self.l_abs = 0
            return
        need = self.l_abs + body * 2 + 64
        while (self.prefix_valid < len(ents)
               and self.prefix[self.prefix_valid] <= need):
            self._extend_prefix(ents,
                                min(len(ents), self.prefix_valid + 512),
                                w, with_node)
        total = self.prefix[self.prefix_valid]
        target = max(0, min(self.l_abs, total - 1))
        ei = bisect.bisect_right(self.prefix, target) - 1
        ei = max(0, min(ei, len(ents) - 1, self.prefix_valid - 1))
        while ei < len(ents) and self._lines_of(ents, ei, w, with_node) == 0:
            ei += 1
        if ei >= len(ents):            # everything below is hidden
            ei = len(ents) - 1
            while ei > 0 and self._lines_of(ents, ei, w, with_node) == 0:
                ei -= 1
        start = max(0, ei - 8)
        self.vis = []
        self.lo_ent = start
        self.hi_ent = start
        self.vis_first = self.prefix[start]
        self.vis_hi = self.prefix[start]
        while self.hi_ent < len(ents) and self.vis_hi < target + body * 2:
            i = self.hi_ent
            self.hi_ent = i + 1
            if self._lines_of(ents, i, w, with_node) == 0:
                continue
            lines = self._wrap_lines(ents[i], w, with_node)
            wrapped = [(ln, ents[i].sev, ents[i], p) for ln, p in lines]
            self.vis.extend(wrapped)
            self.vis_hi += len(wrapped)

    def recompute_matches(self):
        """Full-text search over all entries (absolute visual indexes)."""
        self.matches = []
        self.match_i = -1
        if not self.needle:
            return
        h, w = self.stdscr.getmaxyx()
        ents = self.log_entries()
        with_node = (self.l_node == ALL_NODE)
        nd = self.needle.casefold()
        for i, e in enumerate(ents):
            self._extend_prefix(ents, i + 1, w, with_node)
            start = self.prefix[i]
            if e.sev in self.hidden:
                continue
            hit = nd in e.msg.casefold()
            if not hit and with_node:
                hit = nd in e.node.casefold()
            if not hit:
                hit = nd in fmt_ts(e.ts)  # e.g. search by time
            if hit:
                n = self._lines_of(ents, i, w, with_node)
                self.matches.extend(range(start, start + n))
        if self.matches:
            self.match_i = 0

    def goto_match(self, direction):
        if not self.matches:
            return
        self.match_i = (self.match_i + direction) % len(self.matches)
        h, _ = self.stdscr.getmaxyx()
        m = self.matches[self.match_i]
        self.l_abs = max(0, m - max(1, h - 3) // 2)

    def search_from_current(self):
        self.recompute_matches()
        if not self.matches:
            return
        # first match at/after the current viewport, wrapping around
        for i, m in enumerate(self.matches):
            if m >= self.l_abs:
                self.match_i = i
                break
        self.goto_match(0)

    # ---------------------------------------------------------- main loop

    def run(self):
        while True:
            busy = False
            if self.session is not None:
                s = self.session
                if not s.fully_loaded:
                    busy = True
                    if s.step(LOAD_STEP_MS) and self.mode == "log":
                        self.ensure_window()
                elif self.mode == "log" and self.follow:
                    s.poll()
                if s.dirty_order:          # entries re-sorted (clock jump)
                    s.dirty_order = False
                    self.reset_log_view()
            if self.mode == "log":
                self.ensure_window()
            try:
                self.draw()
            except curses.error:
                pass
            self.stdscr.timeout(15 if busy else GETCH_TIMEOUT_MS)
            k = self.stdscr.getch()
            try:
                self.handle(k)
            except Quit:
                return

    # ---------------------------------------------------------- handling

    def handle(self, k):
        if k == -1:
            return
        if self.help_on:
            self.help_on = False
            return
        if self.input is not None:
            self.handle_input(k)
            return
        if k in (ord('q'), ord('Q')):
            raise Quit()
        if k == ord('?'):
            self.help_on = True
            return
        if k == curses.KEY_RESIZE or k == 12:   # Ctrl+L
            self.reset_log_view()
            return
        if self.mode == "sessions":
            self.handle_sessions(k)
        elif self.mode == "nodes":
            self.handle_nodes(k)
        else:
            self.handle_log(k)

    def start_input(self, prompt, kind, initial=""):
        self.input = {"prompt": prompt, "value": initial, "kind": kind}
        try:
            curses.curs_set(1)
        except curses.error:
            pass

    def end_input(self):
        self.input = None
        try:
            curses.curs_set(0)
        except curses.error:
            pass

    def handle_input(self, k):
        inp = self.input
        if k in (curses.KEY_ENTER, 10, 13):
            val = inp["value"]
            kind = inp["kind"]
            self.end_input()
            if kind == "search":
                self.needle = val
                self.search_from_current()
            elif kind == "nodefilter":
                self.n_filter = val
                self.n_cur = 0
                self.n_scroll = 0
            return
        if k == 27:  # Esc
            self.end_input()
            return
        if k in (curses.KEY_BACKSPACE, 8, 127):
            inp["value"] = inp["value"][:-1]
            return
        if 32 <= k < 0x110000:
            inp["value"] += chr(k)

    def handle_sessions(self, k):
        n = len(self.sessions)
        if k in (curses.KEY_UP, ord('k')) and n:
            self.s_cur = max(0, self.s_cur - 1)
        elif k in (curses.KEY_DOWN, ord('j')) and n:
            self.s_cur = min(n - 1, self.s_cur + 1)
        elif k == ord('g') or k == curses.KEY_HOME:
            self.s_cur = 0
        elif k in (ord('G'), curses.KEY_END) and n:
            self.s_cur = n - 1
        elif k == curses.KEY_PPAGE:
            h, _ = self.stdscr.getmaxyx()
            self.s_cur = max(0, self.s_cur - max(1, h - 4))
        elif k == curses.KEY_NPAGE:
            h, _ = self.stdscr.getmaxyx()
            self.s_cur = min(n - 1, self.s_cur + max(1, h - 4)) if n else 0
        elif k == ord('r'):
            self.refresh_sessions()
        elif k in (curses.KEY_ENTER, 10, 13) and n:
            self.open_session(self.sessions[self.s_cur]["path"])
        elif k == 27:  # Esc
            if self.session is not None:
                self.mode = "nodes"

    def visible_nodes(self):
        if not self.session:
            return []
        rows = self.session.node_names()
        if self.n_filter:
            nd = self.n_filter.casefold()
            rows = [r for r in rows if nd in r[0].casefold()]
        return rows

    def handle_nodes(self, k):
        rows = self.visible_nodes()
        n = len(rows)
        if k in (curses.KEY_UP, ord('k')) and n:
            self.n_cur = max(0, self.n_cur - 1)
        elif k in (curses.KEY_DOWN, ord('j')) and n:
            self.n_cur = min(n - 1, self.n_cur + 1)
        elif k == ord('g') or k == curses.KEY_HOME:
            self.n_cur = 0
        elif k in (ord('G'), curses.KEY_END) and n:
            self.n_cur = n - 1
        elif k == curses.KEY_PPAGE:
            h, _ = self.stdscr.getmaxyx()
            self.n_cur = max(0, self.n_cur - max(1, h - 6))
        elif k == curses.KEY_NPAGE:
            h, _ = self.stdscr.getmaxyx()
            self.n_cur = min(n - 1, self.n_cur + max(1, h - 6)) if n else 0
        elif k in (curses.KEY_ENTER, 10, 13) and n:
            self.l_node = rows[self.n_cur][0]
            self.mode = "log"
            self.needle = ""
            self.reset_log_view()
        elif k == ord('/'):
            self.start_input("过滤节点: ", "nodefilter", self.n_filter)
        elif k == ord('s'):
            self.refresh_sessions()
            self.mode = "sessions"
        elif k == ord('r'):
            self.reload_session()
        self.clamp_node_scroll()

    def clamp_node_scroll(self):
        h, _ = self.stdscr.getmaxyx()
        body = max(1, h - 5)
        if self.n_cur < self.n_scroll:
            self.n_scroll = self.n_cur
        elif self.n_cur >= self.n_scroll + body:
            self.n_scroll = self.n_cur - body + 1

    def handle_log(self, k):
        h, _ = self.stdscr.getmaxyx()
        body = max(1, h - 3)
        if k in (curses.KEY_UP, ord('k')):
            self.follow = False
            self.l_abs = max(0, self.l_abs - 1)
        elif k in (curses.KEY_DOWN, ord('j')):
            self.follow = False
            self.l_abs += 1
        elif k == curses.KEY_PPAGE:
            self.follow = False
            self.l_abs -= body
        elif k == curses.KEY_NPAGE:
            self.follow = False
            self.l_abs += body
        elif k == 4:  # Ctrl+d
            self.follow = False
            self.l_abs += max(1, body // 2)
        elif k == 21:  # Ctrl+u
            self.follow = False
            self.l_abs -= max(1, body // 2)
        elif k == ord('g') or k == curses.KEY_HOME:
            self.follow = False
            self.l_abs = 0
        elif k in (ord('G'), curses.KEY_END):
            self.follow = False
            ents = self.log_entries()
            total = self._total_lines(ents, self._w(), self._with_node())
            self.l_abs = max(0, total - body)
        elif k == ord('f'):
            self.follow = not self.follow
            if self.follow:
                ents = self.log_entries()
                total = self._total_lines(ents, self._w(), self._with_node())
                self.l_abs = max(0, total - body)
        elif k == ord('w'):
            self.wrap = not self.wrap
            self.reset_log_view()
        elif k == ord('/'):
            self.start_input("搜索: ", "search", self.needle)
        elif k in (ord('n'), ord('N')) and self.needle:
            if not self.matches:
                self.search_from_current()
            else:
                self.goto_match(1 if k == ord('n') else -1)
        elif k in (ord('0'), ord('1'), ord('2'), ord('3'), ord('4'),
                   ord('5')):
            sev = k - ord('0')
            if sev == 0:
                self.hidden.clear()
            elif sev in self.hidden:
                self.hidden.discard(sev)
            else:
                self.hidden.add(sev)
            self.reset_log_view()
        elif k in (27, ord('h'), curses.KEY_LEFT, curses.KEY_BACKSPACE,
                   8, 127):
            self.mode = "nodes"
        elif k == ord('r'):
            self.reload_session()
        self.ensure_window()

    def _w(self):
        return self.stdscr.getmaxyx()[1]

    def _with_node(self):
        return self.l_node == ALL_NODE

    # ---------------------------------------------------------- drawing

    def draw(self):
        self.stdscr.erase()
        h, w = self.stdscr.getmaxyx()
        if h < 8 or w < 24:
            self.addstr(0, 0, "terminal too small (need >=24x8)", self.theme.head)
            return
        if self.mode == "sessions":
            self.draw_sessions(h, w)
        elif self.mode == "nodes":
            self.draw_nodes(h, w)
        else:
            self.draw_log(h, w)
        if self.input is not None:
            self.draw_input(h, w)
        if self.help_on:
            self.draw_help(h, w)
        self.stdscr.noutrefresh()
        curses.doupdate()

    def draw_bottom_bar(self, h, w, text):
        self.addstr(h - 1, 0, clip_to_width(text, w), self.theme.dim)

    def draw_sessions(self, h, w):
        th = self.theme
        self.addstr(0, 0, " rosview ▸ 选择会话", th.head)
        self.addstr(0, max(0, w - len(VERSION)), VERSION, th.dim)
        self.addstr(1, 0, " %s" % ROS_LOG_ROOT, th.dim)
        rows = self.sessions
        if not rows:
            self.addstr(3, 0, " 没有找到任何日志会话目录 (%s)" % ROS_LOG_ROOT,
                        th.sev(SEV_WARN))
            self.draw_bottom_bar(h, w,
                                 " r 刷新  q 退出")
            return
        self.addstr(2, 0, " %-5s %-26s %-11s %s" %
                    ("", "会话目录", "时间", "rosout 大小"), th.head)
        body = max(1, h - 4)
        self.s_scroll = max(0, min(self.s_scroll, max(0, len(rows) - body)))
        if self.s_cur < self.s_scroll:
            self.s_scroll = self.s_cur
        elif self.s_cur >= self.s_scroll + body:
            self.s_scroll = self.s_cur - body + 1
        cur_path = os.path.realpath(self.session.rosout_path) \
            if self.session else None
        for i in range(self.s_scroll, min(len(rows), self.s_scroll + body)):
            r = rows[i]
            mark = "▸" if i == self.s_cur else " "
            cur = "*" if cur_path and os.path.realpath(r["path"]) == cur_path \
                else " "
            line = " %s%s %-26s %s  %6s" % (
                mark, cur, clip_to_width(r["name"], 26),
                time.strftime("%m-%d %H:%M", time.localtime(r["mtime"])),
                "%dK" % max(1, r["size"] // 1024))
            attr = th.sel if i == self.s_cur else 0
            self.addstr(3 + i - self.s_scroll, 0, line.ljust(w)[:w - 1], attr)
        self.draw_bottom_bar(
            h, w,
            " ↑↓ j/k 选择  Enter 打开  r 刷新  Esc 返回  q 退出  ? 帮助")

    def draw_nodes(self, h, w):
        th = self.theme
        s = self.session
        self.addstr(0, 0, " rosview ▸ %s" % s.name, th.head)
        if s.truncated:
            self.addstr(0, max(0, w - 30), "(仅显示每个文件最后 %d 行)" %
                        MAX_LINES_PER_FILE, th.sev(SEV_WARN))
        tmin = s.entries[0].ts if s.entries else None
        tmax = s.entries[-1].ts if s.entries else None
        meta = " 节点 %d │ 日志 %s 条" % (len(s.nodes), commify(len(s.entries)))
        if not s.fully_loaded:
            meta += " │ 加载中 %d%%" % int(s.progress() * 100)
        if tmin is not None and tmax is not None:
            meta += " │ %s ~ %s" % (fmt_ts(tmin), fmt_ts(tmax))
        self.addstr(1, 0, clip_to_width(meta + "  " + s.rosout_path, w - 1),
                    th.dim)
        name_w = max(12, w - 34)
        total_x = 3 + name_w + 1
        self.addstr(2, 0, " %s  %-*s %10s %6s %6s" %
                    ("", name_w, "节点", "日志数", "ERR", "WARN"), th.head)
        rows = self.visible_nodes()
        body = max(1, h - 5)
        self.clamp_node_scroll()
        err_x = total_x + 10 + 1
        warn_x = err_x + 6 + 1
        for i in range(self.n_scroll, min(len(rows), self.n_scroll + body)):
            name, total = rows[i]
            counts = s.node_counts(name)
            err = counts.get(SEV_ERROR, 0) + counts.get(SEV_FATAL, 0)
            warn = counts.get(SEV_WARN, 0)
            mark = "▸" if i == self.n_cur else " "
            disp = "/" + name if name not in (ALL_NODE, UNKNOWN_NODE) else name
            y = 3 + i - self.n_scroll
            if i == self.n_cur:
                line = " %s %-*s %10s %6s %6s" % (
                    mark, name_w, clip_to_width(disp, name_w), commify(total),
                    commify(err) if err else "-", commify(warn) if warn else "-")
                self.addstr(y, 0, line.ljust(w)[:w - 1], th.sel)
                continue
            self.addstr(y, 0, " %s " % mark)
            self.addstr(y, 3, clip_to_width(disp, name_w))
            if total_x + 10 <= w:
                self.addstr(y, total_x, "%10s" % commify(total), th.dim)
            if err and err_x + 6 <= w:
                self.addstr(y, err_x, "%6s" % commify(err), th.sev(SEV_ERROR))
            if warn and warn_x + 6 <= w:
                self.addstr(y, warn_x, "%6s" % commify(warn), th.sev(SEV_WARN))
        filt = " │ 过滤:%s" % self.n_filter if self.n_filter else ""
        self.addstr(h - 2, 0, clip_to_width(
            " 会话 %s%s" % (s.name, filt), w), th.status)
        self.draw_bottom_bar(
            h, w, " ↑↓ j/k 选择  Enter 查看日志  / 过滤  s 会话  r 刷新  q 退出  ? 帮助")

    def _visible_count(self):
        """Entries shown for the current node and level filter."""
        s = self.session
        if s is None:
            return 0
        counts = s.node_counts(self.l_node)
        total = sum(counts.values())
        hidden = sum(v for k, v in counts.items() if k in self.hidden)
        return total - hidden

    def draw_log(self, h, w):
        th = self.theme
        s = self.session
        disp = "/" + self.l_node if self.l_node not in (ALL_NODE, UNKNOWN_NODE) \
            else self.l_node
        head = " %s ▸ %s ▸ %s 条" % (s.name, disp,
                                     commify(self._visible_count()))
        if not s.fully_loaded:
            head += " │ 加载中 %d%%" % int(s.progress() * 100)
        self.addstr(0, 0, clip_to_width(head, w - 12), th.head)
        body = max(1, h - 3)
        ents = self.log_entries()
        with_node = self._with_node()
        # approximate scroll percentage from the window's entry coverage
        if self.hi_ent >= len(ents) and self.vis_hi < self.l_abs + body:
            pct = 100
        elif len(ents) == 0:
            pct = 100
        else:
            span = max(1, self.vis_hi - self.vis_first)
            frac = (self.l_abs + body / 2.0 - self.vis_first) / span
            frac = min(1.0, max(0.0, frac))
            ent = self.lo_ent + frac * (self.hi_ent - self.lo_ent)
            pct = int(100.0 * ent / max(1, len(ents)))
        self.addstr(0, max(0, w - 11), "%4d%%" % pct, th.dim)
        # render the viewport out of the built window
        start = self.l_abs - self.vis_first
        for row in range(body):
            idx = start + row
            if idx < 0 or idx >= len(self.vis):
                continue
            text, sev, _e, plen = self.vis[idx]
            y = 1 + row
            self.addstr(y, 0, text, th.sev(sev))
            if len(text) > plen:
                self.addstr(y, 0, text[:plen], th.dim)
            self.add_spans(y, text, self.needle, th.hi)
        st = " "
        if self.hidden:
            st += "隐藏:%s │ " % ",".join(
                SEV_NAMES[x] for x in sorted(self.hidden))
        st += "换行:%s" % ("开" if self.wrap else "关")
        if self.needle:
            if self.matches:
                st += " │ /%s (%d/%d)" % (self.needle, self.match_i + 1,
                                          len(self.matches))
            else:
                st += " │ /%s (无匹配)" % self.needle
        if self.follow:
            st += " │ ●跟随"
        if s.truncated:
            st += " │ (已截断,仅显示最新部分)"
        self.addstr(h - 2, 0, clip_to_width(st, w), th.status)
        self.draw_bottom_bar(
            h, w, " ↑↓ j/k 滚动  g/G 首尾  PgUp/PgDn 翻页  / 搜索 n/N  "
                  "1-5 级别 0 全显  w 换行  f 跟随  Esc 返回  q 退出")

    def draw_help(self, h, w):
        lines = [
            "rosview v%s — 帮助" % VERSION,
            "",
            "  会话/节点列表:",
            "    ↑↓ 或 j/k     上下移动        g/G      首/末项",
            "    Enter         打开            /        过滤节点名",
            "    s             切换会话        r        重新加载文件",
            "",
            "  日志视图:",
            "    ↑↓ j/k        滚动一行        PgUp/PgDn 或 Ctrl+u/d  翻页",
            "    g / G         跳到顶部/底部   f        跟随模式(类似 tail -f)",
            "    /             搜索,高亮       n / N    下/上一个匹配",
            "    1..5          隐藏/显示 DEBUG/INFO/WARN/ERROR/FATAL",
            "    0             显示全部级别    w        自动换行 开/关",
            "    Esc 或 h      返回节点列表    r        重新加载",
            "",
            "  通用:  q 退出   ? 帮助   Ctrl+L 重绘",
            "",
            "  日志来源: ~/.ros/log/<会话>/rosout.log 及同目录各节点 *.log",
            "  按 Esc 关闭本帮助",
        ]
        bw = min(w - 4, max(max(dwidth(x) for x in lines) + 4, 40))
        bh = len(lines) + 2
        y0 = max(0, (h - bh) // 2)
        x0 = max(0, (w - bw) // 2)
        attr = curses.A_REVERSE if not self.theme.ok else 0
        for dy in range(min(bh, h - y0)):
            row = lines[dy - 1] if 0 < dy <= len(lines) else ""
            self.addstr(y0 + dy, x0, (" " + row).ljust(bw)[:bw - 1],
                        self.theme.head if dy == 1 else attr)

    def draw_input(self, h, w):
        inp = self.input
        text = " %s%s" % (inp["prompt"], inp["value"])
        self.addstr(h - 2, 0, clip_to_width(text + "_", w), self.theme.status)


# ---------------------------------------------------------------- entry

def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="rosview",
        description="htop/vim 风格的 ROS 日志终端查看器")
    p.add_argument("path", nargs="?", help="日志文件或会话目录"
                   "(默认: ~/.ros/log/latest)")
    p.add_argument("-s", "--sessions", action="store_true",
                   help="启动时先选择历史会话")
    p.add_argument("-n", "--no-node-files", action="store_true",
                   help="只读 rosout.log,不合并各节点的 *.log")
    p.add_argument("-v", "--version", action="version",
                   version="rosview " + VERSION)
    return p.parse_args(argv)


def main(argv=None):
    os.environ.setdefault("ESCDELAY", "25")
    try:
        locale.setlocale(locale.LC_ALL, "")
    except locale.Error:
        pass
    args = parse_args(argv if argv is not None else sys.argv[1:])

    if args.path is None and not args.sessions:
        # fall back to picker when there is nothing to auto-open
        if not os.path.isdir(os.path.join(ROS_LOG_ROOT, "latest")):
            args.sessions = True

    def run(stdscr):
        App(stdscr, args).run()

    curses.wrapper(run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
