"""Versioned, manually reviewed BCB SGS metadata start periods.

These dates are scope limits, not observations or evidence of complete coverage.
Each snapshot records when its source metadata was inspected; refreshing it is
an explicit registry revision, never part of ordinary provider refreshes.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from types import MappingProxyType
from typing import Final

REGISTRY_VERSION: Final = "bcb-sgs-start-periods-v2"
SUPPORTED_REGISTRY_VERSIONS: Final = ("bcb-sgs-start-periods-v1", REGISTRY_VERSION)
OBSERVED_ON: Final = date(2026, 10, 29)
_V1_OBSERVED_ON: Final = date(2026, 10, 28)
SOURCE_URL_TEMPLATE: Final = (
    "https://www3.bcb.gov.br/sgspub/consultarmetadados/"
    "consultarMetadadosSeries.do?hdOidSerieSelecionada={code}"
    "&method=consultarMetadadosSeriesInternet"
)
DETAIL_URL: Final = "https://www3.bcb.gov.br/sgspub/JSP/consultarmetadados/cmiDadosBasicos.jsp"


@dataclass(frozen=True, slots=True)
class SGSAvailability:
    series_code: int
    documented_start: date
    first_reference_period: date
    source_url: str
    detail_url: str
    observed_on: date
    basis: str = "documented_start"
    registry_version: str = REGISTRY_VERSION
    registry_sha256: str = ""


# Exact raw metadata dates are DD/MM/YYYY, as displayed by the BCB page.
_V1_STARTS: Final[Mapping[int, str]] = MappingProxyType({
    1: "28/11/1984", 11: "04/06/1986", 12: "06/03/1986", 188: "01/04/1979",
    189: "01/06/1989", 190: "01/02/1944", 192: "01/02/1944", 226: "01/02/1991",
    432: "05/03/1999", 433: "01/01/1980", 1207: "31/12/1962", 1635: "01/01/1991",
    1636: "01/01/1991", 1637: "01/01/1991", 1638: "01/01/1991", 1639: "01/08/1999",
    1640: "01/08/1999", 1641: "01/01/1991", 1642: "01/01/1991", 1643: "01/08/1999",
    1788: "31/01/1946", 1835: "31/07/1988", 3546: "31/12/1970", 4447: "01/01/1991",
    4448: "01/01/1991", 4449: "01/01/1991", 4466: "01/12/1991", 4513: "01/12/2001",
    4639: "31/01/1991", 5727: "01/11/2002", 5760: "01/11/2002", 5793: "01/11/2002",
    7478: "01/05/2000", 10813: "28/11/1984", 10841: "01/01/1991", 10842: "01/01/1991",
    10843: "01/01/1991", 10844: "01/01/1991", 11428: "01/01/1991", 13522: "01/12/1980",
    13762: "01/12/2006", 16122: "01/01/1995", 20716: "01/03/2011", 21084: "01/03/2011",
    21619: "31/12/1998", 22707: "01/01/1995", 22885: "01/01/1995", 22905: "01/01/1995",
    23079: "01/01/1995", 23080: "01/01/1995", 24363: "01/01/2003", 24364: "01/01/2003",
    24369: "01/03/2012", 24382: "01/03/2012", 25353: "01/01/2013", 27574: "01/01/1998",
    27788: "03/12/2001", 27810: "31/07/2001", 29034: "01/03/2005", 29037: "01/01/2005",
    29602: "01/01/2003", 29604: "01/01/2003", 29606: "01/01/2003", 29608: "01/01/2003",
})

_RAW_STARTS: Final[Mapping[int, str]] = MappingProxyType({
    **_V1_STARTS,
    20573: "01/03/2007",
    20574: "01/03/2007",
    20587: "01/03/2007",
    20588: "01/03/2007",
})


def _registry_hash(version: str, observed_on: date, starts: Mapping[int, str]) -> str:
    payload = {
        "version": version,
        "observed_on": observed_on.isoformat(),
        "selection_url": SOURCE_URL_TEMPLATE,
        "detail_url": DETAIL_URL,
        "starts": dict(sorted(starts.items())),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


_V1_HASH: Final = _registry_hash(
    "bcb-sgs-start-periods-v1", _V1_OBSERVED_ON, _V1_STARTS
)
REGISTRY_SHA256: Final = _registry_hash(REGISTRY_VERSION, OBSERVED_ON, _RAW_STARTS)
SUPPORTED_REGISTRY_SHA256: Final = (_V1_HASH, REGISTRY_SHA256)
_RAW_STARTS_BY_VERSION: Final[Mapping[str, Mapping[int, str]]] = MappingProxyType(
    {"bcb-sgs-start-periods-v1": _V1_STARTS, REGISTRY_VERSION: _RAW_STARTS}
)
_SHA_BY_VERSION: Final[Mapping[str, str]] = MappingProxyType(
    {"bcb-sgs-start-periods-v1": _V1_HASH, REGISTRY_VERSION: REGISTRY_SHA256}
)


def _parse_metadata_date(value: str) -> date:
    day, month, year = (int(part) for part in value.split("/"))
    return date(year, month, day)


def availability_for_version(
    registry_version: str, series_code: int, frequency: str | None = None,
) -> SGSAvailability | None:
    """Resolve exact, immutable facts from a supported historical registry."""
    versioned_starts = _RAW_STARTS_BY_VERSION.get(registry_version)
    registry_hash = _SHA_BY_VERSION.get(registry_version)
    if versioned_starts is None or registry_hash is None:
        return None
    raw = versioned_starts.get(series_code)
    if raw is None:
        return None
    documented = _parse_metadata_date(raw)
    # Monthly and moving-quarter series identify calendar-month references;
    # 1207's annual period is conservatively normalized to its calendar year.
    if series_code == 1207:
        first_period = date(documented.year, 1, 1)
    elif series_code == 226 or frequency in {"daily", "market", "full_period"}:
        first_period = documented
    else:
        first_period = date(documented.year, documented.month, 1)
    return SGSAvailability(
        series_code, documented, first_period,
        SOURCE_URL_TEMPLATE.format(code=series_code), DETAIL_URL,
        _V1_OBSERVED_ON if registry_version == "bcb-sgs-start-periods-v1" else OBSERVED_ON,
        registry_version=registry_version, registry_sha256=registry_hash,
    )


def availability_for(series_code: int, frequency: str | None = None) -> SGSAvailability | None:
    """Return the current registry's documented start; unknown stays unknown."""
    return availability_for_version(REGISTRY_VERSION, series_code, frequency)


def registered_series_codes() -> tuple[int, ...]:
    return tuple(sorted(_RAW_STARTS))
