#!/usr/bin/env bash
# rosview 一键安装:把单文件脚本装到 ~/.local/bin(或 /usr/local/bin)
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SCRIPT="$SRC_DIR/rosview"

if [[ ! -f "$SCRIPT" ]]; then
    echo "错误: 找不到 $SCRIPT" >&2
    exit 1
fi

# --- python3 检查(rosview 零第三方依赖,只要有 python3 即可) ---
if ! command -v python3 >/dev/null 2>&1; then
    echo "错误: 未找到 python3,请先安装(如 sudo apt install python3)" >&2
    exit 1
fi

# --- 选择安装目录 ---
PREFIX="${ROSVIEW_PREFIX:-$HOME/.local/bin}"
if [[ ! -w "$(dirname "$PREFIX")" && "$(dirname "$PREFIX")" == "$HOME" ]]; then
    mkdir -p "$PREFIX"
fi
if [[ "$PREFIX" == "$HOME/.local/bin" ]]; then
    mkdir -p "$PREFIX"
    case ":$PATH:" in
        *":$PREFIX:"*) ;;
        *)
            echo "提示: $PREFIX 不在 PATH 中。请把下面一行加入 ~/.bashrc 或 ~/.zshrc:"
            echo "  export PATH=\"\$HOME/.local/bin:\$PATH\""
            ;;
    esac
fi

install -m 0755 "$SCRIPT" "$PREFIX/rosview"

echo "✔ 已安装: $PREFIX/rosview"
echo
echo "用法:"
echo "  rosview              # 打开 ~/.ros/log/latest"
echo "  rosview -s           # 先选择历史会话"
echo "  rosview <日志文件|目录>"
echo
echo "按键: ↑↓/j/k 选择  Enter 进入  / 搜索  n/N 跳转  1-5 级别过滤  f 跟随  ? 帮助  q 退出"
