Create a UiPath coded agent that analyzes sentiment in customer feedback text.

- Input: text (string), language (string, default "en")
- Output: sentiment (string — "positive", "negative", "neutral", "mixed"), confidence (float 0.0-1.0), key_phrases (list of strings — up to 5 phrases that influenced the sentiment)
- Entry point function: analyze_sentiment
- Use keyword-based sentiment analysis (no LLM needed):
  - Positive words: "great", "excellent", "love", "amazing", "wonderful", "fantastic", "best", "happy", "perfect", "awesome"
  - Negative words: "terrible", "awful", "hate", "worst", "horrible", "bad", "disappointed", "poor", "broken", "useless"
  - Count positive and negative word occurrences
  - More positive → "positive"; more negative → "negative"; equal → "mixed"; none → "neutral"
  - confidence = |positive - negative| / (positive + negative + 1), capped at 1.0
  - key_phrases: the actual matching words found, up to 5
- Must use @traced() decorator
- Register in uipath.json under "functions" and run `uv run uipath init`
