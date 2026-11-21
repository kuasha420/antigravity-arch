# antigravity-arch

Native Arch Linux query, packaging, and local `pacman` repository sync suite for the full **Google Antigravity 2.x** stack (`antigravity`, `antigravity-cli`, and `antigravity-ide`).

AUR packages frequently lag hours or days behind Google's official releases. `antigravity-arch` queries Google's official Cloud Run auto-updater JSON APIs directly, overlays upstream AUR `PKGBUILD` recipes, cryptographically verifies every download before `makepkg` runs, and publishes native `.pkg.tar.zst` archives into a local `file://` `pacman` repository (`[antigravity-local]`) — or installs directly via `pacman -U`.

## Highlights

- **Zero external Python runtime dependencies** — pure Python `3.11–3.14+` stdlib plus native Arch tools (`pacman`, `makepkg`, `vercmp`, `repo-add`, `git`).
- **Multi-architecture (`x86_64` & `aarch64`)** — queries official Google Cloud Run endpoints directly with zero brittle HTML scraping.
- **3-tier recipe resilience** — overlays live `aur.archlinux.org` `PKGBUILD`s (preserving Arch `depends=()`, `.desktop` entries, completions, and `-2` packaging fixes), falls back to your cached local AUR clone, and includes cleanroom bundled fallback recipes if AUR is down on a fresh machine.
- **Strict pre-build integrity verification** — verifies Google's own manifest/GCS hashes (**CRC32C** via GCS `x-goog-hash` for `antigravity`, **SHA-512** for `antigravity-cli`, **SHA-256** for `antigravity-ide`) before `makepkg` runs with full checksum validation (never `--skipinteg`).
- **Pacman 7 `alpm` sandbox safe** — stores the default local repository at `/var/cache/pacman/antigravity-local` (`0755`, owned by `$USER`) so unprivileged `makepkg` + `repo-add` work rootlessly while Pacman 7's `alpm` user reads the repository without home-directory `0700` permission errors.
- **Native synced repo packages** — once `[antigravity-local]` is registered, `antigravity`, `antigravity-cli`, and `antigravity-ide` are no longer foreign (`pacman -Qm`) packages, eliminating AUR helper (`yay`/`trizen`) noise and downgrade warnings.
- **Conditional `@parcel/watcher` guard** — wraps `antigravity-ide`'s `watcher.node` replacement so it only patches when the bundled `@parcel/watcher` version is `< 2.5.6`.
- **Rootless `systemd --user` timer** — stages new updates in the background every 6 hours (with optional `notify-send` desktop alerts) so your next `sudo pacman -Syu` picks them up instantly.
- **Bounded disk retention** — automatically retains `keep_packages = 2` (current + 1 rollback) per package and purges superseded downloaded tarballs (`keep_sources = 0`).

---

## Installation

### Option 1: Native Arch Package (Recommended)

Installs `/usr/bin/antigravity-arch` and short alias `/usr/bin/agy-arch`:

```bash
git clone https://github.com/kuasha420/antigravity-arch.git
cd antigravity-arch/packaging
makepkg -si
```

### Option 2: `uv` / `pipx`

```bash
uv tool install git+https://github.com/kuasha420/antigravity-arch.git
# or
pipx install git+https://github.com/kuasha420/antigravity-arch.git
```

---

## Quickstart

### Mode A: Local `file://` Pacman Repository (Default)

```bash
# 1. One-time setup: creates /var/cache/pacman/antigravity-local (owned by you, 0755)
#    and adds Include = /etc/pacman.d/antigravity-local.conf to /etc/pacman.conf
antigravity-arch repo init

# 2. Verify environment, alpm permissions, and repo health
antigravity-arch doctor

# 3. Check Installed vs AUR vs Official Upstream versions
antigravity-arch status

# 4. Build latest releases, publish to [antigravity-local], and install via pacman -Syu
antigravity-arch sync --install

# 5. (Optional) Enable rootless 6h background staging timer
antigravity-arch timer enable
```

From then on, standard `sudo pacman -Syu` (or `yay` / `trizen`) installs staged updates natively.

### Mode B: Direct Install (Zero `/etc/pacman.conf` Changes)

```bash
antigravity-arch sync --mode direct --install
```

---

## CLI Reference

Both `antigravity-arch` and the short alias `agy-arch` are identical:

| Command | Description |
| :--- | :--- |
| `agy-arch status [--json] [--installed-only]` | Table (or JSON) comparing Installed, AUR, and official Upstream versions. |
| `agy-arch check [--installed-only]` | Exits `0` if up to date, `2` if updates are available (ideal for scripts/status bars). |
| `agy-arch sync [--install] [--dry-run] [--packages ...] [--mode local-repo\|direct]` | Queries upstream, verifies hashes, builds `.pkg.tar.zst`, publishes to repo (or installs directly), and prunes old versions. |
| `agy-arch repo init [--path ...] [--name ...] [--gpg-key ...]` | Creates local repo directory, empty DB, drop-in file, and `/etc/pacman.conf` Include block. |
| `agy-arch repo status [--json]` | Lists registration status and staged packages in `[antigravity-local]`. |
| `agy-arch repo prune [--keep-packages N] [--keep-sources N]` | Prunes older built archives and cached source tarballs. |
| `agy-arch repo uninstall [--purge]` | Cleanly removes the managed block from `/etc/pacman.conf` and deletes the drop-in (and repo dir with `--purge`). |
| `agy-arch timer enable\|disable\|status [--interval 6h]` | Manages the rootless `systemd --user` background sync timer. |
| `agy-arch doctor [--json]` | Checks required binaries, Pacman 7 `alpm` directory traversal permissions, and repo DB health. |
| `agy-arch config init\|show` | Generates or displays `~/.config/antigravity-arch/config.toml`. |

---

## Configuration (`~/.config/antigravity-arch/config.toml`)

Generate a commented starter file with `agy-arch config init`:

```toml
[sync]
mode = "local-repo"   # or "direct"
packages = "all"      # "all", "installed", or ["antigravity", "antigravity-cli"]
notify = true
http_timeout = 20

[repo]
name = "antigravity-local"
path = "/var/cache/pacman/antigravity-local"
# gpg_key = "YOUR_GPG_KEY_ID"

[retention]
keep_packages = 2
keep_sources = 0

[timer]
interval = "6h"
```

For headless / SSH servers that only need `agy`, set `packages = ["antigravity-cli"]` or `packages = "installed"`.

### Optional GPG Signing

To sign built packages and the local repository database with GPG (`SigLevel = Required DatabaseOptional`):

```bash
agy-arch repo init --gpg-key YOUR_GPG_KEY_ID
agy-arch sync --gpg-key YOUR_GPG_KEY_ID --install
```

*(Ensure your signing key is trusted in `pacman-key` via `sudo pacman-key --add` and `sudo pacman-key --lsign-key`.)*

---

## Upstream Endpoints & Security Model

| Package | Official Query Endpoint | Pre-`makepkg` Verification |
| :--- | :--- | :--- |
| `antigravity` | `https://antigravity-hub-auto-updater-974169037036.us-central1.run.app/releases` | Castagnoli **CRC32C** vs GCS `x-goog-hash` + `Content-Length` |
| `antigravity-cli` | `https://antigravity-cli-auto-updater-974169037036.us-central1.run.app/manifests/linux_{amd64,arm64}.json` | **SHA-512** from manifest |
| `antigravity-ide` | `https://antigravity-ide-auto-updater-974169037036.us-central1.run.app/api/update/linux-{x64,arm64}/stable/latest` | **SHA-256** from update API |

Each tarball is downloaded once via atomic `.part` staging into `SRCDEST`, verified against Google's official digest, hashed with the algorithm declared by the `PKGBUILD` (`b2sum` / `sha256sum` / `sha512sum`), and reused directly by `makepkg` with full checksum checks enabled.

---

## Clean Uninstall

```bash
agy-arch timer disable --purge
agy-arch repo uninstall --purge
sudo pacman -R antigravity-arch
```

---

## Development

```bash
pytest -v
ruff check src tests
```

## License

MIT — see [LICENSE](LICENSE).
