# 一次性安装：找一个能用的 Python，装依赖，生成 config.json
# 用法：双击 install.cmd，或在命令行里 powershell -File install.ps1
#
# ⚠️ 这个文件必须保存成「UTF-8 with BOM」+ CRLF，否则 Windows PowerShell 5.1
#    会按 ANSI 解码中文，报「字符串缺少终止符」。改完请确认文件头是 EF BB BF。

$ErrorActionPreference = 'Stop'
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $here
try { chcp 65001 > $null } catch {}

Write-Host "== DeepSeek × 个人微信 · 一次性安装 ==" -ForegroundColor Cyan
Write-Host ""

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
    Write-Host "[1/3] 找不到 64 位的 Python" -ForegroundColor Red
    Write-Host "      先装一个（安装时勾选 Add to PATH）："
    Write-Host "      https://www.python.org/downloads/windows/"
    exit 1
}
Write-Host "[1/3] 使用 Python：$py $($pyExtra -join ' ')" -ForegroundColor Green

$req = Join-Path $here 'requirements.txt'
if (-not (Test-Path $req)) {
    Write-Host "[2/3] 找不到 requirements.txt，是不是没把整个仓库下下来？" -ForegroundColor Red
    exit 1
}

Write-Host "[2/3] 安装依赖（首次要下载几百 MB，等几分钟）…"
& $py @pyExtra -m pip install -r $req
if ($LASTEXITCODE -ne 0) {
    Write-Host ""
    Write-Host "      安装失败。国内网络可以换镜像再试：" -ForegroundColor Yellow
    Write-Host "      $py $($pyExtra -join ' ') -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple"
    exit $LASTEXITCODE
}
Write-Host "      依赖就绪" -ForegroundColor Green

Write-Host "[3/3] 生成配置…"
$cfg = Join-Path $here 'config.json'
if (Test-Path $cfg) {
    Write-Host "      config.json 已存在，保持不动" -ForegroundColor Green
} else {
    Copy-Item (Join-Path $here 'config.example.json') $cfg
    Write-Host "      已生成 config.json" -ForegroundColor Green
}

Write-Host ""
Write-Host "装好了。接下来手动做两件事：" -ForegroundColor Cyan
Write-Host "  1) 打开 config.json 填 api_key（或设环境变量 DEEPSEEK_API_KEY）"
Write-Host "  2) 填 whitelist：先只放你自己的 wxid —— 留空会回复所有人的私聊"
Write-Host ""
Write-Host "然后双击 check.cmd 自检，四项全绿再双击 run.cmd 开跑。"