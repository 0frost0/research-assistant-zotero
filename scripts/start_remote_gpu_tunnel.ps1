param([string]$SshHost = 'siat-my-laptop')
$ErrorActionPreference = 'Stop'
if ($SshHost -notmatch '^[a-zA-Z0-9][a-zA-Z0-9_.@-]*$') { throw 'Use an SSH config alias.' }
& ssh -N -L '127.0.0.1:30001:127.0.0.1:30001' -L '127.0.0.1:30000:127.0.0.1:30000' -L '127.0.0.1:30002:127.0.0.1:30002' -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -o ServerAliveCountMax=3 $SshHost
