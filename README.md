# Comet: a Mech Royale bot

Bot for the **Builders Buildathon, Mech Royale** track. It pilots one mech in the 16-mech battle
royale over the `mechfight/1` protocol (JSON lines on stdin/stdout). Python 3.13, standard library only.

| File | What |
| --- | --- |
| `bot.py` | Strategy: laser sniper that keeps house bots at long range, patrols inside the next zone, farms crates and drones early, A* to remembered lasers, drone retaliation, zone fix |
| `geo.py` | Map model: surface/obstacle rasters, line of sight, walkability, resumable A* |
| `data/map.bin.gz` | Precomputed rasters and 4 m nav grid built from the official arena files |
| `mechfight.py` | Protocol wrapper from the official kit (unchanged) |
| `Dockerfile` | Submission image (`python:3.13-slim`, pinned by digest) |

Behaviour switches are read from environment variables with built-in defaults (`FLAGS`
defaults to `patrol,nohide,zfix`); the scorer sets none, so the defaults are what plays.

## Team and credits

- Bot code (`bot.py`, `geo.py`, `data/map.bin.gz`): team member bemikha (github.com/bemikha/mech-bot, v4).
- Earlier versions and research (pocket-comet): team member teetham-t.
- `mechfight.py` and the starting `bot.py` structure come from the event's official `mechfight-kit`.
- No third-party libraries at runtime: Python 3.13 standard library only. The map file was
  precomputed offline from the kit's arena files.
- Development used AI coding assistants (OpenAI Codex and Anthropic Claude).

## License

MIT, see `LICENSE`.
