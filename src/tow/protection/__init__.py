"""Optional site-response diagnostics and protection providers.

Classification works without any provider. Providers are opt-in and never replace
the site's authentication, its quota or a locally verified HTTP response.
"""

from tow.protection.detect import Verdict, classify

__all__ = ["Verdict", "classify"]
