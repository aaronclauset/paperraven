# Security policy

## Reporting a vulnerability

Please report security problems privately, not in a public issue: on the repository's Security
tab, choose **Report a vulnerability**
(https://github.com/aaronclauset/paperraven/security/advisories/new). Only you and the maintainer
can see the report.

Say what you did, what PaperRaven did, and how to reproduce it. If a run is involved, its
`audit/closeout.json` helps; check it holds nothing you would rather not share.

## What counts as a security problem

- A database record, a web page, a PDF or a `.bib` file that makes PaperRaven, or the agent
  running it, do something it should not: run a command, write outside the run's or export's
  folder, download outside `fetch`, or reach a local or private network address.
- PaperRaven writing your OpenAlex API key, or your contact email unredacted, into a run, an
  export or a log, or sending either anywhere other than the documented services.
- `fetch` getting around its own limits: its pace per publisher, its stop at a publisher's first
  refusal, or its refusal to save PDFs where git would track them.

Wrong or missing papers, a judge's verdicts and other ordinary bugs are not security problems;
please open an issue for those at https://github.com/aaronclauset/paperraven/issues.

## Supported versions

Fixes go into the latest release.
