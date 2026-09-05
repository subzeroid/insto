"""One request per process; the caller closes stdin and owns the timeout."""

import asyncio
import sys

from insto.desktop.dispatch import handle
from insto.desktop.protocol import MAX_INPUT_BYTES


def main() -> None:
    raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
    response = asyncio.run(handle(raw))
    sys.stdout.buffer.write(response)
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
