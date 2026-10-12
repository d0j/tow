"""What the stores are built on: ``tow.store`` (state, history, the parsed cache) re-exports it.

- ``errors``: the errors more than one storage module raises;
- ``jsonio``: the JSON codec of the stores: finite numbers, the nesting limit, the bytes written;
- ``atomic``: bounded reads and atomic writes, the count of files written.

The rest of TOW imports these names from ``tow.store``. A test that replaces a name does it in
the module that reads it when it runs.
"""
