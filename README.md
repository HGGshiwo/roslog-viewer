# rosview — 终端 ROS 日志查看器

`htop` / `vim` 风格的 ROS1/ROS2 日志终端查看器。单文件 Python 实现,**零第三方依赖**,只要系统有 `python3` 就能跑。

![keys](https://img.shields.io/badge/keys-vim%20%2B%20htop%20style-blue)

## 功能

- **节点选择**:自动解析 `rosout.log`(及各节点 `*.log`,自动去重),按节点分组统计日志条数 / ERROR / WARN,方向键选择
- **翻页滚动**:vim 式 `j/k/g/G/Ctrl+d/Ctrl+u` + 方向键 + PgUp/PgDn,底部状态栏显示百分比
- **搜索高亮**:`/` 关键字搜索,命中高亮,`n` / `N` 在匹配间跳转
- **级别过滤**:按 `1`~`5` 隐藏/显示 DEBUG/INFO/WARN/ERROR/FATAL,`0` 全部显示
- **实时跟随**:`f` 键进入跟随模式(类似 `tail -f`),日志实时追加并自动滚到底部
- **自动换行**:长消息(含 traceback)按终端宽度折行,`w` 开/关;中文等宽字符正确处理
- **多行消息**:异常堆栈自动归属到触发它的那条日志
- **历史会话**:不止 `latest`,`s` 键或 `rosview -s` 可浏览 `~/.ros/log` 下所有历史 run

## 安装(一键)

把仓库推送到 GitHub 后,一条命令安装(把 `YOUR_GITHUB_USER` 换成你的用户名):

```bash
pip install git+https://github.com/YOUR_GITHUB_USER/roslog-viewer.git
```

安装后自动获得 `rosview` 命令(pip 会装到 `~/.local/bin`,请确保其在 `PATH` 中)。升级:

```bash
pip install --user --upgrade git+https://github.com/YOUR_GITHUB_USER/roslog-viewer.git
```

本地开发安装:

```bash
pip install --user .
```

> 说明: rosview 零第三方依赖,纯标准库实现;只要 `python3 >= 3.6` 即可,ROS 环境无需额外配置。

## 使用

```bash
rosview                    # 打开 ~/.ros/log/latest
rosview -s                 # 先选择历史会话
rosview <日志文件或目录>     # 任意 rosout.log 或 run 目录
rosview -n                 # 只读 rosout.log,不合并各节点 *.log
```

也可以不装直接跑源码: `PYTHONPATH=. python3 -m rosview`

## 按键

| 位置 | 按键 | 作用 |
|---|---|---|
| 通用 | `↑↓` / `j k` | 移动 / 滚动 |
| | `g` / `G` | 首 / 末 |
| | `PgUp` `PgDn` | 翻页 |
| | `?` | 帮助 |
| | `q` | 退出 |
| 节点列表 | `Enter` | 查看该节点日志 |
| | `/` | 按名称过滤节点 |
| | `s` / `r` | 切换会话 / 重新加载 |
| 日志视图 | `Ctrl+d` `Ctrl+u` | 半屏滚动 |
| | `/` + `n` `N` | 搜索 / 跳转匹配 |
| | `1` `2` `3` `4` `5` | 隐藏/显示对应级别,`0` 全显 |
| | `f` | 跟随模式 开/关 |
| | `w` | 自动换行 开/关 |
| | `Esc` `h` | 返回节点列表 |

## 支持的日志格式

- ROS1 `rosout.log` 单行格式:`<秒>.<纳秒> LEVEL /node [file:line(func)] [topics: ...] msg`
- ROS1/ROS2 括号格式:`[INFO] [WallTime: 1234.5] [/node]: msg`、`[/node] [INFO] [1234.5]: msg`
- 各节点 `*.log`:`[logger][LEVEL] 2020-01-01 12:00:00,000: msg`
- 无法解析的行:附加到上一条日志(多行 traceback)或原样显示

## 测试

```bash
python3 tests/test_parse.py    # 解析 / 换行 / 去重 / 会话发现
bash tests/smoke_tmux.sh       # tmux 交互冒烟(需要 tmux)
```

## 推送到 GitHub

```bash
cd ~/roslog-viewer
git init && git add . && git commit -m "rosview: ROS log TUI viewer"
# 在 GitHub 上新建空仓库 roslog-viewer 后:
git remote add origin git@github.com:YOUR_GITHUB_USER/roslog-viewer.git
git push -u origin master
```

之后任何人都可以 `pip install git+https://github.com/YOUR_GITHUB_USER/roslog-viewer.git` 一键安装。

## 项目结构

```
rosview/
  __init__.py    # 全部实现(单文件,零依赖)
  __main__.py    # 支持 python -m rosview
pyproject.toml   # pip 打包配置(console_scripts 入口点)
tests/
  test_parse.py  # 解析 / 换行 / 去重 / 会话发现
  smoke_tmux.sh  # tmux 交互冒烟测试
  fixtures/      # ROS1 日志样例
```
