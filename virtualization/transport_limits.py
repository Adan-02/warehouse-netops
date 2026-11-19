"""UART limits shared by the shell and one-shot probe builders."""

ROUTER_COMMAND_BYTES=380
GUEST_COMMAND_BYTES=3000
UART_WRITE_CHUNK_BYTES=16
UART_WRITE_PAUSE_SECONDS=0.01


def command_limit(role):
    return ROUTER_COMMAND_BYTES if role=='router' else GUEST_COMMAND_BYTES
