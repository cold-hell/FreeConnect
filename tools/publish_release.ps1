# Публикация FreeConnect 0.5.0 (GitHub + зеркало SourceCraft)
#
# Запуск (токены передаются параметрами, нигде не сохраняются):
#   .\tools\publish_release.ps1 -GitHubToken "ghp_..." -SourceCraftToken "..."
#
# Опции:
#   -SkipGitHub   — только зеркало
#   -SkipMirror   — только GitHub
#   -DryRun       — показать план, ничего не менять
#
# Требования: коммит с версией 0.5.0 и тег v0.5.0 уже созданы локально,
# установщик собран: installer\Output\FreeConnect-Setup.exe

[CmdletBinding()]
param(
    [string]$GitHubToken,
    [string]$SourceCraftToken,
    [string]$GitHubRepo = "cold-hell/FreeConnect",
    [string]$MirrorRepo = "git.sourcecraft.dev/millerloves/freeconnect-mirror.git",
    [string]$MirrorSlug = "millerloves/freeconnect-mirror",
    [switch]$SkipGitHub,
    [switch]$SkipMirror,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

# Git запускаем через Start-Process с перенаправлением потоков: иначе PowerShell 5.1
# превращает прогресс git в stderr в NativeCommandError, а EAP=Stop обрывает скрипт
# на первой же успешной команде.
function Invoke-Git {
    param([string[]]$GitArgs, [string]$WorkDir)
    $tmp = Join-Path $env:TEMP ("gitout-" + [guid]::NewGuid().ToString("N").Substring(0, 6))
    $errFile = "$tmp.err"; $outFile = "$tmp.out"
    # Start-Process -ArgumentList склеивает массив пробелами, поэтому аргументы с
    # пробелами ("-m", "Установщик FreeConnect 0.5.0") передаём одной строкой с кавычками.
    $argLine = (@($GitArgs | ForEach-Object { '"' + ([string]$_).Replace('"', '\"') + '"' }) -join " ")
    $p = Start-Process -FilePath $Git -ArgumentList $argLine -WorkingDirectory $WorkDir -Wait -NoNewWindow -PassThru `
        -RedirectStandardError $errFile -RedirectStandardOutput $outFile
    $code = $p.ExitCode
    $lines = @()
    if (Test-Path $outFile) { $lines += Get-Content $outFile -ErrorAction SilentlyContinue }
    if (Test-Path $errFile) { $lines += Get-Content $errFile -ErrorAction SilentlyContinue }
    Remove-Item $outFile, $errFile -Force -ErrorAction SilentlyContinue
    [pscustomobject]@{ Code = $code; Lines = @($lines) }
}

function Invoke-GitChecked {
    param([string[]]$GitArgs, [string]$WorkDir, [string]$What, [switch]$AllowNoChange)
    $r = Invoke-Git -GitArgs $GitArgs -WorkDir $WorkDir
    foreach ($l in @($r.Lines)) { if ($l) { Write-Host "    $l" } }
    if ($r.Code -eq 0) { return }
    # «нечего коммитить» — это не ошибка: желаемое состояние уже достигнуто
    # (например, установщик с тем же содержимым уже лежит в ветке зеркала).
    $noChange = (@($r.Lines) -join " ") -match "nothing to commit|nothing added to commit|no changes added to commit"
    if ($AllowNoChange -and $noChange) {
        Warn "$What — изменений нет, пропускаю"
        return
    }
    throw "$What (код $($r.Code))"
}

$RepoRoot = Split-Path -Parent $PSScriptRoot
$Git = Join-Path (Split-Path -Parent $RepoRoot) "tools\MinGit\cmd\git.exe"
if (-not (Test-Path $Git)) { $Git = "git" }

$Setup = Join-Path $RepoRoot "installer\Output\FreeConnect-Setup.exe"
$Tag = "v0.5.0"
$Version = "0.5.0"

function Info($m) { Write-Host "[*] $m" -ForegroundColor Cyan }
function Ok($m)   { Write-Host "[+] $m" -ForegroundColor Green }
function Warn($m) { Write-Host "[!] $m" -ForegroundColor Yellow }

# ---------------------------------------------------------------- проверки
if (-not (Test-Path $Setup)) { throw "нет установщика: $Setup" }
$sha = (Get-FileHash $Setup -Algorithm SHA256).Hash
$size = (Get-Item $Setup).Length
Info "установщик: $size Б, SHA-256 $sha"

Push-Location $RepoRoot
try {
    $head = (Invoke-Git -GitArgs @("rev-parse", "HEAD") -WorkDir $RepoRoot).Lines[0].Trim()
    $tagObj = (Invoke-Git -GitArgs @("cat-file", "-t", $Tag) -WorkDir $RepoRoot).Lines[0].Trim()
    # аннотированный тег указывает на объект-тег, поэтому разыменовываем до коммита
    $tagCommit = (Invoke-Git -GitArgs @("rev-parse", "$Tag^{commit}") -WorkDir $RepoRoot).Lines[0].Trim()
    Info "HEAD=$head  $Tag=$tagCommit ($tagObj)"
    if ($tagObj -ne "tag") { Warn "тег $Tag не аннотированный" }
    if ($tagCommit -ne $head) { Warn "тег указывает не на HEAD — проверь, что релиз собран из этого коммита" }
    $branch = (Invoke-Git -GitArgs @("rev-parse", "--abbrev-ref", "HEAD") -WorkDir $RepoRoot).Lines[0].Trim()
    if ($branch -ne "main") { Warn "текущая ветка '$branch', на GitHub ветка main" }
} finally { Pop-Location }

# ---------------------------------------------------------------- GitHub
if (-not $SkipGitHub) {
    if (-not $GitHubToken) { throw "нужен -GitHubToken (classic PAT, scope repo)" }
    $ghHeaders = @{
        Authorization          = "Bearer $GitHubToken"
        "User-Agent"           = "FreeConnect-Release"
        Accept                 = "application/vnd.github+json"
        "X-GitHub-Api-Version" = "2022-11-28"
    }

    Info "проверяю токен и права на $GitHubRepo"
    $me = Invoke-RestMethod -Uri "https://api.github.com/user" -Headers $ghHeaders -TimeoutSec 30
    Ok "токен принадлежит: $($me.login)"
    $repoInfo = Invoke-RestMethod -Uri "https://api.github.com/repos/$GitHubRepo" -Headers $ghHeaders -TimeoutSec 30
    if (-not $repoInfo.permissions.push) { throw "у токена нет прав push на $GitHubRepo" }
    Ok "права push есть, ветка по умолчанию: $($repoInfo.default_branch)"

    if ($DryRun) {
        Info "DryRun: git push origin main + $Tag; релиз $Tag с ассетом"
    } else {
        # push ветки и тега (токен только в URL этой команды, в конфиг не пишется)
        $authUrl = "https://x-access-token:$GitHubToken@github.com/$GitHubRepo.git"
        Info "git push main"
        Invoke-GitChecked -GitArgs @("push", $authUrl, "refs/heads/main:refs/heads/main") -WorkDir $RepoRoot -What "push ветки не удался"
        Info "git push $Tag"
        Invoke-GitChecked -GitArgs @("push", $authUrl, "refs/tags/$Tag") -WorkDir $RepoRoot -What "push тега не удался"
        Ok "ветка и тег отправлены"

        # тело релиза (важно: Get-Content -Raw возвращает строку, но в ветке if это надо
        # зафиксировать явно — иначе в $body попадает лишний объект FileInfo и GitHub
        # отвечает 422 «Invalid request»)
        $bodyFile = Join-Path $RepoRoot ".git\RELEASE_BODY_050.md"
        $body = ""
        if (Test-Path $bodyFile) { $body = [string](Get-Content -LiteralPath $bodyFile -Raw -Encoding UTF8) }
        if ($body -notmatch [regex]::Escape($sha)) {
            Warn "в теле релиза нет актуального SHA-256 — подставляю"
            $body = $body -replace "(?m)^[0-9A-Fa-f]{64}(\s+FreeConnect-Setup\.exe)", "$sha`$1"
        }

        # существующий релиз?
        $existing = $null
        try {
            $existing = Invoke-RestMethod -Uri "https://api.github.com/repos/$GitHubRepo/releases/tags/$Tag" `
                -Headers $ghHeaders -TimeoutSec 30
        } catch { }

        $payload = @{ tag_name = $Tag; name = "FreeConnect $Version"; body = $body; draft = $false; prerelease = $false } |
            ConvertTo-Json -Depth 4

        if ($existing) {
            Info "релиз $Tag уже есть (id=$($existing.id)) — обновляю описание и ассет"
            $rel = Invoke-RestMethod -Method Patch -Uri "https://api.github.com/repos/$GitHubRepo/releases/$($existing.id)" `
                -Headers $ghHeaders -Body ([Text.Encoding]::UTF8.GetBytes($payload)) -ContentType "application/json; charset=utf-8" -TimeoutSec 60
            foreach ($a in @($existing.assets)) {
                if ($a.name -eq "FreeConnect-Setup.exe") {
                    Info "удаляю старый ассет id=$($a.id)"
                    Invoke-RestMethod -Method Delete -Uri "https://api.github.com/repos/$GitHubRepo/releases/assets/$($a.id)" -Headers $ghHeaders -TimeoutSec 60 | Out-Null
                }
            }
        } else {
            Info "создаю релиз $Tag"
            $rel = Invoke-RestMethod -Method Post -Uri "https://api.github.com/repos/$GitHubRepo/releases" `
                -Headers $ghHeaders -Body ([Text.Encoding]::UTF8.GetBytes($payload)) -ContentType "application/json; charset=utf-8" -TimeoutSec 60
        }
        Ok "релиз: $($rel.html_url)"

        Info "загружаю установщик ($([math]::Round($size/1MB,1)) МБ), это может занять пару минут"
        $uploadUrl = "https://uploads.github.com/repos/$GitHubRepo/releases/$($rel.id)/assets?name=FreeConnect-Setup.exe"
        $upHeaders = $ghHeaders.Clone()
        $upHeaders["Content-Type"] = "application/octet-stream"
        $asset = Invoke-RestMethod -Method Post -Uri $uploadUrl -Headers $upHeaders -InFile $Setup -TimeoutSec 3600
        Ok "ассет загружен: $($asset.name), $($asset.size) Б, $($asset.browser_download_url)"
    }
}

# ---------------------------------------------------------------- зеркало
if (-not $SkipMirror) {
    if (-not $SourceCraftToken) { throw "нужен -SourceCraftToken (PAT SourceCraft с правом записи в репозиторий зеркала)" }

    $mirrorUrl = "https://anyname:$SourceCraftToken@$MirrorRepo"
    $work = Join-Path $env:TEMP ("fc-mirror-" + [guid]::NewGuid().ToString("N").Substring(0, 8))

    if ($DryRun) {
        Info "DryRun: клон $MirrorRepo -> $work, ветка main: latest.json → $Version; ветка dist: установщик"
    } else {
        Info "клон зеркала во временную папку: $work"
        Invoke-GitChecked -GitArgs @("clone", "--quiet", $mirrorUrl, $work) -WorkDir $RepoRoot -What "клон зеркала не удался (проверь токен SourceCraft)"

        [void](Invoke-GitChecked -GitArgs @("config", "user.name", "FreeConnect Dev") -WorkDir $work -What "config user.name")
        [void](Invoke-GitChecked -GitArgs @("config", "user.email", "dev@freeconnect.local") -WorkDir $work -What "config user.email")

        # --- ветка dist: сам установщик
        Info "ветка dist: обновляю установщик"
        $hasLocalDist = (Invoke-Git -GitArgs @("show-ref", "--verify", "--quiet", "refs/heads/dist") -WorkDir $work).Code -eq 0
        if ($hasLocalDist) {
            [void](Invoke-GitChecked -GitArgs @("checkout", "--quiet", "dist") -WorkDir $work -What "переключение на dist")
        } else {
            [void](Invoke-GitChecked -GitArgs @("checkout", "--quiet", "-b", "dist", "origin/dist") -WorkDir $work -What "создание локальной ветки dist")
        }
        Copy-Item $Setup (Join-Path $work "FreeConnect-Setup.exe") -Force
        [void](Invoke-GitChecked -GitArgs @("add", "--", "FreeConnect-Setup.exe") -WorkDir $work -What "git add установщика")
        Invoke-GitChecked -GitArgs @("commit", "--quiet", "-m", "Установщик FreeConnect $Version") -WorkDir $work `
            -What "commit в dist" -AllowNoChange
        if ((Invoke-Git -GitArgs @("status", "--porcelain") -WorkDir $work).Lines.Count -gt 0) {
            Invoke-GitChecked -GitArgs @("push", "--quiet", "origin", "dist") -WorkDir $work -What "push ветки dist не удался"
            Ok "ветка dist обновлена"
        } else {
            Ok "ветка dist уже содержит этот установщик"
        }

        # --- ветка main: latest.json
        Info "ветка main: обновляю latest.json"
        [void](Invoke-GitChecked -GitArgs @("checkout", "--quiet", "main") -WorkDir $work -What "переключение на main")
        [void](Invoke-GitChecked -GitArgs @("pull", "--quiet", "origin", "main") -WorkDir $work -What "git pull main")
        $latest = [ordered]@{
            version = $Tag
            zipball = "https://codeload.sourcecraft.tech/$MirrorSlug/zipball/refs/heads/dist"
            exe     = "FreeConnect-Setup.exe"
            notes   = "Обход Telegram стал заметно надёжнее, VPN для Discord — стабильнее."
        }
        $json = $latest | ConvertTo-Json -Depth 3
        # ConvertTo-Json экранирует кириллицу в \uXXXX — это допустимо, но читаемее UTF-8
        [System.IO.File]::WriteAllText((Join-Path $work "latest.json"), $json, (New-Object System.Text.UTF8Encoding($false)))
        [void](Invoke-GitChecked -GitArgs @("add", "--", "latest.json") -WorkDir $work -What "git add latest.json")
        Invoke-GitChecked -GitArgs @("commit", "--quiet", "-m", "Зеркало: FreeConnect $Version") -WorkDir $work `
            -What "commit в main" -AllowNoChange
        Invoke-GitChecked -GitArgs @("push", "--quiet", "origin", "main") -WorkDir $work -What "push ветки main не удался"
        Ok "latest.json обновлён на $Tag"
        Get-Content (Join-Path $work "latest.json") -Raw

        Remove-Item $work -Recurse -Force -ErrorAction SilentlyContinue
    }
}

# ---------------------------------------------------------------- проверка
if (-not $DryRun) {
    Info "проверяю канал обновлений (как это делает приложение)"
    Start-Sleep -Seconds 5
    try {
        $meta = Invoke-RestMethod -Uri "https://api.github.com/repos/$GitHubRepo/releases/latest" `
            -Headers @{ "User-Agent" = "FreeConnect" } -TimeoutSec 30
        Ok "GitHub: latest = $($meta.tag_name)"
        foreach ($a in @($meta.assets)) { Ok "  ассет: $($a.name) $($a.size) Б" }
    } catch { Warn "GitHub latest не прочитался: $($_.Exception.Message)" }

    try {
        $tmp = Join-Path $env:TEMP "fc-mirror-check.zip"
        Invoke-WebRequest -Uri "https://codeload.sourcecraft.tech/$MirrorSlug/zipball/refs/heads/main" `
            -OutFile $tmp -TimeoutSec 60 -UseBasicParsing
        Add-Type -AssemblyName System.IO.Compression.FileSystem
        $z = [System.IO.Compression.ZipFile]::OpenRead($tmp)
        $e = $z.Entries | Where-Object { $_.FullName -like "*latest.json" } | Select-Object -First 1
        $sr = New-Object System.IO.StreamReader($e.Open())
        $mirrorJson = $sr.ReadToEnd(); $z.Dispose(); Remove-Item $tmp -Force
        Ok "Зеркало latest.json:"
        $mirrorJson
    } catch { Warn "зеркало не прочиталось: $($_.Exception.Message)" }
}

Ok "Готово."
