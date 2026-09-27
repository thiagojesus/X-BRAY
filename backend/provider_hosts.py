from dataclasses import dataclass
from typing import Final
from urllib.parse import urlparse

ALLOWED_PROVIDER_HOSTS: Final[frozenset[str]] = frozenset(
    {
        "api.bcb.gov.br",
        "olinda.bcb.gov.br",
        "s3-data-prd-use1-precos.s3.us-east-1.amazonaws.com",
        "www.b3.com.br",
        "gamma-api.polymarket.com",
        "clob.polymarket.com",
        "www.tesourotransparente.gov.br",
        "apisidra.ibge.gov.br",
    }
)


@dataclass(slots=True)
class ProviderHostRejectedError(ValueError):
    hostname: str

    def __str__(self) -> str:
        return f"provider host rejected: {self.hostname}"


def require_allowed_provider_url(url: str) -> str:
    parsed = urlparse(url)
    hostname = parsed.hostname or ""
    if parsed.scheme != "https" or hostname not in ALLOWED_PROVIDER_HOSTS:
        raise ProviderHostRejectedError(hostname=hostname)
    return url
