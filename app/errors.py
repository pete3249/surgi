class HttpError(Exception):
    """An error we want to show the user, with the HTTP status to send.

    `fatal=True` means "trying another AI model won't help" (bad key, no credit).
    """

    def __init__(self, status: int, message: str, *, fatal: bool = False):
        super().__init__(message)
        self.status = status
        self.message = message
        self.fatal = fatal
