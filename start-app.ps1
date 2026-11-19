<#
.SYNOPSIS
    一键启动课堂（学生自学）正式版。

.DESCRIPTION
    做四件事，别的一律交给 app.py：
      1) 设 PYTHONUTF8=1（课堂会打印中文与少量 emoji，GBK 控制台下会崩）；
      2) 查依赖（缺了告诉你要装哪两份 requirements）；
      3) 查 .env（缺了告诉你怎么复制；没填 key 只提示，不拦住启动）；
      4) 起 app.py（前台），默认等服务就绪后自动打开浏览器。

.PARAMETER NoBrowser
    不开浏览器（CI / 远程桌面 / 只想看日志时用）。

.PARAMETER Port
    覆盖端口，等价于设 PORT（默认 8000）。

.EXAMPLE
    powershell -ExecutionPolicy Bypass -NoProfile -File ./start-app.ps1

.EXAMPLE
    powershell -ExecutionPolicy Bypass -NoProfile -File ./start-app.ps1 --no-browser

.EXAMPLE
    powershell -ExecutionPolicy Bypass -NoProfile -File ./start-app.ps1 --port 8010
#>

$ErrorActionPreference = "Stop"

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

# 参数不用 param 块读：文档和任务书里写的是 `--no-browser`（Unix 习惯），PowerShell 原生
# 写法是 `-NoBrowser`。没有 param 块时两种都会原样落进 $args，少一层"参数找不到"的坑。
$noBrowser = $false
$port = 0
$i = 0
while ($i -lt $args.Count) {
    $a = [string]$args[$i]
    switch -Regex ($a) {
        '^--?no-?browser$' { $noBrowser = $true }
        '^--?port$'        { $i++; if ($i -lt $args.Count) { $port = [int]$args[$i] } }
        '^--?port=(\d+)$'  { $port = [int]$Matches[1] }
        '-NoBrowser'       { $noBrowser = $true }
        '-Port'            { $i++; if ($i -lt $args.Count) { $port = [int]$args[$i] } }
        default {
            Write-Host ("未知参数：{0}（支持 --no-browser / --port <端口>）" -f $a) -ForegroundColor Yellow
        }
    }
    $i++
}

$env:PYTHONUTF8 = "1"
# 自己这份输出也钉成 UTF-8：输出被管道接走时 PowerShell 默认按控制台 ANSI 码页（GBK）写，
# 中文会变成乱码（日志、CI、`| Tee-Object` 都会中）。子进程的编码由 PYTHONUTF8 管。
try { [Console]::OutputEncoding = [System.Text.Encoding]::UTF8 } catch { }

Write-Host ("=" * 70)
Write-Host "  课堂（学生自学）· 启动检查"
Write-Host ("=" * 70)

# ---- 1) Python -------------------------------------------------------------
$py = Get-Command python -ErrorAction SilentlyContinue
if (-not $py) {
    Write-Host "  ✗ 找不到 python" -ForegroundColor Red
    Write-Host "    装 Python 3.12（安装时勾选 Add python.exe to PATH），重开一个终端再来。"
    exit 1
}
& $py.Source -c "import sys; print('  ✓ Python ' + sys.version.split()[0])"
if ($LASTEXITCODE -ne 0) {
    Write-Host "  ✗ python 起不来（应用商店占位符？），把真 Python 放到 PATH 前面" -ForegroundColor Red
    exit 1
}

# ---- 2) 依赖 ---------------------------------------------------------------
$required = @{ fastapi = "fastapi"; uvicorn = "uvicorn"; pptx = "python-pptx";
               dotenv = "python-dotenv"; lxml = "lxml"; PIL = "Pillow"; multipart = "python-multipart" }
$missing = @()
foreach ($mod in $required.Keys) {
    & $py.Source -c "import $mod" 2>$null
    if ($LASTEXITCODE -ne 0) { $missing += $required[$mod] }
}
if ($missing.Count -gt 0) {
    Write-Host ("  ✗ 缺依赖：{0}" -f ($missing -join ", ")) -ForegroundColor Red
    Write-Host "    装齐再来："
    Write-Host "      python -m pip install -r TeachingFlow\classroom\requirements.txt"
    Write-Host "      python -m pip install -r requirements.txt"
    exit 1
}
Write-Host "  ✓ 依赖齐全（fastapi / uvicorn / python-pptx / python-dotenv / lxml / Pillow / python-multipart）"

# 语音是"少了也能上"的降级项，只提示不拦 —— 但**版本要卡**：
# edge-tts < 7.3.1 算的还是微软已经不认的 v1 令牌，握手回来一律 403。那不是抖动，
# 是**每一句都失败**（重试救不了），课堂上表现为"只有字幕、没有声音"。
# 装旧版比没装更坑：没装至少这行提示会出来，旧版会一路静默到学生开口。
$edgeMin = [version]"7.3.1"
$pipUp = "{0} -m pip install -U ""edge-tts>={1}""" -f $py.Source, $edgeMin
$ttsVer = "$(& $py.Source -c "import edge_tts; print(edge_tts.__version__)" 2>$null)".Trim()
if ($LASTEXITCODE -ne 0 -or -not $ttsVer) {
    Write-Host "  ! 没装 edge-tts：课堂只有字幕、没有语音" -ForegroundColor Yellow
    Write-Host ("    装它：{0}" -f $pipUp)
} else {
    $ttsOk = $true
    try { $ttsOk = ([version]$ttsVer -ge $edgeMin) } catch { $ttsOk = $true }   # 认不出的版本放行
    if ($ttsOk) {
        Write-Host ("  ✓ 语音：edge-tts {0}" -f $ttsVer)
    } else {
        Write-Host ("  ! 语音：edge-tts {0} 太旧（需 >= {1}）—— 微软已改用 v2 令牌，旧版每一句都 403" -f $ttsVer, $edgeMin) -ForegroundColor Yellow
        Write-Host "    课堂照样能起，但只有字幕、没有声音。升级后重启："
        Write-Host ("      {0}" -f $pipUp)
    }
}

# ---- 3) .env ---------------------------------------------------------------
$repoEnv = Join-Path $root "PPT-Agent\.env"
$rootEnv = Join-Path $root ".env"
$envPath = $null
if (Test-Path $repoEnv) { $envPath = $repoEnv }
elseif (Test-Path $rootEnv) { $envPath = $rootEnv }

if (-not $envPath) {
    Write-Host "  ! 没找到 .env：模型与语音都会降级（备课走规则兜底、只有文字），服务照常能起" -ForegroundColor Yellow
    Write-Host "    要连模型就先复制一份再填 key："
    Write-Host "      Copy-Item .env.example PPT-Agent\.env   # 然后填 DEEPSEEK_API_KEY"
} else {
    $hasKey = Select-String -Path $envPath -Pattern '^\s*DEEPSEEK_API_KEY\s*=\s*\S' -Quiet
    if ($hasKey) {
        Write-Host ("  ✓ 配置：{0}（DEEPSEEK_API_KEY 已填）" -f $envPath)
    } else {
        Write-Host ("  ! 配置：{0} 里 DEEPSEEK_API_KEY 还是空的" -f $envPath) -ForegroundColor Yellow
        Write-Host "    照样能起：备课走规则兜底（照读课件），讲解不会像老师讲的话。"
        Write-Host "    要真备课：在其中填 DEEPSEEK_API_KEY=... 后重启即可。"
    }
}

# ---- 4) 端口 ---------------------------------------------------------------
if ($port -gt 0) {
    $env:PORT = "$port"
} elseif ($env:PORT) {
    $port = [int]$env:PORT
} else {
    $port = 8000
}
$url = "http://127.0.0.1:$port/"

# ---- 5) 等就绪后开浏览器（后台等，前台照常起服务） -------------------------
if (-not $noBrowser) {
    Start-Job -ScriptBlock {
        param($target, $p)
        for ($n = 0; $n -lt 120; $n++) {
            Start-Sleep -Milliseconds 500
            $client = New-Object System.Net.Sockets.TcpClient
            try {
                $client.Connect("127.0.0.1", $p)
                $client.Close()
                Start-Process $target
                return
            } catch {
                # 端口还没起来，继续等
            }
        }
    } -ArgumentList $url, $port | Out-Null
    Write-Host "  → 服务就绪后会自动打开 $url（不想开浏览器加 --no-browser）"
} else {
    Write-Host "  → 不自动开浏览器（--no-browser）；就绪后自己访问 $url"
}

# ---- 6) 起服务 -------------------------------------------------------------
Write-Host ("=" * 70)
Write-Host "  启动中…（Ctrl+C 停止；改了 .env 要重启才生效）"
Write-Host ("=" * 70)
& $py.Source "app.py"
exit $LASTEXITCODE
