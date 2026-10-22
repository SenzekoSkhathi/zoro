<#
.SYNOPSIS
    Deploy Zoro Web UI to Azure Container Apps.
.DESCRIPTION
    Provisions or updates the 'zoro-web' Container App with external HTTPS ingress
    inside 'baseform-env-za' in resource group 'LumenAI' (southafricanorth).
#>

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root ".env"
$WebEnvFile = Join-Path $Root "web\.env.local"
$BicepFile = Join-Path $Root "infra\web.bicep"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Zoro Web UI Cloud Deployment            " -ForegroundColor Cyan
Write-Host "  Target: Azure Container Apps (External) " -ForegroundColor Cyan
Write-Host "  Environment: baseform-env-za            " -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

# 1. Check Azure CLI authentication
Write-Host "`n[1/3] Checking Azure authentication..." -ForegroundColor Yellow
$account = az account show --output json 2>$null | ConvertFrom-Json
if (-not $account) {
    Write-Host "Azure CLI is not logged in. Initiating login..." -ForegroundColor Yellow
    az login
    $account = az account show --output json | ConvertFrom-Json
}
Write-Host "  Logged in as: $($account.user.name)" -ForegroundColor Green
Write-Host "  Subscription: $($account.name) ($($account.id))" -ForegroundColor Green

# 2. Collect configuration
Write-Host "`n[2/3] Reading environment secrets..." -ForegroundColor Yellow
$envMap = @{}
if (Test-Path $EnvFile) {
    Get-Content $EnvFile | Where-Object { $_ -match "^[A-Z0-9_]+=" } | ForEach-Object {
        $parts = $_ -split "=", 2
        $envMap[$parts[0].Trim()] = $parts[1].Trim()
    }
}
if (Test-Path $WebEnvFile) {
    Get-Content $WebEnvFile | Where-Object { $_ -match "^[A-Z0-9_]+=" } | ForEach-Object {
        $parts = $_ -split "=", 2
        $envMap[$parts[0].Trim()] = $parts[1].Trim()
    }
}

$livekitUrl = if ($envMap['LIVEKIT_URL']) { $envMap['LIVEKIT_URL'] } else { "wss://zoro-9ty4k1h3.livekit.cloud" }
$livekitApiKey = $envMap['LIVEKIT_API_KEY']
$livekitApiSecret = $envMap['LIVEKIT_API_SECRET']
$supabaseUrl = if ($envMap['SUPABASE_URL']) { $envMap['SUPABASE_URL'] } else { "https://hhumjsjyktaivcmiypfy.supabase.co" }
$supabaseServiceKey = $envMap['SUPABASE_SERVICE_KEY']
$zoroAccessPassword = if ($envMap['ZORO_ACCESS_PASSWORD']) { $envMap['ZORO_ACCESS_PASSWORD'] } else { "local-dev-test-pw" }

Write-Host "  LiveKit URL: $livekitUrl" -ForegroundColor Green
Write-Host "  Supabase URL: $supabaseUrl" -ForegroundColor Green

# 3. Deploy Bicep template
Write-Host "`n[3/3] Deploying 'zoro-web' Container App to Azure Container Apps..." -ForegroundColor Yellow

$deploymentResult = az deployment group create `
    --resource-group LumenAI `
    --template-file $BicepFile `
    --parameters `
        containerAppName=zoro-web `
        environmentName=baseform-env-za `
        location=southafricanorth `
        containerImage=baseformacrza.azurecr.io/zoro-web:latest `
        livekitUrl=$livekitUrl `
        livekitApiKey=$livekitApiKey `
        livekitApiSecret=$livekitApiSecret `
        supabaseUrl=$supabaseUrl `
        supabaseServiceKey=$supabaseServiceKey `
        zoroAccessPassword=$zoroAccessPassword `
    --output json | ConvertFrom-Json

if ($LASTEXITCODE -ne 0) {
    Write-Host "`n[ERROR] Azure deployment failed with exit code $LASTEXITCODE" -ForegroundColor Red
    exit $LASTEXITCODE
}

$url = $deploymentResult.properties.outputs.url.value
$fqdn = $deploymentResult.properties.outputs.fqdn.value

Write-Host "`n==========================================" -ForegroundColor Green
Write-Host "  ZORO WEB UI IS LIVE IN THE CLOUD!" -ForegroundColor Green
Write-Host "  URL: $url" -ForegroundColor Green
Write-Host "  FQDN: $fqdn" -ForegroundColor Green
Write-Host "==========================================" -ForegroundColor Green

return $url

