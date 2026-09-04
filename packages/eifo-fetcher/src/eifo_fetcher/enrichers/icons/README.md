# Provider marks

The logo of each ratings provider, shipped beside the enricher that reads it.
An enricher points at its own file (`ProviderInfo.icon`), the fetcher copies it
into the images root under a content-addressed name, and the API serves it like
any other artwork. A third-party enricher installed as a pip package brings its
own mark the same way; nothing here is a list the client has to be taught.

These are other people's trademarks, used to credit the score beside them and
for nothing else — the same nominative use as the provider's name, which is
what they replace. A score without its source is a rumour, and TMDB's terms ask
for the logo specifically.

| File | Provider | Where it came from |
|---|---|---|
| `tmdb.svg` | TMDB | [themoviedb.org/about/logos-attribution](https://www.themoviedb.org/about/logos-attribution) — the square mark, from the page whose subject is exactly this use |
| `rt.svg` | Rotten Tomatoes | `rt-tomato-logo` from rottentomatoes.com's own asset bundle |
| `imdb.svg` | IMDb | The 2016 wordmark, as [Wikimedia Commons](https://commons.wikimedia.org/wiki/File:IMDB_Logo_2016.svg) holds it. IMDb's own brand-guidelines file is a raster; this is the same mark as vector |
| `seret.png` | סרט | `logo.png` from seret.co.il. A PNG because it is the only form the site publishes, and the white variant it uses in its own header is invisible on a light page |

Square-ish by preference: these are read at the height of a line of text beside
a number, and TMDB's long wordmark is eight times wider than it is tall, which
at that height is a stripe rather than a logo.

If a provider's mark cannot be shipped — the terms disallow it, or there is no
usable file — leave `icon` unset. The chip says the provider's name instead,
which is what it did before any of this existed.
