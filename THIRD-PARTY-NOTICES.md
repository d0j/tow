# Third-party notices

TOW is licensed under the [MIT License](LICENSE). It includes the following third-party material.

## Icons in `src/tow/templates/base.html`

| Icon | Source | License |
|---|---|---|
| bell (notifications) | [Feather Icons](https://github.com/feathericons/feather) | MIT |
| undo-2 (the Undo button) | [Lucide](https://github.com/lucide-icons/lucide) | ISC |

### Feather Icons — MIT License

```text
The MIT License (MIT)

Copyright (c) 2013-2017 Cole Bemis

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

### Lucide — ISC License

```text
ISC License

Copyright (c) for portions of Lucide are held by Cole Bemis 2013-2022 as part of Feather (MIT).
All other copyright (c) for Lucide are held by Lucide Contributors 2022.

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR
ANY SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF
OR IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

## Python dependencies

No Python package is vendored into this repository. TOW's dependencies (FastAPI, Starlette, Uvicorn, httpx,
httpcore, Jinja2, cryptography, Beautiful Soup, PyYAML, regex, python-multipart, websockets, qbittorrent-api,
tzdata and their own dependencies) are installed from PyPI at the exact versions pinned in [`uv.lock`](uv.lock),
each under its own license, which ships with the installed package.

## Windows zip

`TOW-windows-x64.zip` (built by `scripts/build-bundle.py`) carries, next to TOW itself, programs and packages of
others in `TOW\runtime`, each under its own license:

| Component | In the zip | License |
|---|---|---|
| [uv](https://github.com/astral-sh/uv), the version `build-bundle.py` pins | `runtime\bin\uv.exe` | MIT or Apache-2.0, at your option |
| [CPython](https://www.python.org) of `.python-version` (the standalone build uv installs) | `runtime\python\` | Python Software Foundation License Version 2; the licenses of the libraries built into it are in its `LICENSE.txt` |
| The packages below, as wheels (the runtime packages of `uv.lock` for Windows) | `runtime\cache\` | as listed; each wheel holds its license |

| Package | License (SPDX) |
|---|---|
| annotated-doc | MIT |
| annotated-types | MIT |
| anyio | MIT |
| beautifulsoup4 | MIT |
| certifi | MPL-2.0 |
| cffi | MIT-0 |
| charset-normalizer | MIT |
| click | BSD-3-Clause |
| cryptography | Apache-2.0 OR BSD-3-Clause |
| fastapi | MIT |
| h11 | MIT |
| httpcore | BSD-3-Clause |
| httptools | MIT |
| httpx | BSD-3-Clause |
| idna | BSD-3-Clause |
| jinja2 | BSD-3-Clause |
| markupsafe | BSD-3-Clause |
| opentelemetry-api | Apache-2.0 |
| packaging | Apache-2.0 OR BSD-2-Clause |
| pycparser | BSD-3-Clause |
| pydantic | MIT |
| pydantic-core | MIT |
| python-dotenv | BSD-3-Clause |
| python-multipart | Apache-2.0 |
| pyyaml | MIT |
| qbittorrent-api | MIT |
| regex | Apache-2.0 AND CNRI-Python |
| requests | Apache-2.0 |
| soupsieve | MIT |
| starlette | BSD-3-Clause |
| typing-extensions | PSF-2.0 |
| typing-inspection | MIT |
| tzdata | Apache-2.0 |
| urllib3 | MIT |
| uvicorn | BSD-3-Clause |
| watchfiles | MIT |
| websockets | BSD-3-Clause |
