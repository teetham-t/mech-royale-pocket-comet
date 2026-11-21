# Pocket Comet: a Mech Royale bot

Bot for the **Builders Buildathon, Mech Royale** track. It pilots one mech in the 16-mech battle
royale over the `mechfight/1` protocol (JSON lines on stdin/stdout). Python 3.13, standard library only.

| File | What |
| --- | --- |
| `bot.py` | Strategy: map model, A* routes to lasers, target choice, laser sniping, crate/drone farming, zone, upgrades |
| `mechfight.py` | Protocol wrapper from the official kit (unchanged) |
| `Dockerfile` | Submission image (`python:3.13-slim`, pinned by digest) |

## How it plays

- Picks up a laser first (remembers seen lasers and routes around buildings with A*).
- Fights only mechs that threaten it, are close, or are nearly dead, so it does not wake the
  passive house squad; keeps hunters at range where house guns rarely hit.
- Holds a laser charge until the beam is lined up and never releases into cover; fires the gun
  alongside when the shared aim can still hit.
- Farms crates and drones early for cubes, shoots back at drones that hit it, and keeps inside
  the next zone circle.

## Third-party code and tools

- `mechfight.py` and the starting version of `bot.py` come from the event's official
  `mechfight-kit` (Python wrapper and example bot).
- No other libraries: Python 3.13 standard library only.
- Development used AI coding assistants (OpenAI Codex and Anthropic Claude).

## License

MIT, see `LICENSE`.
