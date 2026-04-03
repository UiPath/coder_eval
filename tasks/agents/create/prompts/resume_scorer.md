Create a UiPath coded agent that scores resumes against job requirements.

- Input: resume_skills (list of strings — skills listed on the resume), required_skills (list of strings — skills the job requires), preferred_skills (list of strings — nice-to-have skills)
- Output: match_score (int — 0 to 100), matched_required (list of strings), missing_required (list of strings), matched_preferred (list of strings), recommendation (string — "strong_match", "partial_match", or "no_match")
- Entry point function: score_resume
- Scoring logic:
  - Each required skill match = 10 points (max 70)
  - Each preferred skill match = 5 points (max 30)
  - Cap at 100
  - Comparison should be case-insensitive
  - recommendation: score >= 70 → "strong_match", 40-69 → "partial_match", < 40 → "no_match"
- Must use @traced() decorator
- Register in uipath.json under "functions" and run `uv run uipath init`
