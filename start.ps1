param([switch]$Restart)
$ErrorActionPreference = 'Stop'
Set-Location -LiteralPath $PSScriptRoot
$taskPort = if ($env:PORT) { $env:PORT } else { '8000' }
$taskAppPath = Join-Path $PSScriptRoot 'app.py'
try {
    $taskHealth = Invoke-RestMethod -Uri "http://127.0.0.1:$taskPort/api/v1/health" -TimeoutSec 2
    if ($taskHealth.ok -eq $true) {
        if (-not $Restart) {
            Write-Host "The system is already running. Open http://127.0.0.1:$taskPort in your browser."
            exit 0
        }
        $taskListener = Get-NetTCPConnection -State Listen -LocalPort $taskPort -ErrorAction Stop | Select-Object -First 1
        $taskProcess = Get-CimInstance Win32_Process -Filter "ProcessId = $($taskListener.OwningProcess)"
        if ($taskProcess.Name -ne 'python.exe' -or $taskProcess.CommandLine.IndexOf($taskAppPath, [StringComparison]::OrdinalIgnoreCase) -lt 0) {
            throw 'This port belongs to another process. Close the original server window before restarting.'
        }
        Stop-Process -Id $taskProcess.ProcessId -ErrorAction Stop
        Write-Host 'Previous project server stopped. Starting with the latest settings.' 
    }
} catch {
    if ($taskHealth -and $taskHealth.ok -eq $true -and $Restart) { throw }
    # 沒有執行中的服務，繼續正常啟動。
}
$taskPython = Join-Path $PSScriptRoot '.venv/Scripts/python.exe'
if (Test-Path -LiteralPath $taskPython) {
    & $taskPython -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('flask') else 1)"
    if ($LASTEXITCODE -ne 0) { $taskPython = 'python' }
} else {
    $taskPython = 'python'
}
& $taskPython -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('flask') else 1)"
if ($LASTEXITCODE -ne 0) {
    Write-Host 'Installing required packages...'
    & $taskPython -m pip install -r requirements.txt
    if ($LASTEXITCODE -ne 0) { throw 'Package installation failed. See the message above.' }
}
Write-Host "Open http://127.0.0.1:$taskPort in your browser. Keep this window open."
& $taskPython $taskAppPath
