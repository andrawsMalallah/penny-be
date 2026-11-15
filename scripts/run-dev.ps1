param(
    [switch] $Restart,
    [string] $PhoneAddress = ''
)

$ErrorActionPreference = 'Stop'
$pennyBackendRoot = Split-Path -Parent $PSScriptRoot
$pennyPython = Join-Path $pennyBackendRoot '.venv\Scripts\python.exe'

if ($PhoneAddress) {
    $pennyParsedAddress = $null
    if (-not [Net.IPAddress]::TryParse($PhoneAddress, [ref] $pennyParsedAddress) -or
        $pennyParsedAddress.AddressFamily -ne [Net.Sockets.AddressFamily]::InterNetwork) {
        throw 'PhoneAddress must be this computer''s Wi-Fi IPv4 address.'
    }
    # Session-only phone-preview overrides. Credentials still come from backend/.env.
    $env:PENNY_ALLOWED_HOSTS = "127.0.0.1,localhost,$PhoneAddress"
    $env:PENNY_TRUSTED_ORIGINS = "http://127.0.0.1:5173,http://localhost:5173,http://${PhoneAddress}:5173"
}

$pennyListeners = @(Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue)
if ($pennyListeners -and -not $Restart) {
    throw 'Port 8000 is occupied. Use -Restart to replace this project''s development server.'
}
foreach ($pennyListenerId in ($pennyListeners.OwningProcess | Select-Object -Unique)) {
    $pennyListenerProcess = Get-CimInstance Win32_Process -Filter "ProcessId=$pennyListenerId"
    if ($pennyListenerProcess.CommandLine -notmatch [regex]::Escape($pennyPython)) {
        throw 'Port 8000 belongs to another process; it was not stopped.'
    }
    Stop-Process -Id $pennyListenerId
    Wait-Process -Id $pennyListenerId -Timeout 5 -ErrorAction SilentlyContinue
}
if (Get-NetTCPConnection -LocalPort 8000 -State Listen -ErrorAction SilentlyContinue) {
    throw 'Port 8000 is still occupied; server start aborted.'
}

Push-Location $pennyBackendRoot
try {
    # Check and report the configuration in the actual server process, not a separate probe.
    $pennyStartupCode = @'
import os
os.environ['DJANGO_SETTINGS_MODULE'] = 'config.settings'
import django
django.setup()
from django.conf import settings
from django.core.management import execute_from_command_line
if not settings.LOCAL_HTTP:
    raise SystemExit('This launcher is for local development only; set PENNY_LOCAL_HTTP=1 locally.')
print('Development API PID:', os.getpid(), flush=True)
print('SMTP port loaded from environment:', settings.EMAIL_PORT, flush=True)
print('Sender configured:', bool(settings.DEFAULT_FROM_EMAIL), flush=True)
execute_from_command_line(['manage.py', 'runserver', '127.0.0.1:8000', '--noreload'])
'@
    & $pennyPython -u -c $pennyStartupCode
} finally {
    Pop-Location
}
