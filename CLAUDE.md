# CLAUDE.md

## Notes

- **Running the full pytest suite**: The full pytest run takes more than 5 minutes. Run it in the background (run_in_background) and poll the result, so the foreground call doesn't hit the timeout.
- **Zsh and grep --include globs**: The shell is zsh, and `grep --include=*.py` fails on the unquoted glob. Use the Grep tool, or quote the pattern (`--include='*.py'`).
- **Stray tmp/ dir from local db upgrade**: Running `eifo-fetch db upgrade` with `EIFO_DB_URL=sqlite:///tmp/ci.db` creates a stray `tmp/` directory in the repo root. Delete it before committing. Alternatively, use an absolute path outside the repo for the temp DB.

## Catalog safety (read before changing a source plugin or the sync)

The catalog is protected by three rules that live outside any one plugin, so a change that looks local can break them:

- **Offers are retired only by successful syncs.** The sweep runs only when a sync ends `OK` (`eifo_api/routers/syncing.py`), and an offer is retired only after `MISS_LIMIT = 2` such sweeps in a row missed it (`eifo_core/catalog.py`). A sync that raises ends `FAILED`: what it already sent is kept and nothing is retired.
- **The volume guard.** A sync that returns less than `VOLUME_GUARD_RATIO = 0.5` of the previous successful run (when that run had at least 50 items) ends `ABORTED_SUSPICIOUS`, and nothing is swept.
- **So a plugin must not swallow a failed page.** If one section or page of a catalog fails after the shared HTTP client's retries, let the error end the sync. Catching it and continuing makes a partial catalog look complete: two such nights retire everything on that page, and losing one section of several rarely trips the volume guard. Fail the source, or (if a page is truly optional) say why in a comment and test it.
