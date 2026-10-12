"""What the stores are built on: ``tow.store`` (state, history, the parsed cache) re-exports it.

- ``errors``: the errors more than one storage module raises;
- ``jsonio``: the JSON codec of the stores: finite numbers, the nesting limit, the bytes written;
- ``atomic``: bounded reads and atomic writes, the count of files written;
- ``locks``: the data lock (``persistence_lock``) and the recovery steps it runs first, the check
  lock, the stale temporaries of killed writes;
- ``keys``: the master key: where it comes from, its Fernet, the secrets derived from it, a new
  key file;
- ``secrets``: the encrypted secrets and the settings undo snapshot, and the key operations that
  must look at them first (``generate_master_key``, ``adopt_master_key``);
- ``folders``: the install root, keys/ and data/ private to this account.

The rest of TOW imports these names from ``tow.store``. A test that replaces a name does it in
the module that reads it when it runs.
"""
