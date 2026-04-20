# Installing AttDown4Acumatica

Three paths depending on your environment. The **Prebuilt binary** is the easiest for non-developers.

---

## 1. Prebuilt binary (recommended for most users)

Download the binary for your OS from the [Releases](https://github.com/hallboys/AttDown4Acumatica/releases) page:

| OS | File |
|---|---|
| macOS (Apple Silicon) | `attdown-macos-arm64` |
| macOS (Intel) | `attdown-macos-x64` |
| Windows | `attdown-windows-x64.exe` |
| Linux | `attdown-linux-x64` |

### Before you run

1. In Acumatica, create a **Connected Application (SM303010)** with:
   - OAuth 2.0 Flow: **Authorization Code**
   - Redirect URI: `http://localhost:8080/oauth/callback`
   - Copy the client ID (and secret if you set one).
2. Create an `.env` file next to the binary:
   ```bash
   ACU_URL=https://your-tenant.acumatica.com
   ACU_ENDPOINT=Default/24.200.001
   ACU_CLIENT_ID=...
   ACU_CLIENT_SECRET=...
   ACU_REDIRECT_URI=http://localhost:8080/oauth/callback
   SESSION_SECRET=<run: python3 -c 'import secrets; print(secrets.token_urlsafe(48))' or generate any long random string>
   ```

### macOS

```bash
# One-time permission grant (see "Gatekeeper" note below)
xattr -d com.apple.quarantine attdown-macos-arm64   # or attdown-macos-x64
chmod +x attdown-macos-arm64

./attdown-macos-arm64 serve
```

A browser tab opens automatically to `http://localhost:8080`. You'll be redirected to Acumatica to log in, then back to the app.

**Gatekeeper on first run:** macOS shows a warning because the binary isn't Apple-notarized yet. Either:

- Run the `xattr` command above (quickest), **or**
- Right-click the binary in Finder → **Open** → **Open** again in the confirmation dialog. After that, double-click works normally.

### Windows

1. Place `attdown-windows-x64.exe` and `.env` in the same folder.
2. Double-click the `.exe`. A command window opens and the browser follows.

**SmartScreen on first run:** Windows may show "Windows protected your PC". Click **More info** → **Run anyway**. This warning goes away once the binary has enough downloads to build reputation.

### Linux

```bash
chmod +x attdown-linux-x64
./attdown-linux-x64 serve
```

A browser tab opens to `http://localhost:8080`.

### Verifying the download

Every release ships a `SHA256SUMS.txt`:

```bash
shasum -a 256 attdown-macos-arm64
# compare to the value in SHA256SUMS.txt
```

---

## 2. Docker (no Python or binary install)

```bash
git clone https://github.com/hallboys/AttDown4Acumatica.git
cd AttDown4Acumatica
cp .env.example .env          # edit as above
docker compose up
# open http://localhost:8080
```

Downloaded files land in `./data` (mounted into the container at `/data`).

---

## 3. Python (developers / contributors)

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[all]"       # [all] includes s3, azure, gcs sinks

cp .env.example .env          # edit as above
attdown serve
```

### Cloud sinks

The prebuilt binary only supports `file://` output. For S3, Azure Blob, or GCS, install the matching extras:

```bash
pip install "attdown4acumatica[s3]"        # AWS S3
pip install "attdown4acumatica[azure]"     # Azure Blob
pip install "attdown4acumatica[gcs]"       # Google Cloud Storage
pip install "attdown4acumatica[all]"       # all of the above
```

Then use the appropriate URI in the job form (`s3://bucket/prefix`, `az://container/prefix`, `gs://bucket/prefix`).

---

## Common issues

**"Port 8080 already in use"** — something else is on that port. Run `attdown serve --port 9000` to pick another.

**Browser doesn't open** — go to `http://localhost:8080` manually, or run with `--no-open-browser` and paste the URL into your browser.

**"Missing env var: ACU_URL"** — the `.env` file isn't in the directory you ran the binary from. Either `cd` to its folder, or set `ATTDOWN_ENV_FILE=/full/path/to/.env`.

**OAuth redirect URL mismatch** — the Connected App's redirect URI in SM303010 must match `ACU_REDIRECT_URI` character-for-character.

**Endpoint dropdown is empty** — your OAuth user doesn't have access to any endpoint, or `ACU_ENDPOINT` doesn't match a published endpoint. Check Acumatica → SM207060.

See [CLAUDE.md](../CLAUDE.md) for more troubleshooting notes.
