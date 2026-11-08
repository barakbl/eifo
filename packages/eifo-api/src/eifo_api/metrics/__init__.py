"""What Prometheus reads at ``/api/v1/metrics``.

Three kinds of figure, from three places:

* **runs** (:mod:`.runs`): every plugin run a fetcher reported. A fetcher on a
  laptop opens and closes its runs through the ingest API, so the rows are on
  the server even though the work happened elsewhere, along with everything
  they counted: items seen, ratings found, seconds spent.
* **the catalog** (:mod:`.catalog`): what the database holds right now, such as
  titles per service, ratings per provider and the review queue.
* **traffic** (:mod:`.traffic`): requests this process served, counted as they
  happen.

The first two are read from the database at every scrape rather than counted in
memory, so a restart loses nothing and every worker gives the same answer. The
counters among them count rows that are never deleted, which is what makes them
safe to treat as Prometheus counters, and what lets :mod:`.history` replay them
from the first run onward.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

#: Every name below starts with this, so a dashboard can tell eifo's figures
#: from node-exporter's and cAdvisor's at a glance.
PREFIX = "eifo_"


@dataclass(slots=True)
class Family:
    """One metric and its values, before it is written in either format.

    Neutral on purpose: the live endpoint hands these to prometheus_client,
    and the history import writes them as OpenMetrics with timestamps. Both
    read the same families, so the two cannot drift apart on a name or a label.
    """

    #: Without the ``_total`` a counter is exposed with.
    name: str
    kind: Literal["counter", "gauge"]
    help: str
    labels: tuple[str, ...] = ()
    samples: dict[tuple[str, ...], float] = field(default_factory=dict)

    def set(self, value: float, *labels: str) -> None:
        self.samples[labels] = float(value)

    def add(self, value: float, *labels: str) -> None:
        self.samples[labels] = self.samples.get(labels, 0.0) + float(value)
