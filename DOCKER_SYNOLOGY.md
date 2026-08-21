# Glanceboard on a Synology NAS (Docker)

This repo ships container support: `Dockerfile`, `docker-compose.yml`, and
`.dockerignore`. Nothing here is Synology-only — the same files run on any
Docker host — but the steps below are written for DSM's Container Manager.

The image is built **on the NAS** by Container Manager. That avoids
cross-architecture builds and needs no registry — it works the same on Intel
and ARM Synology units.

---

## 1. Get the files onto the NAS

Create a project folder under the shared `docker` folder. Over SSH:

```bash
mkdir -p /volume1/docker/glanceboard && cd /volume1/docker/glanceboard && git clone https://github.com/google-gemini/glanceboard.git .
```

If you'd rather not use SSH, File Station can upload a clone of the repo to
`docker/glanceboard` instead.

## 2. Choose your settings (optional)

Everything has a working default, so you can skip straight to step 3. To
override the published port or the container timezone:

```bash
cp .env.example .env
```

Then edit `.env`:

- **`GLANCEBOARD_PORT`** — the port you'll open in a browser. Defaults to
  `8000`. DSM itself uses 5000/5001, and 8000 is often taken on a NAS too, so
  pick something free if the project fails to start.
- **`TZ`** — the container clock. Match it to the timezone you pick in the
  dashboard: the app schedules generation at specific hours, so a mismatch
  shows up as images arriving at the wrong time of day.

`.env` is gitignored, so your values stay local.

### Running unprivileged (optional)

The container runs as root by default, which is the reliable choice for DSM
bind mounts and needs no preparation. To drop privileges instead, do both of
the following **before the first start**:

Find your DSM account's ids with `id <your-dsm-username>`, and put them in
`.env` as `PUID` and `PGID`. Then create and chown the data folder:

```bash
mkdir -p /volume1/docker/glanceboard/data/images /volume1/docker/glanceboard/data/uploads
```

```bash
sudo chown -R <PUID>:<PGID> /volume1/docker/glanceboard/data
```

The ordering is not incidental. `app.py` creates `data/images` and
`data/uploads` at import time, so if the container has ever started as root
those directories end up root-owned. A later non-root start can read them but
not write, and character photo uploads fail with a 500. If you switch to
non-root after already starting once, re-run the `chown`.

## 3. Build and start the project

In DSM: **Container Manager → Project → Create**

- **Project name:** `glanceboard`
- **Path:** `/volume1/docker/glanceboard`
- Container Manager detects the existing `docker-compose.yml` — choose to use it
- Click through to **Build**

First build takes roughly 3–6 minutes: it installs the npm dependencies, runs
the Vite build, then installs the Python wheels. Later rebuilds are much faster
because the dependency layers cache.

Watch the build log for failures rather than assuming success — a wheel that
falls back to a source build is the most likely thing to go wrong on an ARM
unit, and it shows up here.

## 4. Configure

Open `http://<NAS-IP>:<GLANCEBOARD_PORT>` and complete the onboarding wizard:

- **Gemini API key** from [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
- **iCal URL** — in Google Calendar, Settings → your calendar → "Secret address in iCal format"
- **Location and timezone** — drives weather and scene accuracy
- **Art style**, and optionally **characters** with reference photos

The key is written to `data/config.json` on the NAS and used server-side only.

## 5. Point the display at the NAS

Which URL to use depends on the firmware running on your display.

**esp32-photoframe firmware** handles TLS and decodes PNG, so it can fetch
straight through a reverse proxy if you have one:

```
https://<your-hostname>:<proxy-port>/api/display
```

**The project's own glanceboard-firmware** cannot do either. It has no CA
bundle, so `https://` fails with "No server verification option set in
esp_tls_cfg_t structure", and `display_show_image()` memcpy's the response body
into the panel framebuffer without decoding anything — a PNG renders as noise.
For that firmware use the `/api/display/raw` endpoint over plain HTTP, direct
to the published container port, bypassing any proxy:

```
http://<NAS-IP>:<GLANCEBOARD_PORT>/api/display/raw
```

`/api/display/raw` returns exactly 192000 bytes of packed 4bpp (800 x 480, two
pixels per byte, even x in the high nibble) — the precise format that firmware
expects.

**Give the NAS a DHCP reservation first.** That URL is baked into the display's
config; if the NAS IP changes on lease renewal, the display silently stops
updating.

---

## Notes and gotchas

**DSM firewall** — if you have it enabled under Control Panel → Security →
Firewall, add an allow rule for the port. The ESP32 connects from your LAN, so
it needs to reach the NAS directly.

**Networking** — the compose file uses `network_mode: bridge` deliberately. DSM
only sets up NAT/masquerade for `docker0`, so a container on a user-defined
Compose network resolves DNS but gets no outbound internet — which shows up as
TCP connect timeouts to every external host, including the Gemini API.

**Python 3.11 is deliberate** — `server/requirements.txt` pins `numpy==2.0.2`
and `Pillow==11.3.0`, which publish no wheels for newer interpreters. Bumping
the base image to 3.12+ will trigger slow source builds or fail outright.

**Updating** — `git pull` in the project folder, then Build again in Container
Manager. Your `data/` folder is a bind mount and survives rebuilds.

**Backup** — `data/` is the only stateful thing. It holds your config (with the
API key), generated images, and character photos.

**Gmail digest widget** — optional and off. Set `INSTALL_EMAIL: "true"` in the
compose build args and follow `EMAIL_SETUP.md` for the OAuth credentials.
