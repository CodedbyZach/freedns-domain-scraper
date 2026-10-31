# freedns-domain-scraper

Scrapes every domain in the [FreeDNS shared domain registry](https://freedns.afraid.org/domain/registry/) every hour with GitHub Actions.

## Files

| File | Contents |
|------|----------|
| [`domains.txt`](domains.txt) | Every domain, one per line, sorted. Rewritten when domains are added or removed. |
| [`domains.csv`](domains.csv) | Same domains with status (public/private), hosts in use, FreeDNS domain ID, owner, owner ID and created date. Rewritten when domains are added or removed. |
| [`changes.md`](changes.md) | Domains added and removed on each run, newest first. Runs with nothing added or removed log "No changes". |

These appear after the first workflow run.

## Schedule

[`.github/workflows/scrape.yml`](.github/workflows/scrape.yml) runs every hour and commits the results. To run it now: **Actions > Scrape FreeDNS registry > Run workflow**.

## Safety checks

A run fails and writes nothing when:

- a page still fails after 7 tries (about 7 minutes of backoff)
- it collected less than 97% of the domains the registry says it has
- the domain count dropped more than 10% since the last run

If a big drop is real, run the workflow manually with **force** checked (or `python scraper.py --force`).

## Run locally

```
pip install -r requirements.txt
python scraper.py                 # scrape and update the files
python scraper.py --dry-run       # print every domain, write nothing
python scraper.py --max-pages 3   # quick test
```

Inspired by [4ngel2769/freedns.afraid.org-domain-scraper](https://github.com/4ngel2769/freedns.afraid.org-domain-scraper).
