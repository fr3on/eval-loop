from typing import Any, Mapping

import requests
from dify_plugin import ToolProvider
from dify_plugin.errors.tool import ToolProviderCredentialValidationError


class EvalLoopProvider(ToolProvider):
    def _validate_credentials(self, credentials: Mapping[str, Any]) -> None:
        missing = [
            name
            for name in ("app", "dify_base_url", "dify_api_key", "eval_model")
            if not credentials.get(name)
        ]
        if missing:
            raise ToolProviderCredentialValidationError(f"Missing required settings: {', '.join(missing)}")

        if credentials.get("save_to_dataset") and not (
            credentials.get("dataset_id") and credentials.get("dataset_api_key")
        ):
            raise ToolProviderCredentialValidationError(
                "'Save Report to Knowledge Base' is on, but Knowledge Base ID/API Key is missing."
            )

        base_url = str(credentials["dify_base_url"]).rstrip("/")
        try:
            resp = requests.get(
                f"{base_url}/parameters",
                headers={"Authorization": f"Bearer {credentials['dify_api_key']}"},
                timeout=10,
            )
        except Exception as e:
            raise ToolProviderCredentialValidationError(f"Could not reach '{base_url}': {e}") from e

        if resp.status_code != 200:
            raise ToolProviderCredentialValidationError(
                f"Dify API Base URL / App API Key check failed: {resp.status_code} {resp.text[:200]}"
            )
