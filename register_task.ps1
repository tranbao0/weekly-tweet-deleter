[CmdletBinding()]
param(
    [string]$Time = "12:00",
    [ValidateSet("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")]
    [string]$Day = "Sunday",
    [string]$Folder,
    [switch]$Unregister,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"
$taskName = "TweetCleanup"

try {
    if ($Unregister) {
        $existingTasks = @(Get-ScheduledTask | Where-Object {
            $_.TaskName -eq $taskName -and $_.TaskPath -eq '\'
        })
        if ($DryRun) {
            if ($existingTasks.Count -gt 0) {
                Write-Output "Would remove scheduled task '$taskName'."
            }
            else {
                Write-Output "No task named '$taskName' exists."
            }
        }
        else {
            foreach ($task in $existingTasks) {
                Unregister-ScheduledTask -InputObject $task -Confirm:$false
            }
            Write-Output "Removed scheduled task '$taskName' if it existed."
        }
        exit 0
    }

    try {
        $parsedTime = [datetime]::ParseExact($Time, "HH:mm", [Globalization.CultureInfo]::InvariantCulture)
    }
    catch {
        throw "Time must use 24-hour HH:mm format, for example 12:00."
    }

    if (-not $Folder) {
        $Folder = Split-Path -Parent $MyInvocation.MyCommand.Path
    }
    $resolvedFolder = $PSCmdlet.GetUnresolvedProviderPathFromPSPath($Folder)
    $scriptPath = Join-Path $resolvedFolder "tweet_cleanup.py"
    if (-not (Test-Path -LiteralPath $scriptPath -PathType Leaf)) {
        throw "Script not found: $scriptPath"
    }
    $arguments = "-3 `"$scriptPath`" run --live"

    try {
        & py -3 --version *> $null
        if ($LASTEXITCODE -ne 0) {
            throw "Python launcher failed."
        }
    }
    catch {
        throw "Python was not found. Install Python with the 'py' launcher available, then run this script again."
    }

    # The task does not search the user PATH, so it gets the full launcher path.
    $launcher = (Get-Command py -CommandType Application | Select-Object -First 1).Source
    $action = New-ScheduledTaskAction -Execute $launcher -Argument $arguments -WorkingDirectory $resolvedFolder
    $now = Get-Date
    $dayOfWeek = [System.DayOfWeek]$Day
    $firstRun = $now.Date.AddDays((7 + [int]$dayOfWeek - [int]$now.DayOfWeek) % 7).Add($parsedTime.TimeOfDay)
    if ($firstRun -le $now) {
        $firstRun = $firstRun.AddDays(7)
    }
    $trigger = New-ScheduledTaskTrigger -Weekly -WeeksInterval 1 -DaysOfWeek $dayOfWeek -At $parsedTime
    $trigger.StartBoundary = $firstRun.ToString('yyyy-MM-ddTHH:mm:ss', [Globalization.CultureInfo]::InvariantCulture)
    $trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $firstRun `
        -RepetitionInterval (New-TimeSpan -Hours 1) `
        -RepetitionDuration (New-TimeSpan -Hours 12)).Repetition
    $settings = New-ScheduledTaskSettingsSet `
        -StartWhenAvailable `
        -AllowStartIfOnBatteries `
        -DontStopIfGoingOnBatteries `
        -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
        -MultipleInstances IgnoreNew
    $principal = New-ScheduledTaskPrincipal -UserId ([System.Security.Principal.WindowsIdentity]::GetCurrent().Name) `
        -LogonType Interactive -RunLevel Limited

    if ($DryRun) {
        foreach ($entry in @(
            @{ Object = $action; Properties = @('Execute', 'Arguments', 'WorkingDirectory') },
            @{ Object = $trigger; Properties = @('StartBoundary', 'WeeksInterval', 'DaysOfWeek') },
            @{ Object = $trigger.Repetition; Properties = @('Interval', 'Duration') },
            @{ Object = $settings; Properties = @('StartWhenAvailable', 'DisallowStartIfOnBatteries', 'StopIfGoingOnBatteries', 'ExecutionTimeLimit', 'MultipleInstances') },
            @{ Object = $principal; Properties = @('UserId', 'LogonType', 'RunLevel') }
        )) {
            foreach ($property in $entry.Properties) {
                Write-Output "${property}: $($entry.Object.$property)"
            }
        }
        exit 0
    }

    Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger `
        -Settings $settings -Principal $principal -Force | Out-Null
    Write-Output "Registered weekly task '$taskName' on $Day at $($parsedTime.ToString('HH:mm'))."
}
catch {
    [Console]::Error.WriteLine(($_.Exception.Message -replace '[\r\n]+', ' '))
    exit 1
}
