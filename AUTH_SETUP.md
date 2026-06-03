# Google Sign-In setup (Google Identity Services) — matches SectorScope

IndexScope uses an in-page **"Sign in with Google"** in the header, exactly like
SectorScope. The dashboard stays publicly viewable; signing in shows the user's
name + avatar and persists for 30 days.

## Already set up (for reference)

- **Google Cloud project:** `indexscope` (owner `Motiwalatanmay0@gmail.com`).
  > Note: SectorScope's client belongs to a *different* owner (Prashant), so
  > IndexScope has its **own** project/client — they're independent.
- **OAuth client ID:** `583500951310-6dapvdgbe6je6k6mi87qn2jt3ori90cj.apps.googleusercontent.com`
  (hardcoded in `index.html` as `GOOGLE_CLIENT_ID`).
- **Authorized JavaScript origins:** `https://indexscope.in`, `https://www.indexscope.in`.
- **OAuth consent screen:** app name "IndexScope", External audience.

The login activates automatically on `indexscope.in` and is skipped on localhost.

## If you ever need to change the authorised origins
Google Cloud Console → **APIs & Services → Credentials** (project `indexscope`,
account `Motiwalatanmay0@gmail.com`) → open the client above → edit **Authorized
JavaScript origins**. Changes take a few minutes to propagate. No Firebase, no card.

## How it behaves
- Visitors see the dashboard normally, with a **"Sign in with Google"** button in
  the top-right header (plus a Google One-Tap prompt on first visit).
- After signing in, the header shows their **avatar + name**; click it to sign out.
- The session lasts **30 days** (stored in `localStorage` as `is_user`), so they
  stay signed in across visits.

## Optional: restrict who can sign in
In `index.html`, set:
```js
var AUTH_ALLOWLIST = ["motiwalatanmay0@gmail.com", "friend@gmail.com"]; // lowercase
```
Empty list = any Google account may sign in. (Note: this is a **client-side** check
— fine for a friendly gate, but not a hard security boundary, since the data files
are publicly served. The login here is for identity/UX, same as SectorScope.)

## Tracking who signs in
Because this is client-side Google Sign-In (no backend), there's no automatic
server-side list of who logged in. Options if you want that later:
- **Counts:** turn on free Cloudflare Web Analytics (the domain is on Cloudflare now).
- **Identities:** add a tiny `/signin` log endpoint to the `indexscope-live` worker
  that records `{email, time}` on each sign-in (small follow-up; needs a worker deploy).

## Turn it off
Set `var AUTH_ENABLED = false;` near `GOOGLE_CLIENT_ID` in `index.html`, or change
the hostname check. The dashboard then shows no sign-in control.
