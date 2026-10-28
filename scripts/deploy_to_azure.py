"""Deploy Zoro 24/7 Cloud Core to Azure Container Apps via ARM deployment."""

import json
import os
import sys
from pathlib import Path
from dotenv import dotenv_values

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
TEMPLATE_PATH = ROOT / "infra" / "template.json"


def get_deployment_payload() -> dict:
    if not ENV_PATH.exists():
        raise FileNotFoundError(f"Missing .env at {ENV_PATH}")
    if not TEMPLATE_PATH.exists():
        raise FileNotFoundError(f"Missing ARM template at {TEMPLATE_PATH}")

    env = dotenv_values(ENV_PATH)
    with open(TEMPLATE_PATH, "r", encoding="utf-8") as f:
        template = json.load(f)

    parameters = {
        "location": {"value": "southafricanorth"},
        "environmentName": {"value": "baseform-env-za"},
        "containerAppName": {"value": "zoro-agent"},
        "containerImage": {"value": "ghcr.io/senzekoskhathi/zoro:latest"},
        "registryServer": {"value": "ghcr.io"},
        "registryUsername": {"value": ""},
        "registryPassword": {"value": ""},
        "azureOpenAiApiKey": {"value": env.get("AZURE_OPENAI_API_KEY", "")},
        "livekitApiKey": {"value": env.get("LIVEKIT_API_KEY", "")},
        "livekitApiSecret": {"value": env.get("LIVEKIT_API_SECRET", "")},
        "deepgramApiKey": {"value": env.get("DEEPGRAM_API_KEY", "")},
        "cartesiaApiKey": {"value": env.get("CARTESIA_API_KEY", "")},
        "anthropicApiKey": {"value": env.get("ANTHROPIC_API_KEY", "")},
        "tavilyApiKey": {"value": env.get("TAVILY_API_KEY", "")},
        "azurePostgresUrl": {"value": env.get("AZURE_POSTGRES_URL", "")},
        "redisUrl": {"value": env.get("REDIS_URL", "")},
        "applicationinsightsConnectionString": {
            "value": env.get("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
        },
        "spotifyClientSecret": {"value": env.get("SPOTIFY_CLIENT_SECRET", "")},
        "youtubeApiKey": {"value": env.get("YOUTUBE_API_KEY", "")},
        "googleOauthClientSecret": {
            "value": env.get("GOOGLE_OAUTH_CLIENT_SECRET", "")
        },
    }

    # Verify all required secrets are non-empty
    missing = [k for k, v in parameters.items() if not v["value"] and "registry" not in k]
    if missing:
        print(f"[warning] The following parameters are empty in .env: {missing}")

    return {
        "template": template,
        "parameters": parameters,
    }


if __name__ == "__main__":
    payload = get_deployment_payload()
    print("Deployment payload generated successfully!")
    print(f"Target Container App: {payload['parameters']['containerAppName']['value']}")
    print(f"Target Environment: {payload['parameters']['environmentName']['value']}")
    print(f"Image: {payload['parameters']['containerImage']['value']}")

