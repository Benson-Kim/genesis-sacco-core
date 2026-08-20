/// Composition root.
///
/// Every dependency is a provider so that a session teardown disposes the
/// objects that held member data, rather than leaving them alive for the next
/// member who signs in on the same device (FM3 in the P17 matrix: log in as A,
/// log out, log in as B, and nothing of A's may remain).
library;

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:gp_api_client/gp_api_client.dart';

import 'env.dart';
import 'session.dart';

/// The build's flavor. Overridden in `main()` per white-label build and in
/// tests; never mutated at runtime.
final Provider<Flavor> flavorProvider = Provider<Flavor>(
  (Ref ref) =>
      throw UnimplementedError('flavorProvider must be overridden in main()'),
);

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
    storage: ref.watch(tokenStoreProvider),
    refresh: (String refreshToken) =>
        ref.read(authRepositoryProvider).refresh(refreshToken),
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

/// Placeholder until MR-1 lands the auth repository. Declared here so the
/// session's refresh seam has a home and the composition root is complete;
/// MR-1 replaces the body, not the wiring.
final Provider<AuthRepository> authRepositoryProvider =
    Provider<AuthRepository>(
  (Ref ref) => AuthRepository(),
);

/// MR-1 fills this in against `POST /member/auth/*`.
class AuthRepository {
  Future<TokenPair> refresh(String refreshToken) =>
      throw UnimplementedError('MR-1: POST /member/auth/refresh');
}
