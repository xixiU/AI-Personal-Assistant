#!/bin/bash
# AI 自动回复助手 - 服务管理脚本
# 用法:
#   ./service.sh start    启动服务（后台运行）
#   ./service.sh stop     停止服务
#   ./service.sh restart  重启服务
#   ./service.sh status   查看状态
#   ./service.sh debug    调试模式（前台运行，输出详细信息）
#   ./service.sh monitor  监控并自动重启（前台运行）

PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$PROJECT_DIR"

LOG_DIR="$PROJECT_DIR/logs"
PID_FILE="$LOG_DIR/service.pid"
NOHUP_LOG="$LOG_DIR/nohup.out"
LOG_FILE="$LOG_DIR/ai-assistant.log"
MONITOR_LOG="$LOG_DIR/monitor.log"

mkdir -p "$LOG_DIR"

# ------------------------------------------------------------------
# 子命令实现
# ------------------------------------------------------------------

cmd_start() {
    if [ -f "$PID_FILE" ]; then
        OLD_PID=$(cat "$PID_FILE")
        if ps -p "$OLD_PID" > /dev/null 2>&1; then
            echo "❌ Service is already running (PID: $OLD_PID)"
            return 1
        else
            echo "⚠️  Stale PID file found, removing..."
            rm -f "$PID_FILE"
        fi
    fi

    echo "🚀 Starting AI Auto-Reply Assistant..."
    nohup uv run run.py > "$NOHUP_LOG" 2>&1 &
    NEW_PID=$!
    echo "$NEW_PID" > "$PID_FILE"

    sleep 2

    if ps -p "$NEW_PID" > /dev/null 2>&1; then
        echo "✅ Service started (PID: $NEW_PID)"
        echo "   View logs:  tail -f $NOHUP_LOG"
    else
        echo "❌ Service failed to start, check logs:"
        echo "   tail -50 $NOHUP_LOG"
        rm -f "$PID_FILE"
        return 1
    fi
}

cmd_stop() {
    if [ ! -f "$PID_FILE" ]; then
        echo "⚠️  PID file not found, service may not be running"
        return 0
    fi

    PID=$(cat "$PID_FILE")

    if ! ps -p "$PID" > /dev/null 2>&1; then
        echo "⚠️  Process $PID is not running, removing stale PID file"
        rm -f "$PID_FILE"
        return 0
    fi

    echo "🛑 Stopping service (PID: $PID)..."
    kill -TERM "$PID" 2>/dev/null || true

    for i in $(seq 1 10); do
        if ! ps -p "$PID" > /dev/null 2>&1; then
            echo "✅ Service stopped gracefully"
            rm -f "$PID_FILE"
            return 0
        fi
        sleep 1
    done

    echo "⚠️  Service did not stop gracefully, forcing..."
    kill -KILL "$PID" 2>/dev/null || true
    sleep 1

    if ! ps -p "$PID" > /dev/null 2>&1; then
        echo "✅ Service stopped (forced)"
        rm -f "$PID_FILE"
    else
        echo "❌ Failed to stop service"
        return 1
    fi
}

cmd_status() {
    echo "=========================================="
    echo "AI Auto-Reply Assistant - Status"
    echo "=========================================="

    if [ ! -f "$PID_FILE" ]; then
        echo "❌ Status: NOT RUNNING (no PID file)"
        return 1
    fi

    PID=$(cat "$PID_FILE")

    if ! ps -p "$PID" > /dev/null 2>&1; then
        echo "❌ Status: NOT RUNNING (process $PID not found)"
        echo "   Run '$0 start' to restart"
        return 1
    fi

    echo "✅ Status: RUNNING (PID: $PID)"
    echo ""
    echo "📊 Process Info:"
    ps -p "$PID" -o pid,user,%cpu,%mem,etime,cmd --no-headers | awk '{
        printf "   User:    %s\n", $2
        printf "   CPU:     %s%%\n", $3
        printf "   Memory:  %s%%\n", $4
        printf "   Uptime:  %s\n", $5
    }'
    echo ""

    if [ -f "$LOG_FILE" ]; then
        echo "📝 Recent Logs (last 10 lines):"
        tail -10 "$LOG_FILE" | sed 's/^/   /'
        echo ""

        LAST_LOG_TIME=$(stat -c %Y "$LOG_FILE" 2>/dev/null || stat -f %m "$LOG_FILE" 2>/dev/null)
        CURRENT_TIME=$(date +%s)
        TIME_DIFF=$((CURRENT_TIME - LAST_LOG_TIME))

        if [ $TIME_DIFF -lt 600 ]; then
            echo "💓 Heartbeat: OK (last log ${TIME_DIFF}s ago)"
        else
            echo "⚠️  Heartbeat: WARNING (last log ${TIME_DIFF}s ago, may be stuck)"
        fi
    fi
}

cmd_restart() {
    cmd_stop
    sleep 1
    cmd_start
}

cmd_monitor() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Monitor started" | tee -a "$MONITOR_LOG"

    while true; do
        if [ ! -f "$PID_FILE" ]; then
            echo "[$(date '+%Y-%m-%d %H:%M:%S')] ⚠️  PID file missing, starting service..." | tee -a "$MONITOR_LOG"
            cmd_start >> "$MONITOR_LOG" 2>&1
        else
            PID=$(cat "$PID_FILE")
            if ! ps -p "$PID" > /dev/null 2>&1; then
                echo "[$(date '+%Y-%m-%d %H:%M:%S')] ❌ Process $PID died, restarting..." | tee -a "$MONITOR_LOG"
                rm -f "$PID_FILE"
                cmd_start >> "$MONITOR_LOG" 2>&1
            fi
        fi
        sleep 30
    done
}

cmd_debug() {
    echo "=========================================="
    echo "Debug Mode - $(date)"
    echo "=========================================="
    echo "Working directory: $(pwd)"
    echo "User: $(whoami)"
    echo "Shell PID: $$"
    echo ""

    # 检查配置文件
    if [ -f "config.yaml" ]; then
        echo "✅ config.yaml exists"
    else
        echo "❌ config.yaml not found"
        exit 1
    fi
    echo ""

    # 检查是否有其他实例在运行
    if [ -f "$PID_FILE" ]; then
        OLD_PID=$(cat "$PID_FILE")
        if ps -p "$OLD_PID" > /dev/null 2>&1; then
            echo "⚠️  Another instance is running (PID: $OLD_PID)"
            echo "   Stop it first with: $0 stop"
            exit 1
        else
            echo "⚠️  Stale PID file found, removing..."
            rm -f "$PID_FILE"
        fi
    fi

    echo "🐛 Starting in debug mode (foreground)..."
    echo "   Press Ctrl+C to stop"
    echo "=========================================="
    echo ""

    # 前台运行，所有输出直接显示
    exec uv run run.py
}

# ------------------------------------------------------------------
# 入口
# ------------------------------------------------------------------

case "${1:-}" in
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    restart) cmd_restart ;;
    status)  cmd_status ;;
    debug)   cmd_debug ;;
    monitor) cmd_monitor ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|debug|monitor}"
        echo ""
        echo "  start    启动服务（后台运行）"
        echo "  stop     停止服务"
        echo "  restart  重启服务"
        echo "  status   查看状态"
        echo "  debug    调试模式（前台运行，输出详细信息）"
        echo "  monitor  监控并自动重启（前台运行）"
        exit 1
        ;;
esac
