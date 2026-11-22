"""Soony Supplier Portal API client.

    from soony_api import SoonyClient
    with SoonyClient.from_env() as api:
        report = api.files.sync()        # mirror portal -> ./soony-data
        print(report.summary())

    listener = DataListener(api, interval=60)
    listener.subscribe(handlers.console())
    listener.run_forever()
"""

from .auth import BearerAuth, HeaderAuth, QueryAuth, TokenAuth, register_auth
from .client import SoonyClient
from .config import Settings, load_dotenv
from .errors import (
    AuthenticationError,
    BadRequestError,
    ConfigError,
    ConflictError,
    IntegrityError,
    NotFoundError,
    PermissionDeniedError,
    RateLimitError,
    ServerError,
    SoonyAPIError,
    SoonyError,
    TransportError,
    ValidationError,
)
from .resources import Resource, register_resource
from .resources.files import DataFile
from .events import ChangeEvent, CsvChange
from .listener import DataListener, ListenerStatus
from .sync import SyncLockedError, SyncReport, sync_files
from . import handlers

__all__ = [
    "SoonyClient", "Settings", "load_dotenv",
    "Resource", "register_resource", "DataFile", "SyncReport", "SyncLockedError", "sync_files",
    "DataListener", "ListenerStatus", "ChangeEvent", "CsvChange", "handlers",
    "TokenAuth", "BearerAuth", "HeaderAuth", "QueryAuth", "register_auth",
    "SoonyError", "SoonyAPIError", "ConfigError", "TransportError", "IntegrityError",
    "BadRequestError", "AuthenticationError", "PermissionDeniedError", "NotFoundError",
    "ConflictError", "ValidationError", "RateLimitError", "ServerError",
]
__version__ = "0.1.0"
