# PaperStage — deploy notes (DEPLOYED 2026-11-06)

Target: the same EC2 box as SnapDuo and Starlight Stories
(api.newkwlab.com), wired exactly like duo-web / book-web: a systemd
service running `web/app.py` on a loopback port, with Caddy
reverse-proxying the public path prefix to it.

## Layout on EC2

```
/home/ubuntu/clip-web/
  app.py              <- from web/app.py
  clip_config.json    <- create from clip_config.example.json (see below)
  site/               <- from web/site/ (index.html, llms.txt, media/)
  data/               <- created at runtime (orders/, licenses.json)
```

## Config

```bash
cp clip_config.example.json clip_config.json
# generate the agent token once and paste it in:
python3 -c "import secrets; print(secrets.token_hex(24))"
```

- `gumroad_product_id`: PENDING — fill in the live PaperStage product
  ID once the Gumroad product (Solo $12.99 / Duo $19.99, license keys
  enabled) exists; until then license activation fails closed by
  design.
- `gumroad_buy_url`: PENDING — the live product URL.
- `agent_token`: random hex, shared only with the production side
  (Muse's order-production step). Never commit the real token.
  (The local `web/clip_config.json` already carries a fresh token;
  reuse it or generate a new one on the box — just keep exactly one
  value in both places.)

## systemd unit — /etc/systemd/system/clip-web.service

```ini
[Unit]
Description=PaperStage web app
After=network.target

[Service]
WorkingDirectory=/home/ubuntu/clip-web
Environment=CLIP_PORT=8794
ExecStart=/usr/bin/python3 /home/ubuntu/clip-web/app.py
Restart=always
RestartSec=3
User=ubuntu

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now clip-web
curl http://127.0.0.1:8794/clip/health   # {"ok": true}
```

## Caddy (api.newkwlab.com site block)

Add next to the existing `/duo*` and `/book*` handles (keep the
BingSiteAuth.xml handle untouched):

```caddy
handle /clip* {
    reverse_proxy 127.0.0.1:8794
}
```

```bash
sudo caddy validate --config /etc/caddy/Caddyfile
sudo systemctl reload caddy
curl https://api.newkwlab.com/clip/health
curl https://api.newkwlab.com/clip/llms.txt
```

## Production flow (for reference)

1. `GET /clip/agent/paid` with header `X-Agent-Token: <agent_token>`
   → paid orders with variant + scene + names + photo URLs.
2. `GET /clip/agent/orders/<id>/photo/<n>` (same header) → photo n
   (1 = Person 1, 2 = Person 2 for Duo orders).
3. Produce the film per the paper-cutout pipeline (master cutout →
   A/C/B/C walk poses → scene stickers → jingle music box).
4. `POST /clip/agent/deliver/<id>` (same header), multipart form with
   `mp4` (the finished film) → order becomes `delivered`; buyers
   download from their order page link
   (`/clip/files/<id>/<delivery_token>/film.mp4`).

## Data hygiene

- Unpaid (`received`) orders auto-delete after 7 days; all order data
  after 60 days (same pattern as duo/book).
- Buyer photos live only inside `data/orders/<id>/` — the studio's
  working copy is deleted after delivery per the site privacy copy.
