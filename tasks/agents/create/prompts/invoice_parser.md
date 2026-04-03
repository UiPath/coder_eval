Create a UiPath coded agent that parses structured invoice data from text.

- Input: invoice_text (string)
- Output: vendor_name (string), invoice_number (string), total_amount (float), line_items (list of objects with "description" and "amount" fields), currency (string, default "USD")
- Entry point function: parse_invoice
- Parse the invoice_text assuming this format:
  ```
  Vendor: <name>
  Invoice: <number>
  Currency: <currency>
  Items:
  - <description>: <amount>
  - <description>: <amount>
  Total: <total>
  ```
- If Currency line is missing, default to "USD"
- Must use @traced() decorator
- Register in uipath.json under "functions" and run `uv run uipath init`
