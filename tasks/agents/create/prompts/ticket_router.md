Create a UiPath coded agent that routes support tickets to the right team.

- Input: ticket_title (string), ticket_body (string), customer_tier (string — "free", "pro", "enterprise")
- Output: team (string — "billing", "engineering", "sales", "support"), priority (int — 1 to 4, where 1 is highest), sla_hours (int — response time SLA in hours), escalate (bool — whether this needs immediate human attention)
- Entry point function: route_ticket
- Routing rules:
  - Keywords "payment"/"charge"/"refund"/"invoice" → billing
  - Keywords "bug"/"error"/"crash"/"api"/"integration" → engineering
  - Keywords "upgrade"/"pricing"/"demo"/"contract" → sales
  - Everything else → support
- Priority: enterprise customers = 1, pro = 2, free = 3. Bump up by 1 if "urgent"/"emergency"/"critical" in title.
- SLA: priority 1 = 2 hours, priority 2 = 8 hours, priority 3 = 24 hours, priority 4 = 48 hours
- escalate = true if priority is 1 or if "outage"/"down"/"security" in body
- Must use @traced() decorator
- Register in uipath.json under "functions" and run `uv run uipath init`
