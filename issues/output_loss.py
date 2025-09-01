import os
from pathlib import Path

import pytest

import wexpect


@pytest.fixture()
def replaced_console_reader():
    from wexpect import console_reader

    console_reader_path = Path(console_reader.__file__)

    original_bytes = console_reader_path.read_bytes()

    lines = console_reader_path.read_text(encoding="utf-8").splitlines(keepends=True)

    target = '                deduped_line = self.remove_duplicate_in_logical_line(line)\n'
    replacement = '                deduped_line = line\n'

    for i, line in enumerate(lines):
        if line == target:
            index = i
            break

    else:
        raise ValueError("Target line was not found")

    lines[index] = replacement

    console_reader_path.write_text("".join(lines), encoding="utf-8")

    yield

    console_reader_path.write_bytes(original_bytes)


@pytest.fixture()
def progress_script_path(tmp_path):
    script_path = tmp_path / "progress_example.py"

    script_path.write_text(
        "import sys, time\n"
        "for i in range(0, 101, 10):\n"
        "    sys.stdout.write(f'\\rProgress {i}%')\n"
        "    sys.stdout.flush()\n"
        "    time.sleep(0.2)\n"
    )
    yield script_path

    script_path.unlink()

@pytest.mark.parametrize(
    "str_to_echo",
    [
        "Somewhat lengthy str",
        "Hello",
        "Python 3.12.0",
        "2.34.0"
    ],
    ids=[
        "longer str",
        "shorter str",
        "python version",
        "some version"
    ]
)

def test_echoed_str(str_to_echo):

    command = f'echo {str_to_echo}'
    cwd = os.getcwd()
    prompt_ending = os.path.basename(cwd) + ">"

    with wexpect.SpawnSocket("cmd.exe", interact=True) as child:
        # expecting first prompt appearance
        child.expect(prompt_ending)

        assert "Microsoft Corporation" in child.before
        assert prompt_ending == child.after

        # sending echo {str} command
        child.sendline(command)
        # expecting command input
        child.expect(f"{prompt_ending}{command}")
        # expecting prompt after command, target str should be in child.before
        child.expect(prompt_ending)

        assert str_to_echo in child.before, f"Expected string: {str_to_echo} never appeared in buffer"
        assert prompt_ending == child.after

@pytest.mark.parametrize(
    "str_to_echo",
    [
        "Somewhat lengthy str",
        "Hello",
        "Python 3.12.0",
        "2.34.0"
    ],
    ids=[
        "longer str",
        "shorter str",
        "python version",
        "some version"
    ]
)

def test_echoed_str_replaced_cons_reader(str_to_echo, replaced_console_reader):

    command = f'echo {str_to_echo}'
    cwd = os.getcwd()
    prompt_ending = os.path.basename(cwd) + ">"

    with wexpect.SpawnSocket("cmd.exe", interact=True) as child:
        # expecting first prompt appearance
        child.expect(prompt_ending)

        assert "Microsoft Corporation" in child.before
        assert prompt_ending == child.after

        # sending echo {str} command
        child.sendline(command)
        # expecting command input
        child.expect(f"{prompt_ending}{command}")
        # expecting prompt after command, target str should be in child.before
        child.expect(prompt_ending)

        assert str_to_echo in child.before, f"Expected string: {str_to_echo} never appeared in buffer"
        assert prompt_ending == child.after


def test_progress_output(progress_script_path):
    cwd = os.getcwd()
    prompt_ending = os.path.basename(cwd) + ">"

    with wexpect.SpawnSocket("cmd.exe", interact=True) as child:
        # expecting first prompt appearance
        child.expect(prompt_ending)

        assert "Microsoft Corporation" in child.before
        assert prompt_ending == child.after

        # sending command to run the script
        child.sendline(f"python {progress_script_path}")
        # expecting command input
        child.expect("progress_example.py")
        # expecting prompt after command, target output should be in child.before
        child.expect(prompt_ending)

        expected = [f"Progress {i}%" for i in range(0, 101, 10)]

        for s in expected:
            assert s in child.before, f"'{s}' never appeared in buffer"

def test_progress_output_replaced_cons_reader(progress_script_path, replaced_console_reader):
    cwd = os.getcwd()
    prompt_ending = os.path.basename(cwd) + ">"

    with wexpect.SpawnSocket("cmd.exe", interact=True) as child:
        # expecting first prompt appearance
        child.expect(prompt_ending)

        assert "Microsoft Corporation" in child.before
        assert prompt_ending == child.after

        # sending command to run the script
        child.sendline(f"python {progress_script_path}")
        # expecting command input
        child.expect("progress_example.py")
        # expecting prompt after command, target output should be in child.before
        child.expect(prompt_ending)

        expected = [f"Progress {i}%" for i in range(0, 101, 10)]

        for s in expected:
            assert s in child.before, f"'{s}' never appeared in buffer"


