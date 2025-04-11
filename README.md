# wexpect

I fork it from : [**raczben/wexpect**](https://github.com/raczben/wexpect)

And it is rebuild by uv, for branch [dev](https://github.com/raczben/wexpect/tree/dev).

## Description
Wexpect is a Windows variant of pexpect.

- [x] fix the [Always Timeout](https://github.com/raczben/wexpect/issues/42) && [failed in uv](https://github.com/raczben/wexpect/issues/26)


## Installation

```bash
pip install wexpect-uv
```

## Usage

```python
import wexpect

prompt = '[A-Z]\:.+>'

child = wexpect.spawn('cmd.exe')
child.expect(prompt)    # Wait for startup prompt

child.sendline('dir')   # List the current directory
child.expect(prompt)

print(child.before)     # Print the list
child.sendline('exit')
```

For more information see [examples](https://github.com/XnneHangLab/wexpect-uv) folder.

## How to test and develop locally

You need uv, see [installation](https://docs.astral.sh/uv/getting-started/installation/).

Then,

```bash
git clone https://github.com/XnneHangLab/wexpect-uv.git
uv lock
uv sync
```

Then you can use `uv run wexpect` to use it in command line and `import wexpect` in python. You can modify the code and test then PR to me if you want.

