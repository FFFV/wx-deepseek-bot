# 启动器：自动挑一个能用的 Python，然后跑 wx_bot.py
# 用法：
#   .\run.ps1 --check
#   .\run.ps1 --dry-run
#   .\run.ps1
param([Parameter(ValueFromRemainingArguments = $true)] $Rest)

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here

# 控制台按 UTF-8 显示中文
try { chcp 65001 > $null } catch {}

$vendor = Join-Path $here 'vendor'

function Test-Python($exe, $extra) {
    if (-not $exe) { return $false }
    try {
        # Windows 商店的 python.exe 只是个占位符，会直接报错退出，用 try/catch 吞掉
        $bits = & $exe @extra -c "import struct;print(struct.calcsize('P')*8)" 2>$null
    } catch {
        return $false
    }
    return ("$bits".Trim() -eq '64')
}

function Test-ImportOk($exe, $extra) {
    try {
        & $exe @extra -c "import wechatauto" 2>$null | Out-Null
        return ($LASTEXITCODE -eq 0)
    } catch {
        return $false
    }
}

$py = $null
$pyExtra = @()

foreach ($candidate in @(
        @{ exe = 'py';      extra = @('-3.12') },
        @{ exe = 'py';      extra = @('-3') },
        @{ exe = 'python';  extra = @() },
        @{ exe = 'python3'; extra = @() }
    )) {
    if (Test-Python $candidate.exe $candidate.extra) {
        $py = $candidate.exe
        $pyExtra = $candidate.extra
        break
    }
}

if (-not $py) {
    Write-Host "找不到 64 位的 Python。请先安装 Python 3.12（64 位）：https://www.python.org/downloads/windows/" -ForegroundColor Yellow
    Write-Host "然后运行： pip install wechatauto-replica" -ForegroundColor Yellow
    exit 1
}

Write-Host "使用 Python：$py $($pyExtra -join ' ')" -ForegroundColor DarkGray

if (-not (Test-ImportOk $py $pyExtra)) {
    if (Test-Path $vendor) {
        # 用 --target 装到 vendor 时，pywin32 的 .pth 不会执行，这里手工补路径
        $env:PYTHONPATH = @(
            $vendor,
            (Join-Path $vendor 'pythonwin'),
            (Join-Path $vendor 'win32'),
            (Join-Path $vendor 'win32\lib')
        ) -join ';'
        $env:PATH = (Join-Path $vendor 'pywin32_system32') + ';' + $env:PATH
        Write-Host "已挂载本地 vendor 依赖" -ForegroundColor DarkGray
    } else {
        Write-Host "缺少 wechatauto-replica，正在安装…" -ForegroundColor Yellow
        & $py @pyExtra -m pip install wechatauto-replica
        if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }
    }
}

& $py @pyExtra -B -X utf8 (Join-Path $here 'wx_bot.py') @Rest
exit $LASTEXITCODE
