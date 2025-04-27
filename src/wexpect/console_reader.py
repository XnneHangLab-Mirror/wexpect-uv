"""Wexpect is a Windows variant of pexpect https://pexpect.readthedocs.io.

Wexpect is a Python module for spawning child applications and controlling
them automatically.

console_reader Implements a virtual terminal, and starts the child program.
The main wexpect.spawn class connect to this class to reach the child's terminal.
"""

import time
import logging
import os
import traceback
import psutil
from io import StringIO
import wcwidth

import ctypes
from ctypes import windll
import win32console
import win32process
import win32con
import win32file
import win32gui
import win32pipe
import socket

from .wexpect_util import init_logger
from .wexpect_util import EOF_CHAR
from .wexpect_util import SIGNAL_CHARS
from .wexpect_util import TIMEOUT
from .wexpect_util import setup_logger

#
# System-wide constants
#
screenbufferfillchar = '\0'
maxconsoleY = 8000
default_port = 4321

#
# Create logger: We write logs only to file. Printing out logs are dangerous, because of the deep
# console manipulation.
#
logger = logging.getLogger('wexpect')


class ConsoleReaderBase:
    """Consol class (aka. client-side python class) for the child.

    This class initialize the console starts the child in it and reads the console periodically.
    """

    def __init__(self, path, host_pid, codepage=None, window_size_x=120, window_size_y=25,
                 buffer_size_x=120, buffer_size_y=16000, local_echo=True, interact=False, preserve_colors=True, **kwargs):
        """Initialize the console starts the child in it and reads the console periodically.

        Args:
            path (str): Child's executable with arguments.
            parent_pid (int): Parent (aka. host) process process-ID
            codepage (:obj:, optional): Output console code page.
        """
        self.lastRead = 0
        self.__bufferY = 0
        self.lastReadData = ""
        self.totalRead = 0
        self.__buffer = StringIO()
        self.__currentReadCo = win32console.PyCOORDType(0, 0)
        self.pipe = None
        self.connection = None
        self.consin = None
        self.consout = None
        self.local_echo = local_echo
        self.console_pid = os.getpid()
        self.host_pid = host_pid
        self.host_process = psutil.Process(host_pid)
        self.child_process = None
        self.child_pid = None
        self.enable_signal_chars = True
        self.timeout = 30
        self.child_exitstatus = None
        self.preserve_colors = preserve_colors
        logger = setup_logger(level=logging.DEBUG, log_file="./wexpect_log")

        logger.info(f'ConsoleReader started. location {os.path.abspath(__file__)}')

        if codepage is None:
            codepage = windll.kernel32.GetACP()

        try:
            logger.info("Setting console output code page to %s" % codepage)
            win32console.SetConsoleOutputCP(codepage)
            logger.info(
                "Console output code page: %s" % ctypes.windll.kernel32.GetConsoleOutputCP())
        except Exception as e: # pragma: no cover
            # I hope this code is unreachable...
            logger.error(e)

        try:
            self.create_connection(**kwargs)
            logger.info('Spawning %s' % path)
            try:
                self.initConsole()
                self.child_process = psutil.Popen(path)

                logger.info(f'Child pid: {self.child_pid}  Console pid: {self.console_pid}')

            except Exception: # pragma: no cover
                # I hope this code is unreachable...
                logger.error(traceback.format_exc())
                return

            if interact:
                self.interact()
                self.interact()

            self.read_loop()
        except Exception: # pragma: no cover
            # I hope this code is unreachable...
            logger.error(traceback.format_exc())
        finally:
            try:
                self.terminate_child()
                time.sleep(.01)
                self.send_to_host(self.readConsoleToCursor())
                self.sendeof()
                time.sleep(.1)
                self.close_connection()
                logger.info('Console finished.')
            except Exception: # pragma: no cover
                # I hope this code is unreachable...
                logger.error(traceback.format_exc())

    def read_loop(self):
        last_cursor_y = 0
        
        while True:
            if not self.isalive(self.host_process):
                logger.info('Host process has been died.')
                return
                    
            try:
                self.child_exitstatus = self.child_process.wait(0)
                logger.info(f'Child finished with code: {self.child_exitstatus}')
                return
            except psutil.TimeoutExpired:
                pass
                    
            consinfo = self.consout.GetConsoleScreenBufferInfo()
            cursorPos = consinfo['CursorPosition']
            
            # 检测光标是否移动或有新内容
            has_new_content = (cursorPos.Y > last_cursor_y)
            
            if cursorPos.Y > maxconsoleY:
                logger.info('cursorPos %s' % cursorPos)
                self.suspend_child()
                time.sleep(.2)
                output = self.readConsoleToCursor()
                self.send_to_host(output)
                self.refresh_console()
                self.resume_child()
            elif has_new_content:
                # 读取控制台输出并立即发送
                output = self.readConsoleToCursor()
                if output:
                    self.send_to_host(output)
                    last_cursor_y = cursorPos.Y
            
            # 处理来自主机的输入
            s = self.get_from_host()
            if s:
                logger.debug(f'get_from_host: {s}')
                if self.enable_signal_chars:
                    for sig, char in SIGNAL_CHARS.items():
                        if char in s:
                            self.child_process.send_signal(sig)
                s = s.decode()
                self.write(s)
                
            # 短暂休眠以避免CPU过载
            time.sleep(.01)  # 减少休眠时间，更频繁地检查

    def suspend_child(self):
        """Pauses the main thread of the child process."""
        handle = windll.kernel32.OpenThread(win32con.THREAD_SUSPEND_RESUME, 0, self.child_tid)
        win32process.SuspendThread(handle)

    def resume_child(self):
        """Un-pauses the main thread of the child process."""
        handle = windll.kernel32.OpenThread(win32con.THREAD_SUSPEND_RESUME, 0, self.child_tid)
        win32process.ResumeThread(handle)

    def refresh_console(self):
        """Clears the console after pausing the child and
        reading all the data currently on the console."""
        orig = win32console.PyCOORDType(0, 0)
        self.consout.SetConsoleCursorPosition(orig)
        self.__currentReadCo.X = 0
        self.__currentReadCo.Y = 0
        writelen = self.__consSize.X * self.__consSize.Y
        # Use NUL as fill char because it displays as whitespace
        self.consout.FillConsoleOutputCharacter(screenbufferfillchar, writelen, orig)
        self.__bufferY = 0
        self.__buffer.truncate(0)
        self.__buffer.seek(0)
        self.lastReadData = ""  # 重置最后读取的数据

    def terminate_child(self):
        try:
            if self.child_process:
                self.child_process.kill()
        except psutil.NoSuchProcess:
            logger.info('The process has already died.')
        return

    def isalive(self, process):
        """True if the child is still alive, false otherwise"""
        try:
            self.exitstatus = process.wait(timeout=0)
            return False
        except psutil.TimeoutExpired:
            return True

    def write(self, s):
        """Writes input into the child consoles input buffer."""

        if len(s) == 0:
            return 0
        if s[-1] == '\n' or s[-1] == "\r":
            s = s[:-1]
        records = [self.createKeyEvent(c) for c in str(s)]
        if not self.consout:
            return ""

        # Store the current cursor position to hide characters in local echo disabled mode
        # (workaround).
        consinfo = self.consout.GetConsoleScreenBufferInfo()
        startCo = consinfo['CursorPosition']

        # Send the string to console input
        wrote = self.consin.WriteConsoleInput(records)

        # Wait until all input has been recorded by the console.
        ts = time.time()
        while self.consin.PeekConsoleInput(8) != ():
            if time.time() > ts + len(s) * .1 + .5:
                break
            time.sleep(.05)

        # Hide characters in local echo disabled mode (workaround).
        if not self.local_echo:
            self.consout.FillConsoleOutputCharacter(screenbufferfillchar, len(s), startCo)

        return wrote

    def createKeyEvent(self, char):
        """Creates a single key record corrosponding to
            the ascii character char."""

        evt = win32console.PyINPUT_RECORDType(win32console.KEY_EVENT)
        evt.KeyDown = True
        evt.Char = char
        evt.RepeatCount = 1
        return evt

    def initConsole(self, consout=None, window_size_x=120, window_size_y=25, buffer_size_x=120,
                    buffer_size_y=16000):
        if not consout:
            consout = self.getConsoleOut()
        
        # 启用ANSI处理
        if self.preserve_colors:
            # 获取当前控制台模式
            handle = windll.kernel32.GetStdHandle(win32console.STD_OUTPUT_HANDLE)
            mode = ctypes.c_ulong()
            windll.kernel32.GetConsoleMode(handle, ctypes.byref(mode))
            
            # 启用ANSI处理 (ENABLE_VIRTUAL_TERMINAL_PROCESSING = 0x0004)
            windll.kernel32.SetConsoleMode(handle, mode.value | 0x0004)
        
        self.consin = win32console.GetStdHandle(win32console.STD_INPUT_HANDLE)
        
        # 其余代码保持不变
        rect = win32console.PySMALL_RECTType(0, 0, window_size_x - 1, window_size_y - 1)
        consout.SetConsoleWindowInfo(True, rect)
        size = win32console.PyCOORDType(buffer_size_x, buffer_size_y)
        consout.SetConsoleScreenBufferSize(size)
        pos = win32console.PyCOORDType(0, 0)
        consout.FillConsoleOutputCharacter(screenbufferfillchar, size.X * size.Y, pos)
        
        consinfo = consout.GetConsoleScreenBufferInfo()
        self.__consSize = consinfo['Size']
        logger.info('self.__consSize: ' + str(self.__consSize))
        self.startCursorPos = consinfo['CursorPosition']

    def parseData(self, s):
        """Ensures that special characters are interpretted as
        newlines or blanks, depending on if there written over
        characters or screen-buffer-fill characters."""

        strlist = []
        for i, c in enumerate(s):
            if c == screenbufferfillchar:
                if (self.totalRead - self.lastRead + i + 1) % self.__consSize.X == 0:
                    strlist.append('\r\n')
            else:
                strlist.append(c)

        s = ''.join(strlist)
        return s

    def getConsoleOut(self):
        consfile = win32file.CreateFile(
            'CONOUT$',
            win32con.GENERIC_READ | win32con.GENERIC_WRITE,
            win32con.FILE_SHARE_READ | win32con.FILE_SHARE_WRITE,
            None,
            win32con.OPEN_EXISTING,
            0,
            0)

        self.consout = win32console.PyConsoleScreenBufferType(consfile)
        return self.consout

    def getCoord(self, offset):
        """Converts an offset to a point represented as a tuple."""

        x = offset % self.__consSize.X
        y = offset // self.__consSize.X
        return win32console.PyCOORDType(x, y)

    def getOffset(self, coord):
        """Converts a tuple-point to an offset."""

        return coord.X + coord.Y * self.__consSize.X

    def readConsole(self, startCo, endCo):
        """Reads the console area from startCo to endCo and returns it
        as a string."""

        if startCo is None:
            startCo = self.startCursorPos
            startCo.Y = startCo.Y

        if endCo is None:
            consinfo = self.consout.GetConsoleScreenBufferInfo()
            endCo = consinfo['CursorPosition']
            endCo = self.getCoord(0 + self.getOffset(endCo))

        buff = []
        self.lastRead = 0

        while True:
            startOff = self.getOffset(startCo)
            endOff = self.getOffset(endCo)
            readlen = endOff - startOff

            if readlen <= 0:
                break

            if readlen > 4000:
                readlen = 4000
            endPoint = self.getCoord(startOff + readlen)

            s = self.consout.ReadConsoleOutputCharacter(readlen, startCo)
            self.lastRead += len(s)
            self.totalRead += len(s)
            buff.append(s)

            startCo = endPoint

        return ''.join(buff)

    def _display_width(self, s):
        """计算字符串在终端中的显示宽度，处理特殊情况"""
        try:
            # 处理制表符 - 在大多数终端中宽度为8或4
            s = s.replace('\t', ' ' * 8)
            
            # 处理ANSI转义序列 - 它们不占显示宽度
            # 简单的正则表达式移除ANSI转义序列
            import re
            s_clean = re.sub(r'\x1b\[[\d;]*[a-zA-Z]', '', s)
            
            width = wcwidth.wcswidth(s_clean)
            return width if width >= 0 else len(s_clean)
        except Exception as e:
            logger.error(f"Error calculating width: {e}")
            return len(s)  # 回退到字符计数

    def readConsoleToCursor(self):
        if not self.consout:
            return ""
        consinfo = self.consout.GetConsoleScreenBufferInfo()
        cursorPos = consinfo['CursorPosition']
        start_y = self.__currentReadCo.Y
        end_y = cursorPos.Y
        
        logger.debug(f"Reading console from line {start_y} to {end_y}")
        
        # 如果没有新行可读，返回空
        if start_y > end_y:
            return ""
        
        # 添加调试信息
        logger.debug(f"Current read position: {self.__currentReadCo.Y}, Cursor position: {cursorPos.Y}")
        
        buffer_width = self.__consSize.X
        lines = []
        for y in range(start_y, end_y + 1):
            line = self._read_raw_line(y)
            lines.append(line)
            logger.debug(f"Read line {y}: {repr(line[:20])}...")
        
        # 没有读到任何内容
        if not lines:
            logger.debug("No lines read")
            return ""
        
        logical_lines = []
        current_line = ""
        
        for i, line in enumerate(lines):
            # 去除行尾填充
            line_content = line.rstrip(screenbufferfillchar).rstrip('\x00')
            
            # 跳过全填充行
            if not line_content:
                continue
            # 记录调试信息
            display_width = self._display_width(line_content)
            logger.debug(f"Line {i}: content='{line_content[:20]}...', width={display_width}, buffer_width={buffer_width}")
            
            current_line += line_content
            is_wrapped_line = False
            
            # 仅当不是最后一行，并且显示宽度接近或等于buffer宽度时，认为是包装行
            if i < len(lines) - 1 and display_width >= buffer_width - 5:
                is_wrapped_line = True
                logger.debug(f"Line {i} is wrapped (width)")
                
            # 不再使用下一行是否以空格开头的条件
            # if i < len(lines) - 1 and line_content and not lines[i+1].startswith(' '):
            #     is_wrapped_line = True
            #     logger.debug(f"Line {i} is wrapped (next line)")
                
            if not is_wrapped_line or i == len(lines) - 1:
                if current_line: 
                    logical_lines.append(current_line)
                    logger.debug(f"Added logical line: {repr(current_line[:20])}...")
                current_line = ""
        
        # 处理最后一行
        if current_line:
            logical_lines.append(current_line)
            logger.debug(f"Added last logical line: {repr(current_line[:20])}...")
        
        if logical_lines:
            # 在逻辑行之间添加 \r\n
            result = '\r\n'.join(logical_lines)
            # 清理所有 \x00 字符
            result = result.replace('\x00', '')
            
            # 更新读取位置
            self.__currentReadCo.X = cursorPos.X
            self.__currentReadCo.Y = cursorPos.Y
            self.__bufferY = cursorPos.Y
            
            self.lastReadData = result
            logger.debug(f'Read {len(logical_lines)} logical lines, total length: {len(result)}')
            return result
        
        logger.debug("No logical lines formed")
        return ""

    def _read_raw_line(self, y):
        try:
            line_start = win32console.PyCOORDType(0, y)
            # 读取字符
            line_content = self.consout.ReadConsoleOutputCharacter(self.__consSize.X, line_start)
            
            # 去除填充字符和 \x00
            line_content = line_content.rstrip(screenbufferfillchar).rstrip('\x00')
            
            # 如果需要保留颜色信息
            if self.preserve_colors:
                # 读取字符属性
                line_attrs = self.consout.ReadConsoleOutputAttribute(self.__consSize.X, line_start)
                # 将字符和属性转换为带有ANSI转义序列的文本
                ansi_line = self._convert_attrs_to_ansi(line_content, line_attrs)
                return ansi_line
            else:
                return line_content
        except Exception as e:
            logger.debug(f"Error reading line {y}: {e}")
            return ""

    def _win_color_to_ansi(self, color, is_foreground):
        """将Windows控制台颜色转换为ANSI颜色代码"""
        # Windows颜色映射到ANSI颜色
        # 前景色: 30-37 (黑,红,绿,黄,蓝,紫,青,白)
        # 背景色: 40-47 (黑,红,绿,黄,蓝,紫,青,白)
        base = 30 if is_foreground else 40
        
        # Windows颜色是按位组合的:
        # 位0: 蓝色
        # 位1: 绿色
        # 位2: 红色
        # 位3: 高亮
        
        ansi_color = base
        if color & 1:  # 蓝色位
            ansi_color += 4
        if color & 2:  # 绿色位
            ansi_color += 2
        if color & 4:  # 红色位
            ansi_color += 1
        
        # 处理高亮
        if color & 8:
            if is_foreground:
                # 对于前景色，使用90-97代替30-37表示高亮
                ansi_color += 60
        
        return ansi_color

    def _convert_attrs_to_ansi(self, text, attrs):
        """将字符和属性转换为带有ANSI转义序列的文本，考虑显示宽度"""
        result = []
        current_fg = None
        current_bg = None
        width_index = 0  # 基于显示宽度的索引

        for i, char in enumerate(text):
            # 跳过填充字符
            if char == screenbufferfillchar:
                result.append(char)
                continue

            # 计算当前字符的显示宽度
            char_width = wcwidth.wcwidth(char)
            if char_width < 0:  # 如果无法确定宽度，假设为1
                char_width = 1

            # 由于一个字符可能对应多个宽度单位，取宽度范围内的第一个属性作为代表
            # 这里假设 attrs 数组的长度是基于显示宽度的
            if width_index < len(attrs):
                attr = attrs[width_index]
                fg_color = attr & 0x0F  # 前景色（低4位）
                bg_color = (attr & 0xF0) >> 4  # 背景色（高4位）

                # 只在颜色变化时添加ANSI序列
                needs_reset = False
                needs_fg_change = (current_fg != fg_color and fg_color != 7)  # 7是默认前景色
                needs_bg_change = (current_bg != bg_color and bg_color != 0)  # 0是默认背景色

                # 如果需要重置颜色（从有颜色变为默认颜色）
                if ((current_fg is not None and current_fg != 7 and fg_color == 7) or
                    (current_bg is not None and current_bg != 0 and bg_color == 0)):
                    result.append('\x1b[0m')
                    current_fg = 7
                    current_bg = 0
                    needs_reset = True

                # 添加前景色
                if needs_fg_change:
                    ansi_fg = self._win_color_to_ansi(fg_color, True)
                    result.append(f'\x1b[{ansi_fg}m')
                    current_fg = fg_color

                # 添加背景色
                if needs_bg_change:
                    ansi_bg = self._win_color_to_ansi(bg_color, False)
                    result.append(f'\x1b[{ansi_bg}m')
                    current_bg = bg_color

            result.append(char)
            width_index += char_width  # 累积显示宽度

        # 最后重置所有属性
        if current_fg != 7 or current_bg != 0:
            result.append('\x1b[0m')

        return ''.join(result)

    def interact(self):
        """Displays the child console for interaction."""

        logger.debug('Start interact window')
        win32gui.ShowWindow(win32console.GetConsoleWindow(), win32con.SW_SHOW)

    def sendeof(self):
        """This sends an EOF to the host. This sends a character which inform the host that child
        has been finished, and all of it's output has been send to host.
        """

        self.send_to_host(EOF_CHAR)


class ConsoleReaderSocket(ConsoleReaderBase):

    def create_connection(self, **kwargs):
        try:
            self.port = kwargs['port']
            # Create a TCP/IP socket
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            server_address = ('localhost', self.port)
            self.sock.bind(server_address)
            logger.info(f'Socket started at port: {self.port}')

            # Listen for incoming connections
            self.sock.settimeout(5)
            self.sock.listen(1)
            self.connection, client_address = self.sock.accept()
            self.connection.settimeout(.01)
            logger.info(f'Client connected: {client_address}')
        except Exception as e:  # pragma: no cover
            # I hope this code is unreachable.
            logger.error(f"Port: {self.port} {e}")
            raise

    def close_connection(self):
        if self.connection:
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
            self.connection = None

    def send_to_host(self, msg):
        # convert to bytes
        if isinstance(msg, str):
            msg = str.encode(msg)
        if msg:
            logger.debug(f'Sending msg: {msg}')
        else:
            logger.spam(f'Sending msg: {msg}')
        self.connection.sendall(msg)

    def get_from_host(self):
        try:
            msg = self.connection.recv(4096)
        except socket.timeout as e:
            err = e.args[0]
            # this next if/else is a bit redundant, but illustrates how the
            # timeout exception is setup
            if err == 'timed out':
                logger.debug('recv timed out, retry later')
                return b''
            else:
                raise
        else:
            if len(msg) == 0:
                raise Exception('orderly shutdown on server end')
            else:
                # got a message do something :)
                return msg


class ConsoleReaderPipe(ConsoleReaderBase):
    def create_connection(self, timeout=-1, **kwargs):
        if timeout == -1:
            timeout = self.timeout
        if timeout is None:
            end_time = float('inf')
        else:
            end_time = time.time() + timeout

        try:
            self.pipe_name = kwargs['pipe_file_name']
        except KeyError:
            self.pipe_name = 'wexpect_{}'.format(self.console_pid)

        pipe_full_path = r'\\.\pipe\{}'.format(self.pipe_name)
        logger.info('Start pipe server: %s', pipe_full_path)
        self.pipe = win32pipe.CreateNamedPipe(
            pipe_full_path,
            win32pipe.PIPE_ACCESS_DUPLEX,
            win32pipe.PIPE_TYPE_MESSAGE | win32pipe.PIPE_READMODE_MESSAGE | win32pipe.PIPE_NOWAIT,
            1, 65536, 65536, 10000, None)
        logger.info("waiting for client")
        while True:
            if end_time < time.time():
                raise TIMEOUT('Connect to child has been timed out.')
            try:
                win32pipe.ConnectNamedPipe(self.pipe, None)
                break
            except Exception as e:
                logger.debug(e)
                time.sleep(0.2)
        logger.info('got client')

    def close_connection(self):
        if self.pipe:
            win32file.CloseHandle(self.pipe)

    def send_to_host(self, msg):
        # convert to bytes
        if isinstance(msg, str):
            msg = str.encode(msg)
        if msg:
            logger.debug(f'Sending msg: {msg}')
        else:
            logger.spam(f'Sending msg: {msg}')
        win32file.WriteFile(self.pipe, msg)

    def get_from_host(self):
        data, avail, bytes_left = win32pipe.PeekNamedPipe(self.pipe, 4096)
        logger.spam(f'data: {data}  avail:{avail}  bytes_left{bytes_left}')
        if avail > 0:
            resp = win32file.ReadFile(self.pipe, 4096)
            ret = resp[1]
            return ret
        else:
            return b''
