# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Features

- **release:** Add git-cliff config, changelog, and release workflow ([04f06f2](https://github.com/Razbuzz/HomeLab/commit/04f06f2c76b9a7594ae061e2eb44fe06d7f92764))
- **hermes:** Add hermes agent beta service with ollama and mattermost ([47b5145](https://github.com/Razbuzz/HomeLab/commit/47b51453264c699f67c1bdb20ed6de1aca5fb831))
- **threadfin:** Add threadfin iptv proxy service ([a7a8f1e](https://github.com/Razbuzz/HomeLab/commit/a7a8f1e140feb76e76a76dbbe40aaf958d948fe3))

### Bug Fixes

- **permissions:** Harden env parsing, symlink safety, and sudo checks ([7927f51](https://github.com/Razbuzz/HomeLab/commit/7927f510d2d9ff0fbf8b882cc655a062633d6097))
- **couchdb:** Repair volume ownership for non-root containers ([319ad29](https://github.com/Razbuzz/HomeLab/commit/319ad296b481fb610025df3b4e82ef703b142b8b))
- **seerr:** Correct port mapping, tz quoting, and env config ([afad118](https://github.com/Razbuzz/HomeLab/commit/afad118899eca2c2731b0b26a1b67ef5c2eb2b36))
- **make:** Make setup POSIX-safe and validate all compose profiles ([d46ffd0](https://github.com/Razbuzz/HomeLab/commit/d46ffd05ec40f50b9305f6d9fc90780c1fffe58e))
- **ci:** Pin hadolint-action to v3.5.0 tag ([a700ab3](https://github.com/Razbuzz/HomeLab/commit/a700ab3499029d30b8ade794ebacd307edc6b545))
- **lint:** Clean up compose YAML, setup script, and README env block ([71802c7](https://github.com/Razbuzz/HomeLab/commit/71802c76a8c7967553fef58a5cad81ae8b037d03))

### Documentation

- **release:** Document changelog targets and release workflow ([72c813c](https://github.com/Razbuzz/HomeLab/commit/72c813c5fc13e638f71c02ba5a6993e957ad2784))
- **hermes:** Add operator guide and sync README for hermes agent ([13dd84c](https://github.com/Razbuzz/HomeLab/commit/13dd84c1af0081da468f93e434c0f5020b8d6f34))
- **permissions:** Document Seerr ownership and clean Makefile EOF ([d6aa935](https://github.com/Razbuzz/HomeLab/commit/d6aa935babafe1bafc8d8471c209a5ba77c03c05))
- **couchdb:** Document make permissions and v1.3.0 upgrade steps ([17b1171](https://github.com/Razbuzz/HomeLab/commit/17b117108589a16f63ff7c61d748d992a2d4f8c4))
- **threadfin:** Document threadfin and add dashboard link ([5ec6d23](https://github.com/Razbuzz/HomeLab/commit/5ec6d2393cee9a450395f06ce3db21b4187011b4))
- **seerr:** Sync .env example, ownership, and migration guide ([ce880d2](https://github.com/Razbuzz/HomeLab/commit/ce880d2c0a4b81aa743b406a2b09eaa3f59b2d5e))
- **agents:** Add reusable project agent definitions and AGENTS.md ([8e3691b](https://github.com/Razbuzz/HomeLab/commit/8e3691b9d789da19427a80b792b0f1ef119a033c))
- **readme:** List new make targets and fix .gitignore EOF ([8aef34a](https://github.com/Razbuzz/HomeLab/commit/8aef34a161ea99ada93dc22ca32aeefdc15fba74))
- Add CONTRIBUTING.md, PR template, and CI badge ([820e38f](https://github.com/Razbuzz/HomeLab/commit/820e38fd39344e0c48bc561bc22ae9a3e4503ee0))

### CI/CD

- Harden commitlint, smoke cleanup, and compose checks ([ae29804](https://github.com/Razbuzz/HomeLab/commit/ae29804c5b98358816522daa80061acb59bd1f5b))
- Add quality gate workflow, test suite, and Makefile targets ([ffd1e4f](https://github.com/Razbuzz/HomeLab/commit/ffd1e4fe4cdd5fdfef414104aea1756008822105))

### Testing

- **compose:** Handle escapes, operators, IPv6, ranges, and long ports ([4faff05](https://github.com/Razbuzz/HomeLab/commit/4faff05ba26298872bc80264d19462c00e927ce8))

### Other Changes

- Update ([3ea2e41](https://github.com/Razbuzz/HomeLab/commit/3ea2e41c60d9a200ab944a7b56dd5e809d0d71d1))
- Migrate complete ([330e2e7](https://github.com/Razbuzz/HomeLab/commit/330e2e7ff49ea4c4e3ca6112313f5551064079ff))
- Fix samba to use constant tag ([29e6e26](https://github.com/Razbuzz/HomeLab/commit/29e6e26aa82f895254cf150537223a27c8089cfa))
- Change MATTERMOST_DB_USER placeholder ([d498315](https://github.com/Razbuzz/HomeLab/commit/d498315ef2b89254557122b93dbdfc5fb201532d))
- Add basic internal mattermost for bot support ([6aacab0](https://github.com/Razbuzz/HomeLab/commit/6aacab00a60710294c6f32b00c55822caeb18f42))
- Update couchdb to use default uid & guid ([041e842](https://github.com/Razbuzz/HomeLab/commit/041e8421a0ae42bc63cd21a6db14450cd2c49e24))

## [1.3.0] - 2026-03-10

### Other Changes

- Update relevant files & `.env` ([795ce8d](https://github.com/Razbuzz/HomeLab/commit/795ce8d1875b0dc70400ef5d22847aa33d06d222))
- Update  to support ollama ([cb38fee](https://github.com/Razbuzz/HomeLab/commit/cb38feee068945bda1bba1f255cade198953bb13))
- Add ollama support ([0868a24](https://github.com/Razbuzz/HomeLab/commit/0868a242060b42b0f6922e828da685b2c483bbe1))

## [1.2.0] - 2026-03-02

### Other Changes

- Update .env ([a605771](https://github.com/Razbuzz/HomeLab/commit/a605771675bf8c9025212a2f622b9611816d518d))
- Add flaresolverr ([e7fb65b](https://github.com/Razbuzz/HomeLab/commit/e7fb65bd6e1d49033d941333e0671172d7e2e110))
- Update year 2026 ([398b46e](https://github.com/Razbuzz/HomeLab/commit/398b46e5b3c3dda0509375d3ccd27a8101b0b135))

## [1.1.0] - 2026-02-07

### Other Changes

- Update README.md from Overseer to Jellyseerr ([9e84744](https://github.com/Razbuzz/HomeLab/commit/9e84744b3a66d78274854a90ac55b1f95a816ad2))
- Update .env ([5dcbc34](https://github.com/Razbuzz/HomeLab/commit/5dcbc34d88877d85e6370d27270eff670c11834b))
- Jellyseerr replace overseerr ([c253ee4](https://github.com/Razbuzz/HomeLab/commit/c253ee483ad7592d4f54324de6140d0521d4a4c0))

## [1.0.0] - 2026-01-09

### Other Changes

- Add readme ([5decee9](https://github.com/Razbuzz/HomeLab/commit/5decee9e668066ae0c7351a47bbadc94bf843650))
- Remove pulsarr ([7eaa909](https://github.com/Razbuzz/HomeLab/commit/7eaa909aa740c27e39703a4c2fbe6eb60aee2221))
- Change couch-db image to be tagged ([6fb3ff4](https://github.com/Razbuzz/HomeLab/commit/6fb3ff4386b853eacfa0908e60c715288896c0a7))
- Update pi-hole ([7a8d272](https://github.com/Razbuzz/HomeLab/commit/7a8d27205ec04b6d981608e8e4c7dfe8b93aafc3))
- Chmod & Chown all files ([226bc71](https://github.com/Razbuzz/HomeLab/commit/226bc7194ebc6ccf650870ece29636038a1fda39))
- Remove tailscale containter ([02025c6](https://github.com/Razbuzz/HomeLab/commit/02025c6c2b3afaca4583b4da1f29b9ed1d35e0bc))
- Add pulsarr add folder ([c33748a](https://github.com/Razbuzz/HomeLab/commit/c33748aba880bc530ae00e9b9d89adcf34e04c54))
- Set default ,env file & Add it to git ignore ([154dc80](https://github.com/Razbuzz/HomeLab/commit/154dc8031f2c012db9d1e7023af18e23c0b04c42))
- Init ([3e3d12a](https://github.com/Razbuzz/HomeLab/commit/3e3d12ab9668cea23aa0c20e76a3c99dd899aa55))
- Add initial .env configuration file ([c30e7e7](https://github.com/Razbuzz/HomeLab/commit/c30e7e7c7d15a30f9c84ef00cb030b2a9d46ff06))
- Create LICENSE ([0ebea61](https://github.com/Razbuzz/HomeLab/commit/0ebea619a99d6e2d955cac6ea7588d3d587a99c2))
- Initial commit ([0adf17d](https://github.com/Razbuzz/HomeLab/commit/0adf17d202512c42a5041cbbcc3e90239743eb33))

---
*Generated by [git-cliff](https://git-cliff.org).*
