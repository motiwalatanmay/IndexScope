# Google Sign-In setup (Google Identity Services) — matches SectorScope

IndexScope uses an in-page **"Sign in with Google"** in the header, exactly like
SectorScope. The dashboard stays publicly viewable; signing in shows the user's
name + avatar and persists for 30 days. It **reuses the same Google OAuth client
as SectorScope** (`575713517295-…`), so there's nothing to create — you just have
to authorise the new domain.

The code is already in `index.html` (search `GOOGLE_CLIENT_ID`). It activates
automatically on `indexscope.in` and is skipped on localhost.

## The one required step: authorise indexscope.in on the OAuth client

1. Go to **Google Cloud Console → APIs & Services → Credentials**:
   <https://console.cloud.google.com/apis/credentials>
   (use the same Google account / project where SectorScope's OAuth client lives.)
2. Click the **OAuth 2.0 Client ID** whose ID starts with **`575713517295-`**
   (the one SectorScope uses).
3. Under **Authorized JavaScript origins → + Add URI**, add **both**:
   ```
   https://indexscope.in
   https://www.indexscope.in
   ```
4. (Authorized redirect URIs are NOT needed for GSI — leave them as they are.)
5. **Save.** Changes can take a few minutes to propagate.

That's it — no new project, no Firebase, no billing card.

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
