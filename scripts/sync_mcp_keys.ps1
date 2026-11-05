# 把本地 TinyFish key（data/tf_*.txt）合并后推到服务器，并触发 /mcp/reload 热更。
# 用法：pwsh -File scripts\sync_mcp_keys.ps1
# 依赖：ssh 别名 dedirock 可用（同 deploy.ps1）。
[CmdletBinding()]
param(
    [string]$DeployHost = "dedirock",
    [string]$DeployPath = "/root/bit-api",
    [string]$KeyGlob = "data/tf_*.txt",
    [switch]$SkipReload
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent (Split-Path -Parent $MyInvocation.MyCommand.Path)
Set-Location $root

# 1) 合并去重
$all = Get-ChildItem $KeyGlob -ErrorAction SilentlyContinue |
    ForEach-Object { Get-Content $_.FullName -ErrorAction SilentlyContinue } |
    Where-Object { $_ -match "sk-tinyfish-" } |
    Sort-Object -Unique
if (-not $all) { throw "没有找到 $KeyGlob 里的 TinyFish key" }
$all | Set-Content "data\tf_all.txt"
Write-Host "[merge] $($all.Count) keys -> data\tf_all.txt"

# 2) 上传
& scp -o BatchMode=yes -o ConnectTimeout=15 "data\tf_all.txt" "${DeployHost}:$DeployPath/data/"
if ($LASTEXITCODE -ne 0) { throw "scp 失败" }
Write-Host "[push] 已上传到 ${DeployHost}:$DeployPath/data/tf_all.txt"

# 3) 热更（读服务器 .env.local 里的管理密钥）
if (-not $SkipReload) {
    $cmd = "K=`$(grep -E '^BITAPI_ADMIN_KEY=' $DeployPath/.env.local | cut -d= -f2); " +
           "curl -fsS --max-time 8 -X POST http://127.0.0.1:8081/mcp/reload -H `"Authorization: Bearer `$K`""
    $out = & ssh -o BatchMode=yes -o ConnectTimeout=15 $DeployHost $cmd
    Write-Host "[reload] $out"
}
