# CLAUDE.md

## Notes

- **Running the full pytest suite**: The full pytest run takes more than 5 minutes. Run it in the background (run_in_background) and poll the result, so the foreground call doesn't hit the timeout.
- **Zsh and grep --include globs**: The shell is zsh, and `grep --include=*.py` fails on the unquoted glob. Use the Grep tool, or quote the pattern (`--include='*.py'`).
- **Stray tmp/ dir from local db upgrade**: Running `eifo-fetch db upgrade` with `EIFO_DB_URL=sqlite:///tmp/ci.db` creates a stray `tmp/` directory in the repo root. Delete it before committing. Alternatively, use an absolute path outside the repo for the temp DB.
