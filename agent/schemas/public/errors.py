class AgentError(Exception):
    code = "AGENT_FAILED"
    retryable = False

    def __init__(self, message="Agent operation failed"):
        super().__init__(message)


class JobCancelled(AgentError):
    code = "JOB_CANCELLED"


class AccessRevoked(AgentError):
    code = "ACCESS_REVOKED"


class ExecutionRevoked(AgentError):
    code = "EXECUTION_REVOKED"


class DependencyUnavailable(AgentError):
    code = "DEPENDENCY_UNAVAILABLE"
    retryable = True


class IntegrityError(AgentError):
    code = "INPUT_INTEGRITY_INVALID"


class BudgetExceeded(AgentError):
    code = "MODEL_BUDGET_EXHAUSTED"


class ModelUnavailable(AgentError):
    code = "MODEL_UNAVAILABLE"


class ModelTransientError(ModelUnavailable):
    retryable = True


class ModelFormatError(AgentError):
    code = "MODEL_OUTPUT_INVALID"

    def __init__(self, message="Invalid model output", *, reason="MODEL_FORMAT_INVALID", field_id=None):
        super().__init__(message)
        self.reason = reason
        # Model-generated identifiers are untrusted and must not enter diagnostic logs verbatim.
        from agent.services.extraction_profiles import PROFILES, field_definition
        from agent.services.normalization import CANONICAL_UNITS

        self.field_id = (
            field_id
            if isinstance(field_id, str)
            and (
                field_id in (*CANONICAL_UNITS, "contract_rate")
                or any(field_definition(kind, field_id) for kind in PROFILES)
            )
            else None
        )


class DocumentUnreadable(AgentError):
    code = "DOCUMENT_UNREADABLE"


class OCRUnavailable(AgentError):
    code = "OCR_UNAVAILABLE"


class OCRFailed(AgentError):
    code = "OCR_FAILED"


class OCRRequired(AgentError):
    code = "OCR_REQUIRED"
