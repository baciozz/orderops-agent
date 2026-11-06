# Local demo browser boundary

OrderOps remains an unauthenticated, single-operator loopback demo, not a production
service. Run with `make run` (127.0.0.1:8000); do not expose it to a LAN, public
network, tunnel, or shared reverse proxy. This change adds a browser request
boundary, not user identity, permissions, production authentication, or a CSRF token.

## Policy

- Every HTTP route, including reads/static/health, requires exactly one Host:
  `localhost`, `127.0.0.1`, or `[::1]`, optionally with port 1–65535. No wildcard,
  alternate loopback addresses, custom DNS names, trailing dot, or test-only host.
  This prevents an attacker-controlled DNS name resolving to loopback from being
  accepted as its own trusted origin (DNS rebinding). The Host check does not
  replace binding the server to loopback.
- Every method except GET, HEAD, OPTIONS is checked before reading its body or
  dispatching a route. Explicit Origin must match the request scheme, allowed
  hostname, and effective port. HTTP :80 and HTTPS :443 normalize; distinct
  loopback spellings and ports do not. `null`, malformed origins, URL credentials,
  paths, fragments, queries, lists, and duplicate Origin, Referer, or
  Sec-Fetch-Site headers are rejected.
- Sec-Fetch-Site `cross-site`, `same-site`, and unrecognized values are rejected
  even with a matching Origin. A missing Origin can use a same-origin Referer or
  `same-origin` fetch metadata. `none` alone does not authorize a mutation.
  Other Sec-Fetch-* headers without Origin, Referer or same-origin Site evidence
  are rejected, rather than being mistaken for a header-free CLI request.
  An explicit Referer must also match, including when Origin is present.
- Requests without Origin, Referer, or any Sec-Fetch-* metadata remain compatible with
  ordinary CLI/API clients. No User-Agent sniffing or client-address bypass.
  Browser POSTs normally carry Origin; an older client or intermediary stripping
  all browser evidence cannot be distinguished from a CLI and is outside this
  protection. Do not strip these headers in a proxy.
- X-Forwarded-Host and other arbitrary forwarded headers are not trust sources
  in this check. The ASGI scheme is used; deployment/server proxy processing can
  change it before the app sees a request. Reverse proxy deployment is unsupported
  here and needs a separate trusted-proxy, external-origin and authentication design.
- Responses handled by the request middleware carry `X-Frame-Options: DENY`
  and `Content-Security-Policy: frame-ancestors 'none'` to prevent framing and
  clickjacking. Unhandled server failures may use the server's outer error handler;
  these do not serve an interactive demo page. No session cookies or tokens added.
- Rejections use the normal error envelope and request/correlation IDs:
  `UNTRUSTED_HOST` (400) or `CROSS_ORIGIN_REQUEST` (403), with failed request logs.
  This is independent of CORS: disallowing cross-origin response reads alone does
  not stop simple native URL-encoded form submissions from mutating state.

## Verification and limits

A synthetic TestClient reproduction on the prior version posted a URL-encoded
form to `/ui/demo/submit` with hostile Origin and cross-site metadata: 303 and
one task created. With this boundary: 403 and zero tasks. Tests cover a complete
same-origin native form → fixture worker → approval → commit workflow, CLI
compatibility, rejection before route validation, Host rebinding, duplicate and
malformed headers, default ports, IPv6, and conflicting browser evidence.
These are ASGI tests; no real browser or live business/model call was used.

This does not authenticate CLI clients, defend against malicious local processes,
protect against same-origin XSS, or make a multi-user service safe. GET/HEAD/
OPTIONS must stay side-effect-free. Cross-origin read confidentiality is not
provided by this mutation policy; do not put real private data in the demo.
