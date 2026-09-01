# Fish completions for eifo-fetch.
#
# Install:  ln -s (pwd)/completions/eifo-fetch.fish ~/.config/fish/completions/
#
# Source keys and enricher keys are completed from the configuration file, not
# from the database: a completion runs on every keystroke and must never be the
# reason a shell pauses.

function __eifo_config --description 'The configuration file eifo-fetch would read'
    if set -q EIFO_CONFIG_FILE
        echo $EIFO_CONFIG_FILE
    else if test -f config/eifo.toml
        echo config/eifo.toml
    else if test -f config/eifo.example.toml
        echo config/eifo.example.toml
    end
end

function __eifo_sources --description 'Source keys declared in the configuration'
    set -l file (__eifo_config)
    test -n "$file"; or return
    string replace --regex --filter '^\[sources\.([a-z0-9_]+)\].*' '$1' <$file
end

# The chain of positional words typed so far, so "review list" can be told
# apart from "sources list" - fish's own helpers only look for a word anywhere.
function __eifo_enrichers --description 'Enrichers that --skip understands'
    printf '%s\t%s\n' \
        rt 'Rotten Tomatoes - scraped, and by far the slowest' \
        tmdb 'TMDB ratings and metadata' \
        seret 'Seret - Israeli ratings' \
        imdb 'the IMDb dataset download'
end

function __eifo_chain
    set -l words (commandline -opc)
    set -e words[1]
    set -l chain
    for word in $words
        string match -q -- '-*' $word; and continue
        set -a chain $word
    end
    string join ' ' $chain
end

function __eifo_at --description 'True when the command chain is exactly the argument'
    # Held in a variable first: with nothing typed yet the chain is empty, and
    # an unquoted substitution would leave `test` with one operand and an
    # error message where a completion should be.
    set -l chain (__eifo_chain)
    test "$chain" = "$argv[1]"
end

complete -c eifo-fetch -f

# -- global ------------------------------------------------------------------

complete -c eifo-fetch -s h -l help -d 'show help and exit'
complete -c eifo-fetch -l version -d 'show the version and exit'
complete -c eifo-fetch -s v -l verbose -d 'log at DEBUG level'

# -- commands ----------------------------------------------------------------

complete -c eifo-fetch -n '__eifo_at ""' -a sync -d 'pull catalogs and update availability'
complete -c eifo-fetch -n '__eifo_at ""' -a enrich -d 'refresh ratings and metadata'
complete -c eifo-fetch -n '__eifo_at ""' -a images -d 'download missing artwork'
complete -c eifo-fetch -n '__eifo_at ""' -a all -d 'sync, enrich, then fetch artwork - the nightly run'
complete -c eifo-fetch -n '__eifo_at ""' -a repair-names -d 're-ask TMDB for names stored in the wrong script'
complete -c eifo-fetch -n '__eifo_at ""' -a rematch -d 'give titles that never matched TMDB another, smarter try'
complete -c eifo-fetch -n '__eifo_at ""' -a dedupe -d 'merge titles the catalog holds twice'
complete -c eifo-fetch -n '__eifo_at ""' -a rescore -d 'recompute every aggregate from the ratings already stored'
complete -c eifo-fetch -n '__eifo_at ""' -a seret -d 'build and inspect the Seret page index (Israeli ratings)'
complete -c eifo-fetch -n '__eifo_at ""' -a sources -d 'inspect configured sources'
complete -c eifo-fetch -n '__eifo_at ""' -a review -d 'work through unresolved matches'
complete -c eifo-fetch -n '__eifo_at ""' -a daemon -d 'run phases on the configured schedule'
complete -c eifo-fetch -n '__eifo_at ""' -a db -d 'database maintenance'

# -- sync --------------------------------------------------------------------

complete -c eifo-fetch -n '__eifo_at sync' -l source -x -d 'limit to this source; repeatable' -a '(__eifo_sources)'

# -- enrich ------------------------------------------------------------------

complete -c eifo-fetch -n '__eifo_at enrich' -l force -d 're-enrich regardless of how fresh the ratings are'
complete -c eifo-fetch -n '__eifo_at enrich' -l limit -x -d 'stop after N titles'
complete -c eifo-fetch -n '__eifo_at enrich' -l skip-imdb -d 'skip the IMDb dataset download (tens of megabytes)'
complete -c eifo-fetch -n '__eifo_at enrich' -l skip -x -d 'skip one enricher for this run; repeatable' -a '(__eifo_enrichers)'

# -- images ------------------------------------------------------------------

complete -c eifo-fetch -n '__eifo_at images' -l force -d 're-download existing artwork'
complete -c eifo-fetch -n '__eifo_at images' -l limit -x -d 'stop after N titles'

# -- repair-names ------------------------------------------------------------

complete -c eifo-fetch -n '__eifo_at repair-names' -l limit -x -d 'stop after N titles'

# -- rematch -----------------------------------------------------------------

complete -c eifo-fetch -n '__eifo_at rematch' -l apply -d 'write the matches; without it the plan is only printed'
complete -c eifo-fetch -n '__eifo_at rematch' -l limit -x -d 'stop after N titles'

# -- dedupe ------------------------------------------------------------------

complete -c eifo-fetch -n '__eifo_at dedupe' -l apply -d 'perform the merges; without it the plan is only printed'

# -- seret -------------------------------------------------------------------

complete -c eifo-fetch -n '__eifo_at seret' -a index -d 'crawl the sitemap so titles can be resolved to Seret pages'
complete -c eifo-fetch -n '__eifo_at seret' -a status -d 'show what the index currently holds'
complete -c eifo-fetch -n '__eifo_at "seret index"' -l limit -x -d 'pages to read this run'
complete -c eifo-fetch -n '__eifo_at "seret index"' -l rps -x -d 'requests per second (default 0.5)'
complete -c eifo-fetch -n '__eifo_at "seret index"' -l force -d 're-read every page, however recently indexed'

# -- sources -----------------------------------------------------------------

complete -c eifo-fetch -n '__eifo_at sources' -a list -d 'show every source with its last run'

# -- review ------------------------------------------------------------------

complete -c eifo-fetch -n '__eifo_at review' -a list -d 'show unresolved items'
complete -c eifo-fetch -n '__eifo_at review' -a resolve -d 'attach an item to a title'
complete -c eifo-fetch -n '__eifo_at review' -a skip -d 'not that title - give it one of its own'
complete -c eifo-fetch -n '__eifo_at review' -a dismiss -d 'not a title at all - never offer it again'
complete -c eifo-fetch -n '__eifo_at review' -a auto -d 'clear the part of the queue that is not in doubt'

complete -c eifo-fetch -n '__eifo_at "review list"' -l source -x -d 'limit to one source' -a '(__eifo_sources)'
complete -c eifo-fetch -n '__eifo_at "review list"' -l limit -x -d 'how many to show'
complete -c eifo-fetch -n '__eifo_at "review resolve"' -l title-id -x -d 'the title to attach it to'
complete -c eifo-fetch -n '__eifo_at "review auto"' -l apply -d 'act on it; without this the plan is only counted'

# -- daemon ------------------------------------------------------------------

complete -c eifo-fetch -n '__eifo_at daemon' -l once -d 'run every scheduled phase immediately, then exit'

# -- db ----------------------------------------------------------------------

complete -c eifo-fetch -n '__eifo_at db' -a upgrade -d 'apply migrations (creates the schema)'
complete -c eifo-fetch -n '__eifo_at db' -a downgrade -d 'revert migrations - takes a revision'
complete -c eifo-fetch -n '__eifo_at db' -a current -d 'show the applied migration revision'
