"""Version identifiers shared by refresh-run creation and its client."""

CURRENT_REFRESH_MANIFEST_VERSION = "daily-refresh-v5"
LEGACY_REFRESH_MANIFEST_VERSIONS = (
    "daily-refresh-v4",
    "daily-refresh-v3",
    "daily-refresh-v2",
    "daily-refresh-v1",
)
SUPPORTED_REFRESH_MANIFEST_VERSIONS = (
    CURRENT_REFRESH_MANIFEST_VERSION,
    *LEGACY_REFRESH_MANIFEST_VERSIONS,
)
