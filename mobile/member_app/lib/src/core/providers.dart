/// Composition root.
///
/// Every dependency is a provider so that a session teardown disposes the
/// objects that held member data, rather than leaving them alive for the next
/// member who signs in on the same device (FM3 in the P17 matrix: log in as A,
/// log out, log in as B, and nothing of A's may remain).
library;

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gp_api_client/gp_api_client.dart';

import '../features/auth/data/member_auth_repository.dart';
import '../features/auth/domain/auth_port.dart';
import 'env.dart';
import 'inactivity.dart';
import 'session.dart';

/// The build's flavor. Overridden in `main()` per white-label build and in
/// tests; never mutated at runtime.
final Provider<Flavor> flavorProvider = Provider<Flavor>(
  (Ref ref) =>
      throw UnimplementedError('flavorProvider must be overridden in main()'),
);

/// The wall clock, as a seam.
///
/// Everything time-dependent in this app is a security property — token
/// expiry, the resend cooldown, the inactivity deadline — and a security
/// property that can only be tested by sleeping is a security property nobody
/// tests. Overriding one provider makes all three falsifiable in milliseconds.
final Provider<DateTime Function()> clockProvider =
    Provider<DateTime Function()>((Ref ref) => DateTime.now);

final Provider<TokenStore> tokenStoreProvider = Provider<TokenStore>(
  (Ref ref) => TokenStorage(),
);

final Provider<CertificatePinning> pinningProvider =
    Provider<CertificatePinning>((Ref ref) {
  final Flavor flavor = ref.watch(flavorProvider);
  return CertificatePinning(
    pins: flavor.pinSet,
    enforcement: flavor.pinEnforcement,
    // TODO(#39): route this into the telemetry port once it exists. Until
    // then a report-mode mismatch is invisible, which is the known cost of
    // shipping report before the #11 cutover.
    onMismatch: (String observed) {},
  );
});

/// The session owns token custody and is the only writer of the access token.
final Provider<MemberSession> sessionProvider =
    Provider<MemberSession>((Ref ref) {
  final MemberSession session = MemberSession(
    // `ref.read` inside the callback, not `ref.watch` at build time, and that
    // is load-bearing: the auth port needs the http client, which needs this
    // session. Resolving the port lazily — at the moment a refresh actually
    // happens, by which time all three exist — is what keeps the cycle from
    // closing.
    refresh: (String refreshToken) =>
        ref.read(authPortProvider).refresh(refreshToken),
    storage: ref.watch(tokenStoreProvider),
    now: ref.watch(clockProvider),
  );
  ref.onDispose(session.dispose);
  return session;
});

/// The transport. Note the path guard: this build may name the `/member`
/// surface and nothing else, so a staff path is a programming error caught
/// here rather than a 403 discovered in production (FM-H, client half).
final Provider<GpHttpClient> httpClientProvider =
    Provider<GpHttpClient>((Ref ref) {
  final Flavor flavor = ref.watch(flavorProvider);
  final MemberSession session = ref.watch(sessionProvider);
  final GpHttpClient client = GpHttpClient(
    baseUrl: flavor.baseUrl,
    tenantId: flavor.tenantId,
    accessToken: () => session.currentAccessToken,
    pinning: ref.watch(pinningProvider),
    onSessionEnded: () => session.end(),
    pathGuard: (String path) => path.startsWith('/member/'),
  );
  ref.onDispose(client.close);
  return client;
});

/// `POST /member/auth/*`. The interface is what the rest of the app depends
/// on; the implementation is the only thing here that knows there is HTTP.
final Provider<AuthPort> authPortProvider = Provider<AuthPort>(
  (Ref ref) => MemberAuthRepository(ref.watch(httpClientProvider)),
);

/// Ends the session after a period with no interaction (#43 T0).
///
/// Not started here. `main()` starts it once the binding exists, because
/// [InactivityMonitor.start] registers a `WidgetsBindingObserver`; a provider
/// that reached for `WidgetsBinding.instance` on first read would be a
/// provider that behaves differently depending on who read it first.
final Provider<InactivityMonitor> inactivityMonitorProvider =
    Provider<InactivityMonitor>((Ref ref) {
  final MemberSession session = ref.watch(sessionProvider);
  final InactivityMonitor monitor = InactivityMonitor(
    timeout: ref.watch(flavorProvider).inactivityTimeout,
    onExpired: session.end,
    now: ref.watch(clockProvider),
  );
  ref.onDispose(monitor.dispose);
  return monitor;
});
