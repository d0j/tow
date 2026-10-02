"""``python -m tow``: the autostart entry (``pythonw.exe -m tow run`` shows no console window)."""

from tow.cli import main

raise SystemExit(main())
