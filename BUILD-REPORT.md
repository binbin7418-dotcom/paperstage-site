# PaperStage — build report (local build, NOT deployed)

Built 2026-11-06 by mirroring the Starlight Stories implementation
(`~/workspace/storybook-site/`): stdlib-only Python, JSON order store,
Gumroad license verify fail-closed, agent-token production API,
hand-rolled multipart. Base path `/clip/`, port **8794** (`CLIP_PORT`).

## What was built

- [web/app.py](sandbox://workspace/paperstage-site/web/app.py) — server:
  static site + media (incl. subdirectories and MP4), `POST /clip/api/orders`
  (multipart; variant solo/duo, scene enum of 6, 1 photo for solo /
  2 photos for duo enforced server-side, jpg/png ≤10 MB each),
  `GET /clip/api/orders/<id>`, `POST …/activate` (fail-closed Gumroad
  verify, one key = one film, only last-4 stored), token delivery
  download at `/clip/files/<id>/<token>/film.mp4`, production API
  (`GET /clip/agent/paid` incl. variant + scene + photo URLs,
  `GET /clip/agent/orders/<id>/photo/<n>`,
  `POST /clip/agent/deliver/<id>` multipart `mp4` field → delivered),
  health, config, media path-traversal guard.
- [web/site/index.html](sandbox://workspace/paperstage-site/web/site/index.html)
  — single-page storefront + order desk in the family design system
  (cream paper, pill nav, lights strip, snowfall) with a more editorial
  register: 88 px serif hero ("Step into a *paper-cut* Christmas."),
  autoplay looping Duo film in the hero with poster, Solo sample
  section with the craft story, six-city gallery (plates are the hero
  visual; clicking a city also sets the order form's scene select),
  What-you-get cards, 4-step How-it-works, photo tips + privacy,
  order form (license key panel → variant cards Solo $12.99 / Duo
  $19.99 → scene select → names → email → uploads labelled Person 1 /
  Person 2, second upload appears only for Duo → note), order-status
  view with token MP4 download.
- [web/site/llms.txt](sandbox://workspace/paperstage-site/web/site/llms.txt)
  — Qiaomu pattern: product facts (prices, 6 scenes, format), agent
  steps, human-only steps (Gumroad payment, photo choice/permission),
  safety lines (key never stored beyond last 4, photos private and
  deleted after delivery, never invent IDs/keys/status).
- Configs carry the **PENDING placeholders** as briefed:
  [web/clip_config.json](sandbox://workspace/paperstage-site/web/clip_config.json)
  (`gumroad_product_id` / `gumroad_buy_url` = `"PENDING"`, fresh random
  `agent_token` generated) and
  [clip_config.example.json](sandbox://workspace/paperstage-site/clip_config.example.json).
- [deploy/README.md](sandbox://workspace/paperstage-site/deploy/README.md)
  — EC2 wiring: `/home/ubuntu/clip-web/`, `clip-web.service`
  (`CLIP_PORT=8794`), Caddy `handle /clip*` → 127.0.0.1:8794 next to
  `/duo*` and `/book*`, production flow, data hygiene.

## Media

- `site/media/duo-sample.mp4` — verbatim copy of the trial-v2 Duo film
  (10.0 s, 1080×1920, h264 + AAC); `duo-poster.jpg` extracted at 6.4 s
  (both walkers mid-stride — verified visually).
- `site/media/solo-sample.mp4` — **built for this site, zero new image
  generations**: HER's existing pose cutouts from the trial
  (`herMaster/PoseA/PoseB/PoseC`) recomposed with
  [work/compose_solo.py](sandbox://workspace/paperstage-site/work/compose_solo.py)
  over the London Snow sticker set using the exact plate layout from
  `paperstage-scenes/SCENES.md` (townhouses/clocktower/bus/taxi/tubesign
  pop in staggered, A-C-B-C walk cycle, paper snowflakes + animated
  snowfall, "MERRY CHRISTMAS" cut-letter tiles), with the same
  music-box jingle track (trimmed to 7 s, fade-out). Result: 7.0 s,
  1080×1920, h264 + AAC, 2.5 MB. A mid-walk frame was inspected: she
  reads clearly mid-stride on the zebra crossing in front of the bus.
- `site/media/scenes/*.jpg` — the six plates copied from
  `~/workspace/paperstage-scenes/*/scene-*-plate.jpg`.

## Smoke test — all 28 checks passed (local port 8794)

Script: [work/smoke_test.sh](sandbox://workspace/paperstage-site/work/smoke_test.sh).
Landing / llms.txt / health / config / hero film / poster / solo sample /
all six scene images → 200; media traversal → 404. Solo order created →
`received`; **Duo with a missing second photo → 400**; Duo with both
photos created; invalid scene → 400. **Fake license key rejected 403 —
fail-closed, as expected**: the config's product ID is the `PENDING`
placeholder, so Gumroad verification cannot succeed; this is the
designed pre-launch state, not a defect. Agent endpoints → 403 without
token; photo 1/2 → 200 with token, photo 3 → 404. Deliver flow →
`delivered`; status returns `film_url`; download 200 with the delivery
token, 403 with a wrong one. Server stopped and test data purged
afterwards.

One consequence of fail-closed activation: no order can reach `paid`
until a real Gumroad product exists, so `/clip/agent/paid` returned an
empty list in the smoke run (the deliver path was exercised directly on
a received order, same as the book app allows).

## Screenshot review

[shots/desktop-top.png](sandbox://workspace/paperstage-site/shots/desktop-top.png) ·
[shots/mobile-top.png](sandbox://workspace/paperstage-site/shots/mobile-top.png) ·
[shots/desktop-cities.png](sandbox://workspace/paperstage-site/shots/desktop-cities.png) ·
[shots/desktop-order.png](sandbox://workspace/paperstage-site/shots/desktop-order.png) ·
[shots/desktop-full.png](sandbox://workspace/paperstage-site/shots/desktop-full.png)

Hero (desktop + mobile), gallery, what-you-get cards, tips, license
panel, variant cards, scene select and upload boxes all render cleanly;
no fix round was needed. Capture note: this VM's headless Chromium
freezes CSS animations at t≈0 in one-shot screenshots, so captures were
taken with a temporary animation-off override style that was removed
immediately after (verified absent from the shipped file and the served
page). In a real browser the entrance animations play and settle
normally (same CSS as the live book site). The hero video likewise
shows an early frame in stills; it autoplays in real browsers and the
poster covers pre-play.

## Open items

1. **Gumroad product not yet created** — deploy is blocked on filling
   `gumroad_product_id` / `gumroad_buy_url` in `clip_config.json`
   (Solo $12.99 / Duo $19.99, license keys enabled). Until then the buy
   button shows a "checkout opens at launch" note and activation stays
   fail-closed.
2. **Deploy when approved** — follow deploy/README.md (new systemd
   unit + one Caddy handle; nothing existing is touched).
3. The site sends no email by design (status page + producer-sent
   delivery email), same as duo/book.
4. `work/` holds the solo-sample compose script and the smoke script
   (production tooling, not part of the deploy payload — the payload is
   `web/` + `clip_config.example.json` + `deploy/`).
