# 这个脚本只能用 pwsh(PowerShell 7+)跑,不能用 powershell.exe(5.1)。
# 5.1 把字符串当原生命令参数传时不转义里面的双引号,而这里给 bash.exe -lc 传的
# 命令自带双引号 —— bash 收到的是被截断的命令,却仍然 exit 0。表现就是「上传
# 报成功、远端却没有文件」,再往下卡在完整性校验。下面那道版本闸门专门拦这种跑法,
# 免得下次又从报错反推半天。
#
# 本文件还必须保持 UTF-8 BOM:5.1 读不带 BOM 的文件按系统 ANSI 解码,中文注释被
# GBK 误读会吃掉行尾、把下一行代码并进注释 —— 连这道闸门自己都可能被吞掉。
# 两条都由 tests/test_deploy_script.py 盯着。
[CmdletBinding(SupportsShouldProcess = $true)]
param(
    [string]$DeployHost = "dedirock",
    [string]$DeployPath = "/root/bit-api",
    [string]$ServiceName = "bit-api",
    [string]$HealthUrl = "http://127.0.0.1:8081/health"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

if ($PSVersionTable.PSVersion.Major -lt 6) {
    throw "This script requires pwsh (PowerShell 7+). Windows PowerShell " +
        "$($PSVersionTable.PSVersion) does not escape embedded double quotes when " +
        "passing a command to bash.exe, so the source upload reports success while " +
        "nothing reaches the server. Run: pwsh -File deploy.ps1"
}

$deployRepoRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$deployBranch = "master"
$deployRemote = "origin"
$deploySshOptions = @(
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=15",
    "-o", "ConnectionAttempts=3",
    "-o", "ServerAliveInterval=10",
    "-o", "ServerAliveCountMax=6"
)

function Assert-LastExitCode {
    param([string]$Step)
    if ($LASTEXITCODE -ne 0) {
        throw "$Step failed (exit code $LASTEXITCODE)."
    }
}

function Invoke-DeploySsh {
    param(
        [string]$Command,
        [string]$Step,
        [int]$Attempts = 3
    )

    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        $output = @(& ssh @deploySshOptions $DeployHost $Command)
        $exitCode = $LASTEXITCODE
        if ($exitCode -eq 0) {
            return $output
        }
        if ($output.Count -gt 0) {
            $output | Write-Host
        }
        if ($attempt -eq $Attempts) {
            throw "$Step failed after $Attempts attempts (exit code $exitCode)."
        }
        $delay = 5 * $attempt
        Write-Warning "$Step failed (attempt $attempt/$Attempts); retrying in ${delay}s."
        Start-Sleep -Seconds $delay
    }
}

function Send-ProductionArchive {
    param(
        [string]$GitBash,
        [string]$UnixRepoRoot,
        [int]$Attempts = 3
    )

    $sshOptions = "-o BatchMode=yes -o ConnectTimeout=15 -o ConnectionAttempts=3 " +
        "-o ServerAliveInterval=10 -o ServerAliveCountMax=6"
    $remoteArchive = "$DeployPath/.deploy-source.tar.gz"
    $hashCommand = "cd '$UnixRepoRoot' && git archive --format=tar.gz HEAD | " +
        "sha256sum | cut -d ' ' -f 1"
    $archiveHash = (& $GitBash -lc $hashCommand).Trim()
    Assert-LastExitCode "Archive checksum"
    if ($archiveHash -notmatch '^[0-9a-f]{64}$') {
        throw "Archive checksum has an unexpected format."
    }
    $uploadCommand = "set -o pipefail; cd '$UnixRepoRoot' && " +
        "git archive --format=tar.gz HEAD | ssh $sshOptions $DeployHost " +
        "`"cat > '$remoteArchive'`""
    # 先校验收到的整包，再解压；然后逐字节校验浏览器真会去取的静态图标，
    # 防止 SSH 中断留下可通过 /health、却无法被浏览器解码的半截静态资源。
    # 靶子是「页面声明的图标」而不是「仓里最大的文件」：1254×1254 的母版
    # bit-api-icon-v3.png 页面已经不引了，校验它挡不住任何用户看得见的损坏。
    # 这份清单由 tests/test_portal.py 盯着，必须覆盖页面声明的每个图标。
    $verifyFiles = "static/bit-api-icon-32.png static/bit-api-icon-180.png"
    $activateCommand = "set -o pipefail; " +
        "test `"`$(sha256sum '$remoteArchive' | cut -d ' ' -f 1)`" = " +
        "'$archiveHash' && tar -tzf '$remoteArchive' >/dev/null && " +
        "tar -xzf '$remoteArchive' -C '$DeployPath' && " +
        "for f in $verifyFiles; do " +
        "a=`$(tar -xOzf '$remoteArchive' `"`$f`" | sha256sum | cut -d ' ' -f 1) || exit 1; " +
        "b=`$(sha256sum `"$DeployPath/`$f`" | cut -d ' ' -f 1) || exit 1; " +
        "test `"`$a`" = `"`$b`" || exit 1; " +
        "done && rm -f '$remoteArchive'"
    for ($attempt = 1; $attempt -le $Attempts; $attempt++) {
        & $GitBash -lc $uploadCommand
        $uploadExit = $LASTEXITCODE
        $activateExit = 1
        if ($uploadExit -eq 0) {
            & ssh @deploySshOptions $DeployHost $activateCommand
            $activateExit = $LASTEXITCODE
        }
        if ($uploadExit -eq 0 -and $activateExit -eq 0) {
            return $archiveHash
        }
        if ($attempt -eq $Attempts) {
            & ssh @deploySshOptions $DeployHost "rm -f '$remoteArchive'" *> $null
            throw "Source upload or integrity check failed after $Attempts attempts."
        }
        $delay = 5 * $attempt
        Write-Warning (
            "Source upload or integrity check failed (attempt $attempt/$Attempts); " +
            "retrying in ${delay}s.")
        Start-Sleep -Seconds $delay
    }
}

function Get-ActiveGitHubUser {
    $ghCommand = Get-Command gh.exe -ErrorAction SilentlyContinue
    if (-not $ghCommand) {
        return $null
    }

    $statusJson = & gh auth status --active --hostname github.com --json hosts 2>$null
    if ($LASTEXITCODE -ne 0 -or -not $statusJson) {
        return $null
    }

    $status = $statusJson | ConvertFrom-Json
    $account = $status.hosts.'github.com' |
        Where-Object { $_.active } |
        Select-Object -First 1
    if ($account) {
        return [string]$account.login
    }
    return $null
}

function Push-ProductionCommit {
    param([string]$OriginUrl)

    $targetGitHubUser = $null
    if ($OriginUrl -match 'github\.com[/:]([^/]+)/') {
        $targetGitHubUser = $Matches[1]
    }

    $originalGitHubUser = Get-ActiveGitHubUser
    $switchedGitHubUser = $false
    try {
        if ($targetGitHubUser -and $originalGitHubUser -and
            $targetGitHubUser -ne $originalGitHubUser) {
            Write-Host "[push] Switching GitHub CLI account to $targetGitHubUser"
            & gh auth switch --hostname github.com --user $targetGitHubUser
            Assert-LastExitCode "GitHub account switch"
            $switchedGitHubUser = $true
        }

        Write-Host "[push] $deployRemote/$deployBranch"
        & git -C $deployRepoRoot push $deployRemote $deployBranch
        Assert-LastExitCode "Git push"
    }
    finally {
        if ($switchedGitHubUser) {
            & gh auth switch --hostname github.com --user $originalGitHubUser
            if ($LASTEXITCODE -ne 0) {
                Write-Warning "Could not restore GitHub CLI account $originalGitHubUser."
            }
        }
    }
}

try {
    if ($DeployHost -notmatch '^[A-Za-z0-9][A-Za-z0-9._-]*$') {
        throw "DeployHost contains unsupported characters."
    }
    if ($DeployPath -notmatch '^/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)+$' -or
        ($DeployPath -split '/') -contains '..' -or
        ($DeployPath -split '/') -contains '.') {
        throw "DeployPath must be a precise absolute application path."
    }
    if ($ServiceName -notmatch '^[A-Za-z0-9][A-Za-z0-9@_.-]*$') {
        throw "ServiceName contains unsupported characters."
    }
    if ($HealthUrl -notmatch '^http://127\.0\.0\.1:[0-9]+/[A-Za-z0-9._/-]+$') {
        throw "HealthUrl must be a loopback HTTP URL."
    }

    & git -C $deployRepoRoot rev-parse --is-inside-work-tree *> $null
    Assert-LastExitCode "Git repository check"

    $currentBranch = (& git -C $deployRepoRoot branch --show-current).Trim()
    Assert-LastExitCode "Git branch check"
    if ($currentBranch -ne $deployBranch) {
        throw "Production deploy requires branch '$deployBranch'; current branch is '$currentBranch'."
    }

    $originUrl = (& git -C $deployRepoRoot remote get-url $deployRemote).Trim()
    Assert-LastExitCode "Git remote check"

    # 打包的是 git archive HEAD,未提交的改动根本不会上传。这里必须**拦下**而不是
    # 警告后继续:旧代码照样能启动、照样 /health 通过,于是脚本打印
    # "[done] Deployment is healthy" —— 人以为改了的东西上线了,实际一个都没上去。
    # 「报成功但啥也没生效」比直接失败难查得多,所以宁可不给部署。
    # 确实要带改动上线,先 commit;只想预览流程,用 -WhatIf。
    $dirtyFiles = @(& git -C $deployRepoRoot status --short)
    Assert-LastExitCode "Git status check"
    if ($dirtyFiles.Count -gt 0) {
        Write-Host "Uncommitted changes would NOT be uploaded (deploy packs 'git archive HEAD' only)," -ForegroundColor Yellow
        Write-Host "yet the deploy would still report success because the old code starts fine:" -ForegroundColor Yellow
        $dirtyFiles | ForEach-Object { Write-Host "  $_" -ForegroundColor Yellow }
        throw ("Refusing to deploy: {0} uncommitted path(s). Commit them first, " +
            "or re-run with -WhatIf to preview.") -f $dirtyFiles.Count
    }

    $gitBash = "C:\Program Files\Git\bin\bash.exe"
    if (-not (Test-Path -LiteralPath $gitBash)) {
        $bashCommand = Get-Command bash.exe -ErrorAction SilentlyContinue
        if (-not $bashCommand) {
            throw "Git Bash was not found. Install Git for Windows first."
        }
        $gitBash = $bashCommand.Source
    }

    if ($deployRepoRoot.Contains("'")) {
        throw "Repository paths containing a single quote are not supported."
    }
    $unixRepoRoot = (& $gitBash -lc "cygpath -u '$deployRepoRoot'").Trim()
    Assert-LastExitCode "Repository path conversion"

    if ($PSCmdlet.ShouldProcess($originUrl, "push $deployBranch")) {
        Push-ProductionCommit -OriginUrl $originUrl
    }

    if ($PSCmdlet.ShouldProcess("$DeployHost`:$DeployPath", "deploy current HEAD")) {
        Write-Host "[check] Remote application and service"
        Invoke-DeploySsh -Step "Remote preflight" -Command (
            "test -d '$DeployPath' && test -x '$DeployPath/venv/bin/python' " +
            "&& systemctl cat '$ServiceName' >/dev/null") | Out-Null

        Write-Host "[upload] Streaming compressed HEAD to $DeployHost`:$DeployPath"
        $archiveHash = Send-ProductionArchive -GitBash $gitBash -UnixRepoRoot $unixRepoRoot
        Write-Host "[verify] Archive and deployed icon passed integrity checks"

        Write-Host "[deps] Synchronizing pinned Python dependencies"
        Invoke-DeploySsh -Step "Dependency install" -Command (
            "cd '$DeployPath' && ./venv/bin/python -m pip install " +
            "--disable-pip-version-check -r requirements.txt")

        Write-Host "[restart] $ServiceName"
        Invoke-DeploySsh -Step "Service restart" -Command (
            "systemctl restart '$ServiceName'") | Out-Null

        Write-Host "[health] $HealthUrl"
        $healthCommand = "timeout 75 sh -c 'until curl -fsS --max-time 5 $HealthUrl >/dev/null; do sleep 2; done' && curl -fsS --max-time 5 $HealthUrl"
        $healthOutput = @(Invoke-DeploySsh -Step "Health check" -Command $healthCommand)
        $health = ($healthOutput -join "`n") | ConvertFrom-Json
        if ($health.status -ne "ok") {
            throw "Health endpoint returned status '$($health.status)'."
        }

        Write-Host "[done] Deployment is healthy." -ForegroundColor Green
        Write-Host ($healthOutput -join "`n")
    }
}
catch {
    Write-Error $_
    exit 1
}
