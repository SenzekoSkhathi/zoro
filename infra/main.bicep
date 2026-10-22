targetScope = 'resourceGroup'

@description('The Azure region for the container app.')
param location string = 'southafricanorth'

@description('The name of the existing Container Apps Managed Environment.')
param environmentName string = 'baseform-env-za'

@description('The name of the Container App to create.')
param containerAppName string = 'zoro-agent'

@description('The container image to deploy.')
param containerImage string = 'baseformacrza.azurecr.io/zoro-agent:latest'

@description('Azure Container Registry name.')
param acrRegistryName string = 'baseformacrza'

@description('LiveKit Cloud WebSocket endpoint.')
param livekitUrl string = 'wss://zoro-9ty4k1h3.livekit.cloud'

// --- Secrets ---
@secure()
param azureOpenAiApiKey string = ''

@secure()
param livekitApiKey string = ''

@secure()
param livekitApiSecret string = ''

@secure()
param deepgramApiKey string = ''

@secure()
param cartesiaApiKey string = ''

@secure()
param anthropicApiKey string = ''

@secure()
param tavilyApiKey string = ''

@secure()
param azurePostgresUrl string = ''

@secure()
param redisUrl string = ''

@secure()
param applicationinsightsConnectionString string = ''

@secure()
param spotifyClientSecret string = ''

@secure()
param youtubeApiKey string = ''

@secure()
param googleOauthClientSecret string = ''

resource environment 'Microsoft.App/managedEnvironments@2024-03-01' existing = {
  name: environmentName
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-01-01-preview' existing = {
  name: acrRegistryName
}

resource containerApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: containerAppName
  location: location
  properties: {
    managedEnvironmentId: environment.id
    configuration: {
      activeRevisionsMode: 'Single'
      secrets: [
        {
          name: 'acr-password'
          value: acr.listCredentials().passwords[0].value
        }
        {
          name: 'azure-openai-key'
          value: azureOpenAiApiKey
        }
        {
          name: 'livekit-api-key'
          value: livekitApiKey
        }
        {
          name: 'livekit-api-secret'
          value: livekitApiSecret
        }
        {
          name: 'deepgram-api-key'
          value: deepgramApiKey
        }
        {
          name: 'cartesia-api-key'
          value: cartesiaApiKey
        }
        {
          name: 'anthropic-api-key'
          value: anthropicApiKey
        }
        {
          name: 'tavily-api-key'
          value: tavilyApiKey
        }
        {
          name: 'database-url'
          value: azurePostgresUrl
        }
        {
          name: 'redis-url'
          value: redisUrl
        }
        {
          name: 'appinsights-conn'
          value: applicationinsightsConnectionString
        }
        {
          name: 'spotify-secret'
          value: spotifyClientSecret
        }
        {
          name: 'youtube-api-key'
          value: youtubeApiKey
        }
        {
          name: 'google-oauth-secret'
          value: googleOauthClientSecret
        }
      ]
      registries: [
        {
          server: '${acrRegistryName}.azurecr.io'
          username: acrRegistryName
          passwordSecretRef: 'acr-password'
        }
      ]
    }
    template: {
      containers: [
        {
          name: 'zoro-agent'
          image: containerImage
          resources: {
            cpu: json('0.5')
            memory: '1.0Gi'
          }
          env: [
            {
              name: 'PYTHONUNBUFFERED'
              value: '1'
            }
            {
              name: 'AZURE_OPENAI_ENDPOINT'
              value: 'https://sa-higher-ed-research-resource.cognitiveservices.azure.com/'
            }
            {
              name: 'AZURE_OPENAI_DEPLOYMENT'
              value: 'gpt-6-sol'
            }
            {
              name: 'OPENAI_API_VERSION'
              value: '2024-10-21'
            }
            {
              name: 'AZURE_OPENAI_API_KEY'
              secretRef: 'azure-openai-key'
            }
            {
              name: 'LIVEKIT_URL'
              value: livekitUrl
            }
            {
              name: 'LIVEKIT_API_KEY'
              secretRef: 'livekit-api-key'
            }
            {
              name: 'LIVEKIT_API_SECRET'
              secretRef: 'livekit-api-secret'
            }
            {
              name: 'DEEPGRAM_API_KEY'
              secretRef: 'deepgram-api-key'
            }
            {
              name: 'CARTESIA_API_KEY'
              secretRef: 'cartesia-api-key'
            }
            {
              name: 'ANTHROPIC_API_KEY'
              secretRef: 'anthropic-api-key'
            }
            {
              name: 'TAVILY_API_KEY'
              secretRef: 'tavily-api-key'
            }
            {
              name: 'AZURE_POSTGRES_URL'
              secretRef: 'database-url'
            }
            {
              name: 'REDIS_URL'
              secretRef: 'redis-url'
            }
            {
              name: 'APPLICATIONINSIGHTS_CONNECTION_STRING'
              secretRef: 'appinsights-conn'
            }
            {
              name: 'SPOTIFY_CLIENT_ID'
              value: '059100ebe86e4edb80741c971626ca26'
            }
            {
              name: 'SPOTIFY_CLIENT_SECRET'
              secretRef: 'spotify-secret'
            }
            {
              name: 'SPOTIFY_REDIRECT_URI'
              value: 'http://127.0.0.1:8888/callback'
            }
            {
              name: 'YOUTUBE_API_KEY'
              secretRef: 'youtube-api-key'
            }
            {
              name: 'GOOGLE_OAUTH_CLIENT_ID'
              value: '385707213892-i0k5fbfj60cujrekp5r3pi07jm62efh7.apps.googleusercontent.com'
            }
            {
              name: 'GOOGLE_OAUTH_CLIENT_SECRET'
              secretRef: 'google-oauth-secret'
            }
            {
              name: 'AZURE_SPEECH_REGION'
              value: 'eastus2'
            }
            {
              name: 'AZURE_SPEECH_VOICE'
              value: 'en-GB-SoniaNeural'
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 2
      }
    }
  }
}

output fqdn string = environment.properties.defaultDomain
output containerAppId string = containerApp.id

