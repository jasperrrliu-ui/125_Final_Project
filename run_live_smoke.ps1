param(
    [ValidateSet('return_size', 'return_color', 'return_stain',
                 'promo_code_invalid', 'promo_code_out_of_date',
                 'manage_cancel', 'manage_upgrade', 'manage_downgrade')]
    [string]$CaseId
)

$ErrorActionPreference = 'Stop'
$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectRoot '.venv\Scripts\python.exe'

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Python environment not found: $pythonPath"
}

$secureKey = Read-Host 'Enter your OpenAI API key (input is hidden)' -AsSecureString
try {
    $env:OPENAI_API_KEY = [System.Net.NetworkCredential]::new('', $secureKey).Password
    $env:RETAIL_AGENT_MODEL = 'gpt-5.4-mini'
    Push-Location $projectRoot
    try {
        $pythonArgs = @('-m', 'agent.retail_graph.smoke_agent', '--live')
        if ($CaseId) {
            $reportPath = Join-Path $projectRoot "outputs\retail_graph_demo\smoke_$CaseId.json"
            $pythonArgs += @('--case-id', $CaseId, '--output', $reportPath)
        }
        & $pythonPath @pythonArgs
        $runExitCode = $LASTEXITCODE
    }
    finally {
        Pop-Location
    }
}
finally {
    Remove-Item Env:OPENAI_API_KEY -ErrorAction SilentlyContinue
    Remove-Item Env:RETAIL_AGENT_MODEL -ErrorAction SilentlyContinue
    $secureKey.Dispose()
}

if ($runExitCode -ne 0) {
    throw "Live smoke run failed. Exit code: $runExitCode"
}
