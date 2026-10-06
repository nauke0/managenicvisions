# Client moodboards

A private site that shows each client their moodboard and lets them confirm
they're happy with it. Nothing here is public:

- **Every visit needs an email code.** Cloudflare Access sits in front of the
  whole site. A visitor enters their email and gets a one-time code.
- **Each client sees only their own board.** The Worker (`src/worker.js`)
  checks the signed-in email against the board's client email. Nicole's
  address (`ADMIN_EMAILS`) can open every board.
- **Links are unguessable.** Each board lives at `/m/<random token>/`.
- **Data stays private.** Boards are stored in this private repo and served
  only through the Worker's checks; no file is reachable directly.

## How a moodboard gets to a client

1. In Moodboard Studio, Nicole presses **Send to client**. Gmail opens with the
   email ready, and the Studio records *Last sent*.
2. The moodboard helper session copies the board into `public/data/<token>/`
   (using `publish.py`) and pushes to `main`.
3. The **Deploy client moodboards** workflow publishes the site to Cloudflare.
4. The client opens the link, signs in with the emailed code, looks through
   the board and ticks **I'm happy with it**. The Worker saves that in
   `confirmations/<token>.json`, and the helper shows it in the Studio.

## One-time setup

### 1. Let GitHub deploy to Cloudflare

1. In the Cloudflare dashboard, open **Manage account → Account API tokens**
   and create a token from the **Edit Cloudflare Workers** template.
2. Copy your **Account ID** (Workers & Pages overview, right-hand side).
3. In this GitHub repo, open **Settings → Secrets and variables → Actions** and
   add `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ACCOUNT_ID`.
4. Run **Actions → Deploy client moodboards → Run workflow**. The site appears
   at `https://nicvisions-moodboards.<your-subdomain>.workers.dev`.

### 2. Turn on email codes (Cloudflare Access)

1. In Cloudflare, open **Zero Trust** (free for up to 50 users) and pick a team
   name, e.g. `nicvisions`. Your team domain is
   `nicvisions.cloudflareaccess.com`.
2. **Settings → Authentication**: make sure **One-time PIN** is enabled.
3. In **Workers & Pages → nicvisions-moodboards → Settings → Domains & Routes**,
   turn on **Cloudflare Access** for the `workers.dev` address. (Or add an
   Access application for that hostname under **Access → Applications** with a
   policy that allows **Everyone** to sign in with One-time PIN. The Worker
   decides who sees which board.)
4. Copy the application's **Audience (AUD) tag** and your team domain into
   `wrangler.jsonc` (`ACCESS_AUD`, `TEAM_DOMAIN`), or send them to Claude to do it.
   Until both are set, the site refuses every visitor.

### 3. Let the site save confirmations

1. On GitHub, create a fine-grained personal access token with access to only
   `nauke0/managenicvisions` and **Contents: Read and write**.
2. Add it to this repo's Actions secrets as `MOODBOARD_GITHUB_TOKEN`, then run
   the deploy workflow again.

### 4. Connect the Studio

Tell Claude the site address from step 1; it is saved in the Studio so
**Send to client** can build each client's link.
