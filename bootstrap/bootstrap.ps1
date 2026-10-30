# Make this Windows machine manageable by a Patchyard controller. Run in an ADMIN PowerShell:
#
#   $env:PATCHYARD_ACCOUNT = 'patchyard'
#   $env:PATCHYARD_PUBKEY = 'ssh-ed25519 AAAA... patchyard'
#   $env:PATCHYARD_ALLOWED_SOURCES = '192.0.2.5'
#   Set-ExecutionPolicy -Scope Process Bypass -Force; .\bootstrap.ps1
#
# (The dashboard's "Add machine" shows this with the values filled in, or runs it for you
# over SSH with an administrator's password.) Idempotent. It:
#   1. installs and starts Windows' built-in OpenSSH Server (an optional feature from Microsoft)
#   2. creates a local administrator $PATCHYARD_ACCOUNT with a long random password nobody knows
#      (it signs in with its key only)
#   3. authorizes the controller's key for administrators, usable only from PATCHYARD_ALLOWED_SOURCES
#   4. opens TCP 22 only for PATCHYARD_ALLOWED_SOURCES and makes PowerShell the SSH shell
# It changes nothing else. Undo: Remove-LocalUser $PATCHYARD_ACCOUNT; remove its line from
# C:\ProgramData\ssh\administrators_authorized_keys; Remove-NetFirewallRule -Name Patchyard-SSH
#Requires -RunAsAdministrator
$ErrorActionPreference = 'Stop'
$Account = $env:PATCHYARD_ACCOUNT
$PubKey  = $env:PATCHYARD_PUBKEY
$Sources = $env:PATCHYARD_ALLOWED_SOURCES
if (-not $Account -or $Account -notmatch '^[A-Za-z0-9_-]{1,20}$') { throw 'PATCHYARD_ACCOUNT: letters, digits, _ and - (max 20)' }
if (-not $PubKey -or $PubKey -notmatch '^ssh-ed25519 [A-Za-z0-9+/=]+( .*)?$') { throw 'PATCHYARD_PUBKEY: an ssh-ed25519 public key' }
if (-not $Sources -or $Sources -notmatch '^[0-9a-fA-F.:,/]+$') { throw 'PATCHYARD_ALLOWED_SOURCES: addresses/CIDRs separated by commas' }

# #Requires is ignored in a pasted -EncodedCommand, so check elevation explicitly.
$principal = [Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Not elevated: open PowerShell with "Run as administrator" and paste the command again.'
}

Write-Output '--- 1. OpenSSH Server'
$cap = Get-WindowsCapability -Online -Name 'OpenSSH.Server*' | Select-Object -First 1
if (-not $cap) {
    throw ('This Windows has no OpenSSH Server feature (it needs Windows 10 1809+, 11 or Server 2019+). ' +
           'Install OpenSSH from https://github.com/PowerShell/Win32-OpenSSH/releases, then run this again.')
}
# Remember that this script installed OpenSSH, so a re-run (e.g. after the restart some
# installs need) still treats Windows' allow-from-anywhere firewall rule as ours to disable.
$marker = "$env:ProgramData\Patchyard\installed-openssh"
if ($cap.State -ne 'Installed') {
    Write-Output 'installing from Windows Update (can take a few minutes)...'
    try { Add-WindowsCapability -Online -Name $cap.Name | Out-Null }
    catch {
        throw ("Installing OpenSSH Server failed: $($_.Exception.Message) If it mentions 0x800f0954, Windows " +
               'is told to get features from WSUS or Group Policy: allow "Download repair content and optional ' +
               'features directly from Windows Update" (Group Policy: Computer Configuration > Administrative ' +
               'Templates > System > Specify settings for optional component installation and component repair), ' +
               'or add OpenSSH Server in Settings > Optional features. Then run this again.')
    }
    New-Item -ItemType Directory -Force -Path (Split-Path $marker) | Out-Null
    Set-Content -Path $marker -Value (Get-Date -Format o)
    Write-Output 'installed'
} else { Write-Output 'already installed' }
$newInstall = Test-Path $marker
if (-not (Get-Service sshd -ErrorAction SilentlyContinue)) {
    throw 'OpenSSH Server is installed but its service (sshd) is not there yet: restart Windows, then run this again.'
}
Set-Service sshd -StartupType Automatic
Start-Service sshd                     # the first start creates host keys and sshd_config

Write-Output "--- 2. local administrator $Account"
if (-not (Get-LocalUser -Name $Account -ErrorAction SilentlyContinue)) {
    $bytes = New-Object byte[] 48; [Security.Cryptography.RandomNumberGenerator]::Create().GetBytes($bytes)
    $pw = ConvertTo-SecureString ([Convert]::ToBase64String($bytes) + 'aA1!') -AsPlainText -Force
    New-LocalUser -Name $Account -Password $pw -PasswordNeverExpires -AccountNeverExpires `
                  -Description 'Patchyard service account (key login only)' | Out-Null
    Write-Output 'created'
} else { Write-Output 'exists' }
$admins = (Get-LocalGroup -SID 'S-1-5-32-544').Name
if (-not (Get-LocalGroupMember -Group $admins -Member $Account -ErrorAction SilentlyContinue)) {
    Add-LocalGroupMember -Group $admins -Member $Account
}

Write-Output '--- 3. key (administrators read C:\ProgramData\ssh\administrators_authorized_keys)'
$ak = "$env:ProgramData\ssh\administrators_authorized_keys"
$line = "from=`"$Sources`" $PubKey"
$keyBody = $PubKey.Split(' ')[1]
$existing = if (Test-Path $ak) { Get-Content $ak | Where-Object { $_ -notlike "*$keyBody*" } } else { @() }
@($existing) + $line | Set-Content -Path $ak -Encoding ascii
icacls.exe $ak /inheritance:r /grant 'SYSTEM:F' /grant '*S-1-5-32-544:F' | Out-Null   # sshd requires exactly this
Write-Output 'key installed'

Write-Output '--- 4. firewall + shell'
# Installing OpenSSH adds Windows' own allow-from-anywhere rule. Only when this script did the
# install (now or on an earlier run) is it disabled; a machine that already used SSH keeps its rules.
if ($newInstall) { Get-NetFirewallRule -Name 'OpenSSH-Server-In-TCP' -ErrorAction SilentlyContinue | Disable-NetFirewallRule }
if (Get-NetFirewallRule -Name 'Patchyard-SSH' -ErrorAction SilentlyContinue) {
    Set-NetFirewallRule -Name 'Patchyard-SSH' -RemoteAddress ($Sources.Split(',')) -Enabled True
} else {
    New-NetFirewallRule -Name 'Patchyard-SSH' -DisplayName 'SSH from the Patchyard controller' -Direction Inbound `
        -Protocol TCP -LocalPort 22 -RemoteAddress ($Sources.Split(',')) -Action Allow -Profile Any | Out-Null
}
New-ItemProperty -Path 'HKLM:\SOFTWARE\OpenSSH' -Name DefaultShell -Force -PropertyType String `
    -Value "$env:SystemRoot\System32\WindowsPowerShell\v1.0\powershell.exe" | Out-Null
Restart-Service sshd
Write-Output "OK: $Account ready"
