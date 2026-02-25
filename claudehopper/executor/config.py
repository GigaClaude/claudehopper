"""Executor connection configuration.

Reads from environment variables or explicit parameters.
No file-based config — keeps deployment simple.
"""

import os
import typing as tp


class ExecutorConfig:
    """Executor connection settings."""

    def __init__(
        self,
        url: tp.Optional[str] = None,
        cert_path: tp.Optional[str] = None,
        key_path: tp.Optional[str] = None,
        ssl_verify: bool = True,
    ):
        """
        Args:
            url: Executor server URL (https://host:port).
                 Defaults to EXECUTOR_URL env var.
            cert_path: Path to client certificate PEM file.
                       Defaults to EXECUTOR_CERT_PATH env var.
            key_path: Path to client key PEM file.
                      Defaults to EXECUTOR_KEY_PATH env var.
            ssl_verify: Verify SSL certificates. Set False for self-signed.
        """
        self.url = url or os.getenv("EXECUTOR_URL", "https://localhost:18111")
        self.cert_path = cert_path or os.getenv("EXECUTOR_CERT_PATH")
        self.key_path = key_path or os.getenv("EXECUTOR_KEY_PATH")
        self.ssl_verify = ssl_verify
