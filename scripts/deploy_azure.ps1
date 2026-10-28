<#
.SYNOPSIS
    Deploy Zoro 24/7 Cloud Core to Azure Container Apps.
.DESCRIPTION
    Provisions or updates the 'zoro-agent' Container App inside 'baseform-env-za'
    in resource group 'LumenAI' (southafricanorth).
#>

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
$EnvFile = Join-Path $Root ".env"
$BicepFile = Join-Path $Root "infra\main.bicep"

Write-Host "==========================================" -ForegroundColor Cyan
Write-Host "  Zoro 24/7 Cloud Core Deployment        " -ForegroundColor Cyan
Write-Host "  Target: Azure Container Apps           " -ForegroundColor Cyan
Write-Host "  Environment: baseform-env-za           " -ForegroundColor Cyan
Write-Host "==========================================" -ForegroundColor Cyan

# 1. Verify files
if (-not (Test-Path $EnvFile)) {
    Write-Error "Missing .env file at $EnvFile"
}
if (-not (Test-Path $BicepFile)) {
    Write-Error "Missing Bicep template at $BicepFile"
}

# 2. Check Azure CLI authentication
Write-Host "`n[1/3] Checking Azure authentication..." -ForegroundColor Yellow
$account = az account show --output json 2>$null | ConvertFrom-Json
if (-not $account) {
    Write-Host "Azure CLI is not logged in. Initiating login..." -ForegroundColor Yellow
    az login
    $account = az account show --output json | ConvertFrom-Json
}
Write-Host "  Logged in as: $($account.user.name)" -ForegroundColor Green
Write-Host "  Subscription: $($account.name) ($($account.id))" -ForegroundColor Green

# 3. Read secrets from .env
Write-Host "`n[2/3] Reading environment secrets from .env..." -ForegroundColor Yellow
$envMap = @{}
Get-Content $EnvFile | Where-Object { $_ -match "^[A-Z0-9_]+=" } | ForEach-Object {
    $parts = $_ -split "=", 2
    $envMap[$parts[0].Trim()] = $parts[1].Trim()
}

Write-Host "  Loaded $($envMap.Count) configuration keys." -ForegroundColor Green

# 4. Deploy Bicep template to Azure Container Apps
Write-Host "`n[3/3] Deploying 'zoro-agent' Container App to Azure Container Apps..." -ForegroundColor Yellow

$anthropicApiKey = if ($envMap['ANTHROPIC_API_KEY']) { $envMap['ANTHROPIC_API_KEY'] } else { "disconnected" }

$googleCredsFile = "$HOME\.google_workspace_mcp\credentials\senzekonduli28@gmail.com.json"
$googleCredsB64 = if (Test-Path $googleCredsFile) {
    [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($googleCredsFile))
} else { "none" }

$spotifyTokenFile = "$HOME\.zoro_spotify\token.json"
$spotifyTokenB64 = if (Test-Path $spotifyTokenFile) {
    [Convert]::ToBase64String([System.IO.File]::ReadAllBytes($spotifyTokenFile))
} else { "none" }

$deploymentArgs = @(
    "deployment", "group", "create",
    "--resource-group", "LumenAI",
    "--template-file", $BicepFile,
    "--parameters",
    "containerAppName=zoro-agent",
    "environmentName=baseform-env-za",
    "location=southafricanorth",
    "containerImage=baseformacrza.azurecr.io/zoro-agent:v10",
    "livekitUrl=$($envMap['LIVEKIT_URL'])",
    "azureOpenAiApiKey=$($envMap['AZURE_OPENAI_API_KEY'])",
    "livekitApiKey=$($envMap['LIVEKIT_API_KEY'])",
    "livekitApiSecret=$($envMap['LIVEKIT_API_SECRET'])",
    "deepgramApiKey=$($envMap['DEEPGRAM_API_KEY'])",
    "cartesiaApiKey=$($envMap['CARTESIA_API_KEY'])",
    "anthropicApiKey=$anthropicApiKey",
    "tavilyApiKey=$($envMap['TAVILY_API_KEY'])",
    "azurePostgresUrl=$($envMap['AZURE_POSTGRES_URL'])",
    "redisUrl=$($envMap['REDIS_URL'])",
    "applicationinsightsConnectionString=$($envMap['APPLICATIONINSIGHTS_CONNECTION_STRING'])",
    "spotifyClientSecret=$($envMap['SPOTIFY_CLIENT_SECRET'])",
    "youtubeApiKey=$($envMap['YOUTUBE_API_KEY'])",
    "googleOauthClientSecret=$($envMap['GOOGLE_OAUTH_CLIENT_SECRET'])",
    "googleWorkspaceCredentialsB64=$googleCredsB64",
    "spotifyTokenB64=$spotifyTokenB64",
    "githubToken=$($envMap['GITHUB_TOKEN'])"
)

az @deploymentArgs

if ($LASTEXITCODE -ne 0) {
    Write-Host "`n[ERROR] Azure deployment failed with exit code $LASTEXITCODE" -ForegroundColor Red
    exit $LASTEXITCODE
}

Write-Host "`n[SUCCESS] Zoro 24/7 Cloud Core deployed successfully to baseform-env-za!" -ForegroundColor Green
Write-Host "Zoro is now running 24/7 in the cloud. You no longer need to run python on localhost!" -ForegroundColor Cyan

