# Vendored third-party assets

Files here are copied verbatim from an upstream release and are **excluded
from the formatting hooks** so they stay byte-identical and can be verified.

Do not edit them. To upgrade, replace the file deliberately, record the new
version and hash below, and re-run the test suite.

| File | Version | Source | SHA-256 |
|---|---|---|---|
| `htmx.min.js` | 2.0.4 | `https://unpkg.com/htmx.org@2.0.4/dist/htmx.min.js` | `e209dda5c8235479f3166defc7750e1dbcd5a5c1808b7792fc2e6733768fb447` |

Verify with:

```
(Get-FileHash static\vendor\htmx.min.js -Algorithm SHA256).Hash
```

htmx is distributed under the BSD Zero Clause License.
