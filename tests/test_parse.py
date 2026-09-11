#!/usr/bin/env python3
"""rosview 解析 / 换行 / 会话发现单元测试。运行: python3 tests/test_parse.py"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import rosview as rv


def finish(s):
    """Drive incremental parsing to completion."""
    while not s.fully_loaded:
        s.step(1000)
    return s


FIXTURE = os.path.join(os.path.dirname(__file__),
                       "fixtures", "2026-09-10T12-00-00-demo-1234",
                       "rosout.log")

s = finish(rv.Session(FIXTURE))
nodes = dict(s.node_names())
assert nodes.get("talker") == 3, nodes
assert nodes.get("driver") == 5, nodes      # 4 from rosout + 1 unique node-file line
assert nodes.get("listener") == 2, nodes
assert nodes.get("planner") == 1, nodes
assert nodes.get(rv.ALL_NODE) == 12, nodes

counts = s.node_counts("driver")
assert counts.get(rv.SEV_ERROR) == 2 and counts.get(rv.SEV_FATAL) == 1, counts

# multi-line traceback merged into its triggering entry
drv = s.node_entries("driver")
merged = [e for e in drv if "Exception traceback" in e.msg]
assert merged and "serial.SerialException" in merged[0].msg

# CJK message kept intact
assert any("中文日志" in e.msg for e in s.entries)

# per-node file entries present after dedupe
talk = s.node_entries("talker")
assert any("publishing slower" in e.msg for e in talk)
assert any("hello world 1" in e.msg for e in talk)

# format regexes
assert rv.ROSOUT_RE.match("[ERROR] [1757486400.7] [/n]: x")
assert rv.NODE_FIRST_RE.match("[/planner] [INFO] [1757486401.1]: ok")
m = rv.MONO_RE.match(
    "1789018982.235271692 INFO /n [f.py:75(F)] [topics: /rosout] msg here")
assert m and m.group("node") == "/n" and m.group("msg").startswith("[")
assert "Node Startup"[-3:]

# follow mode: append then poll picks it up
with open(s.rosout_path, "a") as f:
    f.write("[ERROR] [WallTime: 1757486402.0] [/driver]: appended line\n")
assert s.poll() is True
assert any("appended line" in e.msg for e in s.node_entries("driver"))
# strip it back out to keep the fixture pristine
lines = open(s.rosout_path).readlines()
open(s.rosout_path, "w").writelines(
    ln for ln in lines if "appended line" not in ln)

# same-file repeats are distinct events — must NOT be deduped (v1.0.1 fix)
rep = os.path.join(os.path.dirname(__file__), "fixtures", "repeat",
                   "rosout.log")
r = finish(rv.Session(rep, include_node_files=False))
assert len(r.entries) == 6, len(r.entries)      # 5 identical + 1 other
diag = [e for e in r.entries if "Diag" in e.msg]
assert len(diag) == 5, len(diag)
# cross-file dedupe (rosout vs node file) still collapses real duplicates
xf = finish(rv.Session(FIXTURE))
assert dict(xf.node_names()).get(rv.ALL_NODE) == 12

# wrapping
E = lambda msg, sev=rv.SEV_INFO: rv.Entry(1757486400, sev, "t", msg, 0)
lines = rv.entry_visual_lines(E("a " * 30), 40, True)
assert len(lines) > 1 and all(rv.dwidth(l) <= 40 for l, d in lines)
assert all(isinstance(d, tuple) for l, d in lines)
cjk = rv.entry_visual_lines(E("中" * 50), 30, True)
assert all(rv.dwidth(l) <= 30 for l, d in cjk)
flat = rv.entry_visual_lines(E("a " * 100), 40, False)
assert len(flat) == 1
# head dims: timestamp dimmed; source-path bracket dimmed, message bright
e = rv.Entry(1789018982.23, rv.SEV_INFO, "api_routes",
             "[/home/u/api.cpp:273(pub)] [Telemetry Pub] hello", 0)
hl = rv.entry_visual_lines(e, 200, True, False)
text, dims = hl[0]
assert dims[0] == (0, 8), dims
assert any(a == text.index("[/home") for a, b in dims), (dims, text)
rest = text[text.index("[Telemetry"):]
assert not any(text[a:b] == rest for a, b in dims)  # message not dimmed

# real environment: ~/.ros/log exists with sessions and latest
sess = rv.find_sessions()
assert len(sess) > 0, "no sessions under %s" % rv.ROS_LOG_ROOT
# latest may point at a just-started run; test against the biggest session
big = max(rv.find_sessions(), key=lambda r: r["size"])
real = finish(rv.Session(big["path"], include_node_files=False))
assert len(real.entries) > 100, len(real.entries)
assert not any("[topics:" in e.msg for e in real.entries[:10])

print("ALL PARSE TESTS PASSED (%d fixture entries, %d real entries)"
      % (len(s.entries), len(real.entries)))
