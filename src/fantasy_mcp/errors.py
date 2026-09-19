class FantasyError(Exception):
    """Actionable, safe-to-display error. Never include provider response bodies."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")
