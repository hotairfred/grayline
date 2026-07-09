# restart-wsjtx.ps1 — Grayline: restart the SliceA WSJT-X instance + (re)launch the
# Tx-frequency helper, both in the CURRENT session. Run after RDP-ing in — WSJT-X
# handles the console->RDP transition poorly, so one double-click brings both up fresh.
# Only touches the SliceA instance; other --rig-name slices are left running.
$RIG    = 'SliceA'
$HELPER = "$env:USERPROFILE\grayline_txhelper.ps1"

# locate wsjtx.exe (running SliceA -> search C:\WSJT -> default)
$exe = (Get-CimInstance Win32_Process -Filter "Name='wsjtx.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*--rig-name=$RIG*" } | Select-Object -First 1).ExecutablePath
if (-not $exe) { $exe = (Get-ChildItem 'C:\WSJT' -Recurse -Filter 'wsjtx.exe' -ErrorAction SilentlyContinue | Select-Object -First 1).FullName }
if (-not $exe) { $exe = 'C:\WSJT\wsjtx\bin\wsjtx.exe' }

# 1. stop the SliceA instance + any old helper (so the new helper can bind :2299)
Get-CimInstance Win32_Process -Filter "Name='wsjtx.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like "*--rig-name=$RIG*" } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like '*grayline_txhelper*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1

# 2. start the SliceA instance, wait for it
Start-Process $exe -ArgumentList "--rig-name=$RIG"
$n = 0
while (-not (Get-CimInstance Win32_Process -Filter "Name='wsjtx.exe'" -ErrorAction SilentlyContinue |
             Where-Object { $_.CommandLine -like "*--rig-name=$RIG*" }) -and $n -lt 40) { Start-Sleep -Milliseconds 500; $n++ }
Start-Sleep -Seconds 2

# 3. start the helper (hidden), co-located in THIS session
Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',$HELPER
