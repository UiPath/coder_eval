Create a UiPath coded agent that validates e-commerce orders before processing.

- Input: order (string — JSON object with "customer_email", "items" array of {"sku": string, "quantity": int, "price": float}, "shipping_country" string, "discount_code" string optional)
- Output: is_valid (bool), total_before_discount (float), total_after_discount (float), errors (list of strings), item_count (int)
- Entry point function: validate_order
- Validation rules:
  - customer_email must contain "@" and "."
  - Each item quantity must be >= 1 and <= 100
  - Each item price must be > 0
  - shipping_country must be exactly 2 uppercase letters (ISO country code)
  - If discount_code is "SAVE10" → 10% discount, "SAVE20" → 20% discount, anything else → invalid discount code error
  - Order is valid only if there are zero errors
- Must use @traced() decorator
- Register in uipath.json under "functions" and run `uv run uipath init`
