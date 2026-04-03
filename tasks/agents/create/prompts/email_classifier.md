Create a UiPath coded agent that classifies customer emails.

- Input: email_text (string), sender (string)
- Output: category (string — one of "billing", "technical", "sales", "complaint", "general"), priority (string — "low", "medium", "high"), summary (string — one-sentence summary of the request)
- Entry point function: classify_email
- Use simple keyword-based classification (no LLM needed):
  - "billing"/"invoice"/"payment"/"charge" → billing
  - "error"/"bug"/"crash"/"broken"/"not working" → technical
  - "pricing"/"demo"/"trial"/"enterprise" → sales
  - "unhappy"/"terrible"/"worst"/"disappointed"/"unacceptable" → complaint
  - Everything else → general
- Priority: complaint = high, technical = medium, everything else = low
- Summary: first 100 characters of the email text, trimmed at word boundary
- Must use @traced() decorator
- Register in uipath.json under "functions" and run `uv run uipath init`
