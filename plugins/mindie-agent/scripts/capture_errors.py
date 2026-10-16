"""Static capture failure codes shared across the private helper boundary."""

SOURCE_CODES = frozenset({
    "transcript_unavailable",
    "transcript_outside_profile",
    "transcript_not_regular",
    "transcript_header_invalid",
    "transcript_identity_mismatch",
    "transcript_metadata_invalid",
})


class TranscriptSourceError(ValueError):
    def __init__(self, code, message):
        if code not in SOURCE_CODES:
            raise ValueError("unsupported transcript failure code")
        super().__init__(message)
        self.failure = {"stage": "transcript", "code": code}


def validated_failure(value):
    """Never project arbitrary helper error text or identifiers as a code."""
    if (isinstance(value, dict) and set(value) == {"stage", "code"}
            and value["stage"] == "transcript" and isinstance(value["code"], str)
            and value["code"] in SOURCE_CODES):
        return dict(value)
    return None
