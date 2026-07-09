# Start (or restart) just the Grayline Tx-frequency helper in THIS session.
# Does NOT touch WSJT-X. Double-click this when the Clear TX button says the helper's
# unreachable (e.g. after the helper died or landed in the wrong session).
Get-CimInstance Win32_Process -Filter "Name='powershell.exe'" -ErrorAction SilentlyContinue |
  Where-Object { $_.CommandLine -like '*grayline_txhelper*' -and $_.ProcessId -ne $PID } |
  ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
Start-Sleep -Seconds 1
Start-Process powershell -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-WindowStyle','Hidden','-File',"$env:USERPROFILE\grayline_txhelper.ps1"
