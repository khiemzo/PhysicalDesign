<#
  watcher.ps1 — chạy nền trên máy ngoài KHI BẠN BẬT (lệnh `pdbat` / shortcut "PD_Bridge - BAT").
  - Giữ `python bridge.py watch` chạy (tự khởi động lại khi lỗi hoặc khi bridge.py được cập nhật).
  - Tắt bằng `pdtat`: tạo cờ %LOCALAPPDATA%\PD_Bridge\tat -> watcher dừng hẳn, không tự chạy lại.
  - Chặn máy ngủ khi đang chạy.
  - Phát hiện câu hỏi mới, làm mới chỉ mục, dọn file cũ: đều là script cục bộ — 0 token.
  Chạy tay để xem trực tiếp:  powershell -ExecutionPolicy Bypass -File watcher.ps1 -Foreground
#>
param([switch]$Foreground)

$ErrorActionPreference = 'Continue'
try { [Console]::OutputEncoding = [Text.Encoding]::UTF8 } catch {}

$HomeDir = Join-Path $env:LOCALAPPDATA 'PD_Bridge'
New-Item -ItemType Directory -Force -Path $HomeDir | Out-Null
$LogFile = Join-Path $HomeDir 'watcher.log'
$OffFlag = Join-Path $HomeDir 'tat'
$PidFile = Join-Path $HomeDir 'watcher_ps.pid'

function Write-Log([string]$msg) {
    $line = '{0} PS {1}' -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $msg
    try { Add-Content -Path $LogFile -Value $line -Encoding UTF8 } catch {}
    if ($Foreground) { Write-Host $line }
}

# ---- hệ thống đang TẮT (pdtat) -> không chạy
if (Test-Path $OffFlag) {
    if ($Foreground) { Write-Host 'PD_Bridge đang TẮT. Bật bằng: pdbat' }
    exit 0
}

# ---- một phiên bản duy nhất
$created = $false
$mutex = New-Object System.Threading.Mutex($true, 'Local\PD_Bridge_Watcher_PS', [ref]$created)
if (-not $created) {
    if ($Foreground) { Write-Host 'Watcher đã chạy nền rồi (xem watcher.log).' }
    exit 0
}
try { [IO.File]::WriteAllText($PidFile, "$PID") } catch {}

# ---- chặn máy ngủ (ES_CONTINUOUS | ES_SYSTEM_REQUIRED)
try {
    Add-Type -Namespace PDB -Name Power -MemberDefinition @'
[DllImport("kernel32.dll")] public static extern uint SetThreadExecutionState(uint esFlags);
'@ -ErrorAction Stop
    [void][PDB.Power]::SetThreadExecutionState([uint32]2147483649)
} catch { Write-Log "Không đặt được chế độ chống ngủ: $_" }

function Write-Status([string]$cfgDir, [string]$msg) {
    # ghi lỗi ra OneDrive để người hỏi thấy từ xa (khi python không chạy được)
    if (-not $cfgDir) { return }
    $f = Join-Path $cfgDir 'cau_hoi\_TRANG_THAI.md'
    $tail = ''
    try { $tail = (Get-Content $LogFile -Tail 15 -Encoding UTF8) -join "`n" } catch {}
    $fence = '```'
    $body = "# Trạng thái PD_Bridge (máy ngoài)`n`n- Cập nhật lúc: **$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')**`n- ❌ $msg`n`n## Nhật ký gần nhất`n$fence`n$tail`n$fence`n"
    try { [IO.File]::WriteAllText($f, $body, (New-Object Text.UTF8Encoding($false))) } catch {}
}

function Read-Config {
    $p = Join-Path $HomeDir 'config.json'
    if (Test-Path $p) {
        try { return (Get-Content $p -Raw -Encoding UTF8 | ConvertFrom-Json) } catch { Write-Log "config.json lỗi: $_" }
    }
    return $null
}

function Find-Python($cfg) {
    if ($cfg -and $cfg.python -and (Test-Path $cfg.python)) { return $cfg.python }
    foreach ($n in @('python', 'python3')) {
        $c = Get-Command $n -ErrorAction SilentlyContinue
        if ($c -and $c.Source -notmatch 'WindowsApps') { return $c.Source }
    }
    $c = Get-Command py -ErrorAction SilentlyContinue
    if ($c) {
        $exe = (& py -3 -c 'import sys;print(sys.executable)' 2>$null)
        if ($exe) { return $exe.Trim() }
    }
    return $null
}

$fail = 0
Write-Log 'watcher.ps1 bắt đầu'
while ($true) {
    if (Test-Path $OffFlag) { Write-Log 'Hệ thống đã TẮT (pdtat) — watcher.ps1 dừng.'; break }
    $cfg = Read-Config
    $py = Find-Python $cfg
    $bridge = $null
    if ($cfg -and $cfg.bridge_dir) { $bridge = Join-Path $cfg.bridge_dir 'tools\bridge.py' }
    if (-not $bridge -or -not (Test-Path $bridge)) { $bridge = Join-Path $PSScriptRoot 'bridge.py' }
    if (-not $py -or -not (Test-Path $bridge)) {
        Write-Log "Thiếu python ($py) hoặc bridge.py ($bridge) — chạy lại cai_dat.ps1. Thử lại sau 5 phút."
        if ($cfg) { Write-Status $cfg.bridge_dir "Watcher không chạy được: thiếu Python ($py) hoặc bridge.py — chạy lại cai_dat.ps1 trên máy ngoài." }
        for ($i = 0; $i -lt 60 -and -not (Test-Path $OffFlag); $i++) { Start-Sleep -Seconds 5 }
        continue
    }
    try { [void][PDB.Power]::SetThreadExecutionState([uint32]2147483649) } catch {}
    $t0 = Get-Date
    if ($Foreground) {
        & $py $bridge watch
        $rc = $LASTEXITCODE
    } else {
        $err = Join-Path $HomeDir 'bridge_err.log'
        $out = Join-Path $HomeDir 'bridge_out.log'
        $p = Start-Process -FilePath $py -ArgumentList @("`"$bridge`"", 'watch') -WindowStyle Hidden `
             -RedirectStandardError $err -RedirectStandardOutput $out -PassThru -Wait
        $rc = $p.ExitCode
    }
    if ($rc -eq 0) { Write-Log 'bridge.py dừng theo yêu cầu (rc=0).'; break }
    if ($rc -eq 3) { Write-Log 'bridge.py đã cập nhật — khởi động lại.'; $fail = 0; continue }
    if (((Get-Date) - $t0).TotalMinutes -gt 10) { $fail = 0 }
    $fail++
    $wait = [Math]::Min(120, 5 * $fail)
    Write-Log "bridge.py thoát bất thường (rc=$rc) — chạy lại sau $wait giây. Xem bridge_err.log"
    if ($fail -ge 3 -and $cfg) {
        $errTail = ''
        try { $errTail = (Get-Content (Join-Path $HomeDir 'bridge_err.log') -Tail 8 -Encoding UTF8) -join ' | ' } catch {}
        Write-Status $cfg.bridge_dir "bridge.py lỗi liên tục ($fail lần, rc=$rc): $errTail"
    }
    for ($i = 0; $i -lt $wait -and -not (Test-Path $OffFlag); $i++) { Start-Sleep -Seconds 1 }
}
try { Remove-Item $PidFile -ErrorAction SilentlyContinue } catch {}
try { [void][PDB.Power]::SetThreadExecutionState([uint32]2147483648) } catch {}
$mutex.ReleaseMutex()
