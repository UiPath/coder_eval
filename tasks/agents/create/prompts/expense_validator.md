Create a UiPath coded agent that validates expense reports.

- Input: employee_name (string), expenses (string — JSON array of objects with "description", "amount", "category" fields), daily_limit (float, default 500.0)
- Output: total_amount (float), is_valid (bool), violations (list of strings), category_totals (string — JSON object mapping category to sum)
- Entry point function: validate_expenses
- Validation rules:
  - Each individual expense amount must be > 0
  - No single expense can exceed daily_limit
  - Valid categories: "travel", "meals", "supplies", "equipment", "other"
  - Any invalid category is a violation
  - Total across all expenses cannot exceed 3x daily_limit
- Must use @traced() decorator
- Register in uipath.json under "functions" and run `uv run uipath init`
