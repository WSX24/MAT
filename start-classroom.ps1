# MAT 课堂子系统 —— 启动脚本（Windows PowerShell）
#
# 用法（仓库根目录；参数原样透传给 TeachingFlow\classroom\run.py）：
#     powershell -ExecutionPolicy Bypass -File .\start-classroom.ps1
#     powershell -ExecutionPolicy Bypass -File .\start-classroom.ps1 decks
#     powershell -ExecutionPolicy Bypass -File .\start-classroom.ps1 plan <deck_id>
#     powershell -ExecutionPolicy Bypass -File .\start-classroom.ps1 audit <deck_id>
#
# 前提：先跑过 setup.ps1（装依赖、生成 TeachingFlow\classroom\.env）。
# 不带参数 = python run.py = 起课堂服务 http://127.0.0.1:8000
param(
    [Parameter(ValueFromRemainingArguments = $true)]
    [string[]]$RunArgs
)

$ErrorActionPreference = "Stop"
$root = $PSScriptRoot
$classroom = Join-Path $root "TeachingFlow\classroom"

if (-not (Get-Command python -ErrorAction SilentlyContinue)) {
    Write-Host "错误：没找到 python。先装 Python 3.10+ 并加入 PATH，再重跑 setup.ps1" -ForegroundColor Red
    exit 1
}

$envFile = Join-Path $classroom ".env"
if (-not (Test-Path $envFile)) {
    Write-Host "错误：缺少 $envFile" -ForegroundColor Red
    Write-Host "  先生成一份配置（在 TeachingFlow\classroom 目录下执行）：" -ForegroundColor Yellow
    Write-Host "      copy .env.example .env" -ForegroundColor Gray
    Write-Host "  至少要设 DECK_ID（留空 = 用手写演示课件）；配 deepseek 时复用 PPT-Agent\.env 的 key。" -ForegroundColor Gray
    Write-Host "  详见 TeachingFlow\classroom\README.md 第 1 节。" -ForegroundColor Gray
    exit 1
}

$argv = @()
if ($RunArgs) { $argv = $RunArgs }

# 中文 Windows 的控制台默认是 GBK，而课堂会打印 ✅❌⚠️ 这类字符：不开 UTF-8 模式，
# python 在**导入期**就 UnicodeEncodeError，服务连 bind 的机会都没有（实测）。
# 代码侧 config.ensure_utf8_console() 也兜了一层，这里从源头把解释器设对。
$env:PYTHONUTF8 = "1"

# run.py 必须在本目录跑：它按自身位置找 web/、decks/、.env
Push-Location $classroom
try {
    python run.py @argv
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}
exit $code
