# grayline_txhelper.ps1 — Grayline's WSJT-X Tx-frequency helper.
# Runs in the interactive desktop session; sets WSJT-X's Tx audio offset (TxFreqSpinBox)
# on request from Grayline, via UIAutomation. Line protocol over TCP:
#   "SETTX <hz> <token>"   -> sets Tx offset (200..5000), replies "OK settx=<hz>"
#   "GETTX <token>"        -> replies "OK gettx=<hz>"
# Targets the WSJT-X instance whose window title contains $RIG (default SliceA), so
# multiple --rig-name slices running at once don't confuse it. Self-exits when no
# WSJT-X remains (no stale copy in a dead/old session after an RDP session shift).
$PORT  = 2299
$TOKEN = 'gl-txhelper-7k2p'   # shared secret with Grayline (change in both places)
$RIG   = 'SliceA'             # target this rig-name's window (title "WSJT-X - SliceA ...")
$TXID  = 'MainWindow.centralWidget.lower_panel_widget.controls_stack_widget.page.QSO_controls_widget.TxFreqSpinBox'
$LOG   = "$env:USERPROFILE\txhelper.log"

Add-Type -AssemblyName UIAutomationClient
Add-Type -AssemblyName UIAutomationTypes
$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]

function Log($m) { ("{0}  {1}" -f (Get-Date -Format o), $m) | Out-File $LOG -Append -Encoding UTF8 }
function Get-TxSpin {
  # Find the SliceA WSJT-X window among possibly-multiple instances; fall back to
  # whatever single instance exists if the title doesn't match.
  $procs = @(Get-Process wsjtx -ErrorAction SilentlyContinue)
  if ($procs.Count -eq 0) { return $null }
  $target = $null
  foreach ($p in $procs) {
    $wc = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, $p.Id)
    $win = $AE::RootElement.FindFirst($TS::Children, $wc)
    if ($win) {
      if ($win.Current.Name -like "*$RIG*") { $target = $win; break }
      if (-not $target) { $target = $win }
    }
  }
  if (-not $target) { return $null }
  $ic = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, $TXID)
  return $target.FindFirst($TS::Descendants, $ic)
}
function Rvp($spin) { $spin.GetCurrentPattern([System.Windows.Automation.RangeValuePattern]::Pattern) }
function Do-Set($hz) {
  $s = Get-TxSpin; if (-not $s) { return 'ERR wsjtx-or-control-not-found' }
  if ($hz -lt 200 -or $hz -gt 5000) { return 'ERR out-of-range' }
  (Rvp $s).SetValue([double]$hz); return ("OK settx=" + $hz)
}
function Do-Get {
  $s = Get-TxSpin; if (-not $s) { return 'ERR not-found' }
  return ("OK gettx=" + [int](Rvp $s).Current.Value)
}

$listener = New-Object System.Net.Sockets.TcpListener([System.Net.IPAddress]::Any, $PORT)
$listener.Start()
Log "listening on :$PORT  (session $((Get-Process -Id $PID).SessionId), rig $RIG)"
$sawWsjtx = $false
while ($true) {
  if (Get-Process wsjtx -ErrorAction SilentlyContinue) { $sawWsjtx = $true }
  elseif ($sawWsjtx) { Log 'wsjtx exited -> helper exiting'; break }
  if (-not $listener.Pending()) { Start-Sleep -Milliseconds 400; continue }
  try {
    $client = $listener.AcceptTcpClient()
    $s = $client.GetStream()
    $r = New-Object System.IO.StreamReader($s)
    $w = New-Object System.IO.StreamWriter($s); $w.AutoFlush = $true
    $line = $r.ReadLine()
    $parts = ($line -split '\s+')
    $resp = 'ERR bad-request'
    if ($parts.Count -ge 2 -and $parts[-1] -ne $TOKEN) {
      $resp = 'ERR bad-token'
    } elseif ($parts[0].ToUpper() -eq 'SETTX' -and $parts.Count -ge 3 -and $parts[1] -match '^\d+$') {
      $resp = Do-Set([int]$parts[1])
    } elseif ($parts[0].ToUpper() -eq 'GETTX' -and $parts.Count -ge 2) {
      $resp = Do-Get
    }
    $w.WriteLine($resp)
    Log ("{0} -> {1}" -f $line, $resp)
    $client.Close()
  } catch { Log ("EXC " + $_.Exception.Message) }
}
$listener.Stop()
