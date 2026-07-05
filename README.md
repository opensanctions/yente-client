# yente-client

Client SDK, command-line tool, and MCP server for the
[OpenSanctions](https://www.opensanctions.org) matching API and self-hosted
[yente](https://github.com/opensanctions/yente) instances. Screen people,
companies, and other entities against sanctions lists, politically exposed
persons (PEPs), and watchlists.

**Documentation: [yenteclient.followthemoney.tech](https://yenteclient.followthemoney.tech/)**

## Install

```bash
pip install yente-client            # Python SDK
pip install 'yente-client[cli]'     # + the `yente-cli` command-line tool
pip install 'yente-client[mcp]'     # + the `yente-mcp` server for LLM agents
```

## Quickstart

```python
from yente_client import Client, Person

with Client(api_key="...", app_name="MyScreeningApp") as c:
    hits = c.match(
        Person(firstName="Aleksandr", lastName="Zacharov", birthDate="1965"),
        datasets=["sanctions"],
    )
    for match in hits.matches:
        print(match.id, match.caption, match.score)
```

Matching is query-by-example: describe the entity in as much detail as you
can, and the API returns ranked, scored candidates. Use `match` for any
matching task — even with partial input; `search` is for end-user search
boxes, not a fallback for `match`.

Get an API key at
[opensanctions.org/account](https://www.opensanctions.org/account/), or point
`base_url=` at a self-hosted yente (no key needed).

## Learn more

- [Tutorial](https://yenteclient.followthemoney.tech/tutorial/) — install,
  first match, search, fetch, async, errors, the FollowTheMoney data model.
- [CLI overview](https://yenteclient.followthemoney.tech/cli/) — `yente-cli`
  commands, output formats, exit codes.
- [MCP server](https://yenteclient.followthemoney.tech/mcp/) — connect an
  LLM agent to the matching API.
- [API reference](https://yenteclient.followthemoney.tech/api/) — every
  public symbol, rendered from docstrings.

This repository carries the Python package under [`python/`](python/); a
TypeScript SDK sharing the same test fixtures is planned under
[`typescript/`](typescript/).
