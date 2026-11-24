# xdrop

Self-hosted drag & drop file sharing with no file size limit. One small FastAPI app, SQLite and
nginx. Built to run behind Cloudflare Tunnel, so it works around the 100 MB per-request limit.

## Features

- **Chunked, resumable uploads.** Files go up in 4–64 MB chunks (the size adapts to the
  connection), two in parallel. Each chunk is written at an explicit offset, so an upload
  continues after a network error, a closed tab or a server restart. File size is bounded only
  by free disk space.
- **Share links with expiry.** One link per share, valid for 1, 7 or 30 days or forever.
- **Upload links.** Let a friend upload into one of your shares without an account.
- **Fast downloads.** The app checks access, then nginx serves the file with
  `X-Accel-Redirect` (sendfile, HTTP Range, resumable downloads).
- **Network storage friendly.** Storage can be an SMB/NFS/sshfs mount. A marker file proves it is
  really mounted; while it is not, the app answers 503 instead of filling the empty mount point.
  Availability is probed on a background thread, so a hung share never blocks requests.
- **Disk space guard.** Space promised to running uploads is reserved, and a configurable amount
  of free space is always kept.
- **Housekeeping.** Expired shares and stale unfinished uploads are cleaned up automatically.
- **No build step.** Vanilla JS frontend, two Python dependencies.

The UI is in Hungarian.

## Quick start (local)

```sh
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
mkdir -p data/files && touch data/files/.xdrop-storage
export XDROP_SECRET=$(python -c "import secrets; print(secrets.token_hex(32))")
export XDROP_DB=data/xdrop.db XDROP_STORAGE=data/files XDROP_OPEN=1
uvicorn app.main:app --port 8000
```

Open http://127.0.0.1:8000. Without nginx, downloads are not served (they rely on
`X-Accel-Redirect`); use the nginx setup below for a full install.

## Install (Debian/Ubuntu)

1. Create a user and the directories:
   ```sh
   useradd --system --home /opt/xdrop xdrop
   mkdir -p /opt/xdrop /var/lib/xdrop /srv/xdrop /etc/xdrop
   cp -r app requirements.txt /opt/xdrop/
   python3 -m venv /opt/xdrop/venv && /opt/xdrop/venv/bin/pip install -r /opt/xdrop/requirements.txt
   touch /srv/xdrop/.xdrop-storage
   chown -R xdrop:xdrop /var/lib/xdrop /srv/xdrop
   ```
2. Copy `.env.example` to `/etc/xdrop/env` and fill it in (`chmod 600`).
3. Install the service and nginx config:
   ```sh
   cp deploy/xdrop.service /etc/systemd/system/
   cp deploy/nginx-xdrop.conf /etc/nginx/sites-enabled/xdrop
   systemctl daemon-reload && systemctl enable --now xdrop && systemctl reload nginx
   ```
   The nginx `alias` in `/_files/` must point to the same directory as `XDROP_STORAGE`.
4. Put it behind HTTPS (Cloudflare Tunnel, Caddy, or nginx with a certificate). nginx forwards
   `X-Forwarded-Proto` from the proxy in front of it.

### Set the password

```sh
/opt/xdrop/venv/bin/python -c "import getpass, hashlib, secrets; pw = getpass.getpass().encode(); s = secrets.token_bytes(16); print('scrypt\$' + s.hex() + '\$' + hashlib.scrypt(pw, salt=s, n=2**14, r=8, p=1, dklen=32).hex())"
```

Put the output in `XDROP_PASSWORD_HASH` and restart the service.

## Configuration

| Variable | Default | Meaning |
| --- | --- | --- |
| `XDROP_SECRET` | (required) | Key that signs session cookies. |
| `XDROP_PASSWORD_HASH` | empty | Owner password, `scrypt$<salt hex>$<hash hex>`. |
| `XDROP_DB` | `/var/lib/xdrop/xdrop.db` | SQLite database. |
| `XDROP_STORAGE` | `/srv/xdrop` | File storage; needs a `.xdrop-storage` marker file. |
| `XDROP_RESERVE_GB` | `5` | Free space always kept on the storage. |
| `XDROP_STALE_HOURS` | `48` | Unfinished uploads older than this are removed. |
| `XDROP_OPEN` | `0` | `1` turns off login: every visitor can manage all shares. |

## Security notes

- `XDROP_OPEN=1` means anyone who can reach the site can create, read and delete shares. Use it
  only on a private network.
- Share and upload links are random 128-bit tokens; anyone with the link has access until it
  expires or is deleted.
- Files are only reachable through the app's access check (`/_files/` is `internal` in nginx).

## License

MIT, see [LICENSE](LICENSE).
