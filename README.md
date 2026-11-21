# deal-digest

Daily digest of deals for your wishlist from Slickdeals + Reddit (+ optional Google Shopping),
judged against a 6-month benchmark. Output: `digest.html` (and an optional email).

## Run locally
    pip install -r requirements.txt
    python digest.py --open

## Run on GitHub (daily, free)
1. Put this folder in a **private** repo and push.
2. Repo > Settings > Actions > General > Workflow permissions: "Read and write".
3. Optional secrets (Settings > Secrets > Actions) for an emailed digest (Gmail app password):
   `SMTP_USER`, `SMTP_PASS`, `MAIL_TO`. Optional `SERPAPI_KEY` for Google Shopping.
4. Actions tab > daily-deal-digest > "Run workflow" to test. Without email, grab `digest.html`
   from the run's Artifacts, or `git pull` and open it.

## The 6-month benchmark
Free sources can't backfill history, so for each item copy `usual` and `low_6m` once from a
Keepa / CamelCamelCamel / Google price chart into `items.yaml`. The digest also builds its own
history in `prices.db` from today on. Stops itself after `until:` in items.yaml.

## Known caveats
Collectors are untested against the live sites. Reddit often blocks GitHub's IPs (a source error
shows at the bottom of the digest); Slickdeals' RSS search URL may change. Price is the first
`$` amount in a deal title, so "was $399, now $248" style titles can misread.
