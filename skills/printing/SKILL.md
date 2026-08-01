---
name: printing
description: Print a PDF or document to the user's Canon MG3600 printer. Covers the exact printer name, how to print double-sided (duplex), and the rule to only print once. Use whenever the user asks to print something.
---

# Printing to the Canon MG3600

The user has one printer: **Canon-MG3600** (exact name as registered on the system).

## Hard rules

1. **Only ever print ONE copy** — use `-n 1`. Never more.
2. **Print double-sided (duplex)** — always use `-o sides=two-sided-long-edge` unless the user explicitly asks for single-sided.
3. **Do not reprint** — if something goes wrong (wrong file, wrong settings, printer error), tell the user what happened. Do not resend the job without them asking.

## Command

```bash
lp -d Canon-MG3600 -n 1 -o sides=two-sided-long-edge /path/to/file.pdf
```

That's it. One command, one job, one copy, duplex on.

## Checking the printer

- `lpstat -p Canon-MG3600` — check if the printer is idle/ready
- `lpq -P Canon-MG3600` — check the queue for pending jobs
- `cancel Canon-MG3600-<job-id>` — cancel a job if needed
