"""``python -m tow.cli``: the same as ``python -m tow``, whose ``tow.__main__`` is the one entry to the commands."""

import runpy

runpy.run_module("tow", run_name="__main__")
