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
import curses
import glob
import locale
import os
import re
import sys
import time
import unicodedata

VERSION = "1.0.1"
MAX_LINES_PER_FILE = 200_000      # keep last N lines per file
MAX_VIS_LINES = 400_000           # cap built visual lines in log view
GETCH_TIMEOUT_MS = 400            # poll interval for follow mode

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
MONO_RE = re.compile(
    r'^\s*(?P<ts>\d{9,13}\.\d{1,9})\s+'
    r'(?:(?P<sev>DEBUG|INFO|WARN|WARNING|ERROR|ERR|FATAL|FTL)\s+)?'
    r'(?P<node>/[^\s\[]+)?\s*'
    r'(?P<msg>\[[^\n]*|\S.*)$')
MONO_TOPICS_RE = re.compile(r'\s*\[topics:[^\]]*\]')
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
    """Incremental reader for one log file; keeps parse state for follow."""

    def __init__(self, path, default_node, counter, src=0):
        self.path = path
        self.default_node = default_node
        self.counter = counter          # shared itertools-like [n] box
        self.src = src
        self.f = open(path, "r", errors="replace")
        self.size = 0
        self.last = None                # last Entry, for continuation lines
        self.entries = []
        self.truncated = False

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
        m = MONO_RE.match(line)
        if m:
            node = norm_node(m.group("node")) if m.group("node") \
                else self.default_node
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

    def read_available(self):
        try:
            st = os.fstat(self.f.fileno())
        except OSError:
            return False
        changed = False
        while st.st_size > self.size:
            chunk = self.f.read(1 << 20)
            if not chunk:
                break
            changed = True
            self.size += len(chunk.encode("utf-8", "replace"))
            self._feed(chunk)
        return changed

    def _feed(self, chunk):
        self._pending = getattr(self, "_pending", "")
        data = self._pending + chunk
        lines = data.split("\n")
        self._pending = lines.pop()
        for ln in lines:
            self._line(ln)
        if len(self.entries) > MAX_LINES_PER_FILE:
            drop = len(self.entries) - MAX_LINES_PER_FILE
            del self.entries[:drop]
            self.truncated = True
            self.last = self.entries[-1] if self.entries else None

    def close(self):
        try:
            self.f.close()
        except OSError:
            pass


class Session(object):
    """One run directory: rosout.log + per-node *.log files."""

    def __init__(self, rosout_path, include_node_files=True):
        self.rosout_path = os.path.abspath(rosout_path)
        self.dir = os.path.dirname(self.rosout_path)
        self.name = os.path.basename(self.dir) or self.rosout_path
        self.include_node_files = include_node_files
        self.loaders = []
        self.entries = []       # merged, sorted
        self.nodes = {}         # name -> {"counts": {}, "entries": []}
        self.truncated = False
        self.load()

    # -- loading

    def load(self):
        for ld in self.loaders:
            ld.close()
        self.loaders = []
        self.entries = []
        self.nodes = {}
        self.truncated = False
        counter = [0]
        src = 0
        ld = FileLoader(self.rosout_path, UNKNOWN_NODE, counter, src)
        ld.read_available()
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
                fl.read_available()
                self.loaders.append(fl)
        self._merge()

    def _merge(self):
        ents = []
        for ld in self.loaders:
            if ld.truncated:
                self.truncated = True
            ents.extend(ld.entries)
        # stable sort: timestamp when available, otherwise keep load order
        ents.sort(key=lambda e: (e.ts if e.ts is not None else float("inf"),
                                 e.seq))
        # rosout.log already aggregates all nodes; the per-node *.log files
        # largely duplicate it. Dedupe identical (node,sev,msg) only ACROSS
        # files — repeats inside one file are distinct events (e.g. periodic
        # diagnostics) and must be kept.
        seen_by_key = {}
        uniq = []
        for e in ents:
            key = (e.node, e.sev, e.msg)
            srcs = seen_by_key.setdefault(key, set())
            if srcs and e.src not in srcs:
                continue               # cross-file duplicate
            srcs.add(e.src)
            uniq.append(e)
        self.entries = uniq
        self.nodes = {}
        for e in self.entries:
            slot = self.nodes.setdefault(e.node,
                                         {"counts": {}, "entries": []})
            slot["entries"].append(e)
            if e.sev:
                slot["counts"][e.sev] = slot["counts"].get(e.sev, 0) + 1

    def poll(self):
        """Check files for appended data (follow mode). True if changed."""
        changed = any(ld.read_available() for ld in self.loaders)
        if changed:
            self._merge()
        return changed

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

        # log view state
        self.l_node = ALL_NODE
        self.l_scroll = 0
        self.follow = False
        self.wrap = True
        self.hidden = set()          # hidden severities
        self.needle = ""
        self.matches = []            # visual-line indexes
        self.match_i = -1
        self.vis = []                # [(text, sev, entry, prefix_len)]
        self.vis_dirty = True
        self.vis_cache = {}          # (seq, w, wrap, with_node) -> [(line, plen)]
        self.vis_truncated = False

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
        self.vis_dirty = True
        self.needle = ""

    def reload_session(self):
        if self.session is None:
            return
        keep_node = self.l_node
        self.session.load()
        if keep_node not in self.session.nodes and keep_node != ALL_NODE:
            self.l_node = ALL_NODE
        self.vis_dirty = True

    # ---------------------------------------------------------- log view

    def log_entries(self):
        if self.session is None:
            return []
        return self.session.node_entries(self.l_node)

    def rebuild_vis(self):
        h, w = self.stdscr.getmaxyx()
        ents = self.log_entries()
        with_node = (self.l_node == ALL_NODE)
        cache = self.vis_cache
        vis = []
        dropped_oldest = False
        for e in ents:
            if e.sev in self.hidden:
                continue
            key = (e.seq, w, self.wrap, with_node)
            lines = cache.get(key)
            if lines is None:
                plen = len(log_prefix(e, with_node))
                lines = [(ln, plen)
                         for ln in entry_visual_lines(e, w, self.wrap,
                                                      with_node)]
                if len(cache) > 120_000:
                    cache.clear()
                cache[key] = lines
            vis.extend((ln, e.sev, e, p) for ln, p in lines)
            if len(vis) > MAX_VIS_LINES:
                # keep the NEWEST entries: drop what we just built past cap
                del vis[MAX_VIS_LINES:]
                dropped_oldest = True
                break
        # when the full view doesn't fit, keep only the last MAX_VIS_LINES
        if dropped_oldest and len(ents) > len(vis):
            vis = []
            for e in reversed(ents):
                if e.sev in self.hidden:
                    continue
                key = (e.seq, w, self.wrap, with_node)
                lines = cache.get(key)
                if lines is None:
                    plen = len(log_prefix(e, with_node))
                    lines = [(ln, plen)
                             for ln in entry_visual_lines(e, w, self.wrap,
                                                          with_node)]
                    if len(cache) > 120_000:
                        cache.clear()
                    cache[key] = lines
                vis.extend((ln, e.sev, e, p) for ln, p in lines)
                if len(vis) >= MAX_VIS_LINES:
                    break
            vis.reverse()
        self.vis_truncated = dropped_oldest and True or False
        self.vis = vis
        self.vis_dirty = False
        self.clamp_scroll()
        if self.needle:
            self.recompute_matches()
            if self.match_i >= len(self.matches):
                self.match_i = len(self.matches) - 1

    def clamp_scroll(self):
        h, _ = self.stdscr.getmaxyx()
        body = max(1, h - 3)
        self.l_scroll = max(0, min(self.l_scroll, max(0, len(self.vis) - body)))

    def scroll_bottom(self):
        h, _ = self.stdscr.getmaxyx()
        self.l_scroll = max(0, len(self.vis) - max(1, h - 3))

    def recompute_matches(self):
        self.matches = []
        self.match_i = -1
        if not self.needle:
            return
        nd = self.needle.casefold()
        for i, (text, _, _, _) in enumerate(self.vis):
            if nd in text.casefold():
                self.matches.append(i)
        if self.matches:
            self.match_i = 0

    def goto_match(self, direction):
        if not self.matches:
            return
        self.match_i = (self.match_i + direction) % len(self.matches)
        h, _ = self.stdscr.getmaxyx()
        self.l_scroll = max(0, min(self.matches[self.match_i],
                                   max(0, len(self.vis) - max(1, h - 3))))

    def search_from_current(self):
        self.recompute_matches()
        if not self.matches:
            return
        # first match at/after current scroll, wrapping around
        for i, m in enumerate(self.matches):
            if m >= self.l_scroll:
                self.match_i = i
                break
        h, _ = self.stdscr.getmaxyx()
        self.l_scroll = max(0, min(self.matches[self.match_i],
                                   max(0, len(self.vis) - max(1, h - 3))))

    # ---------------------------------------------------------- main loop

    def run(self):
        while True:
            if self.mode == "log" and self.follow and self.session:
                if self.session.poll():
                    self.vis_dirty = True
            if self.vis_dirty and self.mode == "log":
                self.rebuild_vis()
            try:
                self.draw()
            except curses.error:
                pass
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
            self.vis_dirty = True
            self.clamp_scroll()
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
            self.vis_dirty = True
            self.needle = ""
            self.matches = []
            self.rebuild_vis()
            self.scroll_bottom()
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
        maxs = max(0, len(self.vis) - body)
        if k in (curses.KEY_UP, ord('k')):
            self.follow = False
            self.l_scroll = max(0, self.l_scroll - 1)
        elif k in (curses.KEY_DOWN, ord('j')):
            self.follow = False
            self.l_scroll = min(maxs, self.l_scroll + 1)
        elif k in (curses.KEY_PPAGE,):
            self.follow = False
            self.l_scroll = max(0, self.l_scroll - body)
        elif k == curses.KEY_NPAGE:
            self.follow = False
            self.l_scroll = min(maxs, self.l_scroll + body)
        elif k == 4:  # Ctrl+d
            self.follow = False
            self.l_scroll = min(maxs, self.l_scroll + max(1, body // 2))
        elif k == 21:  # Ctrl+u
            self.follow = False
            self.l_scroll = max(0, self.l_scroll - max(1, body // 2))
        elif k == ord('g') or k == curses.KEY_HOME:
            self.follow = False
            self.l_scroll = 0
        elif k in (ord('G'), curses.KEY_END):
            self.follow = False
            self.scroll_bottom()
        elif k == ord('f'):
            self.follow = not self.follow
            if self.follow and self.session:
                if self.session.poll():
                    self.vis_dirty = True
                    self.rebuild_vis()
                self.scroll_bottom()
        elif k == ord('w'):
            self.wrap = not self.wrap
            self.vis_dirty = True
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
            self.vis_dirty = True
        elif k in (27, ord('h'), curses.KEY_LEFT, curses.KEY_BACKSPACE,
                   8, 127):
            self.mode = "nodes"
        elif k == ord('r'):
            self.reload_session()
        self.clamp_scroll()

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

    def draw_log(self, h, w):
        th = self.theme
        if self.vis_dirty:
            self.rebuild_vis()
        s = self.session
        disp = "/" + self.l_node if self.l_node not in (ALL_NODE, UNKNOWN_NODE) \
            else self.l_node
        head = " %s ▸ %s ▸ %s 条" % (s.name, disp, commify(len(self.vis)))
        self.addstr(0, 0, clip_to_width(head, w - 12), th.head)
        body = max(1, h - 3)
        maxs = max(0, len(self.vis) - body)
        pct = 100 if not maxs else int(100.0 * self.l_scroll / maxs)
        if self.follow:
            pct = 100
        self.addstr(0, max(0, w - 11), "%4d%%" % pct, th.dim)
        for row in range(body):
            idx = self.l_scroll + row
            if idx >= len(self.vis):
                break
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
        if s.truncated or self.vis_truncated:
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
