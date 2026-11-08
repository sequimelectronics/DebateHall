# SPDX-License-Identifier: AGPL-3.0-only
# Copyright (C) 2026 Becky Northaven and the Debate Hall contributors
# The Debate Hall's audience door: make its firewall rule ONCE, switched OFF (the keeper + a security review, 10-28).
# Run in an ADMIN PowerShell. The rule lets in only the devices you list, on the door's port, on Private networks.
# Turn it on when a debate opens and off when it closes (the hall prints the off line when the judge closes):
#   Enable-NetFirewallRule  -DisplayName 'Debate Hall audience door'
#   Disable-NetFirewallRule -DisplayName 'Debate Hall audience door'
#
# Example:
#   .\audience_door_firewall.ps1 -Port 8812 -Devices 192.168.1.21,192.168.1.22
param(
    [Parameter(Mandatory = $true)][ValidateRange(1024, 65535)][int]$Port,
    # Specific devices only (security review): each device that may watch (a phone, a tablet, a home server running an audience agent). No subnets.
    [Parameter(Mandatory = $true)][string[]]$Devices
)

$name = 'Debate Hall audience door'
foreach ($d in $Devices) {
    $ip = $null
    if (-not [System.Net.IPAddress]::TryParse($d, [ref]$ip) -or $d.Contains('/')) {
        throw "Each device is one address (no ranges or subnets): '$d'"
    }
}
if (Get-NetFirewallRule -DisplayName $name -ErrorAction SilentlyContinue) {
    throw "A rule named '$name' already exists. Remove it first: Remove-NetFirewallRule -DisplayName '$name'"
}
New-NetFirewallRule -DisplayName $name -Direction Inbound -Action Allow -Protocol TCP -LocalPort $Port `
    -RemoteAddress $Devices -Profile Private -Enabled False | Out-Null
Get-NetFirewallRule -DisplayName $name | Select-Object DisplayName, Enabled, Profile, Direction, Action
Get-NetFirewallRule -DisplayName $name | Get-NetFirewallAddressFilter | Select-Object RemoteAddress
Write-Host "Made, and OFF. On when a debate opens:  Enable-NetFirewallRule -DisplayName '$name'"
