<#
.SYNOPSIS
Start the local Streamlit app with saved Windows judge credentials.
.DESCRIPTION
Reloads DASHSCOPE_API_KEY and DEEPSEEK_API_KEY from User, then Machine,
then the current process. Never prints credential values or persists changes.
#>
[CmdletBinding()]
param(
    [ValidateRange(1, 65535)]
    [int]$Port = 8501,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'
if (-not (Test-Path -LiteralPath $pythonPath -PathType Leaf)) {
    throw 'Project .venv is missing. Create it with: py -3.11 -m venv .venv'
}

foreach ($variableName in @('DASHSCOPE_API_KEY', 'DEEPSEEK_API_KEY')) {
    $credentialValue = $null
    $credentialSource = 'Missing'
    foreach ($scope in @('User', 'Machine', 'Process')) {
        $candidateValue = [Environment]::GetEnvironmentVariable($variableName, $scope)
        if (-not [string]::IsNullOrWhiteSpace($candidateValue)) {
            $credentialValue = $candidateValue.Trim()
            $credentialSource = $scope
            break
        }
    }
    if ($credentialSource -ne 'Missing') {
        [Environment]::SetEnvironmentVariable($variableName, $credentialValue, 'Process')
    }
    Write-Host ("{0}: {1}" -f $variableName, $credentialSource)
    $credentialValue = $null
    $candidateValue = $null
}

$pythonVersion = & $pythonPath -X utf8 -c 'import sys; print(sys.version_info.major * 100 + sys.version_info.minor)'
if ($LASTEXITCODE -ne 0 -or $pythonVersion -ne '311') {
    throw 'Project .venv must use Python 3.11.x.'
}
if ($CheckOnly) {
    Write-Host 'Startup check passed. No server or evaluation was started.'
    return
}

Write-Host "Workspace: http://127.0.0.1:$Port/"
Write-Host "Release:   http://127.0.0.1:$Port/release"
Write-Host "Chat:      http://127.0.0.1:$Port/chat"
Write-Host 'Keep this terminal open. Press Ctrl+C to stop. Evaluation requires a page action.'
Push-Location -LiteralPath $projectRoot
try {
    & $pythonPath -X utf8 -m streamlit run streamlit_app.py --server.address 127.0.0.1 --server.port $Port --server.headless true
    if ($LASTEXITCODE -ne 0) {
        throw 'Streamlit exited with an error. Check the output above (including port availability).'
    }
} finally {
    Pop-Location
}
