# Site access

Ordinary HTTP, configured mirrors and account sign-in work without an external service. The optional access layer
is disabled when `protection` is omitted. It does not install a browser, change your system proxy or download media.

## Connecting an optional service

Use this only when ordinary mirrors are blocked by a Cloudflare browser challenge. A working site does not
need an additional connection. Settings → Site access shows four short steps until a connection is saved; after
that each connection shows its last answer as a pill (“Service responded” means the service answered, not that the
site is reachable). This page and the in-app Guide have the full explanation and troubleshooting. Top-level
settings sections remain closed until opened.

If an existing card says **On demand**, its trusted module is already installed. Enable that connection and
the overall switch, then Save; no manual startup or OS autostart is needed. The steps below apply when adding
a separately operated service. Selecting its type in TOW does not install it. An address such as
`http://127.0.0.1:8191` names the service on the TOW computer, not a tracker mirror or a topic.

1. Run a trusted [FlareSolverr](https://github.com/FlareSolverr/FlareSolverr),
   [Byparr](https://github.com/ThePhaseless/Byparr), [FlareBypasser](https://github.com/yoori/flare-bypasser) or
   [Trawl](https://github.com/germondai/trawl) service separately. Keep its API private and isolate browser egress
   from LAN, link-local and cloud metadata destinations. The browser is outside TOW's network sandbox.
2. On the TOW computer, open the local web address → Settings → Site access → Add a connection. Choose its type, enter
   its loopback/private IP address and, with two or more connections, its order. Only `/v1` is accepted; localhost
   becomes 127.0.0.1. A saved connection is removed by its own **Remove** button.
3. Tick **Use this connection** and **Use challenge recovery**, then Save. Saving does not contact or validate the
   service; the API remains unchecked until a real applying check has a reason to use it. An API answer is not proof of
   site access; its observed status and any service cooldown are shown separately.

After saving, open a failed topic on Home and use **Check access now**, or wait for its scheduled retry.
Only another ordinary Cloudflare refusal can start recovery; a working mirror does not call a service just
to test its settings. The API status is its last observed call, not a live probe. Reset saved access removes
clearance and recovery/service cooldown records, not site passwords, connection settings or client torrents;
it neither installs a module nor cancels a server's rate limit.

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

On Sites → the site's **Advanced** → **Site-access services**, **As in Settings** permits the optional layer; **Off
for this site** disables it for that site. **Sign in through a browser window** uses the existing explicit
browser-auth flow instead: external providers do not
replace that session's User-Agent. It is not an unattended browser solver. A password sign-in sent with verified
access keeps only the account cookies the site's answer set: the clearance stays in the encrypted access store and
is never saved with the site's sign-in (one an earlier version saved there is not sent and goes at the next
successful sign-in). A browser sign-in keeps its cookies as saved.

## What a check does

### Installed Windows module: on demand

An owner-installed FlareSolverr runtime module may supply a private, pinned
`runtime/services/flaresolverr/manifest.json` (`tow-local-service/v1`). This is not a web-configurable command:
the manifest binds the loopback API, fixed relative executable, version, SHA-256 and idle interval. TOW does
not download or install it while checking. Missing modules retain the ordinary external-service path.
The SHA-256 covers the launcher executable only: it detects a changed launcher, not the integrity of the whole
module folder or who published it. Settings and service requests reuse its digest while the file's size, time and
identity stay the same; every start of the module hashes the file again.

Only an actual provider call can launch it: rendering settings, diagnostics, previews, disabled services and
ordinary successful site requests do not start a process. A small independent broker lazily starts the native
service, serializes browser calls and shares the caller's deadline with startup. It forwards only bounded,
anonymous requests. If a cold start exceeds one request, that request ends on time while the owned startup may
finish within a separate two-minute cap; a later request can reuse it instead of restarting from cold.
Only cookie requests for audited public paths are accepted. Application tokens and proxy variables are not inherited.
Its owned Windows process tree is bound to the broker even on a crash. After the manifest's idle interval
(30 seconds to 15 minutes) it stops its own service and exits. A listener started manually is reused without
ownership or stopping. No OS autostart is needed.

Settings show **On demand** for a matching installed module. Disabling the connection prevents further launches;
an already issued request may finish, after which the idle timer stops the module. A damaged manifest/binary
refuses automatic startup. Native demand management currently supports the Windows package; Linux/macOS keep
the existing independently managed external API. As for every external browser service, browser egress is outside
TOW's socket sandbox and should be isolated from private destinations. TOW checks only the entry address it sends:
it must resolve to public addresses, even for a site allowed private addresses. The browser resolves names and
follows redirects itself.

While it runs, the native service listens on a random `127.0.0.1` port without authentication; only the broker's
own API checks that a request is local. Any program on the TOW computer can reach that port during the module's
short on-demand lifetime. The random port is not a password: binding to loopback, the idle stop, the bound
process tree and the disposable work folder are the containment. This is a known residual risk; do not install
the module on a computer shared with untrusted local users.

Only the module's temporary files are redirected into its folder. Its browser may still use the Windows user
profile folders (`%APPDATA%`, `%LOCALAPPDATA%`, `%USERPROFILE%`) for its own files: a known limitation of the
portable install, not verified to stay inside it.

### Request sequence

1. Ask ordinary mirrors, with any still-valid previously verified access for the exact origin.
2. Only when ordinary mirrors failed with a detected Cloudflare browser challenge, try enabled providers in order.
3. Send the provider only an audited public page: the root by default, `/login.php` for Kinozal. Never send a
   password, account cookie, topic, passkey or download. The trusted site module chooses the probe; arbitrary
   configuration URLs and query strings are not accepted.
4. Validate the bounded response and replay that page from TOW using the returned clearance and matching User-Agent.
   Redirects stay within the deadline and audited anonymous paths; a path-specific probe cannot redirect to
   an open home page and pass. No probe follows a topic, download or query-bearing redirect. A passed replay means
   *probe access verified*: the public page answered without a challenge. It does not prove that the requested
   page's challenge was solved or that a download works; the resource's own answer in step 5 decides the check.
5. Fetch the requested resource through TOW's existing guarded HTTP, with normal account sign-in if needed.

Kinozal's catalog sends anonymous visitors to `/login.php?m=5`. Its trusted probe uses the sign-in page directly,
without query parameters or account credentials, so that redirect cannot invalidate restored access. A reachable
sign-in page proves only that the browser challenge was cleared; the actual resource must still confirm account
access. A sign-in probe cannot redirect to the catalog, home page, a query-bearing URL or another origin.

Cloudflare's [`cf-mitigated: challenge`](https://developers.cloudflare.com/cloudflare-challenges/challenge-types/challenge-pages/detect-response/)
header is definitive evidence. Known check-page titles with their challenge status are a fallback; an ordinary
Cloudflare proxy, embedded script or forum quotation is not. Access bans, rate limits, origin outages, maintenance,
quota and account sign-in are separate reasons, not instructions to call a solver. A download answered by a page
with a real password field (at any length; comments and scripts do not count) is an expired sign-in, and TOW signs
in again once; a removed-topic message and a daily limit take precedence. Preview and diagnostics never
invoke one. Cross-origin download redirects use a fresh cookie-free client; parent-domain metadata cannot grant
cross-origin credential reuse.

A mirror's rate limit immediately pauses requests to that mirror, including password sign-in and manual checks.
`Retry-After` accepts seconds or HTTP-date, bounded to 24 hours; without a usable hint, backoff starts at 60 seconds
and caps at 30 minutes. Other mirrors remain eligible. A preview records nothing and no worker sleeps on the pause.
The server-rate deadline is separate from the last observed response: an unrelated successful page or a late
earlier answer cannot erase or shorten it. Dedicated sign-in origins have independent deadlines.

An encrypted `data/protection.enc` stores verified clearance, matching User-Agent, expiry and durable cooldown leases.
Defaults: 30 seconds for the recovery chain, at most 2 service calls, 1,800 seconds before another attempt.
The call limit is shared across all mirrors, counting only service calls actually started. Each later challenged
mirror that can still be recovered keeps one of those calls: one mirror's last-chance call cannot take another
mirror's only call. Mirrors with the oldest
recovery attempt run first, so later mirrors remain reachable even when checks are farther apart than the retry
interval. Missing/paused services and origins for which no call fits do not start a new cooldown.
An otherwise successful anonymous service response without a clearance cookie allows one last-chance call to
that connection, after the other enabled connections. It uses the same reservation and remaining call/time budget,
including the verification margin; repeated missing cookies never loop. Ambiguous cookies, invalid responses,
scope/origin mismatches, API pauses and refused resource replays do not authorize this retry.
The lease is written before calling the service, so a crash does not cause an attempt on every tick. A local
module's failed or late start, or its "still starting" answer, asks no site: that call gives the origin's
reservation back, while the module's own pause stays. A configuration
change invalidates the old clearance but does not erase cooldowns. Disabling the layer immediately prevents its use
in subsequent requests; an already issued request cannot be recalled.

Connection failures, a local module that does not start in time and API-level busy/protocol responses pause only
that service across sites: exponential
backoff starts at 30 seconds and caps at 1,800 seconds; a valid `Retry-After` may extend it up to 24 hours. A site's
refused replay, slow challenge or HTTP 500 is not enough to disable the service globally. Renaming or reordering a
connection cannot erase this cooldown. Each persisted attempt has its own identity: responses from before a reset
or a newer reservation cannot repopulate the cache, revoke a new credential or restore an old service cooldown.
Encrypted format v2 reads v1 without rewriting it on inspection; a new write upgrades only this optional cache.
Failures older than 24 hours no longer raise the next day's first error to maximum backoff. If the computer's
clock was set back, the next recovery moves future reservation and pause times to the current time once, keeping
each wait's length. The solver's share of
the chain budget leaves up to 10 seconds (one third for smaller budgets) for independent verification. A transient
probe transport error or origin outage preserves verified access and does not justify another solve attempt.
Starting a new reservation retains still-valid verified access for unchanged settings with its own identity;
the new attempt's identity cannot relabel it. Failed attempts retain provider rotation; a confirmed replacement
resets that origin's provider order. Tagged legacy credentials are preserved on write, without rewriting a read.
Older optional-cache readers safely ignore credentials with the new nested identity; passwords and torrent state
are unchanged. Untagged or expired legacy access is not carried into a new reservation.
Recovery does not replay a cached credential which the ordinary request already tried. A newly verified probe
whose real resource is still challenged retains that origin's access but reports the path refusal; solving the
same successful probe again is not justified. A missing probe preserves the original resource's mirror reasons.
If the front page or trusted public probe refuses a cached session, only the exact session used for that request
is discarded. The ordinary front-page check may include account cookies. A path-only challenge with a healthy
probe, a quota, rate limit or outage does not revoke it. Preview
never revokes access, and a late old refusal cannot discard a newer session even with identical cookie values.
An outage or path refusal ends only that origin's attempt; other eligible mirrors can still recover within the
shared budget. Their original reasons remain until the resource itself supplies a newer answer. A rate limit
pauses that origin once; a quota or explicit ban ends all further recovery calls in that check. A topic is called
gone only when every mirror actually tried reports it gone, not when another mirror remains inaccessible.
Mixed failures keep each mirror's evidence: a ban on one mirror does not assert that all mirrors banned access.

Explicit browser sign-in has one absolute deadline including connection and setup. Each command also has a fixed
response deadline; unrelated browser events cannot extend either limit. Cancellation closes the dedicated channel;
its bounded close handshake (up to two seconds) and existing process/profile cleanup may finish after the deadline.

If that store is unreadable, ordinary HTTP remains available and the damaged file is not silently replaced. On the
TOW computer, Settings → Site access → **Reset saved access** explicitly moves the old encrypted bytes
to TOW's private temporary folder, until normal cleanup. It does not remove site passwords or service settings;
reset is refused while an applying check is active. The same action resets saved service cooldowns.

## Current version without another download

A site may expose `infohash_path` with `{id}`. Kinozal's preset supplies its service-information page. Only a unique
labelled current hash is accepted. If it matches the stored version, the client must independently confirm the
owned torrent, path and verified selection before TOW skips downloading its `.torrent`. Progress reconciliation
still runs. Unknown format or a missing optional endpoint falls back to the normal download; a host-level failure
is reported without repeating the same failing requests. A changed rule, missing/foreign/pending task or changed
hash takes the full client-confirmed path. This is not a quota bypass or proof from a cached old torrent.

Kinozal's current download route is `/download.php?id={id}`. Its exact obsolete default
`/.dl./download.php?id={id}` is read as that route, including old imported settings; reading does not rewrite
the configuration file. Custom routes remain unchanged. Old pasted mirror links remain recognizable.

## Limits and troubleshooting

Temporary tracker outages have their own bounded supervisor retry job: up to three extra checks per continuous
outage, coalesced by site. Transport failures start after five minutes; Cloudflare waits at least 30 minutes
or the configured recovery interval. Each failure doubles the delay up to two hours, with up to 30 seconds of
stable site-level jitter; server mirror pauses remain a floor. The ordinary HTTP and provider guards still
enforce their own leases and pauses. These checks never move the global or personal timer and cannot authorize
re-adding a torrent removed by the owner. After the extra attempts, ordinary cadence remains in force.
Pause, deletion, successful checks and changed topic/site rules invalidate an old batch. Reservations are written
before spawn, so a failed or interrupted child cannot cause an every-tick retry. Authentication, quota, explicit
bans, paused sites and completed one-time topics do not enter this queue.

Home groups one site's temporary outage into one warning with its affected-topic count, keeps each mirror's
actual reason in Details, and opens the Check access now control directly. A retry time is shown only when the
running supervisor supplied a matching plan; inspection never schedules a check or starts a module. Error
notifications are grouped from two affected topics, and switching between transport classes does not announce
the same outage again. Confirmed recovery closes a previously announced failure once.

Recovery events record closed, credential-free reason and phase codes, plus elapsed time: module startup,
service request, response validation, public probe, or real-resource replay. API success is still not site access.

- A service's `status: ok` is not proof; only TOW's independent replay can verify access.
- A public probe cannot solve every challenge restricted to one particular download path. An external service may
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
