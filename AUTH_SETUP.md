# Google Sign-In setup (Firebase Auth)

The login gate is already coded in `index.html` but **inert** — the site behaves
exactly as before until you paste your Firebase config. Follow these steps once.

Total time: ~10 minutes. Cost: free (Firebase Spark plan).

## 1. Create a Firebase project
1. Go to <https://console.firebase.google.com> and sign in with your Google account.
2. **Add project** → name it `indexscope` (or reuse an existing project) → you can
   disable Google Analytics for the project, it's not needed → **Create**.

## 2. Register a Web app & copy the config
1. On the project overview, click the **`</>` (Web)** icon → register app
   (nickname `indexscope-web`; you do **not** need Firebase Hosting).
2. Firebase shows a `firebaseConfig = { ... }` block. Copy these four values:
   `apiKey`, `authDomain`, `projectId`, `appId`.
   > These are **not secrets** — they're safe to commit to a public repo. Security
   > comes from the authorized-domains list (step 4), not from hiding the key.

## 3. Turn on Google sign-in
1. Left menu → **Build → Authentication → Get started**.
2. **Sign-in method** tab → **Google** → toggle **Enable** → pick a support email → **Save**.

## 4. Authorize your domain
1. Authentication → **Settings → Authorized domains → Add domain**.
2. Add **`indexscope.in`**. (`localhost` and `*.firebaseapp.com` are already there,
   so local testing works too.)

## 5. Paste the config into the site
In `index.html`, find `var FIREBASE_CONFIG = {` (near the bottom, in the
"GOOGLE SIGN-IN" section) and fill in the four values:

```js
var FIREBASE_CONFIG = {
  apiKey: "AIza...your-key...",
  authDomain: "indexscope.firebaseapp.com",
  projectId: "indexscope",
  appId: "1:1234567890:web:abcdef..."
};
```

Commit & push. The login wall goes live on the next deploy. (`AUTH_ENABLED`
flips to true automatically once `apiKey` is non-empty.)

## How to TRACK who's using it
Firebase console → **Authentication → Users**. You get a live table of every
person who signed in: **email, display name, sign-up date, last sign-in date,
and the total count** at the top. That's your usage record — no extra code.

## How to CONTROL access
- **Block one person:** Authentication → Users → row menu → **Disable account**
  (or Delete). A disabled user can no longer sign in.
- **Pre-restrict to a known list:** in `index.html` set
  `var AUTH_ALLOWLIST = ["you@gmail.com", "friend@gmail.com"];` (lowercase).
  Anyone not on the list is signed out with a notice. Empty list = any Google
  account may sign in (default).

## Notes & limits
- **Soft gate.** This gates the *UI*. The underlying `data/*.json` files are still
  public URLs on GitHub Pages, so a technical user could fetch them directly. For
  most purposes (knowing your audience + a block switch) this is what you want. If
  you ever need *hard* gating, the data has to move behind the Cloudflare Worker
  with token verification — a larger change.
- **Consent screen.** Firebase auto-creates an OAuth consent screen using your
  project name. With only basic email/profile scopes, sign-in works immediately;
  some users may see an "unverified app" notice until you complete Google's app
  verification (optional for a small audience).
- **To turn the gate OFF again:** blank out `apiKey` in `FIREBASE_CONFIG` and push.
