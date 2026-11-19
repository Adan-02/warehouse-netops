# Run only from an administrator PowerShell. No network configuration is rewritten.
$ErrorActionPreference = 'Stop'
$taskIdentity = [Security.Principal.WindowsIdentity]::GetCurrent()
$taskPrincipal = [Security.Principal.WindowsPrincipal]::new($taskIdentity)
if (-not $taskPrincipal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run this script from an administrator PowerShell; no action has been taken.'
}
$taskRuntime = Join-Path $env:USERPROFILE 'Documents\Codex\warehouse-netops-runtime\vms\'
$taskList = & 'D:\VMware\vmrun.exe' list
if ($LASTEXITCODE -ne 0) { throw 'Cannot inspect running VMware guests.' }
$taskForeign = @($taskList | Select-Object -Skip 1 | Where-Object {
    $_ -and -not $_.StartsWith($taskRuntime, [StringComparison]::OrdinalIgnoreCase)
})
if ($taskForeign.Count -gt 0) { throw 'Other VMware guests are active. NAT was left untouched.' }
$taskConfig = 'C:\ProgramData\VMware\vmnetnat.conf'
$taskBefore = (Get-FileHash -LiteralPath $taskConfig -Algorithm SHA256).Hash
Restart-Service -Name 'VMware NAT Service'
$taskAfter = (Get-FileHash -LiteralPath $taskConfig -Algorithm SHA256).Hash
if ($taskBefore -ne $taskAfter) { throw 'NAT configuration changed unexpectedly; stop and inspect.' }
Get-Service -Name 'VMware NAT Service' | Select-Object Name,Status
Write-Output 'NAT configuration unchanged. Existing DHCP state and all cloud servers were untouched.'
& ping.exe -S 192.168.200.254 -n 2 -w 1000 192.168.200.1
