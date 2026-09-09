# ads-audit-tools

Small, deterministic scripts for auditing ad-platform data exports — no LLM required, no data leaves your machine.

Each tool is self-contained: the problem it solves, the code, and how to run it. All example identifiers are placeholders.

## Tools

| Tool | Problem it solves |
|---|---|
| [keyword-audit](./keyword-audit) | Cleans and ranks a Google Ads keyword export, flags bid inconsistencies and capped bids, and checks a negative-keyword list for volume imbalance and self-blocking conflicts. |

## Conventions

- Scripts take raw platform CSV exports as input — no manual pre-cleaning required.
- Every numeric-parsing quirk specific to a platform's export format (encoding, currency symbols, rollup rows) is handled in code, not documented as a manual step.
- Placeholder identifiers: `ORG -` prefixes, `example.com`.

## License

MIT — see [LICENSE](./LICENSE).
