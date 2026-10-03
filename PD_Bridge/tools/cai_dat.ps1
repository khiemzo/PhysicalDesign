<#
  cai_dat.ps1 — cài PD_Bridge trên máy ngoài (chạy một lần; chạy lại khi đổi máy/thư mục dữ liệu).

    powershell -ExecutionPolicy Bypass -File "C:\Users\khiem\OneDrive\PD_Bridge\tools\cai_dat.ps1"

  Tuỳ chọn:
    -OneDriveAccount <email>            tài khoản OneDrive chứa PD_Bridge (mặc định khuongmat456@gmail.com)
    -StoreDir "D:\PD_Bridge_Kho"         kho project trên máy ngoài, KHÔNG đồng bộ OneDrive (mặc định %USERPROFILE%\PD_Bridge_Kho)
    -DataDir "D:\K\K\physical design"   thư mục dữ liệu (mặc định: giữ cấu hình cũ hoặc D:\K\K\physical design)
    -Yes                                trả lời Y cho mọi câu hỏi
    -AutoStart                          tự BẬT khi đăng nhập Windows (mặc định: KHÔNG — chỉ chạy khi gõ pdbat)
    -NoStart                            cài xong không bật ngay
    -Uninstall                          gỡ watcher (giữ nguyên câu hỏi/trả lời/kiến thức)
    -Runner routine -RoutineUrl <url> -RoutineToken <token>
                                        dự phòng khi không cài được Claude Code (gọi routine trên cloud)
#>
[CmdletBinding()]
param(
    [switch]$Uninstall,
    [ValidateSet('local', 'routine')][string]$Runner = 'local',
    [string]$DataDir = '',
    [string]$OneDriveAccount = 'khuongmat456@gmail.com',
    [string]$StoreDir = '',
    [string]$RoutineUrl = '',
    [string]$RoutineToken = '',
    [switch]$Yes,
    [switch]$NoStart,
    [switch]$AutoStart
)

$ErrorActionPreference = 'Stop'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$TaskName = 'PD_Bridge Watcher'
$HomeDir  = Join-Path $env:LOCALAPPDATA 'PD_Bridge'
$CfgPath  = Join-Path $HomeDir 'config.json'
$BinDir   = Join-Path $HomeDir 'bin'
$OffFlag  = Join-Path $HomeDir 'tat'
$SrcRoot  = Split-Path -Parent $PSScriptRoot            # thư mục PD_Bridge chứa tools\
$DefaultData = 'D:\K\K\physical design'
New-Item -ItemType Directory -Force -Path $HomeDir | Out-Null

function Say([string]$m, [string]$c = 'Gray') { Write-Host $m -ForegroundColor $c }
function Step([string]$m) { Say ''; Say "== $m" 'Cyan' }
function Ok([string]$m) { Say "  [OK] $m" 'Green' }
function Warn([string]$m) { Say "  [!]  $m" 'Yellow' }
function Ask([string]$q) {
    if ($Yes) { Say "  $q -> Y"; return $true }
    $a = Read-Host "  $q [Y/n]"
    return ($a -eq '' -or $a -match '^[yYcC]')
}
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' +
                [Environment]::GetEnvironmentVariable('Path', 'User') + ';' +
                (Join-Path $env:USERPROFILE '.local\bin')
}
function Has-Winget { return [bool](Get-Command winget -ErrorAction SilentlyContinue) }
function Winget-Install([string]$id, [string]$extra = '') {
    if (-not (Has-Winget)) { throw "Không có winget để cài $id — hãy cài thủ công rồi chạy lại script." }
    $wargs = "install -e --id $id --silent --accept-source-agreements --accept-package-agreements $extra"
    $p = Start-Process winget -ArgumentList $wargs -Wait -PassThru -NoNewWindow
    Refresh-Path
    return $p.ExitCode
}

function Stop-Watcher {
    try { Stop-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue } catch {}
    try {
        Get-CimInstance Win32_Process -ErrorAction SilentlyContinue |
            Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -and
                           ($_.CommandLine -match 'bridge\.py"?\s+watch' -or $_.CommandLine -match 'watcher\.ps1') } |
            ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    } catch {}
}

function Remove-Shortcuts {
    $desk = [Environment]::GetFolderPath('Desktop')
    foreach ($n in @('PD_Bridge - BAT.lnk', 'PD_Bridge - TAT.lnk', 'PD_Bridge - TRANG THAI.lnk')) {
        $f = Join-Path $desk $n
        if (Test-Path $f) { Remove-Item $f -Force }
    }
}

function Remove-UserPath([string]$dir) {
    $cur = [Environment]::GetEnvironmentVariable('Path', 'User')
    if (-not $cur) { return }
    $new = ($cur -split ';' | Where-Object { $_ -and ($_.TrimEnd('\') -ne $dir.TrimEnd('\')) }) -join ';'
    if ($new -ne $cur) { [Environment]::SetEnvironmentVariable('Path', $new, 'User') }
}

function Remove-StartupShortcut {
    $lnk = Join-Path ([Environment]::GetFolderPath('Startup')) 'PD_Bridge Watcher.lnk'
    if (Test-Path $lnk) { Remove-Item $lnk -Force }
}

# =============================================================================== GỠ CÀI ĐẶT
if ($Uninstall) {
    Step 'Gỡ PD_Bridge watcher'
    Set-Content -Path $OffFlag -Value 'go cai dat' -Encoding UTF8
    Stop-Watcher
    try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue } catch {}
    Remove-StartupShortcut
    Remove-Shortcuts
    Remove-UserPath $BinDir
    if (Test-Path $BinDir) { Remove-Item $BinDir -Recurse -Force }
    Ok "Đã gỡ watcher. Cấu hình/nhật ký vẫn ở $HomeDir (xoá tay nếu muốn)."
    Ok 'Thư mục PD_Bridge trong OneDrive (câu hỏi, trả lời, kiến thức) được giữ nguyên.'
    exit 0
}

Say 'PD_Bridge — cài đặt trên máy ngoài' 'Cyan'
Say "Nguồn: $SrcRoot"

# =============================================================================== 1. ONEDRIVE CÁ NHÂN
Step "1. OneDrive ($OneDriveAccount)"
function Find-OneDriveFolder([string]$email) {
    # 1) tài khoản OneDrive có email đúng; 2) tài khoản Personal; 3) biến môi trường; 4) %USERPROFILE%\OneDrive
    $acc = @()
    try {
        Get-ChildItem 'HKCU:\Software\Microsoft\OneDrive\Accounts' -ErrorAction Stop | ForEach-Object {
            $p = Get-ItemProperty $_.PSPath -ErrorAction SilentlyContinue
            if ($p -and $p.UserFolder -and (Test-Path $p.UserFolder)) {
                $acc += [pscustomobject]@{ Path = $p.UserFolder; Email = "$($p.UserEmail)"; Name = $_.PSChildName }
            }
        }
    } catch {}
    $hit = $acc | Where-Object { $_.Email -ieq $email } | Select-Object -First 1
    if ($hit) { return $hit.Path }
    $hit = $acc | Where-Object { $_.Name -eq 'Personal' } | Select-Object -First 1
    if ($hit) { return $hit.Path }
    foreach ($v in @($env:OneDriveConsumer, $env:OneDrive, (Join-Path $env:USERPROFILE 'OneDrive'))) {
        if ($v -and (Test-Path $v) -and ((Split-Path $v -Leaf) -notmatch '^OneDrive - ')) { return $v }
    }
    return $null
}

$Bridge = $SrcRoot
$od = Find-OneDriveFolder $OneDriveAccount
if ($od) {
    $dest = Join-Path $od 'PD_Bridge'
    $srcFull = (Resolve-Path $SrcRoot).Path.TrimEnd('\')
    if ($srcFull -ieq $dest.TrimEnd('\')) {
        Ok "PD_Bridge đã nằm trong OneDrive: $dest"
    } elseif (Ask "Chuyển PD_Bridge vào OneDrive $OneDriveAccount ($dest)?") {
        Stop-Watcher
        New-Item -ItemType Directory -Force -Path $dest | Out-Null
        # dữ liệu: không ghi đè file mới hơn ở đích; tools: luôn lấy bản đang cài
        & robocopy "$srcFull" "$dest" /E /XO /R:2 /W:2 /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy lỗi (mã $LASTEXITCODE)" }
        & robocopy "$srcFull\tools" "$dest\tools" /E /R:2 /W:2 /NFL /NDL /NJH /NJS /NP | Out-Null
        if ($LASTEXITCODE -ge 8) { throw "robocopy tools lỗi (mã $LASTEXITCODE)" }
        if (-not (Test-Path (Join-Path $dest 'tools\bridge.py'))) { throw 'Sao chép chưa đầy đủ.' }
        $Bridge = $dest
        Ok "Đã sao chép sang $dest"
        if (Ask "Xoá bản cũ ở $srcFull (tránh gửi nhầm câu hỏi vào đó)?") {
            Set-Location $env:USERPROFILE
            try { Remove-Item $srcFull -Recurse -Force; Ok 'Đã xoá bản cũ.' }
            catch { Warn "Chưa xoá được bản cũ ($_) — xoá tay sau." }
        }
    }
} else {
    Warn "Chưa thấy OneDrive của $OneDriveAccount (đăng nhập ứng dụng OneDrive bằng tài khoản này rồi chạy lại)."
    Warn "Tạm dùng: $SrcRoot"
}
foreach ($d in @('cau_hoi', 'tong_hop', 'kien_thuc', 'so_tay')) { New-Item -ItemType Directory -Force -Path (Join-Path $Bridge $d) | Out-Null }
try { & attrib +P -U "$Bridge\*" /S /D 2>$null | Out-Null; & attrib +P -U "$Bridge" 2>$null | Out-Null } catch {}   # luôn giữ trên máy

# =============================================================================== 2. PYTHON
Step '2. Python'
function Find-Python {
    foreach ($n in @('python', 'python3')) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c -and $c.Source -notmatch 'WindowsApps') {
            $exe = (& $c.Source -c 'import sys;print(sys.executable)' 2>$null)
            if ($exe -and (Test-Path $exe.Trim())) { return $exe.Trim() }
        }
    }
    if (Get-Command py -ErrorAction SilentlyContinue) {
        $exe = (& py -3 -c 'import sys;print(sys.executable)' 2>$null)
        if ($exe -and (Test-Path $exe.Trim())) { return $exe.Trim() }
    }
    $g = Get-ChildItem "$env:LOCALAPPDATA\Programs\Python\Python3*\python.exe" -ErrorAction SilentlyContinue |
         Sort-Object FullName -Descending | Select-Object -First 1
    if ($g) { return $g.FullName }
    return $null
}
$py = Find-Python
if (-not $py) {
    if (Ask 'Chưa có Python. Cài Python 3.12 (winget, chỉ cho user hiện tại)?') {
        Winget-Install 'Python.Python.3.12' '--scope user' | Out-Null
        $py = Find-Python
    }
}
if (-not $py) { throw 'Không có Python. Cài từ https://www.python.org/downloads/ rồi chạy lại.' }
$ver = (& $py -c 'import sys;print(sys.version.split()[0])').Trim()
if ([version]$ver -lt [version]'3.9') { throw "Python $ver quá cũ (cần >= 3.9)." }
Ok "Python $ver : $py"
$fts = (& $py -c "import sqlite3;c=sqlite3.connect(':memory:');c.execute('create virtual table t using fts5(a)');print('ok')" 2>$null)
if ($fts -ne 'ok') { Warn 'SQLite của Python không có FTS5 — chỉ mục vẫn chạy nhưng chậm hơn.' }
& $py -m pip install --user --quiet --disable-pip-version-check --upgrade pypdf 2>$null | Out-Null
if ($LASTEXITCODE -eq 0) { Ok 'pypdf (đọc PDF cho chỉ mục)' } else { Warn 'Chưa cài được pypdf — PDF sẽ không được đánh chỉ mục (pip install pypdf).' }

# =============================================================================== 3. CLAUDE CODE
$claude = $null
if ($Runner -eq 'local') {
    Step '3. Claude Code'
    $hasBash = (Get-Command git -ErrorAction SilentlyContinue) -or (Test-Path "$env:ProgramFiles\Git\bin\bash.exe")
    if (-not $hasBash) {
        if (Ask 'Claude Code trên Windows cần Git for Windows (Git Bash). Cài Git (winget)?') {
            Winget-Install 'Git.Git' | Out-Null
        } else { Warn 'Chưa có Git Bash — Claude Code có thể không chạy được lệnh python.' }
    }
    function Find-Claude {
        $c = Get-Command claude -ErrorAction SilentlyContinue
        if ($c) { return $c.Source }
        foreach ($p in @("$env:USERPROFILE\.local\bin\claude.exe", "$env:APPDATA\npm\claude.cmd")) {
            if (Test-Path $p) { return $p }
        }
        return $null
    }
    $claude = Find-Claude
    if (-not $claude) {
        if (Ask 'Chưa có Claude Code. Cài bằng trình cài đặt chính thức (claude.ai/install.ps1)?') {
            Invoke-RestMethod 'https://claude.ai/install.ps1' | Invoke-Expression
            Refresh-Path
            $claude = Find-Claude
        }
    }
    if (-not $claude) { throw 'Chưa có Claude Code. Xem https://code.claude.com/docs rồi chạy lại, hoặc dùng -Runner routine.' }
    Ok "claude: $claude ($((& $claude --version 2>$null) -join ' '))"

    if ([Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY', 'User') -or [Environment]::GetEnvironmentVariable('ANTHROPIC_API_KEY', 'Machine')) {
        Warn 'Có biến ANTHROPIC_API_KEY trên máy. PD_Bridge sẽ gỡ nó khi gọi claude để luôn dùng gói Claude (không tốn tiền API).'
    }
    function Get-Auth { try { return ((& $claude auth status --json 2>$null) -join "`n") } catch { return '' } }
    $auth = Get-Auth
    if ($auth -notmatch '"loggedIn"\s*:\s*true') {
        Say '  Trình duyệt sẽ mở để đăng nhập Claude — chọn tài khoản Claude (Pro) của bạn.' 'White'
        & $claude auth login --claudeai
        $auth = Get-Auth
    }
    if ($auth -match '"authMethod"\s*:\s*"([^"]*)"' -and $Matches[1] -match 'api.?key|console') {
        Warn "Claude Code đang đăng nhập bằng API key ($($Matches[1])) — sẽ tốn tiền API."
        if (Ask 'Đăng xuất và đăng nhập lại bằng tài khoản Claude (gói Pro)?') {
            & $claude auth logout | Out-Null
            & $claude auth login --claudeai
            $auth = Get-Auth
        }
    }
    if ($auth -match '"loggedIn"\s*:\s*true') { Ok 'Đã đăng nhập Claude.' }
    elseif ($auth) { Warn 'Chưa đăng nhập. Mở PowerShell, gõ: claude  rồi đăng nhập; watcher sẽ tự chờ.' }
    else { Warn 'Không kiểm tra được đăng nhập (bản claude cũ?) — chạy "claude update".' }
} else {
    Step '3. Runner: routine (dự phòng)'
    if (-not $RoutineUrl) { $RoutineUrl = Read-Host '  URL API trigger của routine' }
    if (-not $RoutineToken) { $RoutineToken = Read-Host '  Token của routine' }
    Warn 'Routine phải được tạo tay trong Claude desktop app, bật "Require this computer" và API trigger.'
}

# =============================================================================== 4. THƯ MỤC DỮ LIỆU
Step '4. Thư mục dữ liệu physical design'
$old = $null
if (Test-Path $CfgPath) { try { $old = Get-Content $CfgPath -Raw -Encoding UTF8 | ConvertFrom-Json } catch {} }
if (-not $DataDir) {
    if ($old -and $old.data_dir -and (Test-Path $old.data_dir)) { $DataDir = $old.data_dir } else { $DataDir = $DefaultData }
}
if (-not (Test-Path $DataDir)) {
    Warn "Không thấy $DataDir — đang tìm thư mục 'physical design'…"
    $found = @()
    foreach ($drv in (Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Root -and (Test-Path $_.Root) })) {
        $found += Get-ChildItem $drv.Root -Directory -Recurse -Depth 3 -Filter 'physical design' -ErrorAction SilentlyContinue |
                  Select-Object -ExpandProperty FullName
    }
    if ($found.Count -ge 1 -and (Ask "Dùng $($found[0])?")) { $DataDir = $found[0] }
    else { $DataDir = Read-Host '  Nhập đường dẫn thư mục physical design' }
}
if (-not (Test-Path $DataDir)) { throw "Không thấy thư mục dữ liệu: $DataDir" }
Ok "Dữ liệu: $DataDir"

# =============================================================================== 5. CẤU HÌNH
Step '5. Cấu hình'
$cfg = [ordered]@{}
if ($old) { foreach ($p in $old.PSObject.Properties) { $cfg[$p.Name] = $p.Value } }
$cfg['bridge_dir'] = $Bridge
$cfg['data_dir']   = $DataDir
$cfg['python']     = $py
$cfg['claude_cmd'] = $claude
$cfg['runner']     = $Runner
if (-not $StoreDir) {
    if ($old -and $old.store_dir) { $StoreDir = $old.store_dir } else { $StoreDir = Join-Path $env:USERPROFILE 'PD_Bridge_Kho' }
}
if ($StoreDir -match 'OneDrive') { Warn "Kho đang nằm trong OneDrive ($StoreDir) — nên để ngoài OneDrive." }
New-Item -ItemType Directory -Force -Path (Join-Path $StoreDir 'du_an') | Out-Null
$cfg['store_dir']  = $StoreDir
Ok "Kho project (máy ngoài): $StoreDir"
if (-not $cfg.Contains('models')) {
    $cfg['models'] = [ordered]@{ nhanh = 'sonnet'; chuan = 'opus'; sau = 'opus'; tong_hop = 'sonnet'; fallback = 'sonnet' }
}
if ($Runner -eq 'routine') { $cfg['routine'] = [ordered]@{ url = $RoutineUrl; token = $RoutineToken; headers = @{} } }
$cfg | ConvertTo-Json -Depth 6 | Set-Content -Path $CfgPath -Encoding UTF8
Ok "Đã ghi $CfgPath"

# =============================================================================== 6. LỆNH BẬT / TẮT
Step '6. Lệnh bật/tắt (chỉ chạy khi bạn cần)'
Stop-Watcher
$bridgePy = Join-Path $Bridge 'tools\bridge.py'
$watcher  = Join-Path $Bridge 'tools\watcher.ps1'
$psExe = Join-Path $env:SystemRoot 'System32\WindowsPowerShell\v1.0\powershell.exe'

# 6a. lệnh pdbat / pdtat / pdtt / pd (gõ được ở mọi cửa sổ cmd/PowerShell mới)
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null
$cmds = [ordered]@{
    'pdbat' = 'start'
    'pdtat' = 'stop'
    'pdtt'  = 'status'
    'pd'    = '%*'
}
foreach ($k in $cmds.Keys) {
    $body = "@echo off`r`n`"$py`" `"$bridgePy`" $($cmds[$k])`r`n"
    [IO.File]::WriteAllText((Join-Path $BinDir "$k.cmd"), $body, (New-Object Text.UTF8Encoding($false)))
}
$userPath = [Environment]::GetEnvironmentVariable('Path', 'User')
if (-not $userPath) { $userPath = '' }
if (-not (($userPath -split ';') | Where-Object { $_.TrimEnd('\') -eq $BinDir.TrimEnd('\') })) {
    [Environment]::SetEnvironmentVariable('Path', (($userPath.TrimEnd(';') + ';' + $BinDir).TrimStart(';')), 'User')
}
if (-not (($env:Path -split ';') -contains $BinDir)) { $env:Path = "$env:Path;$BinDir" }
Ok "Lệnh: pdbat (bật) · pdtat (tắt) · pdtt (trạng thái) · pd <lệnh> — trong $BinDir (mở cửa sổ mới để dùng)"

# 6b. shortcut trên Desktop (double-click)
try {
    $ws = New-Object -ComObject WScript.Shell
    $desk = [Environment]::GetFolderPath('Desktop')
    foreach ($it in @(@('PD_Bridge - BAT', 'start', 'Bật PD_Bridge (chạy nền, ẩn)'),
                      @('PD_Bridge - TAT', 'stop', 'Tắt hẳn PD_Bridge'),
                      @('PD_Bridge - TRANG THAI', 'status', 'Xem PD_Bridge đang bật hay tắt'))) {
        $lnk = $ws.CreateShortcut((Join-Path $desk "$($it[0]).lnk"))
        $lnk.TargetPath = $py
        $lnk.Arguments = "`"$bridgePy`" $($it[1]) --cho 6"
        $lnk.WorkingDirectory = $HomeDir
        $lnk.Description = $it[2]
        $lnk.Save()
    }
    Ok 'Desktop: "PD_Bridge - BAT", "PD_Bridge - TAT", "PD_Bridge - TRANG THAI"'
} catch { Warn "Không tạo được shortcut Desktop ($_) — dùng lệnh pdbat/pdtat." }

# 6c. tự bật khi đăng nhập: chỉ khi chọn -AutoStart (mặc định tắt để không tốn tài nguyên)
Remove-StartupShortcut
try { Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction SilentlyContinue } catch {}
if ($AutoStart) {
    $arg = "-NoProfile -WindowStyle Hidden -ExecutionPolicy Bypass -Command `"Remove-Item -LiteralPath '$OffFlag' -ErrorAction SilentlyContinue; & '$watcher'`""
    $user = "$env:USERDOMAIN\$env:USERNAME"
    try {
        $action = New-ScheduledTaskAction -Execute $psExe -Argument $arg
        $trigger = New-ScheduledTaskTrigger -AtLogOn -User $user
        $settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
                    -ExecutionTimeLimit ([TimeSpan]::Zero) -MultipleInstances IgnoreNew
        $principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited
        Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger -Settings $settings `
                               -Principal $principal -Force | Out-Null
        Ok "Tự BẬT khi đăng nhập Windows (Task Scheduler '$TaskName'). Bỏ: chạy lại cai_dat.ps1 không có -AutoStart"
    } catch { Warn "Không tạo được Task Scheduler ($_) — bật tay bằng pdbat." }
} else {
    Ok 'Không tự chạy khi mở máy — chỉ chạy khi bạn gõ pdbat (hoặc double-click "PD_Bridge - BAT").'
}

if ($NoStart) {
    Set-Content -Path $OffFlag -Value 'cai dat -NoStart' -Encoding UTF8
    Ok 'Chưa bật (NoStart). Bật: pdbat'
} else {
    & $py $bridgePy start
}

# =============================================================================== 6b. NGUỒN ĐIỆN
if (Ask 'Giữ máy chạy khi gập nắp laptop (cắm sạc) để watcher không bị ngắt?') {
    try {
        & powercfg /setacvalueindex SCHEME_CURRENT SUB_BUTTONS LIDACTION 0 | Out-Null
        & powercfg /setacvalueindex SCHEME_CURRENT SUB_SLEEP STANDBYIDLE 0 | Out-Null
        & powercfg /setactive SCHEME_CURRENT | Out-Null
        Ok 'Khi cắm sạc: gập nắp không ngủ, không tự ngủ.'
    } catch { Warn "Không đổi được cài đặt nguồn ($_) — tự chỉnh trong Control Panel > Power Options." }
}

# =============================================================================== 7. KIỂM TRA
Step '7. Kiểm tra (0 token)'
& $py (Join-Path $Bridge 'tools\bridge.py') doctor

Say ''
Say 'XONG. Cách dùng:' 'Cyan'
Say "  - Tạo file .md trong: $(Join-Path $Bridge 'cau_hoi')  (ví dụ: [sau] hold sau cts.md)"
Say '  - Vài giây sau khi lưu, Claude bắt đầu; câu trả lời là Qnnn_traloi.md cùng thư mục.'
Say '  - Tên file: [nhanh] / [chuan] / [sau] (+ -sonnet/-opus) để chọn mode/model; thư mục = câu hỏi kèm dữ liệu.'
Say '  - BẬT: pdbat   ·   TẮT: pdtat   ·   TRẠNG THÁI: pdtt   (hoặc shortcut PD_Bridge trên Desktop)' 'Green'
Say '  - Chỉ khi BẬT mới nhận câu hỏi; câu gửi lúc tắt sẽ được trả lời khi bật lại.'
Say "  - Nhật ký: $(Join-Path $HomeDir 'watcher.log')"
Say '  - Khi đang bật: để máy chạy, không gập nắp laptop (máy không tự ngủ trong lúc bật).'
$test = Join-Path $Bridge 'cau_hoi\cts skew uncertainty.md'
if (Test-Path $test) { Say "  - Câu hỏi thử '$([IO.Path]::GetFileName($test))' sẽ được trả lời ngay (kiểm tra toàn tuyến)." }
