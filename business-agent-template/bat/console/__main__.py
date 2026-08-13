"""`python3 -m bat.console`"""

from __future__ import annotations

import argparse

from bat.console.server import HOST, PORT, serve

parser = argparse.ArgumentParser(description="The build console")
parser.add_argument("--host", default=HOST)
parser.add_argument("--port", type=int, default=PORT)
args = parser.parse_args()

serve(args.host, args.port)
