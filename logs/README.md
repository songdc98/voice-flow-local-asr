# Local runtime logs

Voice Flow keeps local transcript logs here when `retention.keep_logs` is enabled.

Raw logs are intentionally ignored by Git because they can contain private dictated
text, active application names, and local filesystem paths. A public-safe summary of
the development and exploration process is kept in `docs/EXPLORATION_LOG.md`.
