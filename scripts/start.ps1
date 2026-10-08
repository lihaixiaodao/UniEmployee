<#
.SYNOPSIS
  一键启动 UniEmployee 前后端（Windows / PowerShell）。

.DESCRIPTION
  与 scripts/start.sh 行为一致，供 Windows 使用。

  Windows 特有注意：本项目用 psycopg 异步连 PostgreSQL，而 Windows 默认的
  ProactorEventLoop 不被 psycopg 支持。uvicorn 只有在 use_subprocess 为真时
  才改用 SelectorEventLoop（见 uvicorn/config.py 的 use_subprocess ->
  uvicorn/loops/asyncio.py），因此这里两种模式都带 --reload：
  dev 模式本来就需要热重载；single 模式带上它是为了让事件循环变成 Selector，
  否则启动会直接失败并报 "Psycopg cannot use the 'ProactorEventLoop'"。

.PARAMETER Mode
  dev    （默认）后端 uvicorn --reload + 前端 vite 热重载，改代码即时生效，访问 5173。
  single 仅后端，使用已构建的 frontend/dist，单端口 8787，最接近实际使用形态。

.PARAMETER BackendPort
  后端端口，默认 8787。

.PARAMETER NoDb
  跳过 PostgreSQL 容器检查与启动（数据库已在别处运行时用）。

.PARAMETER Open
  启动后打开浏览器。

.EXAMPLE
  .\scripts\start.ps1
  .\scripts\start.ps1 -Mode single -Open
#>
[CmdletBinding()]
param(
    [ValidateSet('dev', 'single')]
    [string]$Mode = 'dev',

    [int]$BackendPort = 8787,

    [switch]$NoDb,

    [switch]$Open
)

# 原生命令的 stderr 会变成 ErrorRecord，全局 Stop 会误终止脚本，因此用 Continue + 显式检查。
$ErrorActionPreference = 'Continue'

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

$FrontendPort = 5173
$DbContainer = 'uniemployee-pg'

function Write-Step($msg) { Write-Host "==> $msg" -ForegroundColor Cyan }
function Write-Warn($msg) { Write-Host "!!! $msg" -ForegroundColor Yellow }
function Die($msg) { Write-Host "!!! $msg" -ForegroundColor Red; exit 1 }

function Test-PortBusy([int]$Port) {
    [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Get-DbHealth {
    $out = docker inspect --format '{{.State.Health.Status}}' $DbContainer 2>$null
    if ($LASTEXITCODE -ne 0) { return 'missing' }
    return "$out".Trim()
}

# 必须用 127.0.0.1，不能写 localhost：Windows 上 localhost 会先试 ::1，而 uvicorn
# 只绑 IPv4，那次失败的尝试要 ~2s 才回落到 127.0.0.1。首次尝试一旦超时，::1 这个失败
# 路径不会被缓存，于是每次请求都重走一遍——配小 TimeoutSec 会让轮询永远失败。
# 也不用 Invoke-WebRequest：PS 5.1 下它每次都有可观开销，HttpWebRequest 只要十几毫秒。
function Test-BackendReady([int]$Port) {
    try {
        $req = [System.Net.HttpWebRequest]::Create("http://127.0.0.1:$Port/health")
        $req.Timeout = 3000
        $resp = $req.GetResponse()
        $code = [int]$resp.StatusCode
        $resp.Close()
        return ($code -eq 200)
    } catch {
        return $false
    }
}

# ---------- 1. 前置检查 ----------

if (-not (Test-Path '.venv\Scripts\python.exe')) {
    Die "找不到 .venv。先建虚拟环境：`n    python -m venv .venv`n    .venv\Scripts\python.exe -m pip install -r backend\requirements.lock.txt"
}

if (-not (Test-Path 'backend\app\main.py')) {
    Die "没找到 backend\app\main.py，确认脚本放在项目的 scripts\ 目录下。"
}

if (-not $NoDb) {
    Write-Step '检查 PostgreSQL'
    docker info --format '{{.ServerVersion}}' > $null 2>&1
    if ($LASTEXITCODE -ne 0) {
        Die "Docker 守护进程没运行，请先启动 Docker Desktop（或用 -NoDb 跳过数据库检查）。"
    }

    $health = Get-DbHealth
    if ($health -ne 'running' -and $health -ne 'healthy') {
        Write-Step "启动数据库容器 $DbContainer"
        docker compose up -d db | Out-Null
        if ($LASTEXITCODE -ne 0) { Die 'docker compose up -d db 失败。' }
    }

    Write-Step '等待数据库就绪'
    $ok = $false
    for ($i = 0; $i -lt 30; $i++) {
        if ((Get-DbHealth) -eq 'healthy') { $ok = $true; break }
        Start-Sleep -Seconds 2
    }
    if (-not $ok) { Die "数据库 60 秒内未变为 healthy，用 docker logs $DbContainer 排查。" }
    Write-Host '    数据库就绪'
}

# ---------- 2. 端口检查 ----------

if (Test-PortBusy $BackendPort) {
    Die "端口 $BackendPort 已被占用。停掉旧实例，或改用 -BackendPort <其他端口>。`n    查看占用： Get-NetTCPConnection -LocalPort $BackendPort -State Listen | Select-Object OwningProcess"
}

if ($Mode -eq 'dev' -and (Test-PortBusy $FrontendPort)) {
    Die "端口 $FrontendPort（vite）已被占用，先停掉旧的 vite 进程。"
}

# ---------- 3. 前端产物 / 依赖 ----------

if ($Mode -eq 'single') {
    if (-not (Test-Path 'frontend\dist\index.html')) {
        Write-Warn 'frontend\dist 不存在，正在构建前端（首次约 1 分钟）'
        Push-Location frontend
        if (-not (Test-Path 'node_modules')) { npm install | Out-Null }
        npm run build
        $buildExit = $LASTEXITCODE
        Pop-Location
        if ($buildExit -ne 0) { Die '前端构建失败。' }
    }
} else {
    if (-not (Test-Path 'frontend\node_modules')) {
        Write-Step '安装前端依赖（首次较慢）'
        Push-Location frontend
        npm install
        $npmExit = $LASTEXITCODE
        Pop-Location
        if ($npmExit -ne 0) { Die 'npm install 失败。' }
    }
}

# ---------- 4. 启动 ----------

$backendCmd = "`$env:PYTHONPATH='backend'; .venv\Scripts\python.exe -m uvicorn app.main:app --reload --port $BackendPort"

if ($Mode -eq 'single') {
    Write-Step "启动后端（单进程模式，端口 $BackendPort）"
    Write-Host '    访问 http://localhost:' -NoNewline; Write-Host $BackendPort -ForegroundColor Green
    Write-Host '    Ctrl+C 停止'
    Write-Host ''
    if ($Open) { Start-Process "http://localhost:$BackendPort/" }
    Invoke-Expression $backendCmd
    exit $LASTEXITCODE
}

Write-Step '启动后端（新窗口 1）'
Start-Process powershell -WorkingDirectory $Root `
    -ArgumentList '-NoExit', '-NoProfile', '-Command', $backendCmd | Out-Null

Write-Step '启动前端 vite（新窗口 2）'
Start-Process powershell -WorkingDirectory "$Root\frontend" `
    -ArgumentList '-NoExit', '-NoProfile', '-Command', 'npm run dev' | Out-Null

# 轮询后端就绪，避免给了地址其实还没起来。
# 冷启动要加载 jieba 词典、连 7 个业务库、预热 MCP 连接器，实测约 8~20 秒；
# 首次跑（jieba 无缓存、npx 现拉包）会更久，所以给到 90 秒。
Write-Step '等待后端就绪（首次启动较慢，最多 90 秒）'
$ready = $false
for ($i = 0; $i -lt 180; $i++) {
    if (Test-BackendReady $BackendPort) { $ready = $true; break }
    if ($i % 20 -eq 19) { Write-Host ('    已等 {0} 秒...' -f [int](($i + 1) / 2)) -ForegroundColor DarkGray }
    Start-Sleep -Milliseconds 500
}

Write-Host ''
if ($ready) {
    Write-Host "后端已就绪: http://localhost:$BackendPort/health" -ForegroundColor Green
} else {
    Write-Warn "90 秒内没等到后端就绪，看窗口 1 的日志排查。"
}
Write-Host "前端访问:  http://localhost:$FrontendPort/  （vite 会把 /api 代理到 $BackendPort）" -ForegroundColor Green
Write-Host '停止：关掉那两个窗口，或 Ctrl+C。'
Write-Host ''

if ($Open) { Start-Process "http://localhost:$FrontendPort/" }
