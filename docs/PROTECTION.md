# Site access

Ordinary HTTP, configured mirrors and account sign-in work without an external service. The optional access layer
is disabled when `protection` is omitted. It does not install a browser, change your system proxy or download media.

## Connecting an optional service

1. Run a trusted [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr),
   [Byparr](https://github.com/ThePhaseless/Byparr), [FlareBypasser](https://github.com/yoori/flare-bypasser) or
   [Trawl](https://github.com/germondai/trawl) service separately. Keep its API private and isolate browser egress
   from LAN, link-local and cloud metadata destinations. The browser is outside TOW's network sandbox.
2. On the TOW computer, open the local web address → Settings → Site access → Add a connection. Choose its type, enter
   its loopback/private IP address and choose its order. Only `/v1` is accepted; localhost becomes 127.0.0.1.
3. Enable that connection and the overall access layer, then Save. Saving does not contact or validate the service;
   the API remains unchecked until a real applying check has a reason to use it. An API answer is not proof of site
   access; its observed status and any service cooldown are shown separately.

Multiple connections of one kind are supported. Lower order numbers run first. A missing module, disabled
connection, refused response or failed attempt does not prevent another enabled connection from being considered
within the same finite attempt/time budget. Nothing promises successful access to every protection scheme.
If that budget's attempt limit is reached without verified access, the next eligible check continues after the
last attempted connection, so later connections are not permanently starved. A configuration edit resets this
order, not the retry interval. Missing modules and services in an API cooldown do not consume an attempt slot.

Select FlareSolverr for a compatible `/v1` implementation, including a separately operated nodriver-based fork;
the library nodriver itself is not an HTTP service. FlareBypasser uses `request.get_cookies`; Trawl uses
`request.get` with `returnOnlyCookies`. These protocols are checked against upstream source, not assumed from the
name of their browser engine. TOW neither imports nor bundles nodriver, zendriver or Camoufox. Pin and review the
external service's version and its browser dependencies; a compatible envelope does not prove challenge success.

On Sites → the site's advanced options, **Automatic** permits the optional layer; **Off** disables it for that site.
**Sign in through the browser** uses the existing explicit browser-auth flow instead: external providers do not
replace that session's User-Agent. It is not an unattended browser solver.

## What a check does

1. Ask ordinary mirrors, with any still-valid previously verified access for the exact origin.
2. Only when ordinary mirrors failed with a detected Cloudflare browser challenge, try enabled providers in order.
3. Send the provider only the public origin's root URL: never a password, account cookie, topic, passkey or download.
4. Validate the bounded response and replay that root from TOW using the returned clearance and matching User-Agent.
5. Fetch the requested resource through TOW's existing guarded HTTP, with normal account sign-in if needed.

Cloudflare's [`cf-mitigated: challenge`](https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/detect-response/)
header is definitive evidence. Known check-page titles with their challenge status are a fallback; an ordinary
Cloudflare proxy, embedded script or forum quotation is not. Access bans, rate limits, origin outages, maintenance,
quota and account sign-in are separate reasons, not instructions to call a solver. Preview and diagnostics never
invoke one. Cross-origin download redirects use a fresh cookie-free client; parent-domain metadata cannot grant
cross-origin credential reuse.

An encrypted `data/protection.enc` stores verified clearance, matching User-Agent, expiry and durable cooldown leases.
Defaults: 30 seconds for the recovery chain, at most 2 available providers, 1,800 seconds before another attempt.
The lease is written before calling the service, so a crash does not cause an attempt on every tick. A configuration
change invalidates the old clearance but does not erase cooldowns. Disabling the layer immediately prevents its use
in subsequent requests; an already issued request cannot be recalled.

Connection failures and API-level busy/protocol responses pause only that service across sites: exponential
backoff starts at 30 seconds and caps at 1,800 seconds; a valid `Retry-After` may extend it up to 24 hours. A site's
refused replay, slow challenge or HTTP 500 is not enough to disable the service globally. Renaming or reordering a
connection cannot erase this cooldown. Each persisted attempt has its own identity: responses from before a reset
or a newer reservation cannot repopulate the cache, revoke a new credential or restore an old service cooldown.
Encrypted format v2 reads v1 without rewriting it on inspection; a new write upgrades only this optional cache.
Failures older than 24 hours no longer raise the next day's first error to maximum backoff. The solver's share of
the chain budget leaves up to 10 seconds (one third for smaller budgets) for independent verification. A transient
root transport error or origin outage preserves verified access and does not justify another solve attempt.

If that store is unreadable, ordinary HTTP remains available and the damaged file is not silently replaced. On the
TOW computer, Settings → Site access → Recovery → **Reset saved access** explicitly moves the old encrypted bytes
to TOW's private temporary folder, until normal cleanup. It does not remove site passwords or service settings;
reset is refused while an applying check is active. The same action resets saved service cooldowns.

## Current revision without another download

A site may expose `infohash_path` with `{id}`. Kinozal's preset supplies its service-information page. Only a unique
labelled current hash is accepted. If it matches the stored revision, the client must independently confirm the
owned torrent, path and verified selection before TOW skips downloading its `.torrent`. Progress reconciliation
still runs. Unknown format or a missing optional endpoint falls back to the normal download; a host-level failure
is reported without repeating the same failing requests. A changed rule, missing/foreign/pending task or changed
hash takes the full client-confirmed path. This is not a quota bypass or proof from a cached old torrent.

## Limits and troubleshooting

- A service's `status: ok` is not proof; only TOW's independent replay can verify access.
- Root-only recovery cannot solve a challenge restricted to one particular download path. An external service may
  attempt an interactive challenge, but TOW does not promise to complete it; the owner's action may still be needed.
  Browser/TLS fingerprints or a different public IP can make replay fail safely.
- Cloudflare's [Precursor](https://developers.cloudflare.com/cloudflare-challenges/precursor/) may re-evaluate an
  ongoing session and invalidate its clearance. A successful home page is not proof that another path works or
  that the same cookies work in an independent HTTP client. TOW checks the real resource and logs a refused replay;
  it does not label the API's response as restored site access.
- TOW bounds socket operations and response processing; synchronous OS DNS cannot be interrupted. A late answer
  is refused before use, but a stalled OS resolver can exceed the nominal wall-clock budget.
- Tracker HTTP advertises only gzip, deflate and identity. Compressed wire bytes, each decoding layer and the final
  body share bounded size limits; invalid/truncated or unsupported coding is refused, not decoded without a bound.
  Empty responses, including redirects and HEAD, remain supported. Nonempty compressed streams with bad CRC,
  truncation or trailing garbage are intentionally refused even when a more tolerant client would accept them.
- A malformed settings file remains a configuration error. An unavailable configured service does not prevent
  ordinary tracker operations. In-process extensions are trusted code, not sandboxed.
- The last observed mirror response is not proof that every endpoint on that mirror works. A healthy front page
  can coexist with a broken download endpoint; read the topic's actual error.

Contracts and extension tests: [EXTENDING.md](EXTENDING.md#site-access-provider). Security boundary:
[SECURITY.md](../SECURITY.md).
