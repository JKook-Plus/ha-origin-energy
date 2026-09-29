# Getting your Origin Energy session cookie

This integration does not automate Origin's password form. That one
endpoint (`POST /usernamepassword/login`) sits behind **Kasada** bot
defence, which replies to any non-browser client with a body-less
`HTTP 429` no matter what credentials, IP address or headers it sends.

Nothing else is protected. The silent authorization endpoint, the token
exchange and the whole GraphQL API all accept an ordinary HTTP client. So
you sign in once in a real browser — exactly what that endpoint exists to
allow — and hand the integration the resulting session cookie. It mints
its own short-lived API tokens from there.

## Steps

1. Open a normal browser window, go to <https://www.originenergy.com.au>
   and sign in.

   **Keep DevTools closed while you sign in.** Origin's bot protection
   detects an open DevTools panel and fails the login silently. Sign in
   first, then open DevTools.

2. Still in the same tab, navigate to:

   ```
   https://id.originenergy.com.au/.well-known/jwks.json
   ```

   You'll get a page of JSON — that's just Auth0's public signing keys,
   nothing sensitive. Its only purpose is to load a document from the
   `id.originenergy.com.au` origin.

   This step is necessary because DevTools only lists a cookie store for
   origins currently loaded in the tab. While you're on
   `www.originenergy.com.au`, the `id.` store isn't shown at all.

   > Don't try `https://id.originenergy.com.au/authorize` for this. With no
   > query parameters Auth0 rejects it and **redirects you back to `www`**
   > with `error=invalid_request`, so the tab ends up on the wrong origin
   > again and the cookie store still doesn't appear. `jwks.json` is a plain
   > static document that returns 200 and stays put.

3. Now open DevTools (<kbd>F12</kbd>) → **Application** tab (Chrome/Edge)
   or **Storage** tab (Firefox) → **Cookies** →
   `https://id.originenergy.com.au`.

4. Find the cookie named **`auth0`**. Click the row and copy the full
   value from the detail panel at the bottom — the Value column truncates
   it, and this value is long.

   As a sanity check, it starts with `s%3A` (a URL-encoded `s:`, Auth0's
   signed-cookie prefix) and contains a `.` separating the session id from
   its signature. Copy it exactly as shown, including the `%` escapes —
   don't URL-decode it.

   Take care not to grab **`auth0_compat`**, which sits directly next to it
   and looks nearly identical. That one is a legacy fallback for old
   browsers and isn't used here. Ignore `did`, `did_compat`, `KP_UIDz` and
   the rest.

   `auth0.is.authenticated`, over on the `www` store, is *not* it either —
   that's just a non-HttpOnly boolean flag the web app sets so it knows to
   attempt a silent refresh.

5. Paste that value into the integration when Home Assistant asks for it.

### If the cookie store still doesn't appear

Use the Network tab instead — it doesn't depend on the cookie tree at all.
HttpOnly cookies are hidden from JavaScript but appear plainly in request
headers.

1. Go back to your account dashboard on `www.originenergy.com.au`.
2. DevTools → **Network** → tick **Preserve log**.
3. Reload the page.
4. Filter for `id.originenergy.com.au`.
5. Click any `/authorize` request — the web app fires several silent ones
   on each page load.
6. **Headers → Request Headers → `Cookie`**.

Copy the part after `auth0=`, up to the `;`.

## How long it lasts

Origin issues the cookie with a rolling expiry roughly **100 days** out,
and Auth0 pushes that expiry forward every time the integration
successfully refreshes. The integration polls every 4 hours by default
(configurable from 1 to 24 hours), so the session should renew itself
indefinitely and you shouldn't need to repeat this.

If the session ever does lapse — you sign out everywhere, change your
password, or Origin invalidates sessions server-side — Home Assistant
raises a **Reauthentication required** notification. Repeat these steps
and paste the new value; no need to remove and re-add the integration, and
your statistics history is preserved.
