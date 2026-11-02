param(
    [Parameter(Mandatory=$true)][string]$SshHost,
    [int]$LocalPort = 30001,
    [int]$RemotePort = 30001
)
$ErrorActionPreference = 'Stop'
if ($SshHost -notmatch '^[a-zA-Z0-9][a-zA-Z0-9_.@-]*$') { throw 'Use an SSH config alias.' }
if ($LocalPort -lt 1024 -or $LocalPort -gt 65535 -or $RemotePort -lt 1024 -or $RemotePort -gt 65535) { throw 'Invalid port.' }
# Foreground tunnel: closing this terminal stops forwarding, not the remote model.
& ssh -N -L "127.0.0.1:${LocalPort}:127.0.0.1:${RemotePort}" -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 $SshHost
