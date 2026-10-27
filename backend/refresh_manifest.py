"""Version identifiers shared by refresh-run creation and its client."""

CURRENT_REFRESH_MANIFEST_VERSION = "daily-refresh-v6"
LEGACY_REFRESH_MANIFEST_VERSIONS = (
    "daily-refresh-v4",
    "daily-refresh-v5",
    "daily-refresh-v3",
    "daily-refresh-v2",
    "daily-refresh-v1",
)
V1_REFRESH_MANIFEST_VERSION = "daily-refresh-v1"
V2_REFRESH_MANIFEST_VERSION = "daily-refresh-v2"
V3_REFRESH_MANIFEST_VERSION = "daily-refresh-v3"
V4_REFRESH_MANIFEST_VERSION = "daily-refresh-v4"
V5_REFRESH_MANIFEST_VERSION = "daily-refresh-v5"
SUPPORTED_REFRESH_MANIFEST_VERSIONS = (
    CURRENT_REFRESH_MANIFEST_VERSION,
    *LEGACY_REFRESH_MANIFEST_VERSIONS,
)
