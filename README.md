# Content Ethics Advisor

Streamlit app that answers developers' content-ethics questions from the Expression Ethics
Unit's case history, with a panel of cultural-perspective reviewers critiquing each answer.

## Files
- `app.py` — the app (login, case search, lead reviewer, panel, final answer)
- `prompts.py` — every prompt and persona; edit this to tune behavior
- `prepare_cases.py` — run locally to build the anonymized case file
- `.streamlit/secrets.toml.example` — secrets template

## Setup
1. **Build the case file (on your machine, not the server):**
   `python prepare_cases.py <original Japanese case log>.xlsx --region us-west-2`
   Optionally add `--extra-terms terms.txt` with in-game place/character names to block.
   Spot-check `cases_review.csv`.
2. **Commit `cases_sanitized.enc`** to the repo alongside `app.py`. It's encrypted, so it's
   unreadable without the key. `.gitignore` keeps the key and the plain files out.
3. **Secrets:** fill in `secrets.toml.example`, putting the contents of
   `cases_encryption_key.txt` in `CASES_ENCRYPTION_KEY`, and add it to the app's secrets.
4. **Updating cases later:** re-run `prepare_cases.py` in the same folder. It reuses the key,
   so you only commit the new `.enc` file and reboot the app.

## How a question is answered
1. Lead reviewer (Opus) searches the anonymized cases and drafts an answer.
2. A quick check (Haiku) picks which enabled reviewers are relevant.
3. Those reviewers (Sonnet) critique the draft in parallel, plus the creative-director
   counterweight.
4. If anyone raised a concern, the lead reviewer writes the final answer with their input.
   If not, the draft goes out as is.

## Moving to the internal server
Either keep the encrypted file and key as they are, or remove `CASES_ENCRYPTION_KEY` from
secrets and set `CASES_LOCAL_PATH` to the plain `cases_sanitized.json`. Nothing else changes.
