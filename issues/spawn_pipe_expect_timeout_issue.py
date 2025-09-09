import logging
import types

import pywintypes
import win32file
import win32pipe
import winerror

import wexpect
from wexpect import EOF
from wexpect.wexpect_util import EOF_CHAR


logger = logging.getLogger()
logger.setLevel(logging.DEBUG)


def fixed_read_nonblocking(self, size=1):
    if self.closed:
        logger.warning('I/O operation on closed file in read_nonblocking().')
        raise ValueError('I/O operation on closed file in read_nonblocking().')

    try:
        _data, bytes_avail, bytes_left = win32pipe.PeekNamedPipe(self.pipe, 0)

        if not bytes_avail:
            return ""

        to_read = min(bytes_avail, size)
        raw = win32file.ReadFile(self.pipe, to_read)[1]

        if raw:
            logger.debug(f"Readed: {raw}")
        else:
            logger.spam(f"Readed: {raw}")

        if EOF_CHAR in raw:
            self.flag_eof = True
            logger.info("EOF: EOF charachter has arrived")
            raw = raw.split(EOF_CHAR)[0]

        return raw.decode()

    except pywintypes.error as e:
        if e.args and e.args[0] in (winerror.ERROR_BROKEN_PIPE, winerror.ERROR_NO_DATA):
            self.flag_eof = True
            logger.info("EOF: pipe closed")
            raise EOF('broken pipe / pipe closed')
        raise

def test_spawn_pipe_expect_string():
    with wexpect.SpawnPipe("cmd.exe", interact=True) as child:

        expected_str = "Expected str in output"

        child.sendline(f'echo "{expected_str}"')

        result = child.expect([expected_str, wexpect.TIMEOUT], timeout=3)

        assert result == 0

def test_spawn_pipe_expect_timeout_hanging():

    with wexpect.SpawnPipe("cmd.exe", interact=True) as child:

        expected_str = "Expected str in output"
        unexpected_str = "Unexpected str in output"

        child.sendline(f'echo "{expected_str}"')

        # this code leaves test hanging due to read_nonblocking() blocking behaviour
        result = child.expect([unexpected_str, wexpect.TIMEOUT], timeout=3)

        assert result == 1

def test_spawn_pipe_expect_timeout_fixed():

    with wexpect.SpawnPipe("cmd.exe", interact=True) as child:

        # patching read_nonblocking() with non-blocking behaviour
        child.read_nonblocking = types.MethodType(fixed_read_nonblocking, child)

        expected_str = "Expected str in output"
        unexpected_str = "Unexpected str in output"

        child.sendline(f'echo "{expected_str}"')
        result = child.expect([unexpected_str, wexpect.TIMEOUT], timeout=3)

        assert result == 1
