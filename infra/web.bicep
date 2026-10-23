targetScope = 'resourceGroup'

@description('The Azure region for the container app.')
param location string = 'southafricanorth'

@description('The name of the existing Container Apps Managed Environment.')
param environmentName string = 'baseform-env-za'

@description('The name of the Container App to create.')
param containerAppName string = 'zoro-web'

@description('The container image to deploy.')
param containerImage string = 'baseformacrza.azurecr.io/zoro-web:latest'

@description('Azure Container Registry name.')
param acrRegistryName string = 'baseformacrza'

@description('LiveKit Cloud WebSocket endpoint.')
param livekitUrl string = 'wss://zoro-9ty4k1h3.livekit.cloud'

@description('Supabase URL.')
param supabaseUrl string = 'https://hhumjsjyktaivcmiypfy.supabase.co'

// --- Secrets ---
@secure()
param livekitApiKey string

@secure()
param livekitApiSecret string

@secure()
param supabaseServiceKey string

@secure()
param zoroAccessPassword string

@secure()
param azurePostgresUrl string = ''

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
      ingress: {
        external: true
        targetPort: 3000
        transport: 'auto'
        allowInsecure: false
      }
      secrets: [
        {
          name: 'acr-password'
          value: acr.listCredentials().passwords[0].value
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
          name: 'supabase-service-key'
          value: supabaseServiceKey
        }
        {
          name: 'zoro-access-password'
          value: zoroAccessPassword
        }
        {
          name: 'azure-postgres-url'
          value: azurePostgresUrl
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
          name: 'zoro-web'
          image: containerImage
          resources: {
            cpu: json('0.5')
            memory: '1.0Gi'
          }
          env: [
            {
              name: 'NODE_ENV'
              value: 'production'
            }
            {
              name: 'PORT'
              value: '3000'
            }
            {
              name: 'HOSTNAME'
              value: '0.0.0.0'
            }
            {
              name: 'NEXT_TELEMETRY_DISABLED'
              value: '1'
            }
            {
              name: 'NEXT_PUBLIC_LIVEKIT_URL'
              value: livekitUrl
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
              name: 'SUPABASE_URL'
              value: supabaseUrl
            }
            {
              name: 'SUPABASE_SERVICE_KEY'
              secretRef: 'supabase-service-key'
            }
            {
              name: 'ZORO_ACCESS_PASSWORD'
              secretRef: 'zoro-access-password'
            }
            {
              name: 'AZURE_POSTGRES_URL'
              secretRef: 'azure-postgres-url'
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

output fqdn string = containerApp.properties.configuration.ingress.fqdn
output url string = 'https://${containerApp.properties.configuration.ingress.fqdn}'
output containerAppId string = containerApp.id

