# MCP optimisation axes

The levers available for improving the answers MCP consumers give, measured
against [`mcp_use_cases.md`](mcp_use_cases.md). The obvious ones are which
tools exist, what their output looks like, and the server instructions and
tool docstrings. The rest fall into four groups.

## Inside the MCP server

- **Tool granularity.** Separate from which tools exist: small building
  blocks the model chains together, or bigger tools that run a whole workflow
  (e.g. "screen these parties", "what changed since date X"). Small tools
  stay flexible but leave the chaining to the model; workflow tools make
  answers consistent but lock in one way of working.
- **Server-side defaults.** What happens when the model leaves arguments out:
  threshold, cutoff, topic and dataset scope, result limits. Models rarely
  set optional arguments, so in practice the defaults are the behaviour.
- **Doing the work before the model sees it.** Labels, glossaries, a
  pre-built designations table, or the explanation `detail` written out as
  text. Every derivation the server does is one the model can't get wrong.
  This partly conflicts with testing whether models can read the raw
  explanation themselves.
- **Response size and paging.** How much comes back by default, how
  truncation is signalled, and whether a summary comes first with detail on
  request. A bloated response crowds out the model's reasoning.
- **Errors as guidance.** Error text that says what to do next (e.g. "no
  candidates above cutoff; retry with a lower cutoff to see why") works like
  a docstring at exactly the moment it's needed.

## Other MCP features

- **Resources and prompts.** Glossaries, the schema and dataset catalogues
  could be exposed as resources rather than tools. MCP prompts package a
  workflow (e.g. "screen a party") that clients often show as slash commands.
- **Client-side features.** Elicitation lets the server ask the user for a
  missing fact, such as a date of birth to settle a weak match, instead of
  the model guessing. MCP Apps (UI resources) let the host render a real
  table, moving formatting out of the model entirely.

## Outside the MCP

- **The API and the data.** Some failures belong upstream: missing positions
  on a PEP record are a data issue, and the shape of the match explanation
  is set by the yente matcher. Fixes in nomenklatura or yente help every
  client, not just this MCP.
- **Things shipped alongside the server.** Skills such as
  `browse-opensanctions`, agent templates, or a suggested system prompt for
  clients. Anthropic's
  [`kyc-screener`](https://github.com/anthropics/financial-services/tree/main/plugins/agent-plugins/kyc-screener)
  shows that much of "how to answer" can live in the consumer's skill rather
  than in the server.
- **Naming and discovery.** Server name, tool titles and the MCP registry
  listing decide whether a client picks our tools at all, and for which
  questions.

## Measurement

- **The harness itself.** Which client and model we test with, whether runs
  are manual or judged automatically by an LLM, and how many times each case
  is run. Answers vary from run to run, so a single run per case can mislead
  in either direction.

## Where to start

Server-side defaults and passing through the explanation `detail` look like
the most gain for the least work: together they unblock the false-positive,
birth-date and thin-data cases. After that, fix whatever turns out to be a
data problem upstream.
