---
name: verify
summary: Verify producer CLIs without invoking external publication.
---

For producer changes, run the public module CLI with its safe `--dry-run` mode
and capture the JSON build plan. Probe invalid flag combinations to confirm
publishing/catalogue prerequisites fail before any checkout or network action.
Never invoke `--publish` during local verification.
