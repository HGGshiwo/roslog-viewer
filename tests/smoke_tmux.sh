#!/usr/bin/env bash
# tmux 交互冒烟测试:驱动真实终端验证各视图。需要 tmux。
set -euo pipefail
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
S=rosview_smoke
cleanup() { tmux kill-session -t $S 2>/dev/null || true; }
trap cleanup EXIT
cleanup
# latest 可能指向刚启动的空会话;选最大的历史会话保证有内容
BIG=$(python3 - <<'PY'
import glob, os
cands = sorted(glob.glob(os.path.expanduser("~/.ros/log/*/rosout.log")),
               key=os.path.getsize, reverse=True)
# prefer a mid-size session: content-rich but fast to load in CI
mid = [c for c in cands if 200_000 <= os.path.getsize(c) <= 5_000_000]
print((mid or cands[:1])[0] if cands else "")
PY
)
[ -n "$BIG" ] || { echo "no ros logs found"; exit 1; }
tmux new-session -d -x 110 -y 28 -s $S "cd '$DIR' && PYTHONPATH='$DIR' python3 -m rosview '$BIG' -n"
sleep 1.2

cap() { tmux capture-pane -t $S -p; }

# 节点列表渲染
cap | grep -q "节点" && cap | grep -q "ERR"
sleep 0.3
# 进入日志视图并到底部
tmux send-keys -t $S Enter; sleep 0.6
tmux send-keys -t $S G; sleep 2.5
cap | grep -q "跟随\|100%\|%"
# 搜索
tmux send-keys -t $S /; sleep 0.2
tmux send-keys -t $S -l "the"; sleep 0.2
tmux send-keys -t $S Enter; sleep 4.5
cap | grep -qE "/the \([0-9]+/[0-9]+\)|/the \(无匹配\)"
# 级别过滤: 隐藏 ERROR 后条数应变化
N1=$(cap | sed -n 1p)
tmux send-keys -t $S 4; sleep 0.4
N2=$(cap | sed -n 1p)
tmux send-keys -t $S 0; sleep 0.3
[ "$N1" != "$N2" ] || { echo "level filter had no effect: $N1 vs $N2"; exit 1; }
# 帮助
tmux send-keys -t $S -l '?'; sleep 0.5
cap | grep -q "rosview v"
tmux send-keys -t $S -l 'x'; sleep 0.3
# 换行开关
tmux send-keys -t $S w; sleep 0.4
cap | grep -q "换行:关"
# 跟随模式
tmux send-keys -t $S f; sleep 0.4
cap | grep -q "●跟随"
# 返回节点列表, 打开会话选择器
tmux send-keys -t $S Escape; sleep 0.3
tmux send-keys -t $S s; sleep 0.5
cap | grep -q "选择会话"
tmux send-keys -t $S q; sleep 0.4

echo "ALL TMUX SMOKE TESTS PASSED"
