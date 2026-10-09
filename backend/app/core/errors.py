class DomainError(Exception):
    def __init__(
        self, code: str, message: str, status: int = 400, *, retryable: bool = False
    ) -> None:
        super().__init__(message)
        self.code, self.message, self.status, self.retryable = code, message, status, retryable
