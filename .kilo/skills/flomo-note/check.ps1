# check.ps1 - flomo 卡片库只读质检（包装 audit.py 引擎）
# 按 .kilo/skills/flomo-note/SKILL.md 质量标尺校验 memo/index 卡片。
# 用法: powershell -ExecutionPolicy Bypass -File check.ps1 [-Strict]
# 脚本只读，不修改任何卡片文件。Strict 模式遇错误级缺陷退出码 1。
# 质检逻辑位于同目录 audit.py（引擎），本文件仅做 PS 入口与参数转发。

param([switch]$Strict, [switch]$NoReport)

$ErrorActionPreference = 'Stop'
$scriptDir = Split-Path -Parent $PSCommandPath
if (-not $scriptDir) { $scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path }
if (-not $scriptDir) { $scriptDir = 'D:\OpenClaw\flomo-note\.kilo\skills\flomo-note' }

$py = Join-Path $scriptDir 'audit.py'
$pyExe = $null
if (Get-Command python -ErrorAction SilentlyContinue) { $pyExe = 'python' }
elseif (Get-Command python3 -ErrorAction SilentlyContinue) { $pyExe = 'python3' }

if (-not $pyExe) {
    Write-Output "ERROR: python / python3 not found on PATH. The engine is audit.py in this folder."
    exit 2
}

$argList = @($py)
if ($Strict) { $argList += '-s' }
if ($NoReport) { $argList += '--no-report' }

& $pyExe @argList
exit $LASTEXITCODE