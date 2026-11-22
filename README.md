# Comet: a Mech Royale bot

Bot for the **Builders Buildathon, Mech Royale** track. It pilots one mech in the 16-mech battle
royale over the `mechfight/1` protocol (JSON lines on stdin/stdout). Python 3.13, standard library only.

| File | What |
| --- | --- |
| `bot.py` | Strategy (v8 "squad hunter"): sticky low-HP-first targeting, short-range finishing, a 16-way step search that keeps line of sight while a shot is ready and breaks it while reloading, retreat to cover only while a fight is being lost, laser / crate / drone / cube farming, deadline-based zone travel |
| `geo.py` | Map model: surface/obstacle rasters, line of sight, walkability, resumable A* |
| `data/map.bin.gz` | Precomputed rasters and 4 m nav grid built from the official arena files |
| `mechfight.py` | Protocol wrapper from the official kit (unchanged) |
| `Dockerfile` | Submission image (`python:3.13-slim`, pinned by digest) |

Tuning knobs are read from environment variables with built-in defaults (`FLAGS` is empty by
default); the scorer sets none, so the defaults are what plays.

## Team and credits

- `geo.py`, `data/map.bin.gz`: team member bemikha (github.com/bemikha/mech-bot, v4).
- `bot.py` v8: written by our team. Some decision ideas (target stickiness and low-HP bonus, a
  "losing fight" retreat rule, peek-style step search, upgrade order) were learned by reading the
  public MIT-licensed bot by giddygiddyboy (github.com/justnau/giddygiddyboy-mech-royale) and other
  teams' public write-ups; no code was copied.
- Sulertia’s public Mech Royale write-up informed the decision to avoid initiating distant fights with idle house bots. Our implementation uses observed approach velocity.
- Earlier versions and research (pocket-comet): team member teetham-t.
- `mechfight.py` and the starting `bot.py` structure come from the event's official `mechfight-kit`.
- No third-party libraries at runtime: Python 3.13 standard library only. The map file was
  precomputed offline from the kit's arena files.
- Development used AI coding assistants (OpenAI Codex and Anthropic Claude).

## License

MIT, see `LICENSE`.
