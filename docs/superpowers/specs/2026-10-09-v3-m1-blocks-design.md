# V3 milestone 1: more blocks

Part of V3 (smarter flows, running it for real, more blocks). Status: built. Everything is
standard-library only except the two database blocks, which import a driver if it is installed.

| Group | Block | Settings that matter | Notes |
|---|---|---|---|
| web | `webhook` | url, body, method, headers | any service that takes a webhook; fails on 4xx/5xx |
| | `slack_message` | webhook_url (secret), text, username | Slack incoming webhook; shows Slack's refusal text |
| | `teams_message` | webhook_url (secret), text, title | adaptive-card message for a Teams workflow webhook |
| | `llm_prompt` | prompt, api_key (secret), provider, model, system, max_tokens, base_url | Anthropic Messages API, or `openai` for any compatible server; returns `{text, model, usage}`; the key is masked even if the provider echoes it |
| | `s3_get`, `s3_put` | bucket, key, access_key and secret_key (secret), region, endpoint, path/content | AWS Signature V4 written with `hmac`/`hashlib`; a custom `endpoint` uses path-style addressing (MinIO etc.) |
| office | `read_xlsx` | path, sheet, header | zip + XML; shared strings, inline strings, booleans, numbers, gaps; refuses files with a DTD |
| | `write_xlsx` | path, rows, sheet, columns | one sheet with a header row; strings escaped; opens in Excel |
| text | `date_format`, `date_add` | value or rows+field+into, format / weeks, days, hours... | ISO text, text in `input_format`, or epoch seconds; a date stays a date |
| | `date_diff` | start, end, unit | negative if end is earlier |
| | `text_template` | rows, row (`{field}`), header, footer, separator | `{count}` in header and footer; explains a missing field |
| databases | `postgres`, `mysql` | query, database, user, password (secret), host, port, parameters, many | `%s` placeholders; clear `pip install` message without the driver |

## Decisions

- **Secrets.** Webhook URLs, API keys and cloud credentials are `secret=True` settings, so a flow
  that writes one in plain text is refused at load time and errors show `••••`.
- **S3 signing** is checked against the worked GET Object example in AWS's documentation; the
  signature was reproduced independently with AWS's own `botocore` (used once, not a dependency).
- **XML safety.** Python's XML parser would otherwise expand entity bombs, and a third-party
  fix (`defusedxml`) would break the standard-library rule; a spreadsheet never needs a DTD, so
  `read_xlsx` refuses any part that has `<!DOCTYPE` or `<!ENTITY`.
- **Databases** are plugins in spirit: the driver is imported only when the block runs, and
  `pyproject.toml` is unchanged (no new dependency, no lock-file change).
- **Not included:** Excel styles, formulas as formulas, charts; S3 multipart uploads and listing.
