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
        last_line_content = ""  # 新增：记录当前行的内容，用于检测同一行更新
        
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
            
            # 新增：读取当前行内容，检查是否变化
            current_line_start = win32console.PyCOORDType(0, cursorPos.Y)
            try:
                current_line_content = self.consout.ReadConsoleOutputCharacter(self.__consSize.X, current_line_start)
            except Exception as e:
                logger.debug(f"Error reading current line: {e}")
                current_line_content = last_line_content
            
            # 检测光标是否移动或当前行内容是否有更新
            has_new_content = (cursorPos.Y > last_cursor_y) or (current_line_content != last_line_content)
            
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
                    last_line_content = current_line_content  # 更新记录的当前行内容
            
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
                
            # 增加休眠时间，避免CPU过载和重复读取
            time.sleep(.05)  # 调整为稍长的休眠时间

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
            lines.append((y, line))  # 记录行号和内容，用于去重
            logger.debug(f"Read line {y}: {repr(line[:20])}...")
        
        # 没有读到任何内容
        if not lines:
            logger.debug("No lines read")
            return ""
        
        logical_lines = []
        current_line = ""
        seen_lines = set()  # 用于去重，记录已处理的行内容和行号
        
        for i, (y, line) in enumerate(lines):
            # 去除行尾填充
            line_content = line.rstrip(screenbufferfillchar).rstrip('\x00')
            
            # 跳过全填充行
            if not line_content:
                continue
            
            # 对于同一行内容，允许更新，不进行严格去重
            line_key = (y, hash(line_content))
            if line_key in seen_lines and y != cursorPos.Y:  # 只有非当前行才去重
                logger.debug(f"Skipping duplicate line {y}: {repr(line_content[:20])}...")
                continue
            seen_lines.add(line_key)
            
            # 记录调试信息
            display_width = self._display_width(line_content)
            logger.debug(f"Line {y}: content='{line_content[:20]}...', width={display_width}, buffer_width={buffer_width}")
            
            current_line += line_content
            is_wrapped_line = False
            
            # 仅当不是最后一行，并且显示宽度接近或等于buffer宽度时，认为是包装行
            if i < len(lines) - 1 and display_width >= buffer_width - 5:
                is_wrapped_line = True
                logger.debug(f"Line {y} is wrapped (width)")
            
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
            # 修改：对每个逻辑行内部的连续重复内容进行去重
            unique_logical_lines = []
            for index,line in enumerate(logical_lines):
                if not line:
                    # unique_logical_lines.append(line)
                    continue
                
                deduped_line = self.remove_duplicate_in_logical_line(line)
                if index == 0:
                    # 处理第一行，直接添加
                    unique_logical_lines.append(deduped_line)
                elif unique_logical_lines[-1] != deduped_line:
                    unique_logical_lines.append(deduped_line)
                logger.debug(f"Added deduped logical line to result: {repr(deduped_line[:20])}...")
            
            unique_logical_lines_without_use_less_reset_color = []
            for line in unique_logical_lines:
                # 参见 line 600, 我需要在这一行处理所有的颜色字符
                # 当然，这个的前提是，每个颜色最多都只影响一行，不超过一行。更多的需要在下一行重新写一个颜色开头加颜色重置
                line = self.process_color_reset(line)
                unique_logical_lines_without_use_less_reset_color.append(line)

            result = '\r\n'.join(unique_logical_lines_without_use_less_reset_color)
            # 清理所有 \x00 字符
            result = result.replace('\x00', '')
            
            # 更新读取位置，确保不重复读取
            self.__currentReadCo.X = cursorPos.X
            self.__currentReadCo.Y = cursorPos.Y
            self.__bufferY = cursorPos.Y
            
            self.lastReadData = result
            return result
        
        logger.debug("No logical lines formed")
        return ""

    def remove_duplicate_in_logical_line(self,line):
        """
        使用二分法或全查找检测并去除一行中的重复内容。
        假设重复内容是两段完全相同的字符串，中间或两侧有少量不同字符（不超过5个）。
        对于长度小于10的字符串，直接全查找；对于长度大于或等于10的字符串，使用二分法，最多进行25次检查。
        
        参数:
            line (str): 输入的一行字符串，可能包含重复内容
            
        返回:
            str: 去重后的字符串
        """
        if not line:
            return line
        
        logger.debug(f"Processing line for duplicate removal: {repr(line[:20])}...")
        
        if len(line) < 10:
            # 对于短字符串（长度小于10），直接全查找
            return self.full_search_duplicates(line)
        else:
            # 对于较长字符串，使用二分法
            return self.binary_search_duplicates(line)

    def full_search_duplicates(self,line):
        """
        对短字符串（长度小于10）进行全查找，检测并去除重复内容。
        尝试所有可能的分割点，检查是否存在重复内容。
        """
        if not line:
            return line
        
        logger.debug(f"Full search for short line: {repr(line)}")
        result = line
        
        # 尝试所有可能的分割点，检查前半部分和后半部分是否相同
        for i in range(1, len(line) // 2 + 1):
            for offset in range(-5, 6):  # 容错偏移量，允许两侧或中间有不超过5个不同字符
                if i + offset < 0 or i + offset >= len(line):
                    continue
                    
                first_half = line[:i + offset]
                second_half_start = max(i + offset, i - 5)
                second_half = line[second_half_start:second_half_start + len(first_half)]
                
                if len(second_half) == len(first_half) and first_half == second_half:
                    # 找到重复内容，去除后半部分
                    result = line[:second_half_start]
                    logger.debug(f"Found duplicate in full search at length {i + offset}, removed: {repr(second_half)}")
                    return result
        
        logger.debug(f"No duplicate found in full search for line: {repr(line)}")
        return result

    def binary_search_duplicates(self,line):
        """
        对长字符串（长度大于或等于10）使用二分法检测并去除重复内容。
        最多进行25次检查。
        """
        max_checks = 25  # 最多检查次数
        check_count = 0
        result = line
        
        # 二分查找重复内容，初始检查长度为行长度的一半
        low = len(line) // 4  # 最小检查长度，假设重复内容至少占行长度的1/4
        high = len(line) // 2 + 1  # 最大检查长度，假设重复内容接近行长度的一半
        found_duplicate = False
        
        while low <= high and check_count < max_checks and not found_duplicate:
            check_count += 1
            mid = (low + high) // 2  # 当前检查的片段长度
            
            # 尝试找到重复片段，考虑中间或两侧可能有少量不同字符
            for offset in range(-5, 6):  # 容错偏移量，允许两侧或中间有不超过5个不同字符
                if mid + offset < 0 or mid + offset >= len(line):
                    continue
                    
                # 取前半部分和后半部分进行比较，允许偏移
                first_half = line[:mid + offset]
                second_half_start = max(mid + offset, mid - 5)  # 后半部分起始位置，考虑偏移
                second_half = line[second_half_start:second_half_start + len(first_half)]
                
                if len(second_half) == len(first_half) and first_half == second_half:
                    # 找到重复内容，去除后半部分
                    result = line[:second_half_start]
                    logger.debug(f"Found duplicate chunk at length {mid + offset}, removed: {repr(second_half[:20])}...")
                    found_duplicate = True
                    break
            
            if found_duplicate:
                break
                
            # 如果没找到，调整二分范围
            if mid * 2 > len(line):
                high = mid - 1  # 缩短检查长度
            else:
                low = mid + 1  # 增加检查长度
        
        if not found_duplicate:
            logger.debug(f"No duplicate found after {check_count} checks for line: {repr(line[:20])}...")
            pass
        
        return result


    def process_color_reset(self,line):
        """
        处理一行中的颜色代码，确保每个颜色设置组（连续的 \x1b[{ansi_fg}m）只对应一个最近的 \x1b[0m，
        从后往前移除多余的 \x1b[0m。
        
        参数:
            line (str): 输入的一行字符串，包含 ANSI 颜色代码
            
        返回:
            str: 处理后的字符串
        """
        result = []
        open_color_count = 0
        i = 0
        color_reset = '\x1b[0m'
        
        while i < len(line):
            if i + 1 < len(line) and line[i:i+2] == '\x1b[':
                # 找到一个可能的 ANSI 代码起始
                j = i
                while j < len(line) and line[j] != 'm':
                    j += 1
                if j < len(line) and line[j] == 'm':
                    # 完整匹配到一个 ANSI 代码，例如 \x1b[31m 或 \x1b[0m
                    code = line[i:j+1]
                    if code == color_reset:
                        # 如果是重置代码 \x1b[0m
                        if open_color_count > 0:
                            result.append(color_reset)
                            open_color_count -= 1
                        i = j + 1
                    else:
                        # 如果是颜色起始代码（不是 \x1b[0m）
                        # 检查是否是颜色组的第一个代码
                        if i == 0 or result and result[-1] != code and not result[-1].startswith('\x1b['):
                            open_color_count += 1
                        result.append(code)
                        i = j + 1
                        # 继续检查下一个是否也是颜色代码，如果是则不增加计数
                        while i < len(line) and line[i:i+2] == '\x1b[':
                            j = i
                            while j < len(line) and line[j] != 'm':
                                j += 1
                            if j < len(line) and line[j] == 'm':
                                next_code = line[i:j+1]
                                if next_code != color_reset:
                                    result.append(next_code)
                                    i = j + 1
                                else:
                                    break
                            else:
                                break
                else:
                    # 不是完整的 ANSI 代码，按普通字符处理
                    result.append(line[i])
                    i += 1
            else:
                # 普通字符，直接添加到结果中
                result.append(line[i])
                i += 1
        
        return ''.join(result)

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
        # TODO 看起来这个似乎无法直接移除，这个直接移除后，我的颜色添加就达到了一个理想状态，但是我的换行却开始犯病了
        # 似乎换行和颜色逻辑在某处有耦合但是我没发现，目前最佳做法（保证换行），保证不添加过多字符，那就是得在添加逻辑行时保证每个 `\x1b[0m` 都与对应一个颜色前置否则就移除。
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
