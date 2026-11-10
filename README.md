# TeraBox Link Parser

A standalone server app for resolving TeraBox shares, playing captured HLS, and downloading captured video as MP4. The server keeps a single encrypted saved session for the authenticated operator.

## Deploy

Set these variables in your host or copy `.env.example` to `.env` for the Docker commands below.

| Variable | Purpose |
| --- | --- |
| `RIPPER_PUBLIC_ORIGIN` | Exact public HTTPS origin, for example `https://terabox.example.com` |
| `RIPPER_SERVER_USER` | Your app login name |
| `RIPPER_SERVER_PASSWORD` | Your app login password |
| `TERABOX_SESSION_KEY` | Fernet key used to encrypt the server's saved cookie file |

Generate the key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"` after installing `cryptography`. Keep the key stable across restarts and store it with the other deployment secrets.

### Docker

```sh
docker build -t terabox-link-parser .
docker run --name terabox-link-parser --restart unless-stopped --env-file .env \
  -p 127.0.0.1:5050:5050 -v terabox-parser-data:/data terabox-link-parser
```

Point your HTTPS reverse proxy at `http://127.0.0.1:5050` and preserve the public `Host` and `Authorization` headers. Open the public address and sign in when prompted. For a managed container host, configure the same secrets in its settings, use port `5050`, and mount a writable volume at `/data`.

### Without Docker

Install Python and FFmpeg on the server, then run the following commands. On Linux, use the Playwright install command with `--with-deps chromium` if Chromium system libraries are missing.

```sh
python -m pip install -r requirements.txt
python -m playwright install chromium
python app.py
```

Export the settings in your shell or service manager before starting. The app reads environment variables from the process. It binds to `127.0.0.1:5050` by default. Use `RIPPER_BIND_HOST=0.0.0.0` only on a private container port behind your HTTPS proxy. `PORT` overrides `5050`; `TERABOX_DATA_DIR` sets the writable persistent storage directory. The entry point runs one Waitress process with multiple threads so the player and MP4 relay can access captures. Set the proxy timeout to at least five minutes for MP4 preparation.

## Use

Open **Saved session settings** to upload a domain-scoped cookie export from a browser session that can play the share. Then paste the share and click **Resolve full video**. The app tries the official download first and may save a copy to My TeraBox if the site offers that route for a preview. Use **Play video** or **Download MP4** for a captured stream. Available qualities and full-video access depend on what the saved session can access.

The archive includes the local HLS.js player and its license. The original app and its unrelated scanner routes are separate.
