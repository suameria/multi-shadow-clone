# Private repository checkpoint

The initial private repository checkpoint is based on the previously verified local source preview `5daeccbb0dc5e727` (archive SHA-256 `cb862effc1916b585a57158add2ca8221dbee375c878feef2c12847bebdb4132`). That package passed 291 offline tests in a fresh extraction. The owner requested complete terminology alignment: the source package is `src/multi_shadow_clone`, CLI scripts are in `tools/multi-shadow-clone`, and display/operational names are `multi-shadow-clone`. The old checkpoint is renamed as a separate save-only change; its offline checks are rerun. Existing runtime records and historical evidence are not rewritten.

The newer local development snapshot had 342 passing offline tests, but includes unfinished maintenance/coordination integration. Its differences are deliberately not included in these commits. No claim is made that this preview completes the full project or certifies specialist quality.

Planned distribution: GitHub `multi-shadow-clone`, Homebrew `multi-shadow-clone`, and a free macOS application. Homebrew packaging, a signed native application, and iPhone remote operation remain future work. No live Codex inference is needed to verify this saved package.
