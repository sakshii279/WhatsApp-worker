def handle_success(email: str, msg: str = "OK") -> int:            return 1
def handle_stop(email: str) -> int:                                 return 0
def handle_auth_error(email: str, exc: Exception) -> int:          return -1
def handle_connection_timeout(email: str, exc: Exception) -> int:  return -1
def handle_fetch_error(email: str, exc: Exception) -> int:         return -1
def handle_uid_error(email: str, uid: str, exc: Exception) -> int: return -1
def handle_parse_error(email: str, msg_id: str, exc: Exception) -> int: return -1
def handle_attachment_error(email: str, filename: str, exc: Exception) -> int: return -1
def handle_json_error(email: str, exc: Exception) -> int:          return -1
def handle_checkpoint_error(email: str, exc: Exception) -> int:    return -1
def handle_kv_error(email: str, operation: str, exc: Exception) -> int: return -1
def handle_poll_error(email: str, exc: Exception) -> int:          return -1
def handle_worker_crash(email: str, exit_code: int) -> int:        return -1
def handle_worker_restart(email: str) -> int:                       return 0
