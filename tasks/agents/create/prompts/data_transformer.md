Create a UiPath coded agent that transforms JSON data records.

- Input: records (string — JSON array of objects), operation (string — "filter", "sort", "aggregate"), field (string — field name to operate on), value (string, optional — filter value or sort direction "asc"/"desc")
- Output: result (string — JSON string of transformed records), record_count (int), operation_applied (string)
- Entry point function: transform_data
- Operations:
  - "filter": Keep only records where the field equals the value
  - "sort": Sort records by the field (value="asc" or "desc", default "asc")
  - "aggregate": Return a single record with count and, if the field is numeric, sum and average
- Must use @traced() decorator
- Register in uipath.json under "functions" and run `uv run uipath init`
