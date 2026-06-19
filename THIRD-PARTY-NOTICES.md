# Third-Party Notices

This project is **proprietary** (see [LICENSE](LICENSE)) but incorporates third-party components
that remain under **their own licenses**. Those components are **NOT** covered by the proprietary
LICENSE and retain the rights granted by their original licenses. Their copyright and permission
notices must be preserved.

## Kronos model — vendored under `kronos/`
- **Component:** Kronos, an OHLCV time-series foundation model.
- **Upstream:** NeoQuasar / Kronos (Hugging Face: `NeoQuasar/Kronos-base`, `NeoQuasar/Kronos-Tokenizer-base`).
- **License:** MIT.
- The files under `kronos/` are the property of their upstream authors and are used under the MIT
  License. The proprietary LICENSE of this repository does **not** apply to them; do not relicense
  them. Retain the upstream MIT license text and copyright attribution. If the upstream MIT
  `LICENSE` file is not vendored alongside the code, it should be added to remain in compliance.

## Python dependencies
Installed via `pip` (see the project environment metadata / `requirements`). These retain their own
licenses (predominantly MIT, BSD-3-Clause, and Apache-2.0). They are not redistributed in this
repository's source tree.

## JavaScript dependencies (dashboard)
Installed via `npm` (see `dashboard/package-lock.json`, where each package's `license` field is
recorded — predominantly MIT). They are not redistributed in this repository's source tree.

---
*Note: this notice lists known third-party components; it is informational, not legal advice. For a
distribution-grade attribution audit, generate an SBOM (e.g. `pip-licenses`, `license-checker`).*
