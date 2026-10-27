# MAT 教学流水线 —— 一键安装 + 自检（Windows PowerShell）
#
# 用法（仓库根目录）：
#     powershell -ExecutionPolicy Bypass -File .\setup.ps1
#
# 前提：Python 3.10+、git。
$ErrorActionPreference = "Stop"
$root = $PSScriptRoot

Write-Host "`n== 1/5 准备 ppt-master（出片 skill，第三方仓库）==" -ForegroundColor Cyan
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

Write-Host "`n== 2/5 安装依赖 ==" -ForegroundColor Cyan
python -m pip install --upgrade pip
python -m pip install -r (Join-Path $root "requirements.txt")
$skillReq = Join-Path $skill "requirements.txt"
if (Test-Path $skillReq) { python -m pip install -r $skillReq }

Write-Host "`n== 3/5 生成 .env 模板 ==" -ForegroundColor Cyan
$envFile = Join-Path $root "PPT-Agent\.env"
if (Test-Path $envFile) {
    Write-Host "  已存在，跳过"
} else {
    Copy-Item (Join-Path $root ".env.example") $envFile
    Write-Host "  已生成 $envFile —— 请填入 DEEPSEEK_API_KEY" -ForegroundColor Yellow
}

Write-Host "`n== 4/5 离线自检（不花钱）==" -ForegroundColor Cyan
Push-Location (Join-Path $root "TeachingFlow")
python tools\smoke_cases.py
python tools\prompt_lint.py
Pop-Location

Write-Host "`n== 5/5 课堂子系统（TeachingFlow\classroom，可选）==" -ForegroundColor Cyan
$classroom = Join-Path $root "TeachingFlow\classroom"
$classroomReq = Join-Path $classroom "requirements.txt"
if (Test-Path $classroomReq) {
    python -m pip install -r $classroomReq
} else {
    Write-Host "  没找到 $classroomReq，跳过依赖安装" -ForegroundColor Yellow
}
$classroomEnv = Join-Path $classroom ".env"
if (Test-Path $classroomEnv) {
    Write-Host "  已存在，跳过"
} else {
    Copy-Item (Join-Path $classroom ".env.example") $classroomEnv
    Write-Host "  已生成 $classroomEnv —— 请设 DECK_ID（留空 = 手写演示课件；缺 .env 时课堂测试有 10 条失败）" -ForegroundColor Yellow
}
$agentEnv = Join-Path $root "PPT-Agent\.env"
$hasKey = (Test-Path $agentEnv) -and ((Get-Content $agentEnv -Raw) -match "(?m)^\s*DEEPSEEK_API_KEY\s*=\s*\S")
if ($hasKey) {
    Write-Host "  检测到 PPT-Agent\.env 里的 DEEPSEEK_API_KEY：课堂侧设 LLM_PROVIDER=deepseek 即复用同一条 key"
} else {
    Write-Host "  PPT-Agent\.env 里还没有 DEEPSEEK_API_KEY：课堂默认用本地 ollama，或先填 key 再设 LLM_PROVIDER=deepseek" -ForegroundColor Yellow
}

Write-Host "`n完成。下一步：" -ForegroundColor Green
Write-Host @"
    cd TeachingFlow
    python flow.py "我想简单学一下《祥林嫂》这篇课文"
    python flow.py "我想系统学习高中语文" --no-input
    python flow.py "…" --mock --to plan        # 不花 token 验链路

    课堂（学生自学）正式版 —— 流水线出片后会自动备课，也可单独起服务：
    powershell -ExecutionPolicy Bypass -File .\start-app.ps1       # → http://127.0.0.1:8000
    # 或：python app.py   （只起服务，不开浏览器）
    详见 TeachingFlow\classroom\README.md 与 TeachingFlow\classroom\docs\使用说明.md
"@ -ForegroundColor Gray
