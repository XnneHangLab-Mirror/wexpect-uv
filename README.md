# wexpect

I fork it from : [**raczben/wexpect**](https://github.com/raczben/wexpect)

And it is rebuild by uv, for branch [dev](https://github.com/raczben/wexpect/tree/dev).

## Description

Wexpect is a Windows variant of pexpect.

- [x] v0.0.1: fix the [Always Timeout](https://github.com/raczben/wexpect/issues/42) && [failed in uv](https://github.com/raczben/wexpect/issues/26)
- [x] v0.0.2: fix `cannot read console if endwith '\r' print("",end="\r").` .
- [x] v0.0.3: change the catch logic(catch all even cursor not change) and catch ascii colors.
- [x] v0.0.4: fix bugs in v0.0.3: remove extra `\x1b[0m`, catch all process-bar protect between `\r`.

## Installation

```bash
pip install wexpect-uv
```

## Usage

```python
import wexpect as pexpect

# You can use it like pexpect
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

Then you can use `uv run wexpect` to use it in command line or `import wexpect` in python. You can modify the code and test then PR to me if you want.
