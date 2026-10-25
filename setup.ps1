# MAT 教学流水线 —— 一键安装 + 自检（Windows PowerShell）
#
# 用法（仓库根目录）：
#     powershell -ExecutionPolicy Bypass -File .\setup.ps1
#
# 前提：Python 3.10+、git。
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

Write-Host "`n== 1/4 准备 ppt-master（出片 skill，第三方仓库）==" -ForegroundColor Cyan
$skill = Join-Path $root "ppt-master\skills\ppt-master"
if (Test-Path $skill) {
    Write-Host "  已存在，跳过"
} else {
    if (-not (Get-Command git -ErrorAction SilentlyContinue)) {
        Write-Host "  ❌ 没装 git。请手动: git clone https://github.com/hugohe3/ppt-master.git ppt-master" -ForegroundColor Red
        exit 1
    }
    git clone --depth 1 https://github.com/hugohe3/ppt-master.git (Join-Path $root "ppt-master")
}

Write-Host "`n== 2/4 安装依赖 ==" -ForegroundColor Cyan
python -m pip install --upgrade pip
python -m pip install -r (Join-Path $root "requirements.txt")
$skillReq = Join-Path $skill "requirements.txt"
if (Test-Path $skillReq) { python -m pip install -r $skillReq }

Write-Host "`n== 3/4 生成 .env 模板 ==" -ForegroundColor Cyan
$envFile = Join-Path $root "PPT-Agent\.env"
if (Test-Path $envFile) {
    Write-Host "  已存在，跳过"
} else {
    Copy-Item (Join-Path $root ".env.example") $envFile
    Write-Host "  已生成 $envFile —— 请填入 DEEPSEEK_API_KEY" -ForegroundColor Yellow
}

Write-Host "`n== 4/4 离线自检（不花钱）==" -ForegroundColor Cyan
Push-Location (Join-Path $root "TeachingFlow")
python tools\smoke_cases.py
python tools\prompt_lint.py
Pop-Location

Write-Host "`n完成。下一步：" -ForegroundColor Green
Write-Host @"
    cd TeachingFlow
    python flow.py "我想简单学一下《祥林嫂》这篇课文"
    python flow.py "我想系统学习高中语文" --no-input
    python flow.py "…" --mock --to plan        # 不花 token 验链路
"@ -ForegroundColor Gray
