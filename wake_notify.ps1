Add-Type -AssemblyName System.Windows.Forms
$notification = New-Object System.Windows.Forms.NotifyIcon
$notification.Icon = [System.Drawing.SystemIcons]::Information
$notification.BalloonTipTitle = "YouTube学習アプリ"
$notification.BalloonTipText = "スリープから復帰しました。アプリを再起動します..."
$notification.Visible = $true
$notification.ShowBalloonTip(5000)
Start-Sleep 2
$notification.Dispose()
