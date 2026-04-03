Create a UiPath coded agent that finds available meeting slots.

- Input: busy_slots (string — JSON array of objects with "start" and "end" as "HH:MM" strings), meeting_duration_minutes (int), work_start (string, default "09:00"), work_end (string, default "17:00")
- Output: available_slots (list of objects with "start" and "end" as "HH:MM" strings), slot_count (int), longest_gap_minutes (int)
- Entry point function: find_slots
- Logic:
  - Find all free gaps between work_start and work_end that are not covered by any busy_slot
  - Only return gaps >= meeting_duration_minutes
  - Sort available_slots by start time
  - longest_gap_minutes: the duration of the longest free gap (whether or not it fits a meeting)
- Must use @traced() decorator
- Register in uipath.json under "functions" and run `uv run uipath init`
