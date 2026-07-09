# restart-wsjtx.ps1 — Grayline: restart WSJT-X + (re)launch the Tx-frequency helper,
# both landing in the CURRENT session. Run this after RDP-ing in — WSJT-X handles the
# console->RDP transition poorly, so one double-click brings both up fresh and co-located.
$HELPER = "$env:USERPROFILE\grayline_txhelper.ps1"

# locate wsjtx.exe: prefer the running instance's path, else search C:\WSJT, else default
$exe = (Get-Process wsjtx -ErrorAction SilentlyContinue | Select-Object -First 1).Path
if (-not $exe) { $exe = (Get-ChildItem 'C:\WSJT' -Recurse -Filter 'wsjtx.exe' -ErrorAction SilentlyContinue | Select-Object -First 1).FullName }
if (-not $exe) { $exe = 'C:\WSJT\wsjtx\bin\wsjtx.exe' }

# 1. stop WSJT-X + any existing helper (so the new helper can bind :2299)
Get-Process wsjtx -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like '*grayline_txhelper*' } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1

# 2. start WSJT-X, wait for it to come up
Start-Process $exe
$n = 0; while (-not (Get-Process wsjtx -ErrorAction SilentlyContinue) -and $n -lt 30) { Start-Sleep -Milliseconds 500; $n++ }
Start-Sleep -Seconds 2

# 3. start the helper (hidden), co-located in THIS session
Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',$HELPER
