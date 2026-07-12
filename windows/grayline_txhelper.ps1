# grayline_txhelper.ps1 — Grayline's WSJT-X Tx-frequency helper.
# Runs in the interactive desktop session; sets WSJT-X's Tx audio offset (TxFreqSpinBox)
# on request from Grayline, via UIAutomation. Line protocol over TCP:
#   "SETTX <hz> <token>"   -> sets Tx offset (200..5000), replies "OK settx=<hz>"
#   "GETTX <token>"        -> replies "OK gettx=<hz>"
#   "ENABLETX <token>"     -> clicks WSJT-X's Enable Tx button ON (no UDP command exists)
#   "LISTBTN <token>"      -> dumps all button Names/AutomationIds (discovery/debug)
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
Add-Type @"
using System; using System.Runtime.InteropServices;
public class W32 {
  [DllImport("user32.dll")] public static extern bool SetCursorPos(int X, int Y);
  [DllImport("user32.dll")] public static extern bool GetCursorPos(out POINT p);
  [DllImport("user32.dll")] public static extern void mouse_event(uint dwFlags, uint dx, uint dy, uint dwData, IntPtr dwExtraInfo);
  public struct POINT { public int X; public int Y; }
}
"@
$AE = [System.Windows.Automation.AutomationElement]
$TS = [System.Windows.Automation.TreeScope]

function Log($m) { ("{0}  {1}" -f (Get-Date -Format o), $m) | Out-File $LOG -Append -Encoding UTF8 }
function Get-TxSpin {
  # WSJT-X opens SEVERAL top-level windows per instance (Main, Wide Graph, Fast/Echo
  # Graph, ...) and their titles ALL contain the rig name -- so FindFirst can grab the
  # Wide Graph and miss the control (the intermittency). Enumerate ALL of the target
  # rig's windows and return the one that actually contains TxFreqSpinBox (the main one).
  $ic = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, $TXID)
  foreach ($p in @(Get-Process wsjtx -ErrorAction SilentlyContinue)) {
    $wc = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, $p.Id)
    foreach ($win in $AE::RootElement.FindAll($TS::Children, $wc)) {
      if ($win.Current.Name -notlike "*$RIG*") { continue }
      $spin = $win.FindFirst($TS::Descendants, $ic)
      if ($spin) { return $spin }
    }
  }
  return $null
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
function Get-WsjtxWin {
  # The main WSJT-X window for the target rig = the one that holds TxFreqSpinBox.
  $ic = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, $TXID)
  foreach ($p in @(Get-Process wsjtx -ErrorAction SilentlyContinue)) {
    $wc = New-Object System.Windows.Automation.PropertyCondition($AE::ProcessIdProperty, $p.Id)
    foreach ($win in $AE::RootElement.FindAll($TS::Children, $wc)) {
      if ($win.Current.Name -notlike "*$RIG*") { continue }
      if ($win.FindFirst($TS::Descendants, $ic)) { return $win }
    }
  }
  return $null
}
function Do-EnableTx {
  # WSJT-X's "Enable Tx" (autoButton) is a checkable button. TogglePattern.Toggle() sets its
  # 'checked' state but does NOT fire WSJT-X's on_autoButton_clicked handler, so Tx never
  # engages (button stays yellow). Fix: force a clean UNchecked baseline via Toggle, then do
  # a REAL mouse click at the button center (fires clicked -> engages -> green).
  $idc = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, 'MainWindow.centralWidget.lower_panel_widget.autoButton')
  $nc  = New-Object System.Windows.Automation.PropertyCondition($AE::NameProperty, 'Enable Tx')
  $btn = $null
  for ($i = 0; $i -lt 10 -and -not $btn; $i++) {
    $win = Get-WsjtxWin
    if ($win) {
      $btn = $win.FindFirst($TS::Descendants, $idc)
      if (-not $btn) { $btn = $win.FindFirst($TS::Descendants, $nc) }
    }
    if (-not $btn) { Start-Sleep -Milliseconds 250 }
  }
  if (-not $btn) { return 'ERR enable-tx-not-found-after-retries' }
  # Force unchecked baseline so the real click below always ends checked=true -> engaged.
  try {
    $tp = $btn.GetCurrentPattern([System.Windows.Automation.TogglePattern]::Pattern)
    if ($tp.Current.ToggleState -ne [System.Windows.Automation.ToggleState]::Off) { $tp.Toggle(); Start-Sleep -Milliseconds 200 }
  } catch {}
  $r = $btn.Current.BoundingRectangle
  if ($r.Width -le 0 -or $r.Height -le 0) { return 'ERR enable-tx-no-bounding-rect' }
  $x = [int]($r.X + $r.Width / 2); $y = [int]($r.Y + $r.Height / 2)
  # Save + restore the cursor so we don't yank the operator's mouse away.
  $save = New-Object W32+POINT; [void][W32]::GetCursorPos([ref]$save)
  [void][W32]::SetCursorPos($x, $y); Start-Sleep -Milliseconds 70
  [W32]::mouse_event(0x0002, 0, 0, 0, [IntPtr]::Zero); Start-Sleep -Milliseconds 40   # left down
  [W32]::mouse_event(0x0004, 0, 0, 0, [IntPtr]::Zero)                                  # left up
  Start-Sleep -Milliseconds 60
  [void][W32]::SetCursorPos($save.X, $save.Y)
  return ("OK enabletx real-click @" + $x + "," + $y)
}
function Do-GenMsgs {
  # Click "Generate Std Msgs" (genStdMsgsPushButton) to rebuild Tx1-6 for the current
  # DX call. WSJT-X does NOT regenerate messages off a UDP Configure, so this forces it.
  $nc  = New-Object System.Windows.Automation.PropertyCondition($AE::NameProperty, 'Generate Std Msgs')
  $idc = New-Object System.Windows.Automation.PropertyCondition($AE::AutomationIdProperty, 'MainWindow.centralWidget.lower_panel_widget.controls_stack_widget.page.QSO_controls_widget.tabWidget.qt_tabwidget_stackedwidget.tab.genStdMsgsPushButton')
  $btn = $null
  for ($i = 0; $i -lt 10 -and -not $btn; $i++) {
    $win = Get-WsjtxWin
    if ($win) {
      $btn = $win.FindFirst($TS::Descendants, $nc)
      if (-not $btn) { $btn = $win.FindFirst($TS::Descendants, $idc) }
    }
    if (-not $btn) { Start-Sleep -Milliseconds 250 }
  }
  if (-not $btn) { return 'ERR genmsgs-not-found-after-retries' }
  try { $btn.GetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern).Invoke(); return 'OK genmsgs invoked' }
  catch { return 'ERR genmsgs-no-invoke-pattern' }
}
function Do-ListButtons {
  # Discovery: dump EVERY named control (any type) with its control type + AutomationId.
  # Broad on purpose — WSJT-X's checkable/toggle buttons (Enable Tx, Monitor, Tune,
  # Decode) don't expose as plain Buttons, so a Button-only scan misses them.
  $win = Get-WsjtxWin; if (-not $win) { return 'ERR wsjtx-not-found' }
  $out = @()
  foreach ($e in $win.FindAll($TS::Descendants, [System.Windows.Automation.Condition]::TrueCondition)) {
    $n = $e.Current.Name
    if ([string]::IsNullOrWhiteSpace($n)) { continue }
    $out += ("'{0}' <{1}> [{2}]" -f $n, $e.Current.LocalizedControlType, $e.Current.AutomationId)
  }
  $joined = ($out -join ' ; ')
  Log ("CONTROLS: " + $joined)
  return ("OK controls: " + $joined)
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
    } elseif ($parts[0].ToUpper() -eq 'ENABLETX' -and $parts.Count -ge 2) {
      $resp = Do-EnableTx
    } elseif ($parts[0].ToUpper() -eq 'GENMSGS' -and $parts.Count -ge 2) {
      $resp = Do-GenMsgs
    } elseif ($parts[0].ToUpper() -eq 'LISTBTN' -and $parts.Count -ge 2) {
      $resp = Do-ListButtons
    }
    $w.WriteLine($resp)
    Log ("{0} -> {1}" -f $line, $resp)
    $client.Close()
  } catch { Log ("EXC " + $_.Exception.Message) }
}
$listener.Stop()
