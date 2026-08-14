/**
 * Account link: PKCE authorization-code client for hosted VMR capture.
 *
 * WHAT THIS REPLACES. Hosted capture used to require the operator to paste a
 * `vmrx1.<key_id>.<secret>` shared credential and a backend URL, and to do it
 * again after every Chrome restart. Nobody types anything now: the extension
 * links itself to the VMR Outbound account the operator is already signed in to,
 * and the link survives a restart.
 *
 * THE FLOW (first-party, no third-party identity provider):
 *
 *   1. `code_verifier` = base64url(32 random bytes);
 *      `code_challenge` = base64url(sha256(code_verifier)) via `crypto.subtle`.
 *   2. `chrome.identity.launchWebAuthFlow` opens
 *      `{backend}/extension/authorize?…` and waits for the app to redirect back
 *      to `https://<extension id>.chromiumapp.org/`.
 *      - already signed in AND already linked -> the app redirects immediately,
 *        so `{interactive:false}` connects with no window and no click;
 *      - signed in but not yet linked -> a consent page;
 *      - signed out -> the app's own sign-in, which is the ONE action a new
 *        operator ever takes.
 *   3. `POST {backend}/extension/token` exchanges the code (plus the verifier,
 *      the extension id and the installation id) for the token pair.
 *
 * WHAT IS HELD, AND WHERE:
 *
 *   installation id  local    non-secret, stable per install
 *   refresh token    local    `vmrr1.…`, ~30 days, ROTATES ON EVERY USE
 *   access token     session  `vmre1.…`, ~15 minutes, memory only
 *   account email    local    shown in the panel so the operator can see WHICH
 *                             account captures land in
 *
 * The refresh token is deliberately persisted: it is the entire reason a
 * restart needs no human. It is not a shared secret — it belongs to one install,
 * the server replaces it on every use, and replaying an old one revokes the link
 * rather than working twice. Whatever the server returns as the new refresh
 * token is written back before the caller is told the refresh succeeded; losing
 * that write is the one bug that would strand an install.
 *
 * Nothing here is ever handed to the panel: `state()` reports connected-or-not
 * and the account email, never a token.
 *
 * Every browser edge (chrome.*, crypto, fetch, clock) is injected, so the whole
 * flow is exercisable in `test/account-linking.test.js` without a browser.
 *
 * UMD module -> Node CommonJS + self.SNCapture.accountLink
 */
(function (root, factory) {
  const g = typeof self !== "undefined" ? self : root;
  const isNode = typeof module !== "undefined" && module.exports;
  const mod = factory(
    isNode ? require("./constants.js") : g.SNCapture.constants,
    isNode ? require("./permissions.js") : g.SNCapture.permissions
  );
  if (isNode) module.exports = mod;
  g.SNCapture = Object.assign(g.SNCapture || {}, { accountLink: mod });
})(typeof globalThis !== "undefined" ? globalThis : this, function (constants, permissions) {
  "use strict";

  const { ACCOUNT_STORAGE, ACCOUNT_LINK, ACCOUNT_LINK_PATHS } = constants;

  // The token endpoints answer quickly or not at all; a hung request must not
  // hold a capture open forever.
  const TOKEN_TIMEOUT_MS = 15000;

  // How long a failed SILENT connect suppresses the next one.
  //
  // Opening the panel asks several questions at once (link state, backend probe,
  // labels, campaigns), and every one of them would otherwise run its own
  // `launchWebAuthFlow` on a signed-out browser. One attempt per minute is
  // plenty to notice that the operator has signed in elsewhere, and it keeps a
  // signed-out panel from launching a burst of hidden auth windows. An
  // INTERACTIVE sign-in — something the operator asked for — is never suppressed.
  const SILENT_RETRY_COOLDOWN_MS = 60000;

  const B64URL_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_";

  /**
   * base64url of raw bytes, without padding.
   *
   * Written out rather than reaching for `btoa` so the same code runs in the
   * service worker and in a bare Node test context, and so nothing depends on a
   * global that a future MV3 change might not provide.
   */
  function base64Url(bytes) {
    let out = "";
    for (let i = 0; i < bytes.length; i += 3) {
      const b0 = bytes[i];
      const b1 = i + 1 < bytes.length ? bytes[i + 1] : null;
      const b2 = i + 2 < bytes.length ? bytes[i + 2] : null;
      out += B64URL_ALPHABET[b0 >> 2];
      out += B64URL_ALPHABET[((b0 & 3) << 4) | (b1 === null ? 0 : b1 >> 4)];
      if (b1 === null) break;
      out += B64URL_ALPHABET[((b1 & 15) << 2) | (b2 === null ? 0 : b2 >> 6)];
      if (b2 === null) break;
      out += B64URL_ALPHABET[b2 & 63];
    }
    return out;
  }

  /** The session id half of `vmre1.<session id>.<secret>`. Never throws. */
  function sessionIdOf(token) {
    if (typeof token !== "string") return null;
    const parts = token.split(".");
    if (parts.length !== 3) return null;
    return parts[1] || null;
  }

  /**
   * The `code`/`state`/`error` a redirect carried.
   *
   * Hostile or truncated input yields empty fields rather than throwing: this
   * runs on whatever the browser handed back, and a parse failure must read as
   * "no authorization", never as a crash in the service worker.
   */
  function parseRedirect(redirectUrl) {
    const empty = { code: null, state: null, error: null };
    if (typeof redirectUrl !== "string" || !redirectUrl) return empty;
    let url;
    try {
      url = new URL(redirectUrl);
    } catch (_e) {
      return empty;
    }
    const search = url.searchParams;
    const hash = new URLSearchParams((url.hash || "").replace(/^#/, ""));
    const pick = (key) => search.get(key) || hash.get(key) || null;
    return { code: pick("code"), state: pick("state"), error: pick("error") };
  }

  /**
   * @param {object} env
   *   chrome           the extension APIs (storage, runtime, identity)
   *   crypto           WebCrypto (getRandomValues, randomUUID, subtle)
   *   fetch            fetch implementation
   *   backendBaseUrl   async () => configured backend base URL
   *   now              () => epoch ms (injectable for expiry tests)
   */
  function createAccountLink(env) {
    const chromeApi = env.chrome;
    const cryptoApi = env.crypto;
    const fetchImpl = env.fetch;
    const now = env.now || (() => Date.now());
    const readBackendBaseUrl = env.backendBaseUrl;

    // Fallback for a browser with no `chrome.storage.session`: the access token
    // then lives only in this worker's memory, which is strictly safer and
    // merely means one extra refresh after a worker restart.
    let memoryAccess = null;
    // When the last silent connect gave up, so a signed-out panel does not open
    // one hidden auth window per question it asks.
    let silentFailedAt = 0;

    const local = () => chromeApi.storage.local;
    const sessionArea = () =>
      (chromeApi.storage && chromeApi.storage.session) || null;

    // ---- storage ------------------------------------------------------------

    async function getInstallationId() {
      try {
        const data = await local().get(ACCOUNT_STORAGE.INSTALLATION_ID);
        const stored = data && data[ACCOUNT_STORAGE.INSTALLATION_ID];
        if (typeof stored === "string" && stored.length >= 16) return stored;
      } catch (_e) {
        /* fall through and mint one */
      }
      const minted = cryptoApi.randomUUID();
      try {
        await local().set({ [ACCOUNT_STORAGE.INSTALLATION_ID]: minted });
      } catch (_e) {
        /* a storage failure must not stop a sign-in from being attempted */
      }
      return minted;
    }

    async function readLink() {
      try {
        const data = await local().get(ACCOUNT_STORAGE.ACCOUNT_LINK);
        const rec = data && data[ACCOUNT_STORAGE.ACCOUNT_LINK];
        if (!rec || typeof rec !== "object") return null;
        if (typeof rec.refreshToken !== "string" || !rec.refreshToken) return null;
        return rec;
      } catch (_e) {
        return null;
      }
    }

    async function writeLink(rec) {
      await local().set({ [ACCOUNT_STORAGE.ACCOUNT_LINK]: rec });
      return rec;
    }

    async function readAccess() {
      const area = sessionArea();
      if (!area) return memoryAccess;
      try {
        const data = await area.get(ACCOUNT_STORAGE.ACCESS_TOKEN);
        const rec = data && data[ACCOUNT_STORAGE.ACCESS_TOKEN];
        return rec && typeof rec.accessToken === "string" ? rec : null;
      } catch (_e) {
        return null;
      }
    }

    async function writeAccess(accessToken, expiresAt) {
      const rec = { accessToken, expiresAt };
      const area = sessionArea();
      memoryAccess = rec;
      if (area) await area.set({ [ACCOUNT_STORAGE.ACCESS_TOKEN]: rec });
      return rec;
    }

    /** Forget everything about the link, locally. Never throws. */
    async function forget() {
      memoryAccess = null;
      try {
        await local().remove(ACCOUNT_STORAGE.ACCOUNT_LINK);
      } catch (_e) {
        /* nothing to do */
      }
      const area = sessionArea();
      if (area) {
        try {
          await area.remove(ACCOUNT_STORAGE.ACCESS_TOKEN);
        } catch (_e) {
          /* nothing to do */
        }
      }
    }

    // ---- the backend this install talks to ----------------------------------

    /**
     * The hosted base URL, or null when the configured backend is not one of the
     * named hosted deployments.
     *
     * A loopback backend is a development configuration with no authenticated
     * intake, and account linking has nothing to do there — so the flow refuses
     * rather than opening a sign-in window against a server that cannot serve it.
     */
    async function hostedBase() {
      let configured;
      try {
        configured = await readBackendBaseUrl();
      } catch (_e) {
        return null;
      }
      const base = String(configured || "").replace(/\/$/, "");
      if (!base) return null;
      return permissions.isHostedUrl(base + "/") ? base : null;
    }

    // ---- PKCE ---------------------------------------------------------------

    function randomSecret() {
      const bytes = new Uint8Array(32);
      cryptoApi.getRandomValues(bytes);
      return base64Url(bytes); // 43 chars, matching the server's expectation
    }

    async function challengeFor(verifier) {
      const digest = await cryptoApi.subtle.digest(
        "SHA-256",
        new TextEncoder().encode(verifier)
      );
      return base64Url(new Uint8Array(digest));
    }

    // ---- token endpoint -----------------------------------------------------

    /**
     * Persist a token response.
     *
     * The rotated refresh token is written BEFORE success is reported. If the
     * server rotated and we failed to keep the new value, the install would be
     * holding a dead token and would be stranded — this is the one write whose
     * ordering actually matters.
     */
    async function persistTokens(data, previous) {
      const accessToken = typeof data.access_token === "string" ? data.access_token : "";
      const rotated = typeof data.refresh_token === "string" ? data.refresh_token : "";
      const refreshToken = rotated || (previous && previous.refreshToken) || "";
      if (!accessToken || !refreshToken) {
        return { ok: false, error: "token_response_invalid" };
      }
      const expiresIn = Number(data.expires_in);
      const ttl =
        Number.isFinite(expiresIn) && expiresIn > 0
          ? expiresIn
          : ACCOUNT_LINK.FALLBACK_ACCESS_TTL_SECONDS;
      const account = data.account && typeof data.account === "object" ? data.account : {};
      const accountEmail =
        typeof account.email === "string" && account.email
          ? account.email
          : (previous && previous.accountEmail) || null;

      await writeLink({
        sessionId: sessionIdOf(accessToken),
        refreshToken,
        accountEmail,
        scope: typeof data.scope === "string" ? data.scope : ACCOUNT_LINK.SCOPE,
        linkedAt: (previous && previous.linkedAt) || new Date(now()).toISOString(),
        refreshedAt: new Date(now()).toISOString(),
      });
      await writeAccess(accessToken, now() + ttl * 1000);
      return { ok: true, accessToken, accountEmail };
    }

    async function postToken(body, previous) {
      const base = await hostedBase();
      if (!base) return { ok: false, error: "account_link_not_hosted" };
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), TOKEN_TIMEOUT_MS);
      let resp;
      try {
        resp = await fetchImpl(base + ACCOUNT_LINK_PATHS.TOKEN, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
          // The code / refresh token plus the extension origin authorise this
          // call. An ambient session cookie must never be what makes it work.
          credentials: "omit",
          signal: controller.signal,
        });
      } catch (e) {
        clearTimeout(timer);
        return {
          ok: false,
          error: e && e.name === "AbortError" ? "timeout" : "network_error",
        };
      }
      clearTimeout(timer);
      if (!resp.ok) {
        // The server answers every refusal identically on purpose; the only
        // thing worth distinguishing here is "this grant is dead" (4xx) from
        // "the server is having a problem" (5xx), because only the first means
        // the stored link should be dropped.
        return {
          ok: false,
          error: resp.status >= 400 && resp.status < 500 ? "invalid_grant" : "token_endpoint_error",
          status: resp.status,
        };
      }
      let data;
      try {
        data = await resp.json();
      } catch (_e) {
        return { ok: false, error: "token_response_invalid" };
      }
      return persistTokens(data || {}, previous);
    }

    // ---- the public surface -------------------------------------------------

    /**
     * Link this install to the signed-in VMR Outbound account.
     *
     * `{interactive:false}` is the silent path: it succeeds only when the app can
     * answer without showing anything, which is exactly the "already signed in,
     * already linked" case. Everything else needs `{interactive:true}`, which is
     * the single "Sign in to VMR Outbound" action.
     */
    async function connect(options) {
      const interactive = !!(options && options.interactive);
      const identity = chromeApi.identity;
      if (!identity || typeof identity.launchWebAuthFlow !== "function") {
        return { ok: false, error: "identity_unavailable" };
      }
      if (!interactive && silentFailedAt && now() - silentFailedAt < SILENT_RETRY_COOLDOWN_MS) {
        return { ok: false, error: "account_link_required" };
      }
      const base = await hostedBase();
      if (!base) return { ok: false, error: "account_link_not_hosted" };

      const extensionId = (chromeApi.runtime && chromeApi.runtime.id) || "";
      const installationId = await getInstallationId();
      const verifier = randomSecret();
      const challenge = await challengeFor(verifier);
      const state = randomSecret();
      const redirectUri = identity.getRedirectURL();

      const query = new URLSearchParams({
        extension_id: extensionId,
        installation_id: installationId,
        code_challenge: challenge,
        code_challenge_method: "S256",
        state,
        redirect_uri: redirectUri,
      });

      let redirect;
      try {
        redirect = await identity.launchWebAuthFlow({
          url: base + ACCOUNT_LINK_PATHS.AUTHORIZE + "?" + query.toString(),
          interactive,
        });
      } catch (_e) {
        // A silent attempt that could not complete without UI is the normal,
        // expected answer for "not linked yet" — not an error to shout about.
        if (!interactive) silentFailedAt = now();
        return {
          ok: false,
          error: interactive ? "sign_in_failed" : "account_link_required",
        };
      }

      const parsed = parseRedirect(redirect);
      if (parsed.error || !parsed.code) {
        if (!interactive) silentFailedAt = now();
        return { ok: false, error: interactive ? "sign_in_failed" : "account_link_required" };
      }
      // A redirect whose state is not the one just minted is not this flow's
      // answer, so its code is not exchanged.
      if (parsed.state !== state) return { ok: false, error: "state_mismatch" };

      const exchanged = await postToken(
        {
          grant_type: "authorization_code",
          code: parsed.code,
          code_verifier: verifier,
          extension_id: extensionId,
          installation_id: installationId,
        },
        null
      );
      if (exchanged.ok) silentFailedAt = 0;
      else if (!interactive) silentFailedAt = now();
      return exchanged;
    }

    /**
     * Trade the stored refresh token for a fresh pair.
     *
     * A 4xx means the grant is dead — revoked, expired, or already rotated — so
     * the local link is dropped rather than retried forever. A 5xx or a network
     * failure keeps it: the operator's link is fine, the server is not.
     */
    async function refresh() {
      const link = await readLink();
      if (!link) return { ok: false, error: "account_link_required" };
      const result = await postToken(
        {
          grant_type: "refresh_token",
          refresh_token: link.refreshToken,
          extension_id: (chromeApi.runtime && chromeApi.runtime.id) || "",
          installation_id: await getInstallationId(),
        },
        link
      );
      if (!result.ok && result.error === "invalid_grant") await forget();
      return result;
    }

    /**
     * A usable access token, or the reason there is none.
     *
     * Cached token -> refresh -> silent connect -> ask for a sign-in. Only the
     * last step is visible to the operator, and only when nothing else worked.
     */
    async function ensureAccessToken() {
      const cached = await readAccess();
      if (
        cached &&
        cached.accessToken &&
        Number(cached.expiresAt) - now() > ACCOUNT_LINK.MIN_ACCESS_REMAINING_MS
      ) {
        return { ok: true, accessToken: cached.accessToken };
      }
      const link = await readLink();
      if (link) {
        const refreshed = await refresh();
        if (refreshed.ok) return refreshed;
        // A server-side or transport failure is not a reason to send the
        // operator through a sign-in window; say what happened instead.
        if (refreshed.error !== "account_link_required" && refreshed.error !== "invalid_grant") {
          return { ok: false, error: refreshed.error, status: refreshed.status };
        }
      }
      const silent = await connect({ interactive: false });
      if (silent.ok) return silent;
      return { ok: false, error: "account_link_required" };
    }

    /**
     * Revoke the link server-side, then forget it locally.
     *
     * The local state is cleared even when the server cannot be reached: the
     * operator asked to disconnect, and leaving a usable refresh token behind
     * because a request failed would be the opposite of what they asked for.
     * The server-side session then simply expires.
     */
    async function disconnect() {
      const base = await hostedBase();
      const token = await ensureAccessToken();
      if (base && token.ok) {
        try {
          await fetchImpl(base + ACCOUNT_LINK_PATHS.REVOKE, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              Authorization: "Bearer " + token.accessToken,
            },
            body: "{}",
            credentials: "omit",
          });
        } catch (_e) {
          /* revoking is best-effort; the local link goes regardless */
        }
      }
      await forget();
      return { ok: true, connected: false };
    }

    /**
     * What the panel is allowed to know: whether this install is linked and to
     * which account. Never a token, never the installation id's secret half —
     * there isn't one.
     */
    async function state() {
      const link = await readLink();
      const access = await readAccess();
      return {
        connected: !!link,
        accountEmail: (link && link.accountEmail) || null,
        scope: (link && link.scope) || null,
        hasAccessToken: !!(access && access.accessToken && Number(access.expiresAt) > now()),
      };
    }

    /**
     * The panel's "open the panel and just be connected" call: report the link,
     * attempting a silent connect first when there is nothing stored yet.
     */
    async function ensureConnected() {
      const current = await state();
      if (current.connected) return { ok: true, account: current };
      const silent = await connect({ interactive: false });
      const next = await state();
      return {
        ok: true,
        account: next,
        attempted: true,
        reason: silent.ok ? null : silent.error,
      };
    }

    return {
      connect,
      refresh,
      ensureAccessToken,
      ensureConnected,
      disconnect,
      state,
      getInstallationId,
    };
  }

  return { createAccountLink, base64Url, parseRedirect, sessionIdOf };
});
