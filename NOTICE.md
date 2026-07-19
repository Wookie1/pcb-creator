# Third-Party Notices & Acknowledgements

PCB-Creator itself is MIT-licensed (see [LICENSE](LICENSE)). It stands on a
number of open-source projects and data sets. This file credits them and records
their licenses.

**No copyleft code is committed to this repository** — nothing under GPL, AGPL,
or CC-BY-SA is vendored here. Every copyleft-licensed work below is either run as
a **separate process** (Freerouting, KiCad), used as an unmodified **library
dependency** installed by the user from PyPI (`cairosvg` LGPL; `easyeda2kicad`
AGPL, optional), or read at runtime from the user's own system (KiCad footprint
libraries). Publishing this MIT source therefore distributes no copyleft
material; those obligations, where they apply at all, attach only when a user
chooses to install and combine those components — not to this repository.

## Runtime tools (invoked as external processes)

These are not distributed with PCB-Creator. They are downloaded or must be
installed by the user, and PCB-Creator calls them as separate programs.

- **Freerouting** — the default autorouter. Downloaded on first use as a
  `.jar` and run in a separate JVM (`optimizers/freerouter.py`).
  License: **GPL-3.0**. https://github.com/freerouting/freerouting
- **KiCad** (`kicad-cli`, `pcbnew`) — used, when installed, for authoritative
  DRC, zone pouring, and export round-tripping. Run as a subprocess / via
  KiCad's bundled Python. License: **GPL-3.0**. https://www.kicad.org

Because Freerouting and KiCad are executed at arm's length as independent
programs (not linked into PCB-Creator), their GPL does not extend to this
project's MIT-licensed code.

## Data / libraries used at runtime

- **KiCad footprint libraries** — parsed at runtime to resolve footprint pad
  geometry (`exporters/kicad_mod_parser.py`, `orchestrator/gather/`).
  License: **CC-BY-SA-4.0**, with the **KiCad Library Exception**:

  > To the extent that the creation of electronic designs that use "Licensed
  > Material" can be considered to be "Adapted Material", then the copyright
  > holder waives article 3 of the license with respect to these designs and
  > any generated files which use data provided as part of the "Licensed
  > Material".

  i.e. boards and Gerbers you generate are **not** subject to share-alike.
  https://gitlab.com/kicad/libraries/kicad-footprints
- **EasyEDA / LCSC** — footprint, 3D-model, and part (stock/price) data fetched
  from the EasyEDA/LCSC web API (`orchestrator/gather/easyeda_lookup.py`).
  Data © EasyEDA / LCSC, used under their terms of service.
  https://easyeda.com · https://www.lcsc.com

## Python dependencies

Installed via `pip` from PyPI; each ships its own license with its distribution.

| Package | License | Role |
|---------|---------|------|
| litellm | MIT | LLM provider client |
| jinja2 | BSD-3-Clause | prompt templating |
| jsonschema | MIT | schema validation |
| gerber-writer | Apache-2.0 | Gerber RS-274X output |
| cairosvg | LGPL-3.0-or-later | SVG → PNG board rendering |
| fastmcp | Apache-2.0 | MCP server framework |
| click | BSD-3-Clause | CLI |
| ezdxf | MIT | DXF board-outline parsing (`dxf` extra) |
| easyeda2kicad | AGPL-3.0 | EasyEDA/LCSC CAD-data fetch (`3d` extra) |

`cairosvg` (LGPL) and `easyeda2kicad` (AGPL) are copyleft. They are used as
unmodified, separately-installed libraries; `easyeda2kicad` is an **optional**
dependency (the `3d` extra) and is only imported when present.

## Standards referenced

- **IPC-7351B** (land-pattern geometry) and **IPC-2221** (trace-width /
  current-capacity) — implemented from the published standards
  (`optimizers/ipc7351.py`, trace sizing in `optimizers/router.py`). IPC
  standards are the property of IPC; only the derived calculations are included
  here, not the standards' text.
