# freedns-domain-scraper

Scrapes every domain in the [FreeDNS shared domain registry](https://freedns.afraid.org/domain/registry/) once a day with GitHub Actions.

## Files

| File | Contents |
|------|----------|
| [`domains.txt`](domains.txt) | Every domain, one per line, sorted. Overwritten each run. |
| [`domains.csv`](domains.csv) | Same domains with status (public/private), hosts in use, FreeDNS domain ID, owner, owner ID and created date. Overwritten each run. |
| [`changes.md`](changes.md) | Domains added and removed on each run, newest first. |

These appear after the first workflow run.

## Schedule

[`.github/workflows/scrape.yml`](.github/workflows/scrape.yml) runs once a day at 07:17 UTC and commits the files when something changed. To run it now: **Actions > Scrape FreeDNS registry > Run workflow**.

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
