# Relay

Relay's gameplay policy is in `src/pilot.ts`. It chooses equipment, routes, upgrades, and actions using each observation. Run it with Node 24 or build the root Dockerfile.

## Code credits

- `src/mechfight.ts` is the unmodified TypeScript protocol wrapper from the official Builders Mech Royale starter kit.
- `src/vec.ts` and `src/terrain.ts` are Raunak Choudhary's unmodified vector helpers and terrain decoder. The decoder is named `src/world/terrain.ts` upstream. The source repository's MIT copyright and permission notice is in `LICENSE.upstream` and is included in the Docker image. [Henka source revision](https://github.com/raunak-choudhary/henka/tree/155c0edf92866fa55aadc79bc6521ecf6126f94c)
