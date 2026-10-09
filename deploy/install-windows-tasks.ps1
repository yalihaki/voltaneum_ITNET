param(
    [string]$ProjectRoot = 'C:\Voltaneum'
)

$python = Join-Path $ProjectRoot 'venv\Scripts\python.exe'
$working = $ProjectRoot
$tasks = @(
    @{ Name = 'Voltaneum Main'; Args = 'main.py' },
    @{ Name = 'Voltaneum Email Notifier'; Args = '-m notifications.email_notifier' },
    @{ Name = 'Voltaneum Model Monitor'; Args = 'run_model_monitor.py' }
)

foreach ($task in $tasks) {
    $action = New-ScheduledTaskAction -Execute $python -Argument $task.Args -WorkingDirectory $working
    $trigger = New-ScheduledTaskTrigger -AtStartup
    $settings = New-ScheduledTaskSettingsSet -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 1)
    Register-ScheduledTask -TaskName $task.Name -Action $action -Trigger $trigger -Settings $settings -RunLevel Limited -Force
}
Write-Host 'Voltaneum tasks installed. Configure the service account and .env ACLs before production.'
