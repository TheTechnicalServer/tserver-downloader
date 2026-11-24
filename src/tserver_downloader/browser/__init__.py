"""Browser extension integration: native messaging + localhost IPC."""
from .ipc import APP_NAME, APP_VERSION, IpcServer, generate_token, read_message, write_message
from .native_host import default_dispatch, get_queue_path, main, user_data_dir

__all__ = [
    "APP_NAME",
    "APP_VERSION",
    "IpcServer",
    "generate_token",
    "read_message",
    "write_message",
    "default_dispatch",
    "get_queue_path",
    "main",
    "user_data_dir",
]
