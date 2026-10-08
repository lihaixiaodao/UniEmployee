#!/usr/bin/env bash
# 一键启动 UniEmployee 前后端（macOS / Linux；Windows 上 Git Bash 也可用）。
# 与 scripts/start.ps1 行为一致。
#
# 用法：
#   ./scripts/start.sh                      # dev：后端 + vite 热重载，访问 5173
#   ./scripts/start.sh --mode single        # 单进程：仅后端 8787，用 frontend/dist
#   ./scripts/start.sh --port 9000          # 换后端端口
#   ./scripts/start.sh --no-db              # 跳过 PostgreSQL 容器检查
#   ./scripts/start.sh --open               # 启动后打开浏览器
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

MODE=dev
BACKEND_PORT=8787
FRONTEND_PORT=5173
SKIP_DB=0
DO_OPEN=0
DB_CONTAINER=uniemployee-pg

usage() {
  sed -n '2,10p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode)     MODE="${2:-}"; shift 2 ;;
    --port)     BACKEND_PORT="${2:-}"; shift 2 ;;
    --no-db)    SKIP_DB=1; shift ;;
    --open)     DO_OPEN=1; shift ;;
    -h|--help)  usage 0 ;;
    *) echo "未知参数: $1" >&2; usage 1 ;;
  esac
done

case "$MODE" in
  dev|single) ;;
  *) echo "!!! --mode 只能是 dev 或 single，收到: $MODE" >&2; exit 1 ;;
esac

step() { printf '\033[36m==> %s\033[0m\n' "$1"; }
warn() { printf '\033[33m!!! %s\033[0m\n' "$1"; }
die()  { printf '\033[31m!!! %s\033[0m\n' "$1" >&2; exit 1; }

# venv 里 python 的位置随平台而异：Unix 是 bin/python，Windows（含 Git Bash）是
# Scripts/python.exe。Git Bash 里跑的是 Windows 版 venv，两种都要认得。
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  PY="$ROOT/.venv/bin/python"
elif [[ -x "$ROOT/.venv/Scripts/python.exe" ]]; then
  # WSL 下同一个 .venv 目录能看到 Scripts/python.exe，但那是 Windows 二进制，跑不了。
  case "$(uname -s)" in
    Linux) die "当前是 WSL，但 .venv 是 Windows 建的（.venv/Scripts/python.exe）。
    WSL 里请另建一个 Linux venv：
    python3 -m venv .venv && .venv/bin/pip install -r backend/requirements.lock.txt" ;;
  esac
  PY="$ROOT/.venv/Scripts/python.exe"
else
  die "找不到 .venv。先建虚拟环境：
    python3 -m venv .venv                                    # macOS / Linux / WSL
    python -m venv .venv                                     # Windows（Git Bash 也走这条）
    然后 .venv/bin/pip install -r backend/requirements.lock.txt
    或     .venv/Scripts/python.exe -m pip install -r backend/requirements.lock.txt"
fi
[[ -f "$ROOT/backend/app/main.py" ]] || die "没找到 backend/app/main.py，确认脚本在项目的 scripts/ 目录下。"

# 端口是否被占用。用 python 探测，避免依赖 ss/lsof 是否存在。
# 必须 v4/v6 都试：uvicorn 绑 127.0.0.1，而 vite 默认绑 ::1，只探 v4 会漏掉正在跑的 vite，
# 结果就是预检放行、静默起第二个 vite（它会自动跳 5174，脚本给出的地址就成了错的）。
port_busy() {
  "$PY" - "$1" <<'EOF'
import socket, sys
port = int(sys.argv[1])
for host, fam in (("127.0.0.1", socket.AF_INET), ("::1", socket.AF_INET6)):
    s = socket.socket(fam, socket.SOCK_STREAM)
    s.settimeout(0.5)
    try:
        if s.connect_ex((host, port)) == 0:
            sys.exit(0)
    except OSError:
        pass
    finally:
        s.close()
sys.exit(1)
EOF
}

db_health() {
  docker inspect --format '{{.State.Health.Status}}' "$DB_CONTAINER" 2>/dev/null || echo missing
}

# 按端口杀掉监听进程，仅用于停止时的兜底。拿不到工具就静默跳过，由调用方 warn。
port_kill() {
  local port="$1"
  case "$(uname -s)" in
    MINGW*|MSYS*|CYGWIN*)
      "$PY" - "$port" <<'EOF' 2>/dev/null || true
import subprocess, sys
port = sys.argv[1]
# 不能加 -p TCP：那只列 IPv4 表，而 vite 监听在 [::1] 上，得看含 TCPv6 的完整表。
out = subprocess.run(["netstat", "-ano"], capture_output=True, text=True).stdout
for line in out.splitlines():
    parts = line.split()
    # 形如: TCP   [::1]:5173    [::]:0      LISTENING  12345
    if (len(parts) >= 5 and parts[0].upper().startswith("TCP")
            and parts[3].upper() == "LISTENING" and parts[1].rsplit(":", 1)[-1] == port):
        subprocess.run(["taskkill", "/T", "/F", "/PID", parts[4]], capture_output=True)
EOF
      ;;
    *)
      if command -v lsof >/dev/null 2>&1; then
        lsof -ti:"$port" -sTCP:LISTEN 2>/dev/null | xargs -r kill 2>/dev/null || true
      elif command -v fuser >/dev/null 2>&1; then
        fuser -k "${port}/tcp" 2>/dev/null || true
      fi
      ;;
  esac
  return 0
}

# 在 Windows 的 Git Bash / WSL 里跑的是 Windows Python，事件循环仍是 Proactor，
# psycopg 会拒绝；uvicorn 靠 --reload 的 use_subprocess 才会切到 SelectorEventLoop。
case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) UVI_EXTRA="--reload" ;;
  *)                    UVI_EXTRA="" ;;
esac

# ---------- 1. 数据库 ----------
if [[ "$SKIP_DB" -eq 0 ]]; then
  step "检查 PostgreSQL"
  if ! docker info --format '{{.ServerVersion}}' >/dev/null 2>&1; then
    die "Docker 守护进程没运行，请先启动 Docker（或用 --no-db 跳过数据库检查）。"
  fi

  health="$(db_health)"
  if [[ "$health" != "running" && "$health" != "healthy" ]]; then
    step "启动数据库容器 $DB_CONTAINER"
    docker compose up -d db >/dev/null
  fi

  step "等待数据库就绪"
  ok=0
  for _ in $(seq 1 30); do
    [[ "$(db_health)" == "healthy" ]] && { ok=1; break; }
    sleep 2
  done
  [[ "$ok" -eq 1 ]] || die "数据库 60 秒内未变为 healthy，用 docker logs $DB_CONTAINER 排查。"
  echo "    数据库就绪"
fi

# ---------- 2. 端口 ----------
port_busy "$BACKEND_PORT" && die "端口 $BACKEND_PORT 已被占用。停掉旧实例，或改用 --port <其他端口>。"
if [[ "$MODE" == "dev" ]] && port_busy "$FRONTEND_PORT"; then
  die "端口 $FRONTEND_PORT（vite）已被占用，先停掉旧的 vite 进程。"
fi

# ---------- 3. 前端产物 / 依赖 ----------
if [[ "$MODE" == "single" ]]; then
  if [[ ! -f "$ROOT/frontend/dist/index.html" ]]; then
    warn "frontend/dist 不存在，正在构建前端（首次约 1 分钟）"
    [[ -d "$ROOT/frontend/node_modules" ]] || (cd frontend && npm install)
    (cd frontend && npm run build)
  fi
else
  if [[ ! -d "$ROOT/frontend/node_modules" ]]; then
    step "安装前端依赖（首次较慢）"
    (cd frontend && npm install)
  fi
fi

# ---------- 4. 启动 ----------
if [[ "$MODE" == "single" ]]; then
  step "启动后端（单进程模式，端口 $BACKEND_PORT）"
  echo "    访问 http://localhost:$BACKEND_PORT"
  echo "    Ctrl+C 停止"
  echo
  [[ "$DO_OPEN" -eq 1 ]] && { command -v xdg-open >/dev/null && xdg-open "http://localhost:$BACKEND_PORT" || true; }

  # 这里刻意不用 exec：多留一层 shell 才装得上 trap。带 --reload 时 uvicorn 另有一个
  # worker 子进程并继承监听 socket，父进程被硬杀会让 worker 变孤儿继续占着端口。
  # 走 trap 发信号，uvicorn 才会自己把 worker 收掉。
  # shellcheck disable=SC2086
  PYTHONPATH=backend "$PY" -m uvicorn app.main:app --port "$BACKEND_PORT" $UVI_EXTRA &
  uvi=$!

  stop_single() {
    trap - INT TERM
    echo
    for _ in 1 2; do
      kill -TERM "$uvi" 2>/dev/null || true
      sleep 2
      port_busy "$BACKEND_PORT" || break
    done
    if port_busy "$BACKEND_PORT"; then
      port_kill "$BACKEND_PORT" || true
      if port_busy "$BACKEND_PORT"; then
        warn "端口 $BACKEND_PORT 仍被占用，手动释放：netstat -ano | findstr :$BACKEND_PORT"
      fi
    fi
    echo "已停止。"
  }
  trap stop_single INT TERM

  rc=0
  wait "$uvi" || rc=$?
  exit "$rc"
fi

step "启动后端 + 前端（Ctrl+C 一起停止）"
echo

pids=()
cleanup() {
  trap - INT TERM EXIT
  echo
  step "正在停止..."
  for p in "${pids[@]:-}"; do
    kill "$p" 2>/dev/null || true
  done
  wait 2>/dev/null || true

  # 兜底：npm 不一定把信号转给 vite，uvicorn --reload 也另有一层子进程，
  # 上面 kill 完仍可能有孤儿占着端口。这里按端口再确认一次。
  for port in "$BACKEND_PORT" "$FRONTEND_PORT"; do
    if port_busy "$port"; then
      port_kill "$port" || true
      if port_busy "$port"; then
        warn "端口 $port 仍被占用，手动释放：netstat -ano | findstr :$port"
      fi
    fi
  done
  echo "已停止。"
}
trap cleanup INT TERM EXIT

PYTHONPATH=backend "$PY" -m uvicorn app.main:app --reload --port "$BACKEND_PORT" &
pids+=($!)
# exec 让 $! 就是 npm 本身而不是子 shell，kill 才打得中
( cd frontend && exec npm run dev ) &
pids+=($!)

# 等后端就绪再报地址，避免给了地址其实还没起来。
# 冷启动要加载 jieba 词典、连 7 个业务库、预热 MCP 连接器，实测约 8~20 秒；
# 首次跑会更久，给到 90 秒。用 127.0.0.1 而非 localhost：uvicorn 只绑 IPv4，
# 而 localhost 在部分环境会先试 ::1，白等一次回落。
step "等待后端就绪（首次启动较慢，最多 90 秒）"
ready=0
for i in $(seq 1 90); do
  if curl -sf --max-time 5 "http://127.0.0.1:$BACKEND_PORT/health" >/dev/null 2>&1; then ready=1; break; fi
  if (( i % 30 == 0 )); then echo "    已等 ${i} 秒..."; fi
  sleep 1
done
echo
if [[ "$ready" -eq 1 ]]; then
  printf '\033[32m后端已就绪: http://localhost:%s/health\033[0m\n' "$BACKEND_PORT"
else
  warn "90 秒内没等到后端就绪，看上面的日志排查。"
fi
printf '\033[32m前端访问:  http://localhost:%s/  （vite 把 /api 代理到 %s）\033[0m\n' "$FRONTEND_PORT" "$BACKEND_PORT"
echo "停止：Ctrl+C"
echo

[[ "$DO_OPEN" -eq 1 ]] && { command -v xdg-open >/dev/null && xdg-open "http://localhost:$FRONTEND_PORT" || true; }

wait
