"""Engine version constants.

Every persisted derived row records the version of the code that produced it.
This is what makes "why did this score change?" answerable, and it is why a
rule change never rewrites history: recomputation writes new rows under a new
version, and old rows keep theirs.

Bump rules:
  * minor -- a formula changed but the output means the same thing
  * major -- the output means something different
"""

FEATURES_VERSION = "FEATURES_V1.0"

# Phases 2-6. Declared here so that every engine takes its version from one
# place rather than embedding a literal at its write site.
VCP_ENGINE_VERSION = "VCP_ENGINE_V1.0"
SMC_ENGINE_VERSION = "SMC_ENGINE_V1.0"
FVG_ENGINE_VERSION = "FVG_ENGINE_V1.0"
RS_ENGINE_VERSION = "RS_ENGINE_V1.0"
SECTOR_ENGINE_VERSION = "SECTOR_ENGINE_V1.0"
REGIME_ENGINE_VERSION = "REGIME_ENGINE_V1.0"
SCORING_RULE_VERSION = "SCORING_V1.0"
CONFLUENCE_RULE_VERSION = "CONFLUENCE_V1.0"

INGEST_VERSION = "INGEST_V1.0"
