"""Bounded SSH stream collection shared by diagnostics and first installation."""
import codecs


def exec_collect(client, command, *, timeout=30, input_data=None, on_line=None, tail_limit=4096):
    """Drain both SSH streams together, retaining only a bounded decoded tail.

    Reading stdout and then stderr can exhaust the shared SSH channel window
    when a command writes heavily to stderr. Combine before sending any input;
    Paramiko also moves already-buffered stderr into the combined stream.
    """
    if tail_limit < 1:
        raise ValueError('SSH output tail limit must be positive')
    inp, out, err = client.exec_command(command, timeout=timeout)
    channel = out.channel
    channel.set_combine_stderr(True)
    decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
    tail = ''
    pending = ''
    try:
        if input_data is not None:
            inp.write(input_data)
            inp.flush()
        inp.channel.shutdown_write()
        while True:
            chunk = channel.recv(65536)
            text = decoder.decode(chunk, final=not chunk)
            tail = (tail + text)[-tail_limit:]
            if on_line:
                pending += text
                lines = pending.split('\n')
                pending = lines.pop()[-16384:]
                for line in lines:
                    on_line(line.rstrip('\r'))
            if not chunk:
                break
        if on_line and pending:
            on_line(pending)
        return channel.recv_exit_status(), tail
    finally:
        channel.close()
